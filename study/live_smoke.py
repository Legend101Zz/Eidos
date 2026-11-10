#!/usr/bin/env python3
"""One arm-1 MiniMax call sequence on a toy regression, with a five-minute ceiling."""
from pathlib import Path
import argparse
import hashlib
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study.common import DEVELOPMENT, ENVELOPE, MODEL, STUDY, dev_command, write_json
from study.run_arm import run_claude


def smoke(root):
    import numpy as np
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("live smoke requires a fresh empty directory")
    task = root / "task"
    task.mkdir(parents=True)
    (root / "out").mkdir()
    for split, n, start in (("train", 24, 0), ("validation", 8, 100)):
        x = np.linspace(-1, 1, n, dtype=np.float32)[:, None]
        np.savez(task / f"{split}.npz", x=x, y=3*x[:, 0]+2, ids=np.arange(start, start+n, dtype=np.int64))
    shutil.copyfile(STUDY / "fixtures/toy_project.py", task / "project.py")
    shutil.copyfile(DEVELOPMENT / "validate_answer.py", task / "answer_schema.py")
    from importlib.metadata import version
    (task / "requirements.lock").write_text(f"numpy=={version('numpy')}\ntorch=={version('torch')}\n")
    write_json(task / "task.json", dict(task_id="TOY-REGRESSION", project="toy", task_type="regression",
                                         metric="rmse", threads=1, device="cpu"))
    (task / "brief.md").write_text(
        "This is a tiny tooling smoke, not a study card. You have at most FIVE MINUTES. "
        "Fit the provided one-dimensional linear regressor with project.py train, reload it, and "
        "report validation evidence. Copy source and requirements.lock into out/. "
        "Do not explore beyond this smoke. Write a valid answer.json with no_positive_claim and "
        "no_improvement_found: we are checking execution, not asserting improvement. "
        "Use task_id TOY-REGRESSION and run_id toy-smoke. Unknown cost fields must be null "
        "and listed in unknown_fields. End with a short explicit claim and evidence.\n")
    write_json(root / "arm.json", dict(arm=1, card="TOY-REGRESSION", run_index=1, block_id="toy-smoke",
                                        model=MODEL, envelope={**ENVELOPE, "wall_s": 300},
                                        charter_sha=hashlib.sha256((STUDY / "prompts/task.md").read_bytes()).hexdigest()))
    record = run_claude(root, 1, wall_s=300)
    print(f"LIVE SMOKE status={record['status']} tokens={record['tokens']} wall_s={record['wall_s']:.2f}")
    if (root / "out/answer.json").exists():
        print(dev_command("validate_answer.py", root / "out/answer.json").stdout.strip())
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    smoke(parser.parse_args().run_dir)
