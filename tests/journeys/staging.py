"""Journey-side staging of built-in component directories for upload.

The CLI's source bundle is text-only (JSON of file contents). Four torch components ship fixtures that
read binary ``fixtures/data/*.ckpt`` files, which are generated, git-ignored and cannot travel in a
text bundle, so the lab's build-job validation cannot find them. ``stage_component`` copies the
directory and drops exactly those fixtures (from disk and from the manifest's ``controls`` list); every
other fixture still runs in the build job. The exclusion is recorded in the journey run record.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def stage_component(src: Path, dst_root: Path) -> tuple[Path, list[str]]:
    dst = dst_root / src.name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.ckpt", "data"))
    dropped: list[str] = []
    manifest_path = dst / "eidos-component.json"
    manifest = json.loads(manifest_path.read_text())
    keep = []
    for rel in manifest.get("controls", []):
        f = dst / rel
        if f.suffix == ".json" and f.is_file() and '"file"' in f.read_text():
            f.unlink()
            dropped.append(rel)
        else:
            keep.append(rel)
    if dropped:
        manifest["controls"] = keep
    # train documents parent_checkpoint as optional; the resolver reads that from ``optional_inputs``.
    if "parent_checkpoint" in (manifest.get("input") or {}) and not manifest.get("optional_inputs"):
        manifest["optional_inputs"] = {"parent_checkpoint": manifest["input"].pop("parent_checkpoint")}
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return dst, dropped
