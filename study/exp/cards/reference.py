"""LLM-free, train-only sklearn search, bagged Caruana selection and fresh evaluation."""

import argparse
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from .common import (
    SEEDS,
    bar_for,
    gain,
    read_json,
    run_cli,
    score,
    sha,
    subprocess_env,
    write_json,
)

RUNTIME = Path(__file__).with_name("reference_runtime.py")


def configurations(task_type, n_train, seed=20261114):
    classification = task_type == "classification"
    families = {
        "linear": [{"C" if classification else "alpha": value} for value in (1.0, 0.01, 0.1, 10, 100)],
        "extra": [{"max_features": value} for value in (1.0, "sqrt", 0.5)],
        "hist": [],
        "knn": [{"n_neighbors": k, "weights": "distance"} for k in (7, 3, 15, 31)],
        "rbf": [{"C": c, "gamma": g} for c in (1, 10, 100) for g in ("scale", 0.01, 0.1)],
        "forest": [{"max_features": value} for value in (1.0, "sqrt", 0.5)],
        "mlp": [{"alpha": a, "learning_rate_init": rate} for a in (0.0001, 0.01) for rate in (0.001, 0.01)],
    }
    rng = random.Random(seed)
    for _ in range(30):
        families["hist"].append(dict(learning_rate=rng.choice([0.03, 0.07, 0.15]),
                                     max_leaf_nodes=rng.choice([7, 15, 31, 63]),
                                     l2_regularization=rng.choice([0.0, 0.1, 1.0, 10.0]),
                                     min_samples_leaf=rng.choice([10, 20, 40]),
                                     max_iter=rng.choice([100, 200, 300])))
    if n_train > 3000:
        families["rbf"] = []
    return [dict(family=family, params=grid[i])
            for i in range(max(map(len, families.values()))) for family, grid in families.items() if i < len(grid)]


def greedy_ensemble(predictions, y, task_type, *, seed=20261114, bags=10, steps=50):
    """Bagged greedy selection with replacement, always compared with the best single OOF score."""
    measured = [score(y, p, task_type) for p in predictions]
    ordered = sorted(range(len(measured)), key=lambda i: -measured[i] if task_type == "classification" else measured[i])
    top = ordered[:max(1, math.ceil(0.2 * len(ordered)))]
    best = ordered[0]
    rng = np.random.default_rng(seed)
    weights = np.zeros(len(predictions))
    bag_libraries = []
    for _ in range(bags):
        library = sorted({best, *rng.choice(top, size=max(1, math.ceil(len(top) * 0.5)), replace=False).tolist()})
        bag_libraries.append(library)
        total = np.zeros_like(predictions[0], dtype=float)
        counts = np.zeros(len(predictions))
        best_counts, best_value = None, -float("inf")
        for step in range(steps):
            values = [score(y, (total + predictions[i]) / (step + 1), task_type) for i in library]
            j = max(range(len(library)), key=lambda j: values[j] if task_type == "classification" else -values[j])
            selected = library[j]
            total += predictions[selected]
            counts[selected] += 1
            value = values[j] if task_type == "classification" else -values[j]
            if value > best_value:
                best_value, best_counts = value, counts.copy() / (step + 1)
        weights += best_counts / bags
    combined = sum(w * p for w, p in zip(weights, predictions, strict=True))
    ensemble_score = score(y, combined, task_type)
    adopted = gain(measured[best], ensemble_score, task_type) > 1e-12
    if not adopted:
        weights[:] = 0
        weights[best] = 1
    return weights, dict(best_single_index=best, best_single_score=measured[best],
                         ensemble_score=ensemble_score, ensemble_adopted=adopted,
                         top_indices=top, bag_libraries=bag_libraries, bags=bags, steps=steps)


