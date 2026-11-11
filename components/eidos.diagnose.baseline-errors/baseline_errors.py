"""Baseline error analysis on visible data (numpy only): split statistics, error slices, cheap invariance probes and
ranked hypotheses, each with a prediction and a falsifier.

Reads only the ports ``train``, ``validation``, ``predictions``, ``groups`` and ``task``: a test or held-out split has no
way in. Exploratory: it makes no claim. Its probes are quick refits on training data scored on visible validation, so
they rank explanations for the baseline's errors; they are not evidence that a recipe improves on new data.
"""
import hashlib
import json
import math
import platform
import re

import numpy as np

REPORT_SCHEMA = "baseline-diagnosis/1"
COMPONENT = "eidos.diagnose.baseline-errors@1.0.0"
ALLOWED_INPUTS = ("train", "validation", "predictions", "groups", "task")
PROBES = ("none", "per_example_center", "per_example_standardize", "split_standardize", "shift_invariant")
LAYOUTS = ("auto", "tabular", "sequence", "image")
MAX_CLASSES = 50
_HELDOUT = re.compile(r"(^|[^a-z0-9])(test(?:set|data|split)?|hold[_\- ]?out|held[_\- ]?out)([^a-z0-9]|$)")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
EPS = 1e-8


class DiagnosisConfigError(ValueError):
    """The diagnosis cannot run as configured; nothing was computed."""


def _names_heldout(name):
    return bool(_HELDOUT.search(_CAMEL.sub(r"\1_\2", str(name)).lower()))


