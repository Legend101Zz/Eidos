#!/usr/bin/env python3
"""Exercise train, evaluate, analysis, baseline diagnosis and a template sweep from the exported toolkit in Seatbelt."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import agent_env, read_json, research_python, sandbox_command, write_json
from study.toolkit.build_toolkit import build_toolkit


def verify(run_dir):
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    toolkit = build_toolkit(root)
    invocation_root = root / f"verification-{time.time_ns()}"
    receipts = {}

    def run(component, spec, name):
        out = invocation_root / name
        command = [
            research_python(),
            str(toolkit / "run_component.py"),
            component,
            "--args",
            json.dumps(spec),
            "--out",
            str(out),
        ]
        result = subprocess.run(
            sandbox_command(root, command),
            env=agent_env(root),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        manifest = read_json(out / "staging/eidos-output-manifest.json")
        assert manifest["status"] == "succeeded", manifest
        receipts[component] = manifest
        print(component, manifest["status"])
        return out / "staging", manifest

    train_spec = read_json(toolkit / "components/eidos.pytorch.train/fixtures/train-from-scratch.json")
    staging, trained = run("eidos.pytorch.train", train_spec, "train")
    checkpoint = next(o for o in trained["outputs"] if o["slot"] == "checkpoint")
    eval_spec = read_json(toolkit / "components/eidos.pytorch.evaluate/fixtures/evaluate-small.json")
    eval_spec["inputs"]["checkpoint"]["file"] = str(staging / checkpoint["filename"])
    run("eidos.pytorch.evaluate", eval_spec, "evaluate")
    analysis = read_json(toolkit / "components/lab.pairs.by_group/fixtures/ties-and-groups.json")
    run("lab.pairs.by_group", analysis, "analysis")
    assessor = read_json(toolkit / "components/eidos.assessors.paired-ci/fixtures/planted-effect.json")
    run("eidos.assessors.paired-ci", assessor, "assessor")
    qualification = read_json(
        toolkit / "components/eidos.qualification-runner/fixtures/runs-assessor-controls.json"
    )
    run("eidos.qualification-runner", qualification, "qualification")
    # ADR 0024: baseline diagnosis, then a one-call typed sweep (recipe-sweep 1.1.0 template) on the same NPZ data
    diagnosis = read_json(toolkit / "components/eidos.diagnose.baseline-errors/fixtures/planted-offset.json")
    data = toolkit / "components/eidos.diagnose.baseline-errors/fixtures/data"
    for port in diagnosis["inputs"]:
        diagnosis["inputs"][port]["file"] = str(data / Path(diagnosis["inputs"][port]["file"]).name)
    _, diagnosed = run("eidos.diagnose.baseline-errors", diagnosis, "diagnosis")
    assert diagnosed["receipt"]["top_hypothesis"] == diagnosis["expect"]["receipt"]["top_hypothesis"], diagnosed
    sweep = {"inputs": {port: dict(diagnosis["inputs"][port]) for port in ("train", "validation")},
             "config": {"template": {"name": "quick", "task_type": "classification", "seeds": [11]},
                        "budget": {"max_trials": 6}},
             "seed": 11}
    run("eidos.search.recipe-sweep", sweep, "template-sweep")
    portfolio_spec = {"inputs": {port: dict(diagnosis["inputs"][port]) for port in ("train", "validation")},
                      "config": {"mode": "portfolio", "task_type": "classification",
                                 "include": ["linear", "neighbors"], "scheme": "cv-train",
                                 "cv_folds": 3, "cv_repeats": 1, "budget": {"max_seconds": 60}}, "seed": 11}
    _, portfolio_manifest = run("eidos.search.recipe-portfolio", portfolio_spec, "recipe-portfolio")
    assert portfolio_manifest["receipt"]["metrics"]["coverage_complete"] == 1
    assert portfolio_manifest["receipt"]["metrics"]["export_eligible"] == 1
    assert {"portfolio_table", "promotion_source", "oof"} <= {o["slot"] for o in portfolio_manifest["outputs"]}
    write_json(root / "toolkit-verification.json", receipts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    verify(parser.parse_args().run_dir)