def select_reference(x_train, y_train, task_type, *, budget_seconds=480, per_fit_timeout=45,
                     seed=20261114, max_configurations=None, size_limit_bytes=48 * 1024 * 1024):
    """Only x_train/y_train enter the worker file. No validation/test arrays are inspected."""
    if task_type not in {"classification", "regression"} or budget_seconds <= 0:
        raise ValueError("invalid task type/budget")
    x, y = np.asarray(x_train, dtype=np.float32), np.asarray(y_train)
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("nonfinite training table")
    if task_type == "classification" and not np.array_equal(np.unique(y), np.arange(len(np.unique(y)))):
        raise ValueError("class labels must be contiguous 0..K-1")
    started = time.monotonic()
    completed, predictions, attempted = [], [], []
    with tempfile.TemporaryDirectory(prefix="cards-search-") as temporary:
        root = Path(temporary)
        # Run outside this package: select.py must not shadow the stdlib select module.
        (root / "worker.py").write_bytes(RUNTIME.read_bytes())
        np.savez(root / "train.npz", x=x, y=y)
        configs = configurations(task_type, len(y), seed)
        if max_configurations is not None:
            configs = configs[:max_configurations]
        extended = False
        deadline_budget = budget_seconds
        queue = list(configs)
        index = 0
        while True:
            if index == len(queue):
                families = {c["config"]["family"] for c in completed}
                if extended or len(families) >= 3:
                    break
                queue.extend(c for c in configs if c["family"] not in families)
                deadline_budget += 0.5 * budget_seconds
                extended = True
                if index == len(queue):
                    break
            config = queue[index]
            index += 1
            remaining = deadline_budget - (time.monotonic() - started)
            if remaining < 0.1:
                if not extended and len({c["config"]["family"] for c in completed}) < 3:
                    extended = True
                    deadline_budget += 0.5 * budget_seconds
                    remaining = deadline_budget - (time.monotonic() - started)
                if remaining < 0.1:
                    break
            config = dict(config, size_limit_bytes=size_limit_bytes)
            write_json(root / "config.json", config)
            fit_started = time.monotonic()
            try:
                subprocess.run([os.environ.get("EIDOS_RESEARCH_PYTHON", sys.executable), str(root / "worker.py"), "cv",
                                "--data", str(root / "train.npz"), "--config", str(root / "config.json"),
                                "--out", str(root / "oof.npz"), "--task-type", task_type, "--seed", str(seed)],
                               check=True, capture_output=True, timeout=min(remaining, per_fit_timeout),
                               env=subprocess_env())
                with np.load(root / "oof.npz", allow_pickle=False) as output:
                    estimated_bytes = int(output["estimated_bytes"])
                    if estimated_bytes > size_limit_bytes:
                        attempted.append(dict(config=config, status="excluded_for_size", estimated_bytes=estimated_bytes))
                        continue
                    prediction = output["predictions"].copy()
                if not np.isfinite(prediction).all():
                    raise ValueError("nonfinite OOF predictions")
                completed.append(dict(config=config, estimated_bytes=estimated_bytes, cv_seconds=time.monotonic() - fit_started,
                                      oof_score=score(y, prediction, task_type)))
                predictions.append(prediction)
                attempted.append(dict(config=config, status="completed"))
            except subprocess.TimeoutExpired:
                attempted.append(dict(config=config, status="timeout"))
            except (subprocess.CalledProcessError, ValueError) as error:
                detail = error.stderr.decode(errors="replace")[-2000:] if isinstance(error, subprocess.CalledProcessError) else str(error)
                attempted.append(dict(config=config, status="failed", detail=detail))
    if len({c["config"]["family"] for c in completed}) < 3:
        raise RuntimeError(f"reference search requires completed configurations from at least three families "
                           f"after one 50% budget extension (completed {len(completed)}): {attempted}")
    weights, ensemble = greedy_ensemble(predictions, y, task_type, seed=seed)
    members = [dict(config=item["config"], weight=float(weight), cv_seconds=item["cv_seconds"],
                    estimated_bytes=item["estimated_bytes"])
               for item, weight in zip(completed, weights, strict=True) if weight > 0]
    return dict(schema_version="eidos-reference/0.1", task_type=task_type, seed=seed,
                preprocessing="StandardScaler fitted inside each fold for linear/kNN/RBF/MLP; MLP: 3-seed mean; regression target scaled inside these fits",
                cv=dict(folds=5, repeats=2 if len(y) < 1000 else 1, train_only=True),
                members=members, ensemble=ensemble, configurations=completed,
                attempts=attempted, n_configurations=len(completed), search_seconds=time.monotonic() - started,
                dropped_for_retrain_budget=[], dropped_for_size=[], budget_extended=extended)


