"""Diverse train-only portfolio search. No lab authority, network, or held-out ports."""
import json
import time
from pathlib import Path

ALLOWED_INPUTS = ("train", "validation", "task", "profile", "groups", "predictions")
CONFIG_KEYS = {"mode", "task_type", "include", "scheme", "budget", "add", "spec", "partition_seed",
               "seed", "cv_folds", "cv_repeats", "per_recipe_timeout_seconds", "purpose", "parents"}


def validate_config(config):
    if not isinstance(config, dict) or set(config) - CONFIG_KEYS:
        raise ValueError("unknown portfolio config fields")
    mode = config.get("mode", "portfolio")
    if mode not in ("portfolio", "refine", "rescore", "export"):
        raise ValueError("mode must be portfolio, refine, rescore or export")
    if mode == "rescore":
        if config.get("purpose", "reproduction") != "reproduction":
            raise ValueError("rescore purpose must be reproduction, never confirmation")
        if "partition_seed" not in config:
            raise ValueError("rescore requires explicit partition_seed")
    if mode in ("refine", "rescore") and not (config.get("add") or config.get("parents")):
        raise ValueError(mode + " requires add or parents recipe specs")
    if mode == "export" and not config.get("spec"):
        raise ValueError("export requires a stated spec")
    if config.get("scheme", "auto") not in ("auto", "cv-train", "train-fit-validation-score"):
        raise ValueError("unknown selection scheme")
    for key, low, high in (("cv_folds", 2, 20), ("cv_repeats", 1, 10), ("partition_seed", 0, 2**32-1), ("seed", 0, 2**32-1)):
        if key in config and (type(config[key]) is not int or not low <= config[key] <= high):
            raise ValueError(key + " outside allowed range")
    budget = config.get("budget", {})
    if not isinstance(budget, dict) or set(budget) - {"max_seconds"}:
        raise ValueError("budget supports max_seconds only; no truncated max_trials grids")
    for value in (budget.get("max_seconds", 350), config.get("per_recipe_timeout_seconds", 60)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
            raise ValueError("budget and recipe timeout must be finite positive seconds <= 3600")
    return config


def _load(path, *, labels=True):
    import numpy as np
    with np.load(path, allow_pickle=False) as data:
        x = np.asarray(data["x"], dtype=float)
        x = x.reshape(len(x), -1)
        y = data["y"] if labels else None
        ids = data["ids"] if "ids" in data else np.arange(len(x))
        groups = data.get("groups", None)
    if len(x) < 3 or not np.isfinite(x).all() or y is not None and (y.shape != (len(x),) or not np.isfinite(y).all()):
        raise ValueError("visible arrays must be finite and aligned")
    if ids.shape != (len(x),) or len(np.unique(ids)) != len(ids):
        raise ValueError("visible row ids must be unique and aligned")
    return x, y, ids, groups


def search(ports, config, scratch, *, seed=11):
    import numpy as np
    from eidos_recipes import cv, export, registry, select, shift, spec
    from eidos_recipes.templates import portfolio_specs
    from eidos_recipes.util import evidence_digest, guarded_ports
    from eidos_recipes.worker import RecipeWorker

    guarded_ports(ports, ALLOWED_INPUTS)  # before any input is opened
    validate_config(config)
    if config.get("mode") == "export":
        spec.validate(config["spec"])
        return None, None, export.source_bundle(config["spec"], basis="explicit-export")
    started = time.monotonic()
    x, y, ids, groups = _load(ports["train"])
    xv, yv, vids, _ = _load(ports["validation"])
    if x.shape[1] != xv.shape[1]:
        raise ValueError("train/validation feature counts differ")
    if "groups" in ports:
        with np.load(ports["groups"], allow_pickle=False) as values:
            if not np.array_equal(values["ids"], ids):
                raise ValueError("group row ids do not align")
            groups = values["groups"]
    kind = config.get("task_type")
    if kind is None and "task" in ports:
        kind = json.loads(Path(ports["task"]).read_text())["task_type"]
    if kind not in ("classification", "regression"):
        raise ValueError("task_type must be classification or regression")
    mode, scheme = config.get("mode", "portfolio"), config.get("scheme", "auto")
    shift_report = None
    if scheme == "auto":
        shift_report = shift.detect(x, xv)
        scheme = "train-fit-validation-score" if shift_report["shift_detected"] else "cv-train"
    if mode == "rescore":
        scheme = "cv-train"
    included = config.get("include", "auto")
    if included != "auto" and (not isinstance(included, list) or not included or set(included) - registry.CLASSES):
        raise ValueError("include must be auto or a nonempty list of hypothesis classes")
    candidates = portfolio_specs(kind, n=len(x), d=x.shape[1]) if mode == "portfolio" else []
    if included != "auto":
        candidates = [s for s in candidates if registry.hypothesis(s) in included]
    if config.get("parents"):
        candidates.extend(spec.expand(config["parents"]))
    if config.get("add"):
        candidates.extend(spec.expand(config["add"]))
    if not candidates or len(candidates) > spec.MAX_RECIPES:
        raise ValueError("portfolio requires 1..400 recipes; refused without truncation")
    if any(s["task_type"] != kind for s in candidates):
        raise ValueError("recipe task_type differs from portfolio")
    split_seed = config.get("partition_seed", 81723)
    folds, repeats = config.get("cv_folds", 5), config.get("cv_repeats")
    partitions = cv.partitions(x, y, kind, seed=split_seed, folds=folds, repeats=repeats, groups=groups)
    planned_classes = registry.applicable(kind) if included == "auto" and mode == "portfolio" else {registry.hypothesis(s) for s in candidates}
    max_seconds = config.get("budget", {}).get("max_seconds", 350)
    recipe_timeout = config.get("per_recipe_timeout_seconds", 60)
    # Reserve one train-fit validation check, including a cold-child restart.
    search_deadline = started + max_seconds * .8
    rows, oof, validation_fits, histories = [], [], {}, {}
    stop_reason = "exhausted"
    with RecipeWorker(scratch, x, y, xv, yv, kind, groups=groups, ids=ids, folds=folds, repeats=repeats,
                      partition_seed=split_seed, seed=config.get("seed", seed), scheme=scheme) as worker:
        for index, s in enumerate(candidates):
            remaining = search_deadline - time.monotonic()
            if remaining <= 0:
                stop_reason = "budget"
                break
            rid = spec.recipe_id(s)
            # Duplicates may be intentional neighbours; ids still uniquely identify rows.
            if any(r["id"] == rid for r in rows):
                rid += "-" + str(index)
            result = worker.evaluate(s, timeout=min(recipe_timeout, remaining))
            cls = registry.hypothesis(s)
            row = dict(id=rid, spec=s, **{"class": cls}, complexity=registry.complexity(s),
                       checkpoint_bytes_est=registry.estimated_bytes(s, len(x), x.shape[1]),
                       export_eligible=False, **{k: v for k, v in result.items() if k not in ("oof", "predictions", "export_eligible", "checkpoint_bytes_est")})
            if result["status"] == "ok":
                row["export_eligible"] = result.get("export_eligible", row["checkpoint_bytes_est"] <= export.MAX_CHECKPOINT_BYTES)
                if "oof" in result:
                    oof.append(dict(id=rid, **result["oof"]))
                if scheme == "train-fit-validation-score":
                    validation_fits[rid] = result
                if mode == "refine":
                    history = histories.setdefault(cls, [])
                    history.append(row)
                    if len(history) >= 7:
                        anchor = history[-7]
                        differences = [np.asarray(r["scores"]) - anchor["scores"] for r in history[-6:]]
                        if all(float(d.mean()) <= float(d.std(ddof=1) / np.sqrt(len(d))) + 1e-12 for d in differences):
                            row["family_stop_reason"] = "family-gain-below-se"
                            # Remaining requests from this family are skipped, other classes continue.
                            histories[cls] = "stopped"
            rows.append(row)
            if histories.get(cls) == "stopped":
                candidates[index + 1:] = [s for s in candidates[index + 1:] if registry.hypothesis(s) != cls]
                stop_reason = "family-gain-below-se"
        selection = select.portfolio_choice(rows)
        # Check actual full-fit size and time before exporting. Oversized or failed
        # finalists become ineligible; never silently fall back to an out-of-band fit.
        while True:
            chosen = next(r for r in rows if r["id"] == selection["chosen_id"])
            if chosen["id"] not in validation_fits:
                result = worker.fit(chosen["spec"], timeout=max(.001, min(recipe_timeout, started + max_seconds - time.monotonic())))
                if result["status"] != "ok" or not result["export_eligible"]:
                    chosen["export_eligible"] = False
                    chosen["export_error"] = result.get("error", "full fit exceeded resource guard")
                    selection = select.portfolio_choice(rows)
                    continue
                validation_fits[chosen["id"]] = result
            check = validation_fits[chosen["id"]]
            break
        # The chosen + up to two runners are the only CV recipes scored on validation.
        finalists = sorted([r for r in rows if r["status"] == "ok" and r["id"] != chosen["id"]], key=lambda r: -r["mean"])[:2]
        if scheme == "cv-train":
            for r in finalists:
                remaining = started + max_seconds - time.monotonic()
                if remaining <= 0:
                    break
                result = worker.fit(r["spec"], timeout=min(recipe_timeout, remaining))
                if result["status"] == "ok":
                    validation_fits[r["id"]] = result
    chosen.update(checkpoint_bytes=check["checkpoint_bytes"], fit_seconds_train=check["fit_seconds"])
    completed = {r["class"] for r in rows if r["status"] == "ok"}
    checks = [dict(id=rid, score=r["mean"], se=r["validation_se"]) for rid, r in validation_fits.items()]
    table = dict(schema="recipe-portfolio-table/1", claim=None, exploratory=True, mode=mode,
                 purpose="reproduction" if mode == "rescore" else config.get("purpose", "open-search"),
                 interpretation="re-scored on resampled train partitions; same data, not independent confirmation" if mode == "rescore" else "visible exploratory selection; independent assessor evidence required",
                 task_type=kind, scheme=scheme, partition_seed=split_seed, model_seed=config.get("seed", seed),
                 cv_folds=folds, cv_repeats=repeats,
                 folds_digest=cv.folds_digest(partitions) if scheme == "cv-train" else evidence_digest(vids.tolist()),
                 library_digest=export.library_digest(), recipes=rows, selection=selection,
                 class_summary={cls: dict(full=sum(r["class"] == cls and r["status"] == "ok" for r in rows)) for cls in sorted(planned_classes)},
                 class_contrasts=select.class_contrasts(rows), shift_check=shift_report,
                 coverage_complete=planned_classes <= completed,
                 applicable_classes=sorted(planned_classes), not_applicable_classes=sorted(registry.CLASSES - registry.applicable(kind)),
                 grid_plan=dict(requested=len(candidates), large_n_density_reduced=len(x) > 5000, halving=False),
                 validation_check=dict(chosen_id=chosen["id"], score=check["mean"], se=check["validation_se"],
                                       predictions=check["predictions"], row_ids=vids.tolist(), checks=checks,
                                       candidates_scored=len(validation_fits), untouched_by_selection=scheme == "cv-train" and mode == "portfolio"),
                 stop_reason=stop_reason, wall_seconds=time.monotonic() - started)
    table["evidence_digest"] = evidence_digest(table)
    oof_doc = dict(schema="recipe-oof/1", claim=None, scheme=scheme, folds_digest=table["folds_digest"],
                   library_digest=table["library_digest"], recipes=oof,
                   status="stored" if scheme == "cv-train" else "not-applicable-validation-row-estimator")
    return table, oof_doc, export.source_bundle(chosen["spec"])


def run(inputs, config, *, reader, writer, context):
    if set(inputs) - set(ALLOWED_INPUTS):
        raise ValueError("undeclared input ports; only visible train/validation/task/profile/groups/predictions allowed")
    validate_config(config)
    # Explicit export has no input dependency; search requires two visible arrays.
    if config.get("mode") != "export" and not {"train", "validation"} <= set(inputs):
        raise ValueError("portfolio requires train and validation ports")
    ports = {key: reader.path(key) for key in inputs}
    table, oof, source = search(ports, config, Path(context.scratch_dir) / "portfolio", seed=context.seed if context.seed is not None else 11)
    writer.stage_json("promotion_source", source, schema="source-bundle/1")
    if table is None:
        return dict(metrics=dict(round_evaluated=0.), library_digest=source["library_digest"], basis="explicit-export", exploratory=True)
    writer.stage_json("portfolio_table", table, schema="recipe-portfolio-table/1")
    writer.stage_json("oof", oof, schema="recipe-oof/1")
    chosen = next(r for r in table["recipes"] if r["id"] == table["selection"]["chosen_id"])
    metrics = dict(cv_chosen=chosen["mean"], se_chosen=table["validation_check"]["se"],
                   validation_chosen=table["validation_check"]["score"], n_recipes=len(table["recipes"]),
                   n_classes_full_cv=sum(v["full"] > 0 for v in table["class_summary"].values()) if table["scheme"] == "cv-train" else 0,
                   coverage_complete=float(table["coverage_complete"]), fit_seconds_chosen=chosen["fit_seconds_train"],
                   checkpoint_bytes_chosen=chosen["checkpoint_bytes"], export_eligible=float(chosen["export_eligible"]), round_evaluated=1.)
    return dict(metrics=metrics, evidence_digest=table["evidence_digest"], library_digest=source["library_digest"],
                stop_reason=table["stop_reason"], basis="one-se-choice", exploratory=True, claim=None,
                purpose=table["purpose"])
