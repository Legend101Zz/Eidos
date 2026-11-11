"""Read a project interpreter's capabilities (ADR 0020, addendum R10): its digest and its installed distributions.

Run by whoever registers an environment, on the host that holds the interpreter, outside any job isolation. So nothing
from that environment runs here: the interpreter starts with ``-I -S`` (no site processing, so no ``.pth`` import lines
and no ``sitecustomize``), the site directories are computed by hand, and only distribution metadata is read. Whether a
module really imports is learned when a sealed job runs it. For the same reason GPU availability is not measured here.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from eidos.domain.environments import (
    CAPABILITIES_SCHEMA,
    KEY_MODULES,
    assess,
    import_name,
    normalize_name,
    parse_requirements,
)
from eidos_wire import canonical_json, sha256_bytes

PROBE_TIMEOUT_SECONDS = 60
REQUIREMENT_FILES = ("requirements.txt", "requirements.lock")
MAX_REPORTED_MODULES = 40
MAX_IMPORT_NAMES = 400
MAX_REQUIRES_DIST = 200
MAX_INCONCLUSIVE = 20
ERROR_CHARS = 400

# Stays Python-3.9 compatible: it runs in the project's interpreter. Standard library only (with -S, -I nothing else is
# importable). ``site.getsitepackages`` with explicit prefixes only computes paths; ``site.venv`` would process .pth files.
_SNIPPET = r"""
import json, os, platform, re, site, sys
from importlib import metadata

def prefixes():
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for root in (exe_dir, os.path.dirname(exe_dir)):
        cfg = os.path.join(root, "pyvenv.cfg")
        if os.path.isfile(cfg):
            with open(cfg) as f:
                options = dict((k.strip().lower(), v.strip().lower()) for k, _, v in (ln.partition("=") for ln in f))
            shared = options.get("include-system-site-packages") == "true"
            return [root] + ([sys.base_prefix] if shared else [])
    return [sys.prefix]

def search_path():
    dirs = [d for d in site.getsitepackages(prefixes()) if os.path.isdir(d)]
    out = list(dirs)
    for d in dirs:  # the path lines of .pth files (legacy editable installs); their import lines are never run
        for name in sorted(os.listdir(d)):
            if not name.endswith(".pth"):
                continue
            try:
                with open(os.path.join(d, name), errors="replace") as f:
                    lines = [ln.strip() for ln in f]
            except OSError:
                continue
            for ln in lines:
                p = os.path.join(d, ln) if ln and not ln.startswith(("#", "import ", "import\t")) else ""
                if p and os.path.isdir(p) and p not in out:
                    out.append(p)
    return out

def top_levels(d):
    text = d.read_text("top_level.txt")
    if text and text.split():
        return sorted(n for n in text.split() if n.isidentifier())
    if d.files is None:
        return None
    out = set()
    for f in d.files:
        parts = f.parts
        if not parts or parts[0] == ".." or parts[0] == "__pycache__" or parts[0].endswith((".dist-info", ".egg-info", ".data")):
            continue
        if parts[-1].endswith((".py", ".so", ".pyd")):
            name = parts[0] if len(parts) > 1 else parts[0].split(".")[0]
            if name.isidentifier():
                out.add(name)
    return sorted(out)

wanted = set(json.loads(sys.argv[1]))
installed, tops, requires = {}, {}, {}
for d in metadata.distributions(path=search_path()):
    try:  # one damaged distribution must not lose the whole inventory
        n = d.metadata["Name"]
        version = d.version
    except Exception:
        continue
    if not n:
        continue
    key = n.lower().replace("_", "-")
    if key in installed:  # the earlier path shadows this one
        continue
    installed[key] = version
    try:
        tops[key] = top_levels(d)
    except Exception:
        tops[key] = None
    if re.sub(r"[-_.]+", "-", n).lower() in wanted:
        requires[key] = d.requires or []

impl = sys.implementation
v = impl.version
impl_version = "%d.%d.%d" % v[:3] + ("" if v[3] == "final" else v[3][0] + str(v[4]))
markers = {"implementation_name": impl.name, "implementation_version": impl_version, "os_name": os.name,
           "platform_machine": platform.machine(), "platform_python_implementation": platform.python_implementation(),
           "platform_release": platform.release(), "platform_system": platform.system(),
           "platform_version": platform.version(), "python_full_version": platform.python_version(),
           "python_version": ".".join(platform.python_version_tuple()[:2]), "sys_platform": sys.platform}
print(json.dumps({"python_version": platform.python_version(), "platform": platform.platform(), "installed": installed,
                  "top_levels": tops, "requires": requires, "marker_environment": markers}), flush=True)
