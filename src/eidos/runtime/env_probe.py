"""Measure a project interpreter (ADR 0020): its digest, what its key modules import as, and its package inventory.

Run by whoever registers an environment, on the host that holds the interpreter. It runs in the job allowlist
environment, isolated from the caller's working directory and user site, so it sees what a job would see.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from eidos.domain.environments import CAPABILITIES_SCHEMA, KEY_MODULES, import_name, parse_requirements

PROBE_TIMEOUT_SECONDS = 180
REQUIREMENT_FILES = ("requirements.txt", "requirements.lock")
MAX_PROBED_MODULES = 40

# One JSON line per module, flushed, so a module whose import kills the interpreter leaves the earlier results.
# Stays Python-3.9 compatible: it runs in the project's interpreter.
_SNIPPET = r"""
import importlib, json, platform, sys
from importlib import metadata
inst = {}
for d in metadata.distributions():
    n = d.metadata.get("Name")
    if n: inst[n.lower().replace("_", "-")] = d.version
dev = {"cpu": True}
print(json.dumps({"python_version": platform.python_version(), "platform": platform.platform(), "installed": inst}), flush=True)
for name in json.loads(sys.argv[1]):
    rec = {"module": name}
    try:
        mod = importlib.import_module(name)
        rec.update(importable=True, version=getattr(mod, "__version__", None))
    except BaseException as exc:
        rec.update(importable=False, error=type(exc).__name__)
        mod = None
    if name == "torch" and mod is not None:
        try:
            rec["devices"] = {"cuda": bool(mod.cuda.is_available()),
                              "mps": bool(getattr(mod.backends, "mps", None) and mod.backends.mps.is_available())}
        except Exception:
            pass
    print(json.dumps(rec), flush=True)
"""


def interpreter_digest(python: str) -> str:
    h = hashlib.sha256()
    with open(os.path.realpath(python), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def declared_requirements(project_root: str | Path | None) -> list[str]:
    """What the project at ``project_root`` declares. A requirements file counts in full; a lock file only for the key
    modules, because the rest of a lock is mostly transitive pins."""
    if not project_root:
        return []
    out: list[str] = []
    for name in REQUIREMENT_FILES:
        path = Path(project_root) / name
        if path.is_file():
            out += parse_requirements(path.read_text(errors="replace"), key_only=name.endswith(".lock"))
    return sorted(set(out))


def probe_environment(python: str, *, project_root: str | Path | None = None, requirements: list[str] | None = None,
                      timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Arguments for ``register_environment``: ``python_version``, ``installed``, ``platform``, ``devices``,
    ``lock_digest``, ``capabilities`` and ``requirements`` (declared by the project and the caller)."""
    from eidos.runtime.launcher import job_environment

    declared = sorted(set(declared_requirements(project_root)) | set(requirements or []))
    names = [m.group(0) for m in (re.match(r"[A-Za-z0-9_.\-]+", r) for r in declared) if m]
    modules = list(dict.fromkeys([*KEY_MODULES, *(import_name(n) for n in names)]))[:MAX_PROBED_MODULES]
    caps: dict[str, Any] = {"schema": CAPABILITIES_SCHEMA, "status": "failed", "interpreter": python, "modules": {}}
    out: dict[str, Any] = {"installed": {}, "capabilities": caps, **({"requirements": declared} if declared else {})}
    try:
        caps["interpreter_digest"] = interpreter_digest(python)
        with tempfile.TemporaryDirectory(prefix="eidos-env-probe-") as cwd:
            r = subprocess.run([python, "-I", "-c", _SNIPPET, json.dumps(modules)], capture_output=True, text=True,
                               cwd=cwd, env=job_environment(os.environ), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        caps["error"] = f"{type(exc).__name__}: {exc}"[:500]
        return out
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    try:
        head = json.loads(lines[0])
        results = [json.loads(ln) for ln in lines[1:]]
    except (IndexError, ValueError):
        caps["error"] = f"probe produced no report (exit {r.returncode}): {r.stderr[-400:]}"
        return out
    devices: dict[str, Any] = {"cpu": True}
    for rec in results:
        name = rec.pop("module")
        devices.update(rec.pop("devices", {}))
        caps["modules"][name] = rec
    out.update(python_version=head["python_version"], platform=head["platform"], installed=head["installed"], devices=devices,
               lock_digest="sha256:" + hashlib.sha256(json.dumps(head["installed"], sort_keys=True).encode()).hexdigest())
    caps["python_version"] = head["python_version"]
    if r.returncode == 0 and len(results) == len(modules):
        caps["status"] = "ok"
    else:  # an import took the interpreter down: what it reached is recorded, but the probe is not relied on
        caps["error"] = f"probe stopped after {len(results)} of {len(modules)} modules (exit {r.returncode})"
    return out
