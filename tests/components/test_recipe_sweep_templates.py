"""Author tests for the typed template mode of eidos.search.recipe-sweep (not independent qualification).

``config.template`` crosses preprocessing with model families on NPZ ``train`` / ``validation`` ports, screens every recipe
on validation, cross-validates the best finalists within train only, and writes ``evidence_table`` and ``best_recipe``.
The recipes are real numpy fits, so the component runs in the numpy interpreter (``EIDOS_NUMPY_PYTHON``); the lab
interpreter that runs pytest may not have numpy, so every NPZ is generated, and every NPZ output read, by a small script
run with that interpreter. Expected values are derived here (counts, orderings, the planted-offset effect, the metric a
reproduction must give), never copied back from the component's own output.

Runtime note: ``svm`` and ``gbm`` need scikit-learn and are slow to start, so only the end-to-end quick run and its
planted-offset check include them (when the numpy interpreter has scikit-learn). Every other test restricts families.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import textwrap
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import COMPONENTS, ROOT, run_component

NAME = "eidos.search.recipe-sweep"
MANIFEST = json.loads((COMPONENTS / NAME / "eidos-component.json").read_text())
ENTRY = MANIFEST["entry"]
RUNNER = COMPONENTS / NAME / "templates" / "fit_recipe.py"
NPZ = "application/x-npz"

NUMPY_PY = os.environ.get("EIDOS_NUMPY_PYTHON")
if not NUMPY_PY or not Path(NUMPY_PY).exists():
    pytest.skip("set EIDOS_NUMPY_PYTHON to an interpreter with numpy", allow_module_level=True)

PREPROCESS = ("none", "standardize", "per_example_center", "per_example_standardize")
CENTERED = ("per_example_center", "per_example_standardize")
NUMPY_FAMILIES = ("linear", "knn", "kernel", "mlp")
SKLEARN_FAMILIES = ("svm", "gbm")
# Independent statement of what each template name must expand to: settings per family (the quick values, the broad counts).
QUICK = {"linear": {"alpha": 1e-3}, "knn": {"k": 5}, "kernel": {"alpha": 1e-2}, "mlp": {"hidden": 64},
         "svm": {"C": 3.0}, "gbm": {"lr": 0.1}}
BROAD_SETTINGS = {"linear": [{"alpha": 1e-4}, {"alpha": 1e-2}], "knn": [{"k": 1}, {"k": 5}, {"k": 15}],
                  "kernel": [{"alpha": 1e-3}, {"alpha": 1e-1}], "mlp": [{"hidden": 64}, {"hidden": 256}]}

# -- synthetic data, written by the numpy interpreter ----------------------------------------------------------------------
GENERATOR = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    import numpy as np

    out = Path(sys.argv[1])


    def smooth(a, width=5):
        return np.convolve(a, np.ones(width) / width, mode="same")


    # blobs: 3 overlapping classes, 16 features; validation ids are strings, train ids integers
    rng = np.random.default_rng(1)
    means = rng.normal(0.0, 0.55, (3, 16))
    def blobs(n):
        y = rng.integers(0, 3, n)
        return means[y] + rng.normal(0.0, 1.0, (n, 16)), y
    xt, yt = blobs(200)
    xv, yv = blobs(100)
    np.savez(out / "blobs_train.npz", x=xt, y=yt, ids=np.arange(200))
    np.savez(out / "blobs_validation.npz", x=xv, y=yv, ids=np.array(["v%03d" % i for i in range(100)]))
    np.savez(out / "blobs_predict.npz", x=xv[:7], ids=np.array(["p%d" % i for i in range(7)]))
    np.savez(out / "blobs_predict_noids.npz", x=xv[:7])
    bad = xt.copy()
    bad[3, 2] = np.nan
    np.savez(out / "bad_nan_train.npz", x=bad, y=yt)
    np.savez(out / "bad_noy_train.npz", x=xt)

    # offset: image-like. 4 smoothed-gaussian prototypes of 64 features, noise 1.0; the validation examples each carry
    # an extra uniform(1, 3) offset that train does not have
    rng = np.random.default_rng(2)
    protos = np.stack([smooth(rng.normal(0, 1, 64)) for _ in range(4)])
    protos = 0.5 * protos / protos.std()
    def images(n):
        y = rng.integers(0, 4, n)
        return protos[y] + rng.normal(0.0, 1.0, (n, 64)), y
    xt, yt = images(200)
    xv, yv = images(100)
    shifted = xv + rng.uniform(1.0, 3.0, (100, 1))
    np.savez(out / "offset_train.npz", x=xt, y=yt, ids=np.arange(200))
    np.savez(out / "offset_validation.npz", x=shifted, y=yv, ids=np.arange(1000, 1100))
    np.savez(out / "offsetclean_validation.npz", x=xv, y=yv, ids=np.arange(1000, 1100))
    np.savez(out / "offset3d_train.npz", x=xt.reshape(200, 8, 8), y=yt, ids=np.arange(200))
    np.savez(out / "offset3d_validation.npz", x=shifted.reshape(100, 8, 8), y=yv, ids=np.arange(1000, 1100))

    # shortcut: the same kind of images, but in train the class is also written into a per-example offset (0, 1.5, 3, 4.5),
    # which validation does not have: it carries an unrelated uniform(0, 5) offset instead. Train-CV cannot see that shift
    rng = np.random.default_rng(4)
    protos = np.stack([smooth(rng.normal(0, 1, 64)) for _ in range(4)])
    protos = 0.5 * protos / protos.std()
    xt, yt = images(200)
    xv, yv = images(100)
    np.savez(out / "shortcut_train.npz", x=xt + (yt * 1.5)[:, None], y=yt, ids=np.arange(200))
    np.savez(out / "shortcut_validation.npz", x=xv + rng.uniform(0, 5, (100, 1)), y=yv, ids=np.arange(1000, 1100))

    # regress: y = x . w + noise (sigma 0.5), 12 features
    rng = np.random.default_rng(3)
    w = rng.normal(0, 1, 12)
    def regress(n):
        x = rng.normal(0, 1, (n, 12))
        return x, x @ w + rng.normal(0, 0.5, n)
    xt, yt = regress(200)
    xv, yv = regress(100)
    np.savez(out / "regress_train.npz", x=xt, y=yt, ids=np.arange(200))
    np.savez(out / "regress_validation.npz", x=xv, y=yv, ids=np.array(["r%03d" % i for i in range(100)]))
    """
)
REGRESS_SIGMA = 0.5
READ_NPZ = textwrap.dedent(
    """
    import json, sys
    import numpy as np
    z = np.load(sys.argv[1], allow_pickle=False)
    print(json.dumps({k: z[k].tolist() for k in z.files}))
    """
)


def read_npz(path):
    done = subprocess.run([NUMPY_PY, "-c", READ_NPZ, str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout)


def probe_sklearn(python):
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('sklearn') else 1)"
    return subprocess.run([python, "-c", code], capture_output=True, timeout=60).returncode == 0


class Data:
    def __init__(self, root: Path):
        self.root = root

    def file(self, name: str) -> Path:
        return self.root / name

    def inputs(self, kind: str) -> dict:
        return {"train": (self.file(f"{kind}_train.npz"), NPZ), "validation": (self.file(f"{kind}_validation.npz"), NPZ)}


@pytest.fixture(scope="module", autouse=True)
def single_thread():
    """One BLAS/OpenMP thread: small fits are faster and bit-for-bit repeatable (the sweep passes these variables on)."""
    with pytest.MonkeyPatch.context() as patch:
        for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            patch.setenv(variable, "1")
        yield


