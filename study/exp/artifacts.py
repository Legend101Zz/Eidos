"""Standard-library artifact hashing, also usable by the research interpreter."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path


def bundle_sha256(directory: Path) -> str:
    files = {}
    if not (directory / "eidos-component.json").is_file():
        raise ValueError(f"missing component manifest: {directory}")
    for path in sorted(directory.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and not path.name.endswith(".pyc")
        ):
            data = path.read_bytes()
            try:
                files[str(path.relative_to(directory))] = data.decode("utf-8")
            except UnicodeDecodeError:
                files[str(path.relative_to(directory))] = {"base64": base64.b64encode(data).decode()}
    return hashlib.sha256(json.dumps({"files": files}, sort_keys=True).encode()).hexdigest()


def toolkit_sources(treatment) -> dict[str, Path]:
    from study.exp import flags

    root = flags.ROOT
    sources = {}

    def tree(source, destination, *, package=False):
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(
                part == "__pycache__"
                or part == ".DS_Store"
                or part.endswith(".pyc")
                or (package and part == "tests")
                for part in relative.parts
            ):
                continue
            if path.is_file():
                sources[f"{destination}/{relative.as_posix()}"] = path

    for name, source in treatment.toolkit_plan().items():
        tree(source, f"components/{name}")
    for name in json.loads((root / "study/exp/toolkit-base.json").read_text())["files"]:
        sources[f"components/{name}"] = root / "components" / name
    for name in ("eidos-wire", "eidos-worker-sdk", "eidos-pytorch"):
        tree(root / "packages" / name, f"packages/{name}", package=True)
    for name in ("__init__", "assessment", "charter", "errors", "stats", "evidence", "qualification"):
        sources[f"support/eidos/domain/{name}.py"] = root / "src/eidos/domain" / f"{name}.py"
    sources["support/eidos/__init__.py"] = root / "src/eidos/__init__.py"
    sources["support/LICENSE"] = root / "LICENSE"
    for name in ("run_component.py", "README.md", "COVERAGE.md"):
        source = (
            "COVERAGE.pilot4.md" if name == "COVERAGE.md" and not treatment.patch.get("toolkit") else name
        )
        sources[name] = root / "study/toolkit" / source
    return dict(sorted(sources.items()))


def toolkit_manifest_sha256(treatment) -> str:
    files = {
        name: hashlib.sha256(source.read_bytes()).hexdigest()
        for name, source in toolkit_sources(treatment).items()
    }
    manifest = {"format": "pilot-toolkit/1", "sha256": files}
    return hashlib.sha256((json.dumps(manifest, indent=2, allow_nan=False) + "\n").encode()).hexdigest()
