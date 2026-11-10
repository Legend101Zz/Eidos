#!/usr/bin/env python3
"""Exercise train, evaluate and analysis from the exported toolkit in Seatbelt."""
from pathlib import Path
import argparse
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import agent_env, read_json, research_python, sandbox_command, write_json
from study.toolkit.build_toolkit import build_toolkit


def verify(run_dir):
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    toolkit = build_toolkit(root)
    receipts = {}

    def run(component, spec, name):
        out = root / name
        command = [research_python(), str(toolkit / "run_component.py"), component,
                   "--args", json.dumps(spec), "--out", str(out)]
        result = subprocess.run(sandbox_command(root, command), env=agent_env(root), cwd=root,
                                capture_output=True, text=True, timeout=120)
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
    write_json(root / "toolkit-verification.json", receipts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    verify(parser.parse_args().run_dir)