def export_reference(spec, directory):
    import json

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "reference_spec.json", spec)
    source = RUNTIME.read_text().replace("SPEC = None", "SPEC = json.loads(" + repr(json.dumps(spec)) + ")")
    (directory / "reference_project.py").write_text(source)
    return directory / "reference_project.py"


def paired_se(y, baseline, reference, task_type, seed=20261114):
    y, baseline, reference = np.asarray(y), np.asarray(baseline), np.asarray(reference)
    if len(y) < 2:
        return 0.0
    if task_type == "classification":
        delta = (reference == y).astype(float) - (baseline == y).astype(float)
        return float(delta.std(ddof=1) / np.sqrt(len(y)))
    # Paired delta method for RMSE(base)-RMSE(ref), with a continuous zero-error limit.
    base_errors, ref_errors = (y - baseline) ** 2, (y - reference) ** 2
    base_rmse, ref_rmse = np.sqrt(base_errors.mean()), np.sqrt(ref_errors.mean())
    influence = (base_errors / (2 * base_rmse) if base_rmse > 0 else np.zeros(len(y)))
    influence -= ref_errors / (2 * ref_rmse) if ref_rmse > 0 else np.zeros(len(y))
    return float(influence.std(ddof=1) / np.sqrt(len(y)))



class CheckpointTooLarge(Exception):
    pass

