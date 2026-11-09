"""Helpers for authors' contract tests: build an Invocation in a temp dir and run it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from eidos_wire import InputBinding, Invocation, OutputManifest, sha256_file
from eidos_worker.run import execute

InputSpec = Union[bytes, str, Path, Tuple[Union[bytes, str, Path, Any], str]]


def make_invocation(
    root: Union[str, Path, None],
    entry: str,
    *,
    inputs: Optional[Mapping[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    source_dir: Union[str, Path, None] = None,
    limits: Optional[Dict[str, Any]] = None,
    ports: Optional[Dict[str, Any]] = None,
    seed: Optional[int] = 0,
    component_id: str = "test.component",
    llm_grant: Optional[Dict[str, Any]] = None,
) -> Invocation:
    """Create ``root/{inputs,staging,scratch}`` and return an :class:`Invocation` for ``entry``.

    ``inputs`` maps a name to one of: ``bytes``; an existing file path; a JSON-able object (stored
    as JSON); or ``(value, media_type)`` with any of those. Rows for csv/jsonl: pass ``bytes``/``str``
    text with the media type, e.g. ``(b"a,b\\n1,2\\n", "text/csv")``.
    """
    base = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="eidos-test-"))
    for sub in ("inputs", "staging", "scratch"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    bindings: Dict[str, InputBinding] = {}
    for name, spec in (inputs or {}).items():
        value, media = (
            spec if isinstance(spec, tuple) and len(spec) == 2 and isinstance(spec[1], str) else (spec, None)
        )
        target = base / "inputs" / name
        if isinstance(value, Path) or (isinstance(value, str) and Path(value).is_file()):
            target.write_bytes(Path(value).read_bytes())
            media = media or "application/octet-stream"
        elif isinstance(value, (bytes, bytearray)):
            target.write_bytes(bytes(value))
            media = media or "application/octet-stream"
        elif isinstance(value, str) and media:
            target.write_text(value, encoding="utf-8")
        else:
            target.write_text(json.dumps(value), encoding="utf-8")
            media = media or "application/json"
        digest, size = sha256_file(target)
        bindings[name] = InputBinding(
            artifact_id=f"art-{name}", digest=digest, path=str(target), media_type=media, size=size
        )
    component: Dict[str, Any] = {"id": component_id, "entry": entry, "release": "0.0.0"}
    if source_dir is not None:
        component["source_dir"] = str(source_dir)
    return Invocation(
        job_id="job-test",
        attempt_id="attempt-test",
        epoch=1,
        project_id="project-test",
        component=component,
        inputs=bindings,
        config=dict(config or {}),
        staging_dir=str(base / "staging"),
        scratch_dir=str(base / "scratch"),
        limits=dict(limits or {}),
        ports=dict(ports or {}),
        seed=seed,
        llm_grant=llm_grant,
    )


def run_in_process(invocation: Invocation) -> OutputManifest:
    """Execute in this interpreter (fast; the entry module is imported into the test process)."""
    return execute(invocation)


def run_subprocess(invocation: Invocation, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Run the real ``python -m eidos_worker.run`` protocol and return the process result.

    Read the manifest afterwards with ``OutputManifest.read(invocation.staging_dir)``.
    """
    path = Path(invocation.staging_dir).parent / "invocation.json"
    invocation.write(path)
    return subprocess.run(
        [sys.executable, "-m", "eidos_worker.run", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ),
    )


def read_output_json(invocation: Invocation, manifest: OutputManifest, slot: str) -> Any:
    entry = next(e for e in manifest.outputs if e.slot == slot)
    return json.loads((Path(invocation.staging_dir) / entry.filename).read_text(encoding="utf-8"))
