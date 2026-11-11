#!/usr/bin/env python3
"""Copy scientific components and the minimal worker packages, never the lab."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import ROOT, STUDY, write_json


def build_toolkit(run_dir):
    destination = Path(run_dir) / "toolkit"
    if (destination / "manifest.json").exists():
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    shutil.copytree(ROOT / "components", destination / "components", dirs_exist_ok=True, ignore=ignore)
    for name in ("eidos-wire", "eidos-worker-sdk", "eidos-pytorch"):
        shutil.copytree(
            ROOT / "packages" / name,
            destination / "packages" / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"),
        )
    # Public assessor/qualification components depend on these pure scientific rules.
    # Export the unchanged dependency closure, without a lab/server installation.
    support = destination / "support/eidos/domain"
    support.mkdir(parents=True, exist_ok=True)
    for name in ("__init__", "assessment", "charter", "errors", "stats", "evidence", "qualification"):
        shutil.copyfile(ROOT / "src/eidos/domain" / f"{name}.py", support / f"{name}.py")
    shutil.copyfile(ROOT / "src/eidos/__init__.py", support.parent / "__init__.py")
    shutil.copyfile(ROOT / "LICENSE", destination / "support/LICENSE")
    for name in ("run_component.py", "README.md", "COVERAGE.md"):
        shutil.copyfile(STUDY / "toolkit" / name, destination / name)
    files = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }
    write_json(destination / "manifest.json", {"format": "pilot-toolkit/1", "sha256": files})
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(build_toolkit(args.run_dir))