# -- inputs -----------------------------------------------------------------------------------------------------------
def _digest(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _load_split(reader, port, keys, group_keys, need_y):
    with np.load(reader.path(port), allow_pickle=False) as data:
        if keys["x"] not in data:
            raise DiagnosisConfigError("%s has no array %r" % (port, keys["x"]))
        raw = np.asarray(data[keys["x"]])
        y = np.asarray(data[keys["y"]]) if keys["y"] in data else None
        ids = np.asarray(data[keys["ids"]]) if keys["ids"] in data else np.arange(len(raw))
        groups = {k: np.asarray(data[k]) for k in group_keys if k in data}
    if raw.ndim < 2 or len(raw) < 2:
        raise DiagnosisConfigError("%s.%s must hold at least two examples with one or more features" % (port, keys["x"]))
    x = raw.reshape(len(raw), -1).astype(np.float64)
    if not np.all(np.isfinite(x)):
        raise DiagnosisConfigError("%s.%s has non-finite values" % (port, keys["x"]))
    if need_y and y is None:
        raise DiagnosisConfigError("%s has no label array %r" % (port, keys["y"]))
    if y is not None and len(y) != len(x):
        raise DiagnosisConfigError("%s labels and inputs differ in length" % port)
    if len(ids) != len(x):
        raise DiagnosisConfigError("%s ids and inputs differ in length" % port)
    return {"x": x, "y": y, "ids": ids, "shape": tuple(int(s) for s in raw.shape[1:]), "groups": groups}


def _task_type(config, task, y_train):
    declared = config.get("task_type") or (task or {}).get("task_type")
    if declared is not None:
        if declared not in ("classification", "regression"):
            raise DiagnosisConfigError("task_type must be classification or regression")
        return declared, "declared"
    integral = np.issubdtype(y_train.dtype, np.integer) or np.all(np.equal(np.mod(y_train, 1), 0))
    if integral and len(np.unique(y_train)) <= MAX_CLASSES:
        return "classification", "inferred from integer labels"
    return "regression", "inferred from non-integer labels"


def _layout(config, shape, n_features):
    kind = config.get("layout", "auto")
    if kind not in LAYOUTS:
        raise DiagnosisConfigError("layout must be one of %s" % (LAYOUTS,))
    if config.get("image_shape") is not None:
        h, w = (int(v) for v in config["image_shape"])
        if h * w != n_features:
            raise DiagnosisConfigError("image_shape %s does not match %d features" % ([h, w], n_features))
        return {"kind": "image", "shape": [h, w], "assumed": False}
    if kind == "image" or (kind == "auto" and len(shape) >= 2):
        if len(shape) >= 2:
            h, w = shape[0], int(np.prod(shape[1:]))
            return {"kind": "image", "shape": [h, w], "assumed": kind == "auto"}
        side = int(round(math.sqrt(n_features)))
        if side * side != n_features:
            raise DiagnosisConfigError("an image layout needs image_shape or a square number of features")
        return {"kind": "image", "shape": [side, side], "assumed": False}
    if kind == "auto":
        side = int(round(math.sqrt(n_features)))
        if side * side == n_features and side >= 8:
            return {"kind": "image", "shape": [side, side], "assumed": True}
        return {"kind": "sequence", "shape": [n_features], "assumed": True}
    return {"kind": kind, "shape": [n_features], "assumed": False}


# -- quick models ----------------------------------------------------------------------------------------------------
def _standardizer(x):
    mu = x.mean(0)
    sd = x.std(0)
    sd = np.where(sd < EPS, 1.0, sd)
    return mu, sd


def _ridge_fit_predict(xtr, ytr, xte, task, classes, alpha):
    mu, sd = _standardizer(xtr)
    a, b = (xtr - mu) / sd, (xte - mu) / sd
    if task == "classification":
        target = (ytr[:, None] == classes[None, :]).astype(np.float64)
    else:
        target = ytr.astype(np.float64)[:, None]
    offset = target.mean(0)
    target = target - offset
    n, d = a.shape
    lam = alpha * max(n, 1) / 100.0
    if d <= n:
        w = np.linalg.solve(a.T @ a + lam * np.eye(d), a.T @ target)
        out = b @ w + offset
    else:
        k = a @ a.T
        c = np.linalg.solve(k + lam * np.eye(n), target)
        out = (b @ a.T) @ c + offset
    if task == "classification":
        return classes[np.argmax(out, 1)]
    return out[:, 0]


def _knn_fit_predict(xtr, ytr, xte, task, classes, k):
    mu, sd = _standardizer(xtr)
    a, b = (xtr - mu) / sd, (xte - mu) / sd
    k = max(1, min(k, len(a)))
    a2 = (a * a).sum(1)
    out = []
    for start in range(0, len(b), 512):
        chunk = b[start:start + 512]
        dist = (chunk * chunk).sum(1)[:, None] + a2[None, :] - 2.0 * chunk @ a.T
        nearest = np.argsort(dist, axis=1, kind="stable")[:, :k]
        if task == "classification":
            labels = ytr[nearest]
            votes = (labels[:, :, None] == classes[None, None, :]).sum(1)
            out.append(classes[np.argmax(votes, 1)])
        else:
            out.append(ytr[nearest].astype(np.float64).mean(1))
    return np.concatenate(out)


def _score(task, y_true, y_pred):
    if task == "classification":
        return float(np.mean(y_true == y_pred))
    y_true = y_true.astype(np.float64)
    sst = float(np.sum((y_true - y_true.mean()) ** 2))
    sse = float(np.sum((y_true - y_pred) ** 2))
    return 1.0 - sse / sst if sst > 0 else (1.0 if sse == 0 else 0.0)


def _models(config):
    return {"ridge": float(config.get("ridge_alpha", 1.0)), "knn": int(config.get("knn_k", 5))}


def _fit_score(xtr, ytr, xte, yte, task, classes, models):
    scores = {}
    for name, value in models.items():
        fit = _ridge_fit_predict if name == "ridge" else _knn_fit_predict
        scores[name] = _score(task, yte, fit(xtr, ytr, xte, task, classes, value))
    return scores


# -- probes ----------------------------------------------------------------------------------------------------------
def _per_example_center(x):
    return x - x.mean(1, keepdims=True)


def _per_example_standardize(x):
    sd = x.std(1, keepdims=True)
    return _per_example_center(x) / np.where(sd < EPS, 1.0, sd)


def _shift_invariant(x, layout):
    """Magnitudes of the discrete Fourier transform of each centered example: unchanged by a circular shift of the
    positions (2-D for an image layout, along the feature order otherwise). Centering first removes the zero-frequency
    term, so an additive offset is not what this probe can explain."""
    xc = _per_example_center(x)
    if layout["kind"] == "image":
        h, w = layout["shape"]
        return np.abs(np.fft.rfft2(xc.reshape(len(x), h, w))).reshape(len(x), -1)
    return np.abs(np.fft.rfft(xc, axis=1))


def _transform(probe, xtr, xva, layout):
    """(train, validation) features for a probe. Every probe is followed by the model's own train-fitted standardization;
    split_standardize standardizes each split with its own feature statistics first, which needs no labels."""
    if probe == "none":
        return xtr, xva
    if probe == "per_example_center":
        return _per_example_center(xtr), _per_example_center(xva)
    if probe == "per_example_standardize":
        return _per_example_standardize(xtr), _per_example_standardize(xva)
    if probe == "split_standardize":
        (mt, st), (mv, sv) = _standardizer(xtr), _standardizer(xva)
        return (xtr - mt) / st, (xva - mv) / sv
    return _shift_invariant(xtr, layout), _shift_invariant(xva, layout)


def _cv(x, y, task, classes, models, folds, rng):
    """Mean over folds of the best quick model's score within one split (how learnable that split is by itself)."""
    order = rng.permutation(len(x))
    parts = np.array_split(order, folds)
    per_model = {name: [] for name in models}
    for i, held in enumerate(parts):
        fit = np.concatenate([p for j, p in enumerate(parts) if j != i])
        if len(held) == 0 or len(fit) == 0:
            continue
        for name, value in _fit_score(x[fit], y[fit], x[held], y[held], task, classes, models).items():
            per_model[name].append(value)
    return {name: float(np.mean(v)) for name, v in per_model.items() if v}, \
        {name: [float(s) for s in v] for name, v in per_model.items() if v}


def _best(scores):
    return max(scores.values()) if scores else float("nan")


# -- the report ------------------------------------------------------------------------------------------------------
def _quantiles(v, qs=(5, 25, 50, 75, 95)):
    return {"p%d" % q: float(np.percentile(v, q)) for q in qs}


def _split_statistics(tr, va, task):
    xt, xv = tr["x"], va["x"]
    mt, st = xt.mean(0), xt.std(0)
    mv, sv = xv.mean(0), xv.std(0)
    smd = (mv - mt) / np.where(st < EPS, 1.0, st)
    ratio = np.where(st < EPS, np.where(sv < EPS, 1.0, np.inf), sv / np.where(st < EPS, 1.0, st))
    finite_ratio = ratio[np.isfinite(ratio)]
    top = np.argsort(-np.abs(smd), kind="stable")[:10]
    pe_mean_t, pe_mean_v = xt.mean(1), xv.mean(1)
    pe_std_t, pe_std_v = xt.std(1), xv.std(1)
    norm_t, norm_v = np.linalg.norm(xt, axis=1), np.linalg.norm(xv, axis=1)
    spread = float(pe_mean_t.std()) or 1.0
    stats = {
        "n_train": int(len(xt)), "n_validation": int(len(xv)), "n_features": int(xt.shape[1]),
        "feature_shift": {
            "standardized_mean_difference": {"mean_abs": float(np.mean(np.abs(smd))), "max_abs": float(np.max(np.abs(smd))),
                                             "fraction_above_0_5": float(np.mean(np.abs(smd) > 0.5))},
            "std_ratio_validation_over_train": {
                "median": float(np.median(finite_ratio)) if finite_ratio.size else None,
                "min": float(np.min(finite_ratio)) if finite_ratio.size else None,
                "max": float(np.max(finite_ratio)) if finite_ratio.size else None,
                "fraction_below_0_5": float(np.mean(ratio < 0.5)), "fraction_above_2": float(np.mean(ratio > 2.0))},
            "top_features": [{"feature": int(i), "standardized_mean_difference": float(smd[i]),
                              "std_ratio": float(ratio[i]) if np.isfinite(ratio[i]) else None} for i in top],
        },
        "per_example": {
            "mean": {"train": _quantiles(pe_mean_t), "validation": _quantiles(pe_mean_v),
                     "shift_in_train_spread_units": float((pe_mean_v.mean() - pe_mean_t.mean()) / spread)},
            "std": {"train": _quantiles(pe_std_t), "validation": _quantiles(pe_std_v),
                    "ratio_of_means": float(pe_std_v.mean() / max(pe_std_t.mean(), EPS))},
            "l2_norm": {"train": _quantiles(norm_t), "validation": _quantiles(norm_v),
                        "ratio_of_medians": float(np.median(norm_v) / max(np.median(norm_t), EPS))},
        },
    }
    if task == "classification" and tr["y"] is not None and va["y"] is not None:
        labels = sorted(set(tr["y"].tolist()) | set(va["y"].tolist()))
        stats["labels"] = {"train": {str(c): int(np.sum(tr["y"] == c)) for c in labels},
                           "validation": {str(c): int(np.sum(va["y"] == c)) for c in labels}}
    elif tr["y"] is not None and va["y"] is not None:
        stats["labels"] = {"train": {"mean": float(np.mean(tr["y"])), "std": float(np.std(tr["y"]))},
                           "validation": {"mean": float(np.mean(va["y"])), "std": float(np.std(va["y"]))}}
    return stats


def _slice(task, y, pred, mask):
    n = int(mask.sum())
    if n == 0:
        return {"n": 0}
    if task == "classification":
        errors = int(np.sum(y[mask] != pred[mask]))
        return {"n": n, "errors": errors, "error_rate": errors / n}
    resid = pred[mask].astype(np.float64) - y[mask].astype(np.float64)
    return {"n": n, "rmse": float(np.sqrt(np.mean(resid ** 2))), "mean_residual": float(np.mean(resid))}


def _quantile_slices(task, y, pred, values, bins):
    edges = np.unique(np.quantile(values, np.linspace(0, 1, bins + 1)))
    slices = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mask = (values >= lo) & ((values <= hi) if i == len(edges) - 2 else (values < hi))
        slices.append(dict(_slice(task, y, pred, mask), low=float(lo), high=float(hi)))
    return slices


def _align_predictions(reader, va, key):
    with np.load(reader.path("predictions"), allow_pickle=False) as data:
        if key not in data:
            raise DiagnosisConfigError("predictions has no array %r" % key)
        pred = np.asarray(data[key]).reshape(-1)
        ids = np.asarray(data["ids"]) if "ids" in data else None
    if ids is None:
        if len(pred) != len(va["x"]):
            raise DiagnosisConfigError("predictions without ids must have one row per validation example")
        return pred
    position = {v: i for i, v in enumerate(ids.tolist())}
    if len(position) != len(ids):
        raise DiagnosisConfigError("duplicate prediction ids")
    missing = [v for v in va["ids"].tolist() if v not in position]
    if missing:
        raise DiagnosisConfigError("%d validation ids have no baseline prediction" % len(missing))
    return pred[[position[v] for v in va["ids"].tolist()]]


def _error_slices(task, va, pred, source, bins, groups):
    y = va["y"]
    report = {"source": source, "overall": _slice(task, y, pred, np.ones(len(y), dtype=bool))}
    if task == "classification":
        report["by_class"] = {str(c): _slice(task, y, pred, y == c) for c in sorted(set(y.tolist()))}
        wrong = y != pred
        pairs = {}
        for t, p in zip(y[wrong].tolist(), pred[wrong].tolist()):
            pairs[(t, p)] = pairs.get((t, p), 0) + 1
        report["top_confusions"] = [{"true": str(t), "predicted": str(p), "count": n}
                                    for (t, p), n in sorted(pairs.items(), key=lambda kv: (-kv[1], str(kv[0])))[:10]]
    else:
        report["by_target_quantile"] = _quantile_slices(task, y, pred, y.astype(np.float64), bins)
    report["by_input_norm_quantile"] = _quantile_slices(task, y, pred, np.linalg.norm(va["x"], axis=1), bins)
    report["by_input_mean_quantile"] = _quantile_slices(task, y, pred, va["x"].mean(1), bins)
    report["by_group"] = {name: {str(g): _slice(task, y, pred, values == g) for g in sorted(set(values.tolist()), key=str)}
                          for name, values in sorted(groups.items())}
    return report


def _cv_noise(probes, tcv, vcv):
    """One standard error of the difference between the best within-train and best within-validation CV scores, from
    their fold-to-fold spread: a gap smaller than this is not read as lost information."""
    def folds(kind, best):
        probe = max(best, key=lambda p: best[p])
        per_model = probes[probe][kind + "_folds"]
        model = max(per_model, key=lambda m: float(np.mean(per_model[m])))
        values = np.asarray(per_model[model])
        return float(values.var(ddof=1)) / len(values) if len(values) > 1 else 0.0
    return float(math.sqrt(folds("train_cv", tcv) + folds("validation_cv", vcv)))


def _hypotheses(task, probes, min_effect, metric):
    val = {p: _best(probes[p]["validation"]) for p in probes}
    tcv = {p: _best(probes[p]["train_cv"]) for p in probes}
    vcv = {p: _best(probes[p]["validation_cv"]) for p in probes}
    ref = val["none"]
    delta = {p: val[p] - ref for p in val}
    center, standard = delta.get("per_example_center", 0.0), delta.get("per_example_standardize", 0.0)
    split = delta.get("split_standardize", 0.0)
    spatial = (val["shift_invariant"] - max(val.get("per_example_center", ref), ref)) if "shift_invariant" in val else None
    learn_train, learn_val = max(tcv.values()), max(vcv.values())
    noise = _cv_noise(probes, tcv, vcv)
    unit = "accuracy" if task == "classification" else "R^2"
    out = []

    def add(cause, score, statement, prediction, falsifier, next_step, evidence):
        out.append({"cause": cause, "score": float(score), "statement": statement, "prediction": prediction,
                    "falsifier": falsifier, "suggested_next": next_step, "evidence": evidence})

    add("per-example-additive-offset", center,
        "Validation examples carry an additive offset (a per-example or acquisition pedestal) that training examples "
        "do not; removing each example's mean removes it.",
        "Per-example centering before an otherwise unchanged recipe raises validation %s by about %.3f." % (metric, center),
        "The same recipe with per-example centering does not beat it without centering by more than %.3f on validation "
        "%s, at matched seeds." % (min_effect, metric),
        "Sweep preprocess per_example_center against none across model families before changing architecture.",
        {"validation_%s_reference" % unit: ref, "validation_%s_centered" % unit: val.get("per_example_center")})
    add("per-example-gain-change", standard - max(center, 0.0),
        "Validation examples differ by a multiplicative gain or contrast change beyond any offset; per-example "
        "standardization removes it.",
        "Per-example standardization raises validation %s by about %.3f more than centering alone." % (metric, standard - max(center, 0.0)),
        "Per-example standardization does not beat per-example centering by more than %.3f on validation %s." % (min_effect, metric),
        "Sweep preprocess per_example_standardize against per_example_center.",
        {"validation_%s_standardized" % unit: val.get("per_example_standardize"),
         "validation_%s_centered" % unit: val.get("per_example_center")})
    add("feature-wise-distribution-shift", split - max(center, standard, 0.0),
        "Individual features are shifted or rescaled between train and validation (covariate shift per feature) in a "
        "way that per-example normalization does not explain.",
        "Standardizing validation features with validation statistics raises validation %s by about %.3f; a recipe "
        "robust to these features (or re-weighting) should do the same without using validation statistics." % (metric, split),
        "Split-wise standardization does not beat per-example normalization by more than %.3f." % min_effect,
        "Inspect split_statistics.feature_shift.top_features; try feature selection or robust scaling in a sweep.",
        {"validation_%s_split_standardized" % unit: val.get("split_standardize")})
    if spatial is not None:
        add("spatial-or-positional-shift", spatial,
            "Validation inputs are spatially or positionally transformed (translated or rolled) relative to training "
            "inputs; a shift-invariant representation recovers accuracy that centering does not.",
            "Shift-invariant features (or translation augmentation) raise validation %s by about %.3f over centering." % (metric, spatial),
            "Shift-invariant features do not beat per-example centering by more than %.3f on validation %s, or "
            "translation augmentation of training data does not help." % (min_effect, metric),
            "Try translation augmentation or shift-invariant features only after ruling out offset and gain.",
            {"validation_%s_shift_invariant" % unit: val["shift_invariant"]})
    add("information-loss", learn_train - learn_val - noise,
        "Validation inputs carry less information about the target than training inputs (noise, masking or a lost "
        "channel): even a model fitted within validation does worse than one fitted within training, whatever the "
        "normalization.",
        "No preprocessing closes the gap: the best within-validation CV %s stays about %.3f below the best within-training "
        "CV %s at equal sample size." % (metric, learn_train - learn_val, metric),
        "A preprocessing or model change raises within-validation CV %s to within %.3f of within-training CV." % (metric, min_effect),
        "Expect a ceiling: report it, and prefer robust or denoising recipes over larger models.",
        {"best_train_cv_%s" % unit: learn_train, "best_validation_cv_%s" % unit: learn_val,
         "fold_noise_allowance": noise})
    gap = tcv["none"] - ref
    add("no-transfer-shift-detected", min_effect,
        "No probe explains the baseline's errors by more than the minimum effect: the baseline is limited by its model "
        "or training rather than by a train-validation difference these probes can see.",
        "A broad sweep over model families and train-fitted preprocessing finds the improvement, if any.",
        "Any probe above explains more than %.3f of validation %s." % (min_effect, metric),
        "Run a broad recipe sweep (eidos.search.recipe-sweep template) and compare model families.",
        {"train_cv_minus_validation_%s_reference" % unit: gap})
    order = sorted(range(len(out)), key=lambda i: (-out[i]["score"], i))
    ranked = []
    for rank, i in enumerate(order, 1):
        h = out[i]
        h.update(rank=rank, id="H%d" % rank, supported=h["score"] >= min_effect and h["cause"] != "no-transfer-shift-detected")
        ranked.append(h)
    return ranked


def run(inputs, config, *, reader, writer, context):
    extra = sorted(set(inputs) - set(ALLOWED_INPUTS))
    if extra:
        raise DiagnosisConfigError("undeclared input ports %s: only %s are read" % (extra, list(ALLOWED_INPUTS)))
    for port in ("train", "validation"):
        if port not in inputs:
            raise DiagnosisConfigError("the %s port is required" % port)
    keys = {"x": config.get("x_key", "x"), "y": config.get("y_key", "y"), "ids": config.get("ids_key", "ids")}
    group_keys = list(config.get("group_keys") or [])
    for name in [*keys.values(), *group_keys]:
        if _names_heldout(name):
            raise DiagnosisConfigError("array name %r names a held-out split; this diagnosis reads visible data only" % name)
    probes_wanted = list(config.get("probes") or PROBES)
    unknown = sorted(set(probes_wanted) - set(PROBES))
    if unknown or "none" not in probes_wanted:
        raise DiagnosisConfigError("probes must be drawn from %s and include none" % (list(PROBES),))
    seed = int(config.get("seed", context.seed if getattr(context, "seed", None) is not None else 0))
    folds = int(config.get("cv_folds", 3))
    bins = int(config.get("quantiles", 4))
    min_effect = float(config.get("min_effect", 0.02))
    if folds < 2 or bins < 2:
        raise DiagnosisConfigError("cv_folds and quantiles must be at least 2")
    tr = _load_split(reader, "train", keys, [], True)
    va = _load_split(reader, "validation", keys, group_keys, True)
    if tr["x"].shape[1] != va["x"].shape[1]:
        raise DiagnosisConfigError("train and validation have different numbers of features")
    task = reader.json("task") if "task" in inputs else None
    task_type, task_source = _task_type(config, task, tr["y"])
    layout = _layout(config, tr["shape"], tr["x"].shape[1])
    metric = "accuracy" if task_type == "classification" else "r2"
    classes = np.unique(np.concatenate([tr["y"], va["y"]])) if task_type == "classification" else None
    rng = np.random.default_rng(seed)
    n_tr = min(len(tr["x"]), int(config.get("max_train_rows", 3000)))
    n_va = min(len(va["x"]), int(config.get("max_validation_rows", 2000)))
    i_tr = np.sort(rng.choice(len(tr["x"]), n_tr, replace=False))
    i_va = np.sort(rng.choice(len(va["x"]), n_va, replace=False))
    i_cv = np.sort(rng.choice(n_tr, min(n_tr, n_va), replace=False))  # equal sizes: within-split CVs are comparable
    xtr, ytr, xva, yva = tr["x"][i_tr], tr["y"][i_tr], va["x"][i_va], va["y"][i_va]
    models = _models(config)
    probes = {}
    for probe in probes_wanted:
        if probe == "shift_invariant" and layout["kind"] == "tabular":
            continue
        a, b = _transform(probe, xtr, xva, layout)
        probes[probe] = {
            "validation": _fit_score(a, ytr, b, yva, task_type, classes, models),
        }
        probes[probe]["train_cv"], probes[probe]["train_cv_folds"] = _cv(
            a[i_cv], ytr[i_cv], task_type, classes, models, folds, np.random.default_rng(seed + 1))
        probes[probe]["validation_cv"], probes[probe]["validation_cv_folds"] = _cv(
            b, yva, task_type, classes, models, folds, np.random.default_rng(seed + 2))
        probes[probe]["best_validation"] = _best(probes[probe]["validation"])
    groups = dict(va["groups"])
    if "groups" in inputs:
        with np.load(reader.path("groups"), allow_pickle=False) as data:
            gkey = config.get("groups_key", "groups")
            gids, gval = np.asarray(data["ids"]), np.asarray(data[gkey])
        position = {v: i for i, v in enumerate(gids.tolist())}
        if any(v not in position for v in va["ids"].tolist()):
            raise DiagnosisConfigError("the groups input does not cover every validation id")
        groups["groups:" + gkey] = gval[[position[v] for v in va["ids"].tolist()]]
    if "predictions" in inputs:
        pred = _align_predictions(reader, va, config.get("prediction_key", "predictions"))
        source = "baseline predictions (input port predictions)"
    else:
        pred = _ridge_fit_predict(xtr, ytr, va["x"], task_type, classes, models["ridge"])
        source = "no baseline predictions given: a quick ridge reference fitted on train"
    hypotheses = _hypotheses(task_type, probes, min_effect, metric)
    report = {
        "schema": REPORT_SCHEMA, "exploratory": True, "claim": None,
        "disclaimer": "Exploratory diagnosis on visible train and validation data. Probes are quick refits ranked on "
                      "validation; they suggest what to try, they are not evidence of improvement on new data.",
        "task_type": task_type, "task_type_source": task_source, "layout": layout, "metric": metric,
        "split_statistics": _split_statistics(tr, va, task_type),
        "error_slices": _error_slices(task_type, va, pred, source, bins, groups),
        "invariance_probes": {
            "models": models, "cv_folds": folds,
            "rows": {"train": int(n_tr), "validation": int(n_va), "within_split_cv": int(len(i_cv))},
            "note": "validation = fitted on train, scored on validation; train_cv and validation_cv = cross-validation "
                    "within one split at equal size. Every probe is followed by train-fitted standardization.",
            "probes": probes,
        },
        "hypotheses": hypotheses,
        "provenance": {
            "component": COMPONENT, "seed": seed, "numpy": np.__version__, "python": platform.python_version(),
            "inputs": {port: _digest(reader.path(port)) for port in sorted(inputs)},
            "config": config, "rows_digest": "sha256:" + hashlib.sha256(
                json.dumps([i_tr.tolist(), i_va.tolist(), i_cv.tolist()]).encode()).hexdigest(),
            "data_read": sorted(inputs), "test_data": "none: no test or held-out port exists",
        },
    }
    writer.stage_json("report", report, schema=REPORT_SCHEMA)
    top = hypotheses[0]
    context.record("top_hypothesis", top["cause"])
    return {"metrics": {"baseline_validation_%s" % ("error_rate" if task_type == "classification" else "rmse"):
                        report["error_slices"]["overall"].get("error_rate", report["error_slices"]["overall"].get("rmse")),
                        "reference_probe_validation_%s" % metric: probes["none"]["best_validation"],
                        "top_hypothesis_score": top["score"]},
            "top_hypothesis": top["cause"], "hypotheses": [h["cause"] for h in hypotheses], "exploratory": True}
