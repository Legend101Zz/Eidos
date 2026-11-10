#!/usr/bin/env python3
"""Controller-side isolation and torch probe. Must run before live work."""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import ROOT, agent_env, research_python, sandbox_command


def probe(run_dir):
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    grading = ROOT.parent / "pilot-1/grading"
    grading.mkdir(parents=True, exist_ok=True)
    denied = [str(ROOT / "README.md"), str(grading), str(Path.home() / ".ssh")]
    # Require actual existing targets: ENOENT would not prove confinement.
    for path in denied:
        if not Path(path).exists():
            raise RuntimeError(f"probe target missing: {path}")
    code = f"""import pathlib, torch
for path in {denied!r}:
    p = pathlib.Path(path)
    try:
        list(p.iterdir()) if p.is_dir() else p.read_bytes()
    except PermissionError:
        print("DENIED", path)
    else:
        raise AssertionError("sandbox allowed read: " + path)
pathlib.Path("write-ok").write_text("ok")
torch.set_num_threads(1)
assert torch.ones(2).sum().item() == 2
print("WRITE_OK TORCH_OK", torch.__version__)
"""
    result = subprocess.run(
        sandbox_command(root, [research_python(), "-c", code]),
        env=agent_env(root),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    print(result.stdout, end="")
    return result.stdout


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    probe(parser.parse_args().run_dir)
