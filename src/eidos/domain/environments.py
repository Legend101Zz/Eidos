"""Environment capabilities and admission preflight (ADR 0020).

A registered environment carries a capability record measured from its interpreter: the interpreter's digest and
which key modules import, with their versions. Admission compares what a component or project declares with that
record, so a job the environment cannot run is refused before it is launched, with a message naming what is missing.

The check only refuses on positive evidence: an environment with no successful probe is reported as unprobed and
never refused, because an absent measurement is not a measured absence.
"""

from __future__ import annotations

import re
from typing import Any

from eidos.domain.resolver import _REQ, _satisfies

CAPABILITIES_SCHEMA = "environment-capabilities/1"
# Probed in every environment: the modules whose absence stopped past investigations.
KEY_MODULES = ("numpy", "torch", "sklearn", "joblib", "scipy")
# Distribution name -> import name, where they differ.
IMPORT_NAMES = {"scikit-learn": "sklearn", "pillow": "PIL", "pyyaml": "yaml", "opencv-python": "cv2"}
# Lab packages are put on the job's path by the launcher, so an interpreter need not have them installed.
LAB_PROVIDED_PREFIX = "eidos"
# Components that run the project's own code, to which the project's declared requirements apply.
PROJECT_CODE_CAPABILITY = "project-command/1"
MAX_REPORTED = 20


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def import_name(package: str) -> str:
    package = normalize_name(package)
    return IMPORT_NAMES.get(package, package.replace("-", "_"))


def key_distributions() -> set[str]:
    """Distribution names of the key modules (a lock file's other entries are mostly transitive)."""
    by_import = {v: k for k, v in IMPORT_NAMES.items()}
    return {by_import.get(m, m) for m in KEY_MODULES}


def parse_requirements(text: str, *, key_only: bool = False) -> list[str]:
    """Requirement strings from a requirements file. Options, comments, URLs and lines with environment markers are
    skipped: an unevaluated marker says nothing about this interpreter. ``key_only`` keeps only the key modules,
    without versions (a lock file pins transitive packages exactly; another patch release is not an incompatibility)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.split(" #")[0].strip().rstrip("\\").strip()
        if not line or line.startswith(("#", "-")) or ";" in line or "@" in line:
            continue
        m = _REQ.match(line)
        if not m:
            continue
        if key_only:
            name = normalize_name(m.group(1))
            if name in key_distributions():
                out.append(name)
        else:
            out.append(line)
    return out


def unsatisfied(requirements: dict[str, list[str]], env: dict[str, Any]) -> list[dict[str, Any]]:
    """What ``requirements`` (owner -> requirement strings) need that this environment measurably lacks."""
    caps = env.get("capabilities") or {}
    if caps.get("status") != "ok":
        return []
    modules = caps.get("modules") or {}
    installed = {normalize_name(k): v for k, v in (env.get("installed") or {}).items()}
    problems: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for owner, reqs in requirements.items():
        for req in reqs or []:
            m = _REQ.match(req)
            if not m:
                continue
            package, op, bound = normalize_name(m.group(1)), m.group(2), m.group(3)
            if package.startswith(LAB_PROVIDED_PREFIX) or (owner, package) in seen:
                continue
            seen.add((owner, package))
            probed = modules.get(import_name(package))
            if probed is not None and not probed.get("importable"):
                problems.append({"owner": owner, "package": package, "requirement": req, "problem": "not-importable",
                                 "error": probed.get("error")})
                continue
            version = (probed or {}).get("version") or installed.get(package)
            if probed is None and version is None:
                if installed:  # the inventory is complete, so an unlisted package is absent
                    problems.append({"owner": owner, "package": package, "requirement": req, "problem": "not-installed"})
                continue
            if op and bound and version and not _satisfies(version, op, bound):
                problems.append({"owner": owner, "package": package, "requirement": req, "problem": "version-mismatch",
                                 "found": version})
    return problems


def describe_problems(problems: list[dict[str, Any]]) -> str:
    parts = []
    for p in problems[:MAX_REPORTED]:
        if p["problem"] == "version-mismatch":
            parts.append(f"{p['requirement']} (found {p['found']})")
        elif p.get("error"):
            parts.append(f"{p['package']} ({p['error']})")
        else:
            parts.append(p["package"])
    more = len(problems) - MAX_REPORTED
    return ", ".join(parts) + (f" and {more} more" if more > 0 else "")


def capability_summary(env: dict[str, Any]) -> dict[str, Any]:
    """The part of a capability record that is useful in a job view or a prompt."""
    caps = env.get("capabilities") or {}
    if caps.get("status") != "ok":
        return {"probed": False, "python_version": env.get("python_version"),
                **({"probe_error": str(caps.get("error"))[:300]} if caps.get("error") else {})}
    return {"probed": True, "python_version": caps.get("python_version") or env.get("python_version"),
            "interpreter_digest": caps.get("interpreter_digest"),
            "modules": {name: (m.get("version") or "present") if m.get("importable") else None
                        for name, m in sorted((caps.get("modules") or {}).items())}}