"""


def interpreter_digest(python: str) -> str:
    h = hashlib.sha256()
    with open(os.path.realpath(python), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def declared_requirements(project_root: str | Path | None) -> list[str]:
    """What the project at ``project_root`` declares. A requirements file counts in full; a lock file only for the key
    modules, because the rest of a lock is mostly transitive pins. Includes inside the project are followed."""
    if not project_root:
        return []
    root = Path(project_root).resolve()

    def read(relative: str) -> str | None:
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target.read_text(errors="replace")

    out: list[str] = []
    for name in REQUIREMENT_FILES:
        text = read(name)  # the same containment as an include: a symlink out of the project is not the project's
        if text is not None:
            out += parse_requirements(text, key_only=name.endswith(".lock"), include=read, path=name)
    return sorted(set(out))


def _run_bounded(argv: list[str], *, cwd: str, env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """Run in its own process group with output to files, so that no descendant can hold the call open past ``timeout``."""
    out, err = Path(cwd) / "probe.out", Path(cwd) / "probe.err"
    with out.open("wb") as so, err.open("wb") as se:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=so, stderr=se, cwd=cwd, env=env, start_new_session=True)
    try:
        code = proc.wait(timeout=timeout)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    return code, out.read_text(errors="replace"), err.read_text(errors="replace")


def _requirement_names(declared: list[str]) -> tuple[list[str], list[str]]:
    """Normalized names of the parsed requirements, and those that ask for extras."""
    names: list[str] = []
    with_extras: list[str] = []
    for raw in declared:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        name = normalize_name(req.name)
        names.append(name)
        if req.extras:
            with_extras.append(name)
    return list(dict.fromkeys(names)), sorted(set(with_extras))


def _module_record(module: str, provider: dict[str, str], installed: dict[str, str], unmapped: list[str]) -> dict[str, Any]:
    dist = provider.get(module)
    if dist is not None:
        return {"provided": True, "distribution": dist, "version": installed.get(dist)}
    return {"provided": None if unmapped else False}


def _capabilities(head: dict[str, Any], with_extras: list[str], names: list[str]) -> dict[str, Any]:
    """The record's content from the interpreter's report: import names come from each distribution's own metadata."""
    tops: dict[str, list[str] | None] = {normalize_name(d): t for d, t in head["top_levels"].items()}
    installed = {normalize_name(d): v for d, v in head["installed"].items()}
    unmapped = sorted(d for d, t in tops.items() if t is None)
    provider = {name: dist for dist, top in sorted(tops.items(), reverse=True) for name in (top or [])}
    modules: dict[str, dict[str, Any]] = {m: _module_record(m, provider, installed, unmapped) for m in KEY_MODULES}
    for dist in names:
        if dist in installed and tops.get(dist):
            for module in tops[dist] or []:
                modules.setdefault(module, {"provided": True, "distribution": dist, "version": installed[dist]})
        elif dist in installed:  # installed, but what it provides is not recorded: only the import name is unknown
            modules.setdefault(import_name(dist), {"provided": None, "distribution": dist, "version": installed[dist]})
        else:
            modules.setdefault(import_name(dist), _module_record(import_name(dist), provider, installed, unmapped))
    # Names that differ from the distribution's own; a name equal to it is already answered by the inventory.
    differing = {name: dist for name, dist in sorted(provider.items())
                 if not name.startswith("_") and name != dist.replace("-", "_")}
    kept = dict(list(differing.items())[:MAX_IMPORT_NAMES])
    requires = {normalize_name(d): [r for r in rs if "extra" in r][:MAX_REQUIRES_DIST] for d, rs in head["requires"].items()}
    return {"status": "ok", "python_version": head["python_version"], "marker_environment": head["marker_environment"],
            "modules": dict(list(modules.items())[:MAX_REPORTED_MODULES + len(KEY_MODULES)]), "import_names": kept,
            **({"import_names_truncated": True} if len(differing) > MAX_IMPORT_NAMES else {}),
            **({"unmapped": unmapped[:MAX_REPORTED_MODULES]} if unmapped else {}),
            **({"distributions": {d: {"requires_dist": requires[d]} for d in with_extras if d in requires}} if with_extras else {})}


def probe_environment(python: str, *, project_root: str | Path | None = None, requirements: list[str] | None = None,
                      timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Arguments for ``register_environment``: ``python_version``, ``installed``, ``platform``, ``devices``,
    ``lock_digest``, ``capabilities`` and ``requirements`` (declared by the project and the caller)."""
    from eidos.runtime.launcher import job_environment

    declared = sorted(set(declared_requirements(project_root)) | set(requirements or []))
    names, with_extras = _requirement_names(declared)
    caps: dict[str, Any] = {"schema": CAPABILITIES_SCHEMA, "status": "failed", "interpreter": python, "modules": {}}
    out: dict[str, Any] = {"installed": {}, "capabilities": caps, **({"requirements": declared} if declared else {})}
    try:
        caps["interpreter_digest"] = interpreter_digest(python)
        with tempfile.TemporaryDirectory(prefix="eidos-env-probe-") as cwd:
            code, stdout, stderr = _run_bounded([python, "-I", "-S", "-c", _SNIPPET, json.dumps(with_extras)], cwd=cwd,
                                                env=job_environment(os.environ), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        caps["error"] = f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]
        return out
    lines = [ln for ln in stdout.splitlines() if ln.startswith("{")]
    try:
        head = json.loads(lines[0])
    except (IndexError, ValueError):
        caps["error"] = f"probe produced no report (exit {code}): {stderr[-ERROR_CHARS:]}"
        return out
    if code != 0:
        caps["error"] = f"probe exited with {code}: {stderr[-ERROR_CHARS:]}"
        return out
    caps.update(_capabilities(head, with_extras, names))
    out.update(python_version=head["python_version"], platform=head["platform"], installed=head["installed"], devices={"cpu": True},
               lock_digest="sha256:" + hashlib.sha256(json.dumps(head["installed"], sort_keys=True).encode()).hexdigest())
    if declared:
        undecided = assess({"the project": declared}, out | {"capabilities": caps})["inconclusive"]
        if undecided:
            caps["inconclusive"] = [{k: u.get(k) for k in ("requirement", "inconclusive")} for u in undecided[:MAX_INCONCLUSIVE]]
    return out


def registration_key(prefix: str, payload: dict[str, Any]) -> str:
    """An idempotency key that changes whenever any part of the registration changes, so a re-probe that found a different
    interpreter, inventory or declaration replaces the old record instead of colliding with its key (R10 #8)."""
    return f"{prefix}:{sha256_bytes(canonical_json(payload).encode())}"
