"""Environment capabilities and admission preflight (ADR 0020, addendum R10).

A registered environment carries a capability record read from its interpreter's installed distribution metadata:
the interpreter's digest, its packages and which import names they provide. Nothing from the environment is imported to
get it (``eidos.runtime.env_probe``). Admission compares what a component or project declares with that record, so a job
the environment cannot run is refused before it is launched, with a message naming what is missing.

The check only refuses on positive evidence: an environment with no successful probe is reported as unprobed and never
refused, because an absent measurement is not a measured absence. A requirement this check cannot decide (a URL, an
unresolved include, an unknown import mapping) is reported as inconclusive; it is recorded and never refused.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from typing import Any

from packaging.markers import UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

CAPABILITIES_SCHEMA = "environment-capabilities/2"
# Reported for every environment: the modules whose absence stopped past investigations.
KEY_MODULES = ("numpy", "torch", "sklearn", "joblib", "scipy")
# Distribution name -> import name, where they differ. Only a guess for a distribution that is not installed (its own
# metadata answers otherwise), and only ever used to turn a refusal into "inconclusive".
IMPORT_NAMES = {"scikit-learn": "sklearn", "pillow": "PIL", "pyyaml": "yaml", "opencv-python": "cv2"}
# Lab packages are put on the job's path by the launcher, so an interpreter need not have them installed.
LAB_PROVIDED_PREFIX = "eidos"
# Components that run the project's own code, to which the project's declared requirements apply.
PROJECT_CODE_CAPABILITY = "project-command/1"
MAX_REPORTED = 20
MAX_INCLUDE_DEPTH = 5
MARKER_VARIABLES = {"implementation_name", "implementation_version", "os_name", "platform_machine",
                    "platform_python_implementation", "platform_release", "platform_system", "platform_version",
                    "python_full_version", "python_version", "sys_platform"}

# pip options that say where packages come from, not which are required.
_SOURCE_OPTIONS = {"-i", "--index-url", "--extra-index-url", "--no-index", "-f", "--find-links", "--trusted-host",
                   "--no-binary", "--only-binary", "--prefer-binary", "--require-hashes", "--pre", "--use-feature",
                   "--no-deps"}
_INCLUDE = re.compile(r"^(?:-r|--requirement)(?:\s+|=)(\S+)$")
_PER_REQUIREMENT_OPTION = re.compile(r"\s+--(?:hash|global-option|install-option|config-settings)[=\s]\S+")
_COMMENT = re.compile(r"(^|\s+)#.*$")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def import_name(package: str) -> str:
    package = normalize_name(package)
    return IMPORT_NAMES.get(package, package.replace("-", "_"))


def key_distributions() -> set[str]:
    """Distribution names of the key modules (a lock file's other entries are mostly transitive)."""
    by_import = {v: k for k, v in IMPORT_NAMES.items()}
    return {by_import.get(m, m) for m in KEY_MODULES}


def _logical_lines(text: str) -> list[str]:
    """Physical lines joined across continuations, without comments or per-requirement options (pip's reading)."""
    joined = re.sub(r"\\\r?\n", " ", text)
    stripped = (_COMMENT.sub("", raw) for raw in joined.splitlines())
    return [line for line in (_PER_REQUIREMENT_OPTION.sub("", s).strip() for s in stripped) if line]


def parse_requirements(text: str, *, key_only: bool = False, include: Callable[[str], str | None] | None = None,
                       path: str = "requirements.txt", _depth: int = 0, _seen: frozenset[str] = frozenset()) -> list[str]:
    """Requirement lines of a requirements file, as written. Included files are followed through ``include`` (their
    text, by path relative to the root; None if unreadable). Lines that are not requirements this code can read (an
    unresolved include, ``-e``, a constraints file, a bare URL) are kept: ``assess`` reports them as inconclusive.

    ``key_only`` keeps only the key modules, without versions (a lock file pins transitive packages exactly; another
    patch release is not an incompatibility)."""
    out: list[str] = []
    for line in _logical_lines(text):
        included = _INCLUDE.match(line)
        if included:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), included[1]))
            body = None
            if include is not None and target not in _seen | {path} and _depth < MAX_INCLUDE_DEPTH \
                    and not target.startswith("..") and not posixpath.isabs(target):
                body = include(target)
            if body is None:
                out.append(line)
            else:
                out += parse_requirements(body, key_only=key_only, include=include, path=target, _depth=_depth + 1,
                                          _seen=_seen | {path})
            continue
        if line.startswith("-"):
            if re.split(r"[=\s]", line, maxsplit=1)[0] not in _SOURCE_OPTIONS:
                out.append(line)
            continue
        if key_only:
            try:
                req = Requirement(line)
            except InvalidRequirement:
                out.append(line)
                continue
            name = normalize_name(req.name)
            if name in key_distributions():
                out.append(line if req.url else name + (f"; {req.marker}" if req.marker else ""))
            continue
        out.append(line)
    return out


def _applies(req: Requirement, marker_env: dict[str, str], extra: str = "") -> bool:
    return req.marker is None or req.marker.evaluate({**marker_env, "extra": extra})


def _version_problem(req: Requirement, version: str) -> dict[str, Any] | None:
    """A version outside ``req``'s constraints; pre-releases count as what they are, since this one is installed."""
    try:
        if req.specifier.contains(Version(version), prereleases=True):
            return None
    except InvalidVersion:
        return {"inconclusive": "version-unreadable", "found": version}
    return {"problem": "version-mismatch", "found": version}


def _absent(package: str, caps: dict[str, Any]) -> dict[str, Any]:
    """The distribution is not in a complete inventory. Another distribution may still provide its module."""
    provider = (caps.get("import_names") or {}).get(import_name(package))
    if provider:
        return {"inconclusive": "provided-by-another-distribution", "found": provider}
    if caps.get("unmapped") or caps.get("import_names_truncated"):
        return {"inconclusive": "unknown-import-mapping"}
    return {"problem": "not-installed"}


