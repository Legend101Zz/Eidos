#!/usr/bin/env python3
"""Copy scientific components and the minimal worker packages, never the lab."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import ROOT, STUDY, write_json


def build_toolkit(run_dir, *, treatment=None):
    from study.exp.treatment import resolve

    treatment = treatment or resolve()
    destination = Path(run_dir) / "toolkit"
    receipt = Path(run_dir) / "toolkit-treatment.json"
    if (destination / "manifest.json").exists():
        if not receipt.is_file() or json.loads(receipt.read_text()) != treatment.record():
            raise ValueError("existing toolkit differs from selected treatment")
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    for name, source in treatment.toolkit_plan().items():
        shutil.copytree(source, destination / "components" / name, dirs_exist_ok=True, ignore=ignore)
    ancillary = json.loads((STUDY / "exp/toolkit-base.json").read_text())["files"]
    for name in ancillary:
        shutil.copyfile(ROOT / "components" / name, destination / "components" / name)
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
        source = "COVERAGE.pilot4.md" if name == "COVERAGE.md" and not treatment.patch.get("toolkit") else name
        shutil.copyfile(STUDY / "toolkit" / source, destination / name)
    files = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }
    write_json(destination / "manifest.json", {"format": "pilot-toolkit/1", "sha256": files})
    write_json(receipt, treatment.record())
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(build_toolkit(args.run_dir))
