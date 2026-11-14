"""Generated recipe project. Train has no CV, readiness subprocess, or pooled refit."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from eidos_recipes import export, models, select
from eidos_recipes.util import guarded_ports


def read(path, *, labels=False):
    with np.load(path, allow_pickle=False) as values:
        x = np.asarray(values["x"], dtype=float)
        x = x.reshape(len(x), -1)
        ids = values["ids"] if "ids" in values else np.arange(len(x))
        y = values["y"] if labels else None
    if not np.isfinite(x).all() or len(np.unique(ids)) != len(ids):
        raise ValueError("nonfinite features or duplicate row ids")
    return x, y, ids


def predictions(path, ids, values):
    if values.shape != ids.shape or not np.isfinite(values).all():
        raise ValueError("invalid prediction vector")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, ids=ids, predictions=values)


def aligned(path, ids):
    with np.load(path, allow_pickle=False) as value:
        if set(value.files) != {"ids", "predictions"} or len(np.unique(value["ids"])) != len(ids) or set(value["ids"].tolist()) != set(ids.tolist()):
            raise ValueError("prediction row ids do not align")
        order = {i: j for j, i in enumerate(value["ids"].tolist())}
        p = value["predictions"][[order[i] for i in ids.tolist()]]
        if p.shape != ids.shape or not np.isfinite(p).all():
            raise ValueError("invalid predictions")
        return p


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("train", "infer", "check", "compare"))
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    recipe = json.loads(Path(__file__).with_name("recipe.json").read_text())
    if args.command == "infer":
        x, _, ids = read(args.inputs)
        state = export.load(args.model)
        predictions(args.out, ids, state["model"].predict(x))
        return
    args.out.mkdir(parents=True, exist_ok=True)
    receipt = dict(schema="project-receipt/1", library_digest=export.library_digest(), seed=args.seed,
                   exploratory=True, claim=None)
    if args.command == "train":
        guarded_ports({"train": args.inputs / "train.npz", "validation": args.inputs / "validation.npz"}, ("train", "validation"))
        x, y, _ = read(args.inputs / "train.npz", labels=True)
        xv, yv, ids = read(args.inputs / "validation.npz", labels=True)
        started = time.perf_counter()
        model = models.build(recipe, args.seed).fit(x, y)
        receipt["fit_seconds"] = time.perf_counter() - started
        if receipt["fit_seconds"] > 60:
            raise ValueError("train fit exceeded 60-second export guard")
        receipt["checkpoint_bytes"] = export.save(args.out / "model.pt", model, recipe)
        p = model.predict(xv)
        predictions(args.out / "validation_predictions.npz", ids, p)
        metric = "validation_accuracy" if recipe["task_type"] == "classification" else "validation_rmse"
        value = select.score(recipe["task_type"], yv, p)
        metrics = {metric: value if recipe["task_type"] == "classification" else -value,
                   metric + "_se": select.score_se(recipe["task_type"], yv, p)}
        receipt.update(metrics=metrics, export_eligible=True, **metrics)
    elif args.command == "check":
        validation = args.inputs / "validation.npz" if args.inputs.is_dir() else args.inputs
        model = args.model or args.inputs / "model.pt"
        expected = args.expected or args.inputs / "extra_a.npz"
        # Run the exact public inference CLI in a fresh interpreter, outside train.
        output = args.out / "fresh_validation.npz"
        infer_started = time.perf_counter()
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "infer", "--model", str(model),
                        "--inputs", str(validation), "--out", str(output)], check=True, timeout=120)
        _, _, ids = read(validation)
        a, b = aligned(expected, ids), aligned(output, ids)
        if not np.allclose(a, b, rtol=1e-5, atol=1e-6):
            raise ValueError("fresh-process predictions differ")
        receipt.update(readiness_ok=True, readiness_max_abs_diff=float(np.max(np.abs(a - b))),
                       infer_seconds=time.perf_counter() - infer_started, metrics=dict(readiness_ok=1.))
    else:
        validation = args.inputs / "validation.npz" if args.inputs.is_dir() else args.inputs
        _, truth, ids = read(validation, labels=True)
        candidate = args.candidate or args.inputs / "extra_a.npz"
        baseline = args.baseline or args.inputs / "extra_b.npz"
        paired = select.paired_interval(recipe["task_type"], truth, aligned(candidate, ids), aligned(baseline, ids))
        paired = select.bootstrap_interval(recipe["task_type"], truth, aligned(candidate, ids), aligned(baseline, ids), seed=args.seed)
        receipt.update(paired_baseline=paired, paired_delta=paired["effect"], paired_delta_lo=paired["lower95"],
                       paired_delta_hi=paired["upper95"], paired_n=len(ids),
                       metrics=dict(paired_gain=paired["effect"], paired_gain_se=paired["se"]))
    (args.out / "receipt.json").write_text(json.dumps(receipt, allow_nan=False, sort_keys=True))


if __name__ == "__main__":
    main()