def _extras_problem(req: Requirement, package: str, caps: dict[str, Any], installed: dict[str, str],
                    marker_env: dict[str, str]) -> dict[str, Any] | None:
    """The first requirement of a requested extra this environment lacks."""
    requires = ((caps.get("distributions") or {}).get(package) or {}).get("requires_dist")
    if requires is None:
        return {"inconclusive": "extras-not-recorded"}
    for extra in sorted(req.extras):
        for raw in requires:
            try:
                dep = Requirement(raw)
                needed = _applies(dep, marker_env, extra) and not _applies(dep, marker_env, "")
            except (InvalidRequirement, UndefinedComparison, UndefinedEnvironmentName, InvalidVersion):
                return {"inconclusive": "extras-not-recorded"}
            if not needed:
                continue
            name = normalize_name(dep.name)
            found = installed.get(name)
            verdict = _absent(name, caps) if found is None else _version_problem(dep, found)
            if verdict:
                return {**verdict, "via_extra": extra, "package": name}
    return None


def _check(raw: str, caps: dict[str, Any], installed: dict[str, str], marker_env: dict[str, str] | None) -> dict[str, Any] | None:
    """What one requirement line lacks here: ``{"problem": ...}``, ``{"inconclusive": ...}``, or None if satisfied."""
    try:
        req = Requirement(raw)
    except InvalidRequirement:
        return {"inconclusive": "not-a-parsed-requirement", "package": None}
    package = normalize_name(req.name)
    if package.startswith(LAB_PROVIDED_PREFIX):
        return None
    found: dict[str, Any] | None = None
    try:
        if req.marker is not None and marker_env is None:
            found = {"inconclusive": "marker-not-evaluated"}
        elif marker_env is not None and not _applies(req, marker_env):
            return None
    except (UndefinedComparison, UndefinedEnvironmentName, InvalidVersion):
        found = {"inconclusive": "marker-not-evaluated"}
    if found is None and req.url:
        found = {"inconclusive": "url-requirement"}
    if found is None:
        version = installed.get(package)
        if version is None:
            found = _absent(package, caps)
        elif req.specifier:
            found = _version_problem(req, version)
        if found is None and req.extras:
            found = _extras_problem(req, package, caps, installed, marker_env or {})
    return None if found is None else {"package": package, **found}


def assess(requirements: dict[str, list[str]], env: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``problems``: what ``requirements`` (owner -> requirement strings) need that this environment measurably lacks.
    ``inconclusive``: what could not be decided. An unprobed environment has neither."""
    out: dict[str, list[dict[str, Any]]] = {"problems": [], "inconclusive": []}
    caps = env.get("capabilities") or {}
    if caps.get("schema") != CAPABILITIES_SCHEMA or caps.get("status") != "ok":
        return out
    installed = {normalize_name(k): v for k, v in (env.get("installed") or {}).items()}
    if not installed:  # an empty inventory says nothing about any package
        return out
    marker_env = caps.get("marker_environment")
    if marker_env is not None and not set(marker_env) >= MARKER_VARIABLES:
        marker_env = None  # packaging would fill the gaps from the interpreter running this code, not the probed one
    seen: set[tuple[str, str, str]] = set()
    for owner, reqs in requirements.items():
        for raw in reqs or []:
            verdict = _check(raw, caps, installed, marker_env)
            if verdict is None:
                continue
            kind = "problem" if "problem" in verdict else "inconclusive"
            package = str(verdict.get("package") or raw)
            if (owner, package, kind) not in seen:
                seen.add((owner, package, kind))
                out["problems" if kind == "problem" else "inconclusive"].append({"owner": owner, "requirement": raw, **verdict})
    return out


def unsatisfied(requirements: dict[str, list[str]], env: dict[str, Any]) -> list[dict[str, Any]]:
    """The refusable part of :func:`assess`."""
    return assess(requirements, env)["problems"]


def describe_problems(problems: list[dict[str, Any]]) -> str:
    parts = []
    for p in problems[:MAX_REPORTED]:
        if p.get("via_extra"):
            found = f", found {p['found']}" if p.get("found") else ""
            parts.append(f"{p['package']} (extra {p['via_extra']}{found})")
        elif p["problem"] == "version-mismatch":
            parts.append(f"{p['requirement']} (found {p['found']})")
        else:
            parts.append(p["package"])
    more = len(problems) - MAX_REPORTED
    return ", ".join(parts) + (f" and {more} more" if more > 0 else "")


def _module_state(module: dict[str, Any]) -> str | None:
    """A module as a prompt or job view shows it: its version, ``present``, ``unknown``, or None when nothing provides it."""
    if module.get("provided") is None:
        return "unknown"
    return (module.get("version") or "present") if module.get("provided") else None


def capability_summary(env: dict[str, Any]) -> dict[str, Any]:
    """The part of a capability record that is useful in a job view or a prompt."""
    caps = env.get("capabilities") or {}
    if caps.get("schema") != CAPABILITIES_SCHEMA or caps.get("status") != "ok":
        return {"probed": False, "python_version": env.get("python_version"),
                **({"probe_error": str(caps.get("error"))[:300]} if caps.get("error") else {})}
    return {"probed": True, "python_version": caps.get("python_version") or env.get("python_version"),
            "interpreter_digest": caps.get("interpreter_digest"),
            "modules": {name: _module_state(m) for name, m in sorted((caps.get("modules") or {}).items())},
            **({"inconclusive_requirements": caps["inconclusive"]} if caps.get("inconclusive") else {})}