def evaluate_spec(spec, bundle, test_inputs, y_test, baseline_predictions, base_score, bar, directory, *, size_limit_bytes=64 * 1024 * 1024):
    directory = Path(directory)
    retrain_seconds, checkpoint_bytes, ref_scores, ref_predictions = [], [], [], []
    with np.load(test_inputs, allow_pickle=False) as data:
        expected_ids = data["ids"]
    while True:
        project = export_reference(spec, directory)
        retrain_seconds.clear()
        checkpoint_bytes.clear()
        ref_scores.clear()
        ref_predictions.clear()
        try:
            for seed in SEEDS:
                with tempfile.TemporaryDirectory(prefix="cards-reference-fit-") as temporary:
                    predictions, ids, seconds, size = run_cli(project, bundle, test_inputs, Path(temporary), seed)
                if not np.array_equal(expected_ids, ids):
                    raise ValueError("reference IDs disagree with test inputs")
                if size > size_limit_bytes:
                    raise CheckpointTooLarge(size)
                retrain_seconds.append(seconds)
                checkpoint_bytes.append(size)
                ref_scores.append(score(y_test, predictions, spec["task_type"]))
                ref_predictions.append(predictions)
            break
        except (subprocess.TimeoutExpired, CheckpointTooLarge) as error:
            size_failure = isinstance(error, CheckpointTooLarge)
            field = "dropped_for_size" if size_failure else "dropped_for_retrain_budget"
            spec.setdefault(field, [])
            if len(spec["members"]) == 1:
                member = spec["members"][0]
                config = member["config"]
                trees = config["params"].get("n_estimators", 100)
                if not size_failure or config["family"] not in {"extra", "forest"} or trees <= 1:
                    raise RuntimeError("single selected model exceeds checkpoint size or 120 s retrain budget") from None
                import copy
                spec[field].append(copy.deepcopy(member))
                config["params"]["n_estimators"] = max(1, trees // 2)
                continue
            removed = max(spec["members"], key=lambda m: m.get("estimated_bytes", 0) if size_failure else m["cv_seconds"])
            spec["members"].remove(removed)
            spec[field].append(removed)
            total = sum(member["weight"] for member in spec["members"])
            for member in spec["members"]:
                member["weight"] /= total
    kind = spec["task_type"]
    mean = float(np.mean(ref_scores))
    ref_gain = gain(base_score, mean, kind)
    se = paired_se(y_test, baseline_predictions, ref_predictions[0], kind)
    clears = all(gain(base_score, measured, kind) >= bar for measured in ref_scores)
    sd = float(np.std(y_test))
    error_base = 1 - base_score if kind == "classification" else base_score / max(sd, 1e-12)
    error_ref = 1 - mean if kind == "classification" else mean / max(sd, 1e-12)
    result = dict(metric="accuracy" if kind == "classification" else "rmse", base_score=base_score,
                  ref_scores=ref_scores, ref_score=mean, ref_gain=ref_gain, bar=bar,
                  clears_bar_all_seeds=clears, null_card=not clears, se_paired=se,
                  headroom=max(ref_gain, 2 * se), err_base=error_base, err_ref=error_ref,
                  search_seconds=spec["search_seconds"], retrain_seconds=retrain_seconds,
                  checkpoint_bytes=checkpoint_bytes, n_configurations=spec["n_configurations"],
                  chosen=spec["members"], dropped_for_size=spec.get("dropped_for_size", []),
                  dropped_for_retrain_budget=spec.get("dropped_for_retrain_budget", []), reference_py_sha256=sha(project.read_bytes()))
    write_json(directory / "reference.json", result)
    np.savez(directory / "reference_test.npz", ids=expected_ids, predictions=ref_predictions[0])
    return result


def run_reference(x_train, y_train, x_test, y_test, task_type, *, budget_seconds=480,
                  output_dir=None, baseline_predictions=None, seed=20261114, **search_options):
    """Array API for legacy cards. Only training arrays are used to choose the spec."""
    spec = select_reference(x_train, y_train, task_type, budget_seconds=budget_seconds, seed=seed, **search_options)
    with tempfile.TemporaryDirectory(prefix="cards-array-reference-") as temporary:
        root = Path(temporary)
        bundle = root / "bundle"
        bundle.mkdir()
        for split in ("train", "validation"):
            np.savez(bundle / f"{split}.npz", x=x_train, y=y_train, ids=np.arange(len(y_train), dtype=np.int64))
        write_json(bundle / "task.json", dict(task_type=task_type,
                   nclasses=len(np.unique(y_train)) if task_type == "classification" else 1))
        np.savez(root / "test_inputs.npz", x=x_test, ids=np.arange(len(y_test), dtype=np.int64))
        if baseline_predictions is None:
            (bundle / "project.py").write_bytes(Path(__file__).with_name("baseline_project.py").read_bytes())
            baseline_predictions, _, _, _ = run_cli(bundle / "project.py", bundle,
                                                   root / "test_inputs.npz", root / "baseline", 11)
        base = score(y_test, baseline_predictions, task_type)
        return evaluate_spec(spec, bundle, root / "test_inputs.npz", y_test, baseline_predictions,
                             base, bar_for(base, task_type), output_dir or root / "controller")


def reference(card_dir, controller_dir, *, budget_seconds=480, **search_options):
    card, controller = Path(card_dir), Path(controller_dir)
    manifest = read_json(card / "manifest.json")
    with np.load(card / "bundle/train.npz", allow_pickle=False) as data:
        spec = select_reference(data["x"], data["y"], manifest["task_type"],
                                budget_seconds=budget_seconds, **search_options)
    # Test material is first opened after selection has completed.
    with np.load(card / "test_labels.npz", allow_pickle=False) as data:
        y, ids = data["y"], data["ids"]
    with np.load(card / "baseline_test.npz", allow_pickle=False) as data:
        if not np.array_equal(ids, data["ids"]):
            raise ValueError("baseline IDs disagree")
        baseline = data["predictions"]
    return evaluate_spec(spec, card / "bundle", card / "test_inputs.npz", y, baseline,
                         score(y, baseline, manifest["task_type"]), manifest["min_reference_gain"], controller)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-dir", required=True)
    parser.add_argument("--controller-dir", required=True)
    parser.add_argument("--budget-seconds", type=float, default=480)
    args = parser.parse_args()
    result = reference(args.card_dir, args.controller_dir, budget_seconds=args.budget_seconds)
    print(f"reference={result['ref_score']:.6g} baseline={result['base_score']:.6g} null_card={result['null_card']}")


if __name__ == "__main__":
    main()