@pytest.fixture(scope="session")
def data(tmp_path_factory):
    script = tmp_path_factory.mktemp("generator") / "generate.py"
    script.write_text(GENERATOR)
    out = tmp_path_factory.mktemp("data")
    done = subprocess.run([NUMPY_PY, str(script), str(out)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return Data(out)


@pytest.fixture(scope="session")
def has_sklearn():
    return probe_sklearn(NUMPY_PY)


@pytest.fixture(scope="session")
def nosk_python(tmp_path_factory, has_sklearn):
    """An interpreter with numpy and without scikit-learn, for the path where svm and gbm are unavailable.

    When the numpy interpreter already lacks scikit-learn it is used as is. Otherwise a throwaway venv is made from it
    (``-m venv``, no network) whose only addition is a ``.pth`` file that makes the numpy interpreter's site-packages
    importable and sets ``sys.modules["sklearn"] = None``: ``importlib.util.find_spec("sklearn")`` is then None and
    ``import sklearn`` raises ImportError, in the component and in every evaluation it starts (the .pth belongs to the venv,
    so it survives the evaluations' scrubbed environment). A venv built by ``uv pip install numpy`` would also work but
    needs an offline wheel cache; this does not.
    """
    if not has_sklearn:
        return NUMPY_PY
    venv = tmp_path_factory.mktemp("nosk") / "venv"
    made = subprocess.run([NUMPY_PY, "-m", "venv", "--without-pip", str(venv)], capture_output=True, text=True, timeout=120)
    python = venv / "bin" / "python"
    if made.returncode or not python.exists():
        pytest.skip(f"could not create a throwaway venv: {made.stderr[-300:]}")
    purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    real = subprocess.run([NUMPY_PY, "-c", "import numpy, pathlib; print(pathlib.Path(numpy.__file__).resolve().parents[1])"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    (Path(purelib) / "no_sklearn.pth").write_text(f"{real}\nimport sys; sys.modules['sklearn'] = None\n")
    check = subprocess.run([str(python), "-c", "import numpy, importlib.util as u; assert u.find_spec('sklearn') is None"],
                           capture_output=True, text=True, timeout=60)
    if check.returncode:
        pytest.skip(f"the throwaway venv does not behave as scikit-learn-free: {check.stderr[-300:]}")
    return str(python)


# -- running the component ------------------------------------------------------------------------------------------------
def tpl(**kw):
    """A template block; a keyword given as None is left out."""
    base = {"name": "quick", "task_type": "classification", "seeds": [1], **kw}
    return {k: v for k, v in base.items() if v is not None}


def run_sweep(root, data, template, *, label, kind="blobs", config=None, extra_inputs=None, drop=(), python=None, cwd=None):
    inputs = {k: v for k, v in data.inputs(kind).items() if k not in drop}
    inputs.update(extra_inputs or {})
    return run_component(root, NAME, ENTRY, inputs=inputs, config={"template": template, **(config or {})},
                         python=python or NUMPY_PY, cwd=cwd, label=label)


@pytest.fixture()
def sweep(tmp_path, data):
    """sweep(template, kind=..., config=..., ...) runs the component once; each call gets its own run directory."""
    counter = itertools.count(1)

    def go(template, **kw):
        return run_sweep(tmp_path, data, template, label=f"s{next(counter)}", **kw)

    return go


def refused(res, *fragments):
    assert res.manifest.status == "failed", res.receipt
    message = res.manifest.error["message"]
    for fragment in fragments:
        assert fragment in message, message
    assert not list(Path(res.invocation.scratch_dir).rglob("trial-*")), "a refused template must not have run anything"
    assert "evidence_table" not in res.slots() and "best_recipe" not in res.slots()
    return message


COUNTERS = {"seed", "train_rows", "validation_rows"}  # numeric receipt fields the table summarizes next to the scores


def scores(metrics):
    """The score metrics of a recipe's (or evaluation's) metrics, without the run's row and seed counters."""
    return set(metrics) - COUNTERS


def key(row):
    return row["parameters"]["family"], row["parameters"]["preprocess"]


def by_key(table):
    rows = {}
    for row in table["recipes"]:
        assert key(row) not in rows, f"{key(row)} appears twice"
        rows[key(row)] = row
    return rows


def recomputed_digest(table):
    """The evidence digest from the staged table: everything recorded except wall seconds, finalists' CV runs included."""
    def timeless(evaluations):
        return [{k: v for k, v in e.items() if k != "seconds"} for e in evaluations]

    stable = []
    for row in table["recipes"]:
        item = {**row, "evaluations": timeless(row["evaluations"])}
        if isinstance(row.get("train_cv"), dict):
            item["train_cv"] = {**row["train_cv"], "evaluations": timeless(row["train_cv"]["evaluations"])}
        stable.append(item)
    return "sha256:" + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def trial_dirs(res, prefix=""):
    return sorted((Path(res.invocation.scratch_dir) / "sweep").glob(f"trial-{prefix}*"))


# -- 1. end to end, deterministic ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def quick_pair(tmp_path_factory, data):
    """The full quick template (every preprocessing x every available family) twice, same seeds, on the planted-offset images."""
    root = tmp_path_factory.mktemp("quick")
    with ThreadPoolExecutor(max_workers=2) as pool:  # two independent worker processes at once: half the wall time
        runs = list(pool.map(lambda label: run_sweep(root, data, tpl(seeds=[5]), kind="offset", label=label),
                             ("first", "second")))
    for res in runs:
        assert res.ok, res.manifest.error
    return runs


def test_quick_template_covers_every_recipe_and_stops_when_the_space_is_exhausted(quick_pair, has_sklearn):
    res = quick_pair[0]
    table, best = res.json("evidence_table"), res.json("best_recipe")
    families = [*NUMPY_FAMILIES, *(SKLEARN_FAMILIES if has_sklearn else ())]
    rows = by_key(table)
    assert set(rows) == {(f, p) for f in families for p in PREPROCESS}  # one setting per family, every preprocessing
    assert len(table["recipes"]) == len(PREPROCESS) * len(families) == table["counts"]["evaluated"]
    assert table["counts"] == {"space_size": len(rows), "evaluated": len(rows), "usable": len(rows), "failed": 0,
                               "finalists": 3, "finalists_cross_validated": 3}
    for (family, _), row in rows.items():
        assert row["status"] == "ok" and row["parameters"]["settings"] == QUICK[family]
        assert [(e["seed"], e["fold"], e["status"]) for e in row["evaluations"]] == [(5, 0, "ok")]
        assert scores(row["metrics"]) == {"validation_accuracy"} and 0.0 <= row["objective"]["mean"] <= 1.0
    assert table["stop_reason"] == res.receipt["stop_reason"] == "space-exhausted"
    assert table["finalist_stop_reason"] == "space-exhausted"
    # svm and gbm are present exactly when scikit-learn is importable; otherwise they are named as skipped
    skipped = table["template"]["skipped_families"]
    assert set(skipped) == (set() if has_sklearn else set(SKLEARN_FAMILIES))
    assert table["template"]["families"] == sorted(families)
    assert table["template"]["preprocess"] == sorted(PREPROCESS)
    assert table["space"] == {"mode": "template", "recipes": len(rows)}
    assert table["objective"] == {"metric": "validation_accuracy", "direction": "maximize"}
    # the receipt and the best recipe
    assert best["status"] == "found" and best["recipe"]["recipe_id"] == res.receipt["best_recipe_id"]
    assert best["recipe"]["recipe_id"] == table["recipes"][0]["recipe_id"] and table["recipes"][0]["rank"] == 1
    assert best["recipe"]["cross_validated"] is True
    assert res.receipt["template"] == "quick" and res.receipt["task_type"] == "classification"
    assert res.receipt["metrics"]["recipes_evaluated"] == len(rows) and res.receipt["metrics"]["recipes_failed"] == 0
    assert res.receipt["metrics"]["best_objective"] == pytest.approx(best["recipe"]["objective"]["mean"])
    assert table["exploratory"] is True and table["claim"] is None and best["lock_required"] is True


def test_the_same_seeds_give_the_same_ranking_and_every_objective_mean(quick_pair):
    first, second = (res.json("evidence_table") for res in quick_pair)
    assert [r["recipe_id"] for r in first["recipes"]] == [r["recipe_id"] for r in second["recipes"]]
    assert [r["rank"] for r in first["recipes"]] == list(range(1, len(first["recipes"]) + 1))
    assert [r["objective"]["mean"] for r in first["recipes"]] == [r["objective"]["mean"] for r in second["recipes"]]
    for a, b in zip(first["recipes"], second["recipes"], strict=True):
        assert a["metrics"] == b["metrics"] and a["stage"] == b["stage"]
        assert a.get("finalist_rank") == b.get("finalist_rank") and a.get("transfer_gap") == b.get("transfer_gap")
        if a["stage"] == "finalist":
            assert a["train_cv"]["objective"] == b["train_cv"]["objective"]
    assert quick_pair[0].json("best_recipe")["recipe"]["recipe_id"] == quick_pair[1].json("best_recipe")["recipe"]["recipe_id"]


def test_the_same_seeds_give_the_same_evidence_digest_and_it_covers_the_finalists_cv(quick_pair):
    a, b = quick_pair
    digest = a.receipt["evidence_digest"]
    assert digest == b.receipt["evidence_digest"]
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    table = a.json("evidence_table")
    assert digest == recomputed_digest(table)
    # the digest is not blind to a finalist's cross-validation: change one number and it moves
    finalist = next(r for r in table["recipes"] if r["stage"] == "finalist")
    cv_metrics = finalist["train_cv"]["evaluations"][0]["metrics"]
    recorded = cv_metrics["train_cv_accuracy"]
    cv_metrics["train_cv_accuracy"] = recorded + 0.5
    assert recomputed_digest(table) != digest
    # wall seconds are the one thing it ignores
    cv_metrics["train_cv_accuracy"] = recorded
    finalist["train_cv"]["evaluations"][0]["seconds"] += 100.0
    finalist["evaluations"][0]["seconds"] += 100.0
    assert recomputed_digest(table) == digest


# -- 2. a planted per-example offset ---------------------------------------------------------------------------------------
def test_per_example_centering_wins_when_validation_carries_a_per_example_offset(quick_pair, has_sklearn):
    table, best = quick_pair[0].json("evidence_table"), quick_pair[0].json("best_recipe")
    rows = by_key(table)
    assert best["recipe"]["parameters"]["preprocess"] in CENTERED
    assert best["recipe"]["objective"]["mean"] > 0.85
    families = {family for family, _ in rows}
    assert families == set(NUMPY_FAMILIES) | (set(SKLEARN_FAMILIES) if has_sklearn else set())
    for family in sorted(families):
        centered = max(rows[family, pre]["objective"]["mean"] for pre in CENTERED)
        plain = rows[family, "none"]["objective"]["mean"]
        assert centered > plain + 0.1, f"{family}: centered {centered} vs none {plain}"
        # train-fitted standardization cannot remove an offset that train never had
        assert rows[family, "standardize"]["objective"]["mean"] < centered - 0.1, family


# -- 3. regression ----------------------------------------------------------------------------------------------------------
def test_a_regression_template_minimizes_rmse_and_ranks_ascending(sweep):
    seeds, folds = [1, 2], 2
    res = sweep(tpl(task_type="regression", families=["linear", "knn", "kernel"], preprocess=["none", "standardize"],
                    seeds=seeds, finalists=2, cv_folds=folds), kind="regress")
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    assert table["objective"] == best["objective"] == {"metric": "validation_rmse", "direction": "minimize"}
    assert res.receipt["task_type"] == "regression" and table["template"]["task_type"] == "regression"
    rows = table["recipes"]
    assert len(rows) == 6 and all(r["status"] == "ok" for r in rows)
    means = [r["objective"]["mean"] for r in rows]
    assert means == sorted(means) and [r["rank"] for r in rows] == list(range(1, 7))  # best (lowest) first
    for row in rows:
        assert row["objective"]["metric"] == "validation_rmse"
        assert scores(row["metrics"]) == {"validation_rmse", "validation_mae"}
        assert [(e["seed"], e["fold"]) for e in row["evaluations"]] == [(s, 0) for s in seeds]
        assert row["metrics"]["validation_mae"]["mean"] <= row["metrics"]["validation_rmse"]["mean"]
    finalists = [r for r in rows if r["stage"] == "finalist"]
    assert [r["rank"] for r in finalists] == [1, 2] and all(r["stage"] == "screened" for r in rows[2:])
    for row in finalists:
        cv = row["train_cv"]
        assert cv["status"] == "ok" and cv["objective"]["metric"] == "train_cv_rmse"
        assert scores(cv["metrics"]) == {"train_cv_rmse", "train_cv_mae"}
        assert [(e["seed"], e["fold"]) for e in cv["evaluations"]] == list(itertools.product(seeds, range(folds)))
        # for a loss, how much worse validation is than train-CV: validation minus train-CV
        assert row["transfer_gap"] == pytest.approx(row["objective"]["mean"] - cv["objective"]["mean"])
    assert best["status"] == "found" and best["recipe"]["recipe_id"] == rows[0]["recipe_id"]
    assert best["recipe"]["train_cv"]["objective"]["metric"] == "train_cv_rmse"
    # the data are linear plus noise of sigma 0.5: ridge reaches about sigma, and no knn or kernel recipe beats it
    assert best["recipe"]["parameters"]["family"] == "linear"
    assert 0.6 * REGRESS_SIGMA < best["recipe"]["objective"]["mean"] < 1.6 * REGRESS_SIGMA
    assert all(r["objective"]["mean"] > best["recipe"]["objective"]["mean"] for r in rows if r["parameters"]["family"] != "linear")


def test_a_classification_template_maximizes_accuracy_and_its_transfer_gap_is_cv_minus_validation(sweep):
    res = sweep(tpl(families=["linear", "knn"], preprocess=["none", "per_example_center"], finalists=2, cv_folds=2))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert table["objective"] == {"metric": "validation_accuracy", "direction": "maximize"}
    means = [r["objective"]["mean"] for r in table["recipes"]]
    assert means == sorted(means, reverse=True)
    for row in (r for r in table["recipes"] if r["stage"] == "finalist"):
        assert row["train_cv"]["objective"]["metric"] == "train_cv_accuracy"
        assert row["transfer_gap"] == pytest.approx(row["train_cv"]["objective"]["mean"] - row["objective"]["mean"])


# -- 4. finalists -----------------------------------------------------------------------------------------------------------
FINALIST_TEMPLATE = {"families": ["linear", "knn"], "preprocess": ["none", "per_example_center"],
                     "seeds": [1, 2], "cv_folds": 3, "finalists": 3}


@pytest.fixture(scope="module")
def finalist_runs(tmp_path_factory, data):
    """4 recipes, 3 finalists, on the shortcut data: train-CV and validation disagree about which finalist is best."""
    root = tmp_path_factory.mktemp("finalists")
    ranks = ("validation", "train-cv")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = pool.map(lambda r: run_sweep(root, data, tpl(**FINALIST_TEMPLATE, rank_by=r), kind="shortcut", label=r), ranks)
        runs = dict(zip(ranks, results, strict=True))
    for res in runs.values():
        assert res.ok, res.manifest.error
    return runs


def test_exactly_the_requested_number_of_finalists_are_cross_validated_within_train(finalist_runs):
    res = finalist_runs["validation"]
    table, best = res.json("evidence_table"), res.json("best_recipe")
    seeds, folds, n = FINALIST_TEMPLATE["seeds"], FINALIST_TEMPLATE["cv_folds"], FINALIST_TEMPLATE["finalists"]
    rows = table["recipes"]
    assert len(rows) == 4
    finalists = [r for r in rows if r["stage"] == "finalist"]
    assert [r["stage"] for r in rows] == ["finalist"] * n + ["screened"] * (len(rows) - n)  # the n best by validation
    assert [r["rank"] for r in finalists] == [1, 2, 3]
    for row in rows[n:]:
        assert "train_cv" not in row and "finalist_rank" not in row and "transfer_gap" not in row
    for row in finalists:
        cv = row["train_cv"]
        assert cv["status"] == "ok" and cv["objective"]["n"] == folds * len(seeds) == 6
        pairs = [(e["seed"], e["fold"]) for e in cv["evaluations"]]
        assert pairs == list(itertools.product(seeds, range(folds)))  # folds 0..K-1 for every seed
        assert {fold for _, fold in pairs} == set(range(folds))
        assert all(e["status"] == "ok" and 0.0 <= e["metrics"]["train_cv_accuracy"] <= 1.0 for e in cv["evaluations"])
        screened = row["evaluations"]
        assert [(e["seed"], e["fold"]) for e in screened] == [(s, 0) for s in seeds]  # one validation fold per seed
    assert [r["finalist_rank"] for r in finalists] == [1, 2, 3]
    assert table["counts"]["finalists"] == table["counts"]["finalists_cross_validated"] == n
    assert table["seed_policy"] == {"seeds": seeds, "folds": 1, "finalist_cv_folds": folds}
    assert table["template"]["finalists"] == n and table["template"]["cv_folds"] == folds
    assert table["finalist_stop_reason"] == "space-exhausted"
    # rank_by validation (the default): the best finalist is the best screened recipe
    assert table["template"]["rank_by"] == "validation" and best["template"]["rank_by"] == "validation"
    assert best["status"] == "found" and best["recipe"]["recipe_id"] == rows[0]["recipe_id"]
    assert best["recipe"]["cross_validated"] is True
    assert best["recipe"]["train_cv"] == rows[0]["train_cv"] and best["recipe"]["transfer_gap"] == rows[0]["transfer_gap"]
    # one work dir per screening evaluation and per CV evaluation, and nothing else
    assert len(trial_dirs(res, "screen-")) == len(rows) * len(seeds)
    assert len(trial_dirs(res, "cv-")) == n * len(seeds) * folds
    assert len(trial_dirs(res)) == len(trial_dirs(res, "screen-")) + len(trial_dirs(res, "cv-"))


def test_rank_by_train_cv_picks_the_finalist_with_the_best_train_cv_mean(finalist_runs):
    by_validation, by_cv = finalist_runs["validation"], finalist_runs["train-cv"]
    table, best = by_cv.json("evidence_table"), by_cv.json("best_recipe")
    reference, validated_best = by_validation.json("evidence_table"), by_validation.json("best_recipe")
    assert table["template"]["rank_by"] == best["template"]["rank_by"] == "train-cv"
    finalists = [r for r in table["recipes"] if r["stage"] == "finalist"]
    assert len(finalists) == 3
    cv_means = {r["recipe_id"]: r["train_cv"]["objective"]["mean"] for r in finalists}
    top = max(cv_means.values())
    # ties go to the better validation rank, which is the table order
    winner = next(r for r in finalists if cv_means[r["recipe_id"]] == top)
    assert best["status"] == "found" and best["recipe"]["recipe_id"] == winner["recipe_id"]
    assert best["recipe"]["train_cv"]["objective"]["mean"] == top and best["recipe"]["cross_validated"] is True
    ordered = sorted(finalists, key=lambda r: r["finalist_rank"])
    assert [r["finalist_rank"] for r in ordered] == [1, 2, 3]
    assert [cv_means[r["recipe_id"]] for r in ordered] == sorted(cv_means.values(), reverse=True)
    assert ordered[0]["recipe_id"] == winner["recipe_id"]
    # screening is the same either way: the same table rows, the same numbers, the same stages
    for a, b in zip(table["recipes"], reference["recipes"], strict=True):
        assert (a["recipe_id"], a["rank"], a["objective"], a["stage"]) == (b["recipe_id"], b["rank"], b["objective"], b["stage"])
        if a["stage"] == "finalist":
            assert a["train_cv"]["objective"] == b["train_cv"]["objective"]
    # the planted shortcut makes the two criteria disagree. Validation picks the recipe that ignores the train-only offset;
    # train-CV, which cannot see the shift, picks one that learned it, and its transfer gap shows how wrong that is
    assert validated_best["recipe"]["parameters"]["preprocess"] == "per_example_center"
    assert best["recipe"]["parameters"]["preprocess"] == "none"
    assert best["recipe"]["recipe_id"] != validated_best["recipe"]["recipe_id"]
    assert best["recipe"]["train_cv"]["objective"]["mean"] > 0.95 and best["recipe"]["objective"]["mean"] < 0.6
    assert best["recipe"]["transfer_gap"] > 0.3
    assert abs(validated_best["recipe"]["transfer_gap"]) < 0.15
    assert validated_best["recipe"]["recipe_id"] == reference["recipes"][0]["recipe_id"]


def test_zero_finalists_gives_a_full_table_and_the_best_screened_recipe_marked_not_cross_validated(sweep):
    res = sweep(tpl(families=["linear", "knn"], preprocess=["none", "standardize", "per_example_center"], finalists=0))
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    rows = table["recipes"]
    assert len(rows) == 6 and all(r["status"] == "ok" and r["stage"] == "screened" for r in rows)
    assert [r["rank"] for r in rows] == list(range(1, 7))
    assert all("train_cv" not in r and "finalist_rank" not in r and "transfer_gap" not in r for r in rows)
    assert table["counts"]["finalists"] == table["counts"]["finalists_cross_validated"] == 0
    assert table["stop_reason"] == "space-exhausted" and table["finalist_stop_reason"] == "not-run"
    assert not trial_dirs(res, "cv-") and len(trial_dirs(res, "screen-")) == 6
    # no finalist won, so the best screened recipe is reported, and says it was never cross-validated
    assert best["status"] == "found" and best["recipe"]["recipe_id"] == rows[0]["recipe_id"]
    assert best["recipe"]["cross_validated"] is False
    assert best["recipe"]["train_cv"] is None and best["recipe"]["transfer_gap"] is None
    assert res.receipt["best_recipe_id"] == rows[0]["recipe_id"] and "best_objective" in res.receipt["metrics"]


def test_zero_finalists_under_rank_by_train_cv_has_no_best_recipe(sweep):
    res = sweep(tpl(families=["linear"], preprocess=["none", "standardize"], finalists=0, rank_by="train-cv"))
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    assert len(table["recipes"]) == 2 and all(r["stage"] == "screened" for r in table["recipes"])
    assert best["status"] == "none" and best["recipe"] is None
    assert res.receipt["best_recipe_id"] is None and "best_objective" not in res.receipt["metrics"]
    assert res.receipt["metrics"]["recipes_evaluated"] == 2


def test_more_finalists_than_recipes_cross_validates_every_recipe(sweep):
    res = sweep(tpl(families=["linear"], preprocess=["none", "standardize"], finalists=10, cv_folds=2))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert [r["stage"] for r in table["recipes"]] == ["finalist", "finalist"]
    assert table["counts"]["finalists"] == 2 and table["template"]["finalists"] == 10


# -- 5. task type -----------------------------------------------------------------------------------------------------------
CHEAP = {"families": ["linear"], "preprocess": ["none"], "finalists": 0}


@pytest.mark.parametrize(("kind", "task", "template_task", "expected_metric", "expected_direction"), [
    ("blobs", {"task_type": "classification"}, None, "validation_accuracy", "maximize"),
    ("regress", {"task_type": "regression", "task_id": "t-1"}, None, "validation_rmse", "minimize"),
    ("regress", {"task_type": "classification"}, "regression", "validation_rmse", "minimize"),  # the template wins
])
def test_task_type_comes_from_the_task_port_when_the_template_omits_it(sweep, kind, task, template_task, expected_metric,
                                                                       expected_direction):
    res = sweep(tpl(task_type=template_task, **CHEAP), kind=kind, extra_inputs={"task": (task, "application/json")})
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert table["objective"] == {"metric": expected_metric, "direction": expected_direction}
    assert res.receipt["task_type"] == table["template"]["task_type"] == (template_task or task["task_type"])
    assert expected_metric in scores(table["recipes"][0]["metrics"])


@pytest.mark.parametrize(("template_task", "task"), [
    (None, None),                                      # missing everywhere
    (None, {"task_id": "t-1"}),                        # the task port names no task_type
    (None, {"task_type": "ranking"}),                  # an unknown task_type from the port
    ("ranking", {"task_type": "classification"}),      # an unknown task_type in the template is not rescued by the port
])
def test_a_missing_or_unknown_task_type_is_refused(sweep, template_task, task):
    extra = None if task is None else {"task": (task, "application/json")}
    res = sweep(tpl(task_type=template_task, **CHEAP), extra_inputs=extra)
    refused(res, "task_type", "classification or regression")


# -- 6. refusals: status failed and nothing run ----------------------------------------------------------------------------
BAD_COMMAND = {"command": {"argv": ["python", "project.py"]}}
SPACE = {"space": {"mode": "grid", "parameters": {"x": {"values": [1]}}}}
REFUSALS = {
    "unknown-key": (tpl(bogus=1), {}, ["unknown template keys", "bogus"]),
    "unknown-name": (tpl(name="slow"), {}, ["template.name must be one of"]),
    "missing-name": ({"task_type": "classification"}, {}, ["template.name must be one of"]),
    "unknown-family": (tpl(families=["linear", "forest"]), {}, ["forest"]),
    "unknown-preprocess": (tpl(preprocess=["whiten"]), {}, ["whiten"]),
    "bad-rank-by": (tpl(rank_by="test"), {}, ["template.rank_by must be validation or train-cv"]),
    "one-cv-fold": (tpl(cv_folds=1), {}, ["cv_folds"]),
    "negative-finalists": (tpl(finalists=-1), {}, ["finalists"]),
    "seeds-not-integers": (tpl(seeds=["a"]), {}, ["template.seeds"]),
    "seeds-bool": (tpl(seeds=[True]), {}, ["template.seeds"]),
    "seeds-repeated": (tpl(seeds=[11, 11]), {}, ["template.seeds", "must not repeat a seed"]),
    "seeds-repeated-apart": (tpl(seeds=[3, 8, 3]), {}, ["template.seeds", "must not repeat a seed"]),
    "template-not-an-object": ("quick", {}, ["template must be an object"]),
    "with-command": (tpl(), {"config": BAD_COMMAND}, ["a template builds its own", "command"]),
    "with-space": (tpl(), {"config": SPACE}, ["a template builds its own", "space"]),
    "with-objective": (tpl(), {"config": {"objective": {"metric": "test_accuracy"}}}, ["a template builds its own", "objective"]),
    "with-recipe": (tpl(), {"config": {"recipe": {"lr": 1}}}, ["a template builds its own", "recipe"]),
    "with-source": (tpl(), {"extra_inputs": {"source": ({"files": {"project.py": "x = 1"}}, "application/json")}},
                    ["not a source bundle"]),
    "no-validation": (tpl(), {"drop": ("validation",)}, ["needs the validation port"]),
    "no-train": (tpl(), {"drop": ("train",)}, ["needs the train port"]),
    "stopping-patience": (tpl(), {"config": {"stopping": {"rule": "patience", "patience": 2}}}, ["stopping.rule must be none"]),
    "stopping-target": (tpl(), {"config": {"stopping": {"rule": "target", "target": 0.9}}}, ["stopping.rule must be none"]),
    "extra-test-port": (tpl(), {"extra_inputs": {"test": (b"npz", NPZ)}}, ["undeclared input ports", "test"]),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_bad_template_is_refused_before_anything_runs(sweep, case):
    template, kw, fragments = REFUSALS[case]
    if isinstance(template, dict):  # keep the refusal cheap even if it were accepted
        template = {**CHEAP, **template}
    refused(sweep(template, **kw), *fragments)


@pytest.mark.parametrize("port", ["test_inputs", "holdout", "heldout_labels", "labels"])
def test_a_test_or_held_out_port_is_refused_with_a_template_too(sweep, port):
    refused(sweep(tpl(**CHEAP), extra_inputs={port: (b"npz", NPZ)}), "undeclared input ports", port)


def test_stopping_rule_none_is_accepted_with_a_template(sweep):
    res = sweep(tpl(**CHEAP), config={"stopping": {"rule": "none"}})
    assert res.ok, res.manifest.error
    assert res.json("evidence_table")["stopping"] == {"rule": "none"}


# -- 7. no test-data access --------------------------------------------------------------------------------------------------
def test_evaluations_only_ever_see_train_validation_and_the_task(data, tmp_path):
    cwd = tmp_path / "project-dir"
    cwd.mkdir()
    for planted in ("test.npz", "heldout.npz", "holdout.csv", "test_labels.json"):
        (cwd / planted).write_bytes(b"planted held-out data")
    task = ({"task_type": "classification"}, "application/json")
    with_task = run_sweep(tmp_path, data, tpl(families=["linear", "knn"], preprocess=["none"], finalists=1, cv_folds=2),
                          label="with-task", extra_inputs={"task": task}, cwd=cwd)
    without_task = run_sweep(tmp_path, data, tpl(families=["linear", "knn"], preprocess=["none"], finalists=1, cv_folds=2),
                             label="without-task", cwd=cwd)
    for res, expected in ((with_task, {"fit_recipe.py", "task.json", "train.npz", "validation.npz"}),
                          (without_task, {"fit_recipe.py", "train.npz", "validation.npz"})):
        assert res.ok, res.manifest.error
        works = sorted((Path(res.invocation.scratch_dir) / "sweep").glob("trial-*/work"))
        stages = Counter(w.parent.name.split("-")[1] for w in works)
        assert stages == {"screen": 2, "cv": 2}  # 2 recipes screened; 1 finalist x 2 folds
        for work in works:
            assert {p.name for p in work.iterdir()} == expected, work
            assert (work / "train.npz").read_bytes() == data.file("blobs_train.npz").read_bytes()
            assert (work / "validation.npz").read_bytes() == data.file("blobs_validation.npz").read_bytes()
            assert (work / "fit_recipe.py").read_text() == RUNNER.read_text()


def test_the_objective_is_always_a_validation_metric_and_a_test_metric_cannot_be_chosen(sweep):
    res = sweep(tpl(families=["linear", "knn"], preprocess=["none"], finalists=1, cv_folds=2))
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    assert table["objective"]["metric"] == best["objective"]["metric"] == "validation_accuracy"
    assert table["selection_split"] == best["selection_split"] == "visible-validation"
    seen = set()
    for row in table["recipes"]:
        for evaluation in row["evaluations"]:
            seen |= scores(evaluation["metrics"])
        if row["stage"] == "finalist":
            for evaluation in row["train_cv"]["evaluations"]:
                seen |= scores(evaluation["metrics"])
    assert seen == {"validation_accuracy", "train_cv_accuracy"}
    # a caller cannot substitute a metric of their own: a template with an objective, test-named or not, is refused
    for metric in ("test_accuracy", "heldout_f1", "validation_accuracy"):
        refused(sweep(tpl(**CHEAP), config={"objective": {"metric": metric}}), "a template builds its own", "objective")
    assert "no test data was given" in table["disclaimer"]


# -- 8. budget ---------------------------------------------------------------------------------------------------------------
def test_a_trials_budget_evaluates_that_many_screened_recipes_and_finalists_come_only_from_them(sweep):
    # 2 families x 4 preprocessing = 8 recipes, in family-major order: linear x (none, standardize, ...), then knn
    res = sweep(tpl(families=["linear", "knn"], finalists=10, cv_folds=2), config={"budget": {"max_trials": 3}})
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    rows = sorted(table["recipes"], key=lambda r: r["index"])
    assert len(rows) == 3 and table["counts"]["evaluated"] == 3 and table["counts"]["space_size"] == 8
    assert table["stop_reason"] == res.receipt["stop_reason"] == "budget-trials"
    assert table["budget"] == {"max_trials": 3}
    assert [r["index"] for r in rows] == [0, 1, 2]
    assert [key(r) for r in rows] == [("linear", "none"), ("linear", "standardize"), ("linear", "per_example_center")]
    assert len(trial_dirs(res, "screen-")) == 3
    # more finalists are allowed than recipes screened, but only screened recipes can be finalists
    finalists = [r for r in rows if r["stage"] == "finalist"]
    assert len(finalists) == 3 and table["counts"]["finalists"] == 3
    assert table["finalist_stop_reason"] == "space-exhausted"
    assert len(trial_dirs(res, "cv-")) == 3 * 2
    assert res.json("best_recipe")["recipe"]["recipe_id"] in {r["recipe_id"] for r in rows}


@pytest.mark.parametrize(("max_trials", "stop_reason", "evaluated"), [
    (1, "budget-trials", 1),
    (2, "space-exhausted", 2),   # exactly the recipe count: the space ran out, the budget did not cut it short
    (50, "space-exhausted", 2),
])
def test_the_trials_budget_boundaries(sweep, max_trials, stop_reason, evaluated):
    res = sweep(tpl(families=["linear"], preprocess=["none", "standardize"], finalists=0),
                config={"budget": {"max_trials": max_trials}})
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert table["stop_reason"] == stop_reason and len(table["recipes"]) == evaluated
    assert table["counts"]["space_size"] == 2


def test_a_time_budget_that_ends_the_screening_skips_cross_validation_and_does_not_crown_a_cv_finalist(sweep):
    # 16 recipes x 5 seeds take far longer than a second; the first recipes are evaluated before the budget ends
    res = sweep(tpl(families=list(NUMPY_FAMILIES), seeds=[1, 2, 3, 4, 5], finalists=3), config={"budget": {"max_seconds": 1}})
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    assert table["stop_reason"] == "budget-seconds" and table["finalist_stop_reason"] == "not-run"
    assert len(table["recipes"]) < 16
    assert not trial_dirs(res, "cv-")
    assert all(r["stage"] == "screened" and "train_cv" not in r for r in table["recipes"])
    usable = [r for r in table["recipes"] if r["status"] == "ok"]
    if usable:
        assert best["status"] == "found" and best["recipe"]["cross_validated"] is False
        assert best["recipe"]["recipe_id"] == usable[0]["recipe_id"] and usable[0]["rank"] == 1
    else:  # a very slow machine: nothing finished in a second, and then nothing is claimed
        assert best["status"] == "none"
    for row in table["recipes"]:
        if row["status"] != "ok":
            assert row["rank"] is None and ("budget ended" in row["error"] or "stopped after" in row["error"])


# -- the template's own expansion --------------------------------------------------------------------------------------------
def test_the_broad_template_has_the_documented_settings_per_family(sweep):
    res = sweep(tpl(name="broad", families=list(NUMPY_FAMILIES), preprocess=["none"], finalists=0))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    rows = sorted(table["recipes"], key=lambda r: r["index"])
    assert [(r["parameters"]["family"], r["parameters"]["settings"]) for r in rows] == [
        (family, settings) for family in NUMPY_FAMILIES for settings in BROAD_SETTINGS[family]
    ]
    assert len(rows) == 9 and table["template"]["name"] == "broad" and res.receipt["template"] == "broad"
    assert len({r["recipe_id"] for r in rows}) == 9


def test_max_rows_caps_the_kernel_family_only(sweep):
    capped = sweep(tpl(families=["linear", "kernel"], preprocess=["per_example_center"], finalists=0, max_rows=2), kind="offset")
    plain = sweep(tpl(families=["linear", "kernel"], preprocess=["per_example_center"], finalists=0), kind="offset")
    assert capped.ok and plain.ok, (capped.manifest.error, plain.manifest.error)
    rows_capped, rows_plain = by_key(capped.json("evidence_table")), by_key(plain.json("evidence_table"))
    assert rows_capped["kernel", "per_example_center"]["parameters"]["settings"] == {"alpha": 1e-2, "max_rows": 2}
    assert rows_capped["linear", "per_example_center"]["parameters"]["settings"] == {"alpha": 1e-3}
    assert rows_plain["kernel", "per_example_center"]["parameters"]["settings"] == {"alpha": 1e-2}
    # two training rows cannot classify four image classes as well as two hundred can
    assert rows_capped["kernel", "per_example_center"]["objective"]["mean"] + 0.2 < \
        rows_plain["kernel", "per_example_center"]["objective"]["mean"]
    assert rows_capped["linear", "per_example_center"]["objective"] == rows_plain["linear", "per_example_center"]["objective"]


def test_seeds_default_to_the_job_seed_and_every_seed_is_evaluated(sweep):
    default = sweep(tpl(seeds=None, **CHEAP))
    assert default.ok, default.manifest.error
    assert default.json("evidence_table")["seed_policy"]["seeds"] == [0]  # the harness job seed
    several = sweep(tpl(seeds=[3, 8, 21], **CHEAP))
    row = several.json("evidence_table")["recipes"][0]
    assert [e["seed"] for e in row["evaluations"]] == [3, 8, 21] and row["objective"]["n"] == 3
    spread = [e["metrics"]["validation_accuracy"] for e in row["evaluations"]]
    assert row["objective"]["mean"] == pytest.approx(sum(spread) / 3)


# -- 9. the reproduction recipe ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("kind", "task_type", "families", "preprocess", "metric"), [
    ("blobs", "classification", ["linear", "knn"], ["none", "standardize"], "validation_accuracy"),
    ("regress", "regression", ["linear", "knn"], ["none", "standardize"], "validation_rmse"),
])
def test_the_reproduce_recipe_rebuilds_the_best_recipes_validation_metric(sweep, data, tmp_path, kind, task_type, families,
                                                                          preprocess, metric):
    res = sweep(tpl(task_type=task_type, families=families, preprocess=preprocess, seeds=[4], finalists=1, cv_folds=2),
                kind=kind)
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    recipe = best["recipe"]
    reproduce = recipe["reproduce"]
    assert reproduce["files"] == ["fit_recipe.py"]
    assert recipe["objective"]["metric"] == metric and recipe["objective"]["n"] == 1
    work = tmp_path / "reproduction"
    work.mkdir()
    shutil.copyfile(data.file(f"{kind}_train.npz"), work / "train.npz")
    shutil.copyfile(data.file(f"{kind}_validation.npz"), work / "validation.npz")
    shutil.copyfile(RUNNER, work / "fit_recipe.py")
    runner_sha = hashlib.sha256((work / "fit_recipe.py").read_bytes()).hexdigest()
    assert runner_sha == table["template"]["runner"]["sha256"] == best["template"]["runner"]["sha256"]
    out = tmp_path / "reproduced"
    argv = [NUMPY_PY if a == "python" else str(out) if a == "<out>" else a for a in reproduce["argv"]]
    assert argv[1:4] == ["fit_recipe.py", "train", "--inputs"] and "--task-type" in argv
    assert argv[argv.index("--seed") + 1] == "4" and argv[argv.index("--task-type") + 1] == task_type
    assert json.loads(argv[argv.index("--params") + 1]) == recipe["parameters"]
    done = subprocess.run(argv, cwd=work, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt[metric] == pytest.approx(recipe["objective"]["mean"], abs=1e-12)  # the single seed's metric is the mean
    assert receipt["seed"] == 4 and receipt["train_rows"] == 200 and receipt["validation_rows"] == 100
    assert json.loads((out / "recipe.json").read_text()) == recipe["parameters"]
    # the predictions carry the validation ids, in order, and score to the same metric
    predictions = read_npz(out / "validation_predictions.npz")
    validation = read_npz(data.file(f"{kind}_validation.npz"))
    assert predictions["ids"] == validation["ids"]
    pred, y = predictions["predictions"], validation["y"]
    assert len(pred) == len(y) == 100
    if task_type == "classification":
        assert sum(p == t for p, t in zip(pred, y, strict=True)) / len(y) == pytest.approx(receipt[metric], abs=1e-12)
    else:
        rmse = math.sqrt(sum((p - t) ** 2 for p, t in zip(pred, y, strict=True)) / len(y))
        assert rmse == pytest.approx(receipt[metric], abs=1e-9)


# -- 10. fit_recipe.py directly -------------------------------------------------------------------------------------------------
def run_runner(*args, python=None, cwd=None):
    return subprocess.run([python or NUMPY_PY, str(RUNNER), *map(str, args)], capture_output=True, text=True, timeout=120,
                          cwd=cwd)


def params(family="linear", preprocess="none", **settings):
    return json.dumps({"preprocess": preprocess, "family": family, "settings": settings})


@pytest.fixture()
def staged(data, tmp_path):
    """stage(kind) copies <kind>_train/validation.npz to <tmp>/inputs-<kind>/train.npz and validation.npz."""
    def stage(kind, *, validation=True):
        target = tmp_path / f"inputs-{kind}"
        target.mkdir(exist_ok=True)
        shutil.copyfile(data.file(f"{kind}_train.npz"), target / "train.npz")
        if validation:
            shutil.copyfile(data.file(f"{kind}_validation.npz"), target / "validation.npz")
        return target

    return stage


@pytest.mark.parametrize(("folds", "fold"), [(1, 0), (0, 0), (3, 3), (3, -1)])
def test_cv_mode_refuses_fewer_than_two_folds_and_a_fold_out_of_range(staged, tmp_path, folds, fold):
    out = tmp_path / "cv-out"
    done = run_runner("cv", "--inputs", staged("blobs"), "--out", out, "--seed", 1, "--fold", fold, "--folds", folds,
                      "--params", params(), "--task-type", "classification")
    assert done.returncode != 0 and "cv needs 0 <= fold < folds and folds >= 2" in done.stderr
    assert not (out / "receipt.json").exists()


def test_cv_mode_without_folds_is_refused_because_folds_default_to_one(staged, tmp_path):
    done = run_runner("cv", "--inputs", staged("blobs"), "--out", tmp_path / "o", "--params", params(),
                      "--task-type", "classification")
    assert done.returncode != 0 and "folds >= 2" in done.stderr


@pytest.mark.parametrize(("kind", "task_type", "metrics"), [
    ("blobs", "classification", {"train_cv_accuracy"}),
    ("regress", "regression", {"train_cv_rmse", "train_cv_mae"}),
])
def test_cv_mode_reads_train_only_and_reports_train_cv_metrics(staged, tmp_path, kind, task_type, metrics):
    inputs = staged(kind, validation=False)  # no validation.npz at all: cross-validation within train must not need it
    receipts = []
    for fold in range(3):
        out = tmp_path / f"cv-{fold}"
        done = run_runner("cv", "--inputs", inputs, "--out", out, "--seed", 7, "--fold", fold, "--folds", 3,
                          "--params", params("knn", k=5), "--task-type", task_type)
        assert done.returncode == 0, done.stderr[-1000:]
        receipt = json.loads((out / "receipt.json").read_text())
        assert set(receipt) == {"seed", "train_rows", *metrics}  # no validation_* key
        receipts.append(receipt)
        assert not (out / "validation_predictions.npz").exists()
    assert all(r["train_rows"] == 200 for r in receipts)
    again = run_runner("cv", "--inputs", inputs, "--out", tmp_path / "again", "--seed", 7, "--fold", 1, "--folds", 3,
                       "--params", params("knn", k=5), "--task-type", task_type)
    assert again.returncode == 0
    assert json.loads((tmp_path / "again" / "receipt.json").read_text()) == receipts[1]  # deterministic for a seed and fold


def test_screen_mode_scores_validation_and_writes_only_a_receipt(staged, tmp_path):
    out = tmp_path / "screen"
    done = run_runner("screen", "--inputs", staged("blobs"), "--out", out, "--seed", 1, "--params", params("knn", k=5),
                      "--task-type", "classification")
    assert done.returncode == 0, done.stderr[-1000:]
    assert sorted(p.name for p in out.iterdir()) == ["receipt.json"]
    receipt = json.loads((out / "receipt.json").read_text())
    assert set(receipt) == {"seed", "train_rows", "validation_rows", "validation_accuracy"} and receipt["validation_rows"] == 100


def test_the_recipe_can_come_from_a_file_instead_of_params(staged, tmp_path):
    inputs, recipe = staged("blobs"), tmp_path / "recipe.json"
    recipe.write_text(params("knn", "standardize", k=3))
    outs = {}
    for label, args in (("file", ["--recipe", recipe]), ("inline", ["--params", params("knn", "standardize", k=3)])):
        outs[label] = tmp_path / label
        done = run_runner("screen", "--inputs", inputs, "--out", outs[label], "--seed", 2, "--task-type", "classification", *args)
        assert done.returncode == 0, done.stderr[-1000:]
    assert (outs["file"] / "receipt.json").read_text() == (outs["inline"] / "receipt.json").read_text()


@pytest.mark.parametrize(("recipe", "fragment"), [
    ({"preprocess": "none", "family": "forest", "settings": {}}, "unknown family"),
    ({"preprocess": "none", "settings": {}}, "unknown family"),
    ({"preprocess": "whiten", "family": "linear", "settings": {}}, "unknown preprocess"),
])
@pytest.mark.parametrize("mode", ["screen", "cv", "train"])
def test_an_unknown_family_or_preprocess_exits_nonzero(staged, tmp_path, mode, recipe, fragment):
    out = tmp_path / "bad"
    done = run_runner(mode, "--inputs", staged("blobs"), "--out", out, "--seed", 1, "--folds", 2, "--params", json.dumps(recipe),
                      "--task-type", "classification")
    assert done.returncode != 0 and fragment in done.stderr
    assert not (out / "receipt.json").exists()


@pytest.mark.parametrize("args", [
    ["--task-type", "classification"],                       # neither --params nor --recipe
    ["--params", params()],                                  # no --task-type
    ["--params", params(), "--task-type", "ranking"],        # a task type the runner does not know
])
def test_missing_or_invalid_arguments_exit_nonzero(staged, tmp_path, args):
    done = run_runner("screen", "--inputs", staged("blobs"), "--out", tmp_path / "o", *args)
    assert done.returncode == 2 and "usage" in done.stderr


@pytest.mark.parametrize(("train_file", "fragment"), [("bad_nan_train.npz", "non-finite"), ("bad_noy_train.npz", "no y array")])
def test_unusable_train_data_is_refused(data, tmp_path, train_file, fragment):
    inputs = tmp_path / "bad-inputs"
    inputs.mkdir()
    shutil.copyfile(data.file(train_file), inputs / "train.npz")
    shutil.copyfile(data.file("blobs_validation.npz"), inputs / "validation.npz")
    done = run_runner("screen", "--inputs", inputs, "--out", tmp_path / "o", "--seed", 1, "--params", params(),
                      "--task-type", "classification")
    assert done.returncode != 0 and fragment in done.stderr


@pytest.mark.parametrize("family", ["linear", "knn", "kernel", "mlp"])
def test_predict_mode_matches_the_train_modes_validation_predictions_and_keeps_ids(staged, data, tmp_path, family):
    inputs, recipe = staged("blobs"), params(family, "standardize")
    train_out = tmp_path / "train-out"
    assert run_runner("train", "--inputs", inputs, "--out", train_out, "--seed", 3, "--params", recipe,
                      "--task-type", "classification").returncode == 0
    predicted = tmp_path / "predicted.npz"
    done = run_runner("predict", "--inputs", inputs, "--data", data.file("blobs_validation.npz"), "--out", predicted,
                      "--seed", 3, "--params", recipe, "--task-type", "classification")
    assert done.returncode == 0, done.stderr[-1000:]
    from_predict, from_train = read_npz(predicted), read_npz(train_out / "validation_predictions.npz")
    assert from_predict == from_train  # same ids, same predictions: predict is train's fit applied to another file
    assert from_predict["ids"] == read_npz(data.file("blobs_validation.npz"))["ids"]


def test_predict_mode_writes_ids_and_predictions_for_a_file_without_labels(staged, data, tmp_path):
    inputs = staged("blobs")
    train_classes = set(read_npz(data.file("blobs_train.npz"))["y"])
    for name, expected_ids in (("blobs_predict.npz", [f"p{i}" for i in range(7)]), ("blobs_predict_noids.npz", list(range(7)))):
        out = tmp_path / f"{name}.out.npz"
        done = run_runner("predict", "--inputs", inputs, "--data", data.file(name), "--out", out, "--seed", 1,
                          "--params", params("knn", k=3), "--task-type", "classification")
        assert done.returncode == 0, done.stderr[-1000:]
        written = read_npz(out)
        assert set(written) == {"ids", "predictions"}
        assert written["ids"] == expected_ids and len(written["predictions"]) == 7
        assert set(written["predictions"]) <= train_classes
    # a regression predict returns one float per row
    out = tmp_path / "regress.out.npz"
    done = run_runner("predict", "--inputs", staged("regress"), "--data", data.file("regress_validation.npz"), "--out", out,
                      "--seed", 1, "--params", params("linear", alpha=1e-3), "--task-type", "regression")
    assert done.returncode == 0, done.stderr[-1000:]
    values = read_npz(out)["predictions"]
    assert len(values) == 100 and all(isinstance(v, float) and math.isfinite(v) for v in values)


def test_inputs_with_more_than_two_dimensions_are_flattened(staged, tmp_path):
    flat, cube = staged("offset"), staged("offset3d")
    results = []
    for label, inputs in (("flat", flat), ("cube", cube)):
        out = tmp_path / label
        done = run_runner("train", "--inputs", inputs, "--out", out, "--seed", 1, "--params",
                          params("knn", "per_example_center", k=5), "--task-type", "classification")
        assert done.returncode == 0, done.stderr[-1000:]
        results.append(read_npz(out / "validation_predictions.npz"))
    assert results[0] == results[1]


def test_per_example_preprocessing_ignores_a_per_example_offset_and_none_does_not(staged, data, tmp_path):
    """The planted offset, isolated: offset validation vs the same validation rows without the offset (knn, deterministic)."""
    shifted, clean = staged("offset"), tmp_path / "inputs-clean"
    clean.mkdir()
    shutil.copyfile(data.file("offset_train.npz"), clean / "train.npz")
    shutil.copyfile(data.file("offsetclean_validation.npz"), clean / "validation.npz")
    receipts = {}
    for pre in PREPROCESS:
        for label, inputs in (("shifted", shifted), ("clean", clean)):
            out = tmp_path / f"{pre}-{label}"
            done = run_runner("train", "--inputs", inputs, "--out", out, "--seed", 1, "--params", params("knn", pre, k=5),
                              "--task-type", "classification")
            assert done.returncode == 0, done.stderr[-1000:]
            receipts[pre, label] = (json.loads((out / "receipt.json").read_text())["validation_accuracy"],
                                    read_npz(out / "validation_predictions.npz")["predictions"])
    for pre in CENTERED:
        assert receipts[pre, "shifted"] == receipts[pre, "clean"], pre
    for pre in ("none", "standardize"):
        assert receipts[pre, "shifted"][0] + 0.2 < receipts[pre, "clean"][0], pre


# -- 11. scikit-learn unavailable --------------------------------------------------------------------------------------------
def test_svm_and_gbm_are_skipped_and_recorded_when_scikit_learn_is_unavailable(tmp_path, data, nosk_python):
    res = run_sweep(tmp_path, data, tpl(preprocess=["none"], finalists=0), label="nosk", python=nosk_python)
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert {r["parameters"]["family"] for r in table["recipes"]} == set(NUMPY_FAMILIES)
    assert len(table["recipes"]) == len(NUMPY_FAMILIES)
    assert table["template"]["families"] == sorted(NUMPY_FAMILIES)
    skipped = table["template"]["skipped_families"]
    assert set(skipped) == set(SKLEARN_FAMILIES) and all("scikit-learn" in reason for reason in skipped.values())
    assert table["counts"]["space_size"] == 4 and table["counts"]["failed"] == 0
    assert res.json("best_recipe")["status"] == "found"


def test_only_the_unavailable_families_of_a_mixed_request_are_skipped(tmp_path, data, nosk_python):
    res = run_sweep(tmp_path, data, tpl(families=["linear", "svm"], preprocess=["none"], finalists=0), label="mixed",
                    python=nosk_python)
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert [r["parameters"]["family"] for r in table["recipes"]] == ["linear"]
    assert list(table["template"]["skipped_families"]) == ["svm"] and table["template"]["families"] == ["linear"]


def test_a_template_asking_only_for_unavailable_families_is_refused(tmp_path, data, nosk_python):
    res = run_sweep(tmp_path, data, tpl(families=["svm", "gbm"]), label="only-sklearn", python=nosk_python)
    refused(res, "no template family is available", "scikit-learn")


def test_the_runner_names_the_missing_package_for_an_svm_recipe(data, tmp_path, nosk_python):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    shutil.copyfile(data.file("blobs_train.npz"), inputs / "train.npz")
    shutil.copyfile(data.file("blobs_validation.npz"), inputs / "validation.npz")
    for family in SKLEARN_FAMILIES:
        done = run_runner("screen", "--inputs", inputs, "--out", tmp_path / family, "--seed", 1, "--params", params(family),
                          "--task-type", "classification", python=nosk_python)
        assert done.returncode != 0 and f"family {family} needs scikit-learn" in done.stderr


# -- 12. the standalone toolkit ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def toolkit(tmp_path_factory):
    sys.path.insert(0, str(ROOT))
    try:
        from study.toolkit.build_toolkit import build_toolkit
    finally:
        sys.path.remove(str(ROOT))
    return build_toolkit(tmp_path_factory.mktemp("kit"))


def test_the_standalone_toolkit_runs_a_template_and_matches_the_lab_runner(toolkit, data, tmp_path):
    shipped = toolkit / "components" / NAME / "templates" / "fit_recipe.py"
    assert shipped.read_bytes() == RUNNER.read_bytes()
    assert f"components/{NAME}/templates/fit_recipe.py" in json.loads((toolkit / "manifest.json").read_text())["sha256"]
    template = tpl(families=["linear", "knn"], preprocess=["none", "per_example_center"], seeds=[3], finalists=1, cv_folds=2)
    spec = {"inputs": {"train": {"file": str(data.file("offset_train.npz")), "media_type": NPZ},
                       "validation": {"file": str(data.file("offset_validation.npz")), "media_type": NPZ}},
            "config": {"template": template}}
    out = tmp_path / "toolkit-out"
    env = {"PATH": "/usr/bin:/bin", "OMP_NUM_THREADS": "1", **({"TMPDIR": os.environ["TMPDIR"]} if "TMPDIR" in os.environ else {})}
    # the toolkit's own run_component.py, under the numpy interpreter in isolated mode, with no lab on the path
    done = subprocess.run([NUMPY_PY, "-I", str(toolkit / "run_component.py"), NAME, "--args", json.dumps(spec), "--out", str(out)],
                          capture_output=True, text=True, timeout=240, cwd=str(tmp_path), env=env)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    staging = out / "staging"
    manifest = json.loads((staging / "eidos-output-manifest.json").read_text())
    assert manifest["status"] == "succeeded", manifest["error"]
    files = {o["slot"]: staging / o["filename"] for o in manifest["outputs"]}
    assert {"evidence_table", "best_recipe"} <= set(files)
    table, best = (json.loads(files[s].read_text()) for s in ("evidence_table", "best_recipe"))
    assert len(table["recipes"]) == 4 and table["stop_reason"] == "space-exhausted"
    assert table["template"]["seeds"] == [3] and best["status"] == "found"
    assert best["recipe"]["parameters"]["preprocess"] == "per_example_center"  # the planted offset, through the toolkit
    assert table["template"]["runner"]["sha256"] == hashlib.sha256(RUNNER.read_bytes()).hexdigest()
    # the lab's runner gives the same deterministic evidence for the same data, config and seeds
    lab = run_sweep(tmp_path, data, template, kind="offset", label="lab")
    assert lab.ok, lab.manifest.error
    assert manifest["receipt"]["evidence_digest"] == lab.receipt["evidence_digest"] == recomputed_digest(table)
    assert manifest["receipt"]["best_recipe_id"] == best["recipe"]["recipe_id"] == lab.receipt["best_recipe_id"]


@pytest.mark.parametrize("field", ["preprocess", "families", "seeds"])
def test_an_empty_list_is_refused_rather_than_replaced_by_the_default(sweep, field):
    """Review of these tests: ``[]`` used to fall back to the defaults silently."""
    refused(sweep(tpl(**{**CHEAP, field: []})))


def test_cv_folds_are_bounded_so_a_template_cannot_ask_for_an_unbounded_search(sweep):
    refused(sweep(tpl(**{**CHEAP, "finalists": 1, "cv_folds": 21})), "cv_folds")


def test_the_runner_refuses_more_cv_folds_than_training_rows(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run([NUMPY_PY, "-c", "import numpy as np; np.savez('train.npz', x=np.zeros((5, 2)), y=np.arange(5) % 2)"],
                   cwd=work, check=True)
    r = subprocess.run([NUMPY_PY, str(RUNNER), "cv", "--out", "o", "--seed", "1",
                        "--fold", "0", "--folds", "6", "--task-type", "classification",
                        "--params", json.dumps({"preprocess": "none", "family": "linear"})],
                   cwd=work, capture_output=True, text=True)
    assert r.returncode != 0 and "at most one fold per training row" in r.stderr
