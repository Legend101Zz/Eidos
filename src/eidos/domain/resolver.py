"""Per-run resolution into an immutable execution lock (design §"Dependency injection").

Precedence: authorized explicit trial override → pinned project profile binding → operator
default frozen in that profile. Required ports need exactly one binding; ambiguity, cycles,
missing grants, unsatisfied hooks, dependency conflicts and schema mismatches are planning
errors raised before any compute is reserved.
"""

from __future__ import annotations

import re
from typing import Any

from eidos.domain.errors import IncompatibleHook, ResolutionError
from eidos_wire import canonical_json, sha256_bytes
from eidos_wire.manifest import PHASES

_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(==|>=|<=|!=|<|>|~=)?\s*([0-9][0-9A-Za-z.+\-]*)?\s*$")


def _ver(v: str) -> tuple:
    parts = []
    for p in re.split(r"[.+\-]", v):
        parts.append((0, int(p)) if p.isdigit() else (1, p))
    return tuple(parts)


def _satisfies(version: str, op: str, bound: str) -> bool:
    a, b = _ver(version), _ver(bound)
    n = max(len(a), len(b))
    a = a + ((0, 0),) * (n - len(a))
    b = b + ((0, 0),) * (n - len(b))
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == "~=":
        return a >= b and a[: max(1, len(_ver(bound)) - 1)] == b[: max(1, len(_ver(bound)) - 1)]
    return True


def dependency_conflicts(requirements: dict[str, list[str]], installed: dict[str, str]) -> list[dict[str, Any]]:
    """Detect unsatisfiable pins across components and against the project's installed packages.

    ``requirements`` maps an owner (component ref) to requirement strings. The project's
    existing versions are fixed: Eidos never upgrades a researcher's packages silently.
    """
    by_pkg: dict[str, list[tuple[str, str, str]]] = {}
    for owner, reqs in requirements.items():
        for req in reqs:
            m = _REQ.match(req)
            if not m:
                continue
            name, op, ver = m.group(1).lower().replace("_", "-"), m.group(2), m.group(3)
            if op and ver:
                by_pkg.setdefault(name, []).append((owner, op, ver))
    conflicts: list[dict[str, Any]] = []
    for name, cons in by_pkg.items():
        inst = installed.get(name)
        if inst is not None:
            bad = [c for c in cons if not _satisfies(inst, c[1], c[2])]
            if bad:
                conflicts.append({"package": name, "installed": inst,
                                  "constraints": [f"{o}: {name}{op}{v}" for o, op, v in bad],
                                  "explanation": f"project has {name} {inst}; Eidos will not change it"})
            continue
        candidates = {v for _, _, v in cons}
        if not any(all(_satisfies(c, op, v) for _, op, v in cons) for c in candidates):
            conflicts.append({"package": name, "installed": None,
                              "constraints": [f"{o}: {name}{op}{v}" for o, op, v in cons],
                              "explanation": "no single version satisfies every constraint; run them as separate trials"})
    return conflicts


def metric_identity(manifest: dict[str, Any], metric: str) -> str | None:
    m = (manifest.get("metrics") or {}).get(metric)
    if m is None:
        return None
    return "sha256:" + sha256_bytes(canonical_json(m).encode())


def _major(cap: str) -> tuple[str, int]:
    name, major = cap.rsplit("/", 1)
    return name, int(major)


def select_release(
    capability: str,
    catalog: list[dict[str, Any]],
    *,
    override: str | None,
    binding: list[str] | None,
    default: str | None,
    consumer: str,
) -> dict[str, Any]:
    """Pick exactly one active release implementing ``capability``."""
    name, major = _major(capability)
    implementing = [r for r in catalog if r["status"] == "active" and _major(r["manifest"]["implements"]) == (name, major)]
    for source, chosen in (("trial-override", override), ("profile-binding", binding), ("operator-default", default)):
        if not chosen:
            continue
        refs = [chosen] if isinstance(chosen, str) else list(chosen)
        found = [r for r in implementing if r["ref"] in refs or r["digest"] in refs]
        if len(found) > 1:
            raise ResolutionError(f"ambiguous binding for {capability} from {source}",
                                  details={"consumer": consumer, "candidates": [r["ref"] for r in found]})
        if found:
            return {**found[0], "selected_by": source}
        raise ResolutionError(
            f"{source} names {refs} for {capability}, but no active release matches",
            details={"consumer": consumer, "capability": capability,
                     "available": [r["ref"] for r in implementing]})
    raise ResolutionError(
        f"no binding for required capability {capability}",
        details={"consumer": consumer, "capability": capability,
                 "available": [r["ref"] for r in implementing]},
        next_action="Bind a release in the project profile or pass an authorized trial override.")


def resolve_invocation(
    *,
    target: dict[str, Any],
    catalog: list[dict[str, Any]],
    profile: dict[str, Any],
    overrides: dict[str, str],
    inputs: dict[str, dict[str, Any]],
    config: dict[str, Any],
    grants: set[str],
    installed: dict[str, str],
    adapters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve a component invocation into an execution lock.

    ``target`` and catalog entries are release records: {"ref", "digest", "status", "manifest",
    "environment"}. ``inputs`` maps port → artifact {"artifact_id","digest","schema"}.
    """
    man = target["manifest"]
    consumer = target["ref"]
    if target["status"] != "active":
        raise ResolutionError(f"{consumer} is {target['status']}; new runs cannot bind it",
                              details={"consumer": consumer, "status": target["status"]})
    missing_grants = sorted(set(man.get("grants", [])) - grants)
    if missing_grants:
        raise ResolutionError(f"{consumer} requests grants that are not held: {missing_grants}",
                              details={"consumer": consumer, "missing_grants": missing_grants})
    # Input contract checks. ``optional_inputs`` ports may be absent (recorded as absent in the lock).
    optional_in = man.get("optional_inputs") or {}
    for port, schema in {**(man.get("input") or {}), **optional_in}.items():
        art = inputs.get(port)
        if art is None and port in optional_in:
            continue
        if art is None:
            raise ResolutionError(f"missing input '{port}' ({schema}) for {consumer}",
                                  details={"consumer": consumer, "port": port, "schema": schema})
        have = art.get("schema")
        if schema != "*" and have != schema:
            adapters_avail = [a["ref"] for a in (adapters or [])
                              if (a["manifest"].get("input") or {}) and have in a["manifest"]["input"].values()
                              and schema in (a["manifest"].get("output") or {}).values()]
            raise ResolutionError(
                f"input '{port}' of {consumer} needs {schema}, artifact provides {have}",
                details={"consumer": consumer, "port": port, "required_contract": schema,
                         "available_contract": have, "approved_adapters": adapters_avail})
    extra = sorted(set(inputs) - set(man.get("input") or {}) - set(optional_in))
    if extra:
        raise ResolutionError(f"unexpected inputs {extra} for {consumer}", details={"consumer": consumer})
    # Port bindings with cycle detection.
    ports: dict[str, Any] = {}
    visiting: set[str] = set()

    def bind(cap: str, owner: str, required: bool) -> None:
        if cap in ports:
            return
        if cap in visiting:
            raise ResolutionError(f"dependency cycle through {cap}", details={"consumer": owner})
        visiting.add(cap)
        try:
            rel = select_release(cap, catalog, override=overrides.get(cap),
                                 binding=(profile.get("bindings") or {}).get(cap),
                                 default=(profile.get("defaults") or {}).get(cap), consumer=owner)
        except ResolutionError:
            if required:
                raise
            ports[cap] = {"absent": True, "fallback": "documented-absent-value"}
            visiting.discard(cap)
            return
        ports[cap] = {"ref": rel["ref"], "digest": rel["digest"], "selected_by": rel["selected_by"]}
        for sub in rel["manifest"].get("requires", []):
            if sub not in BUILTIN_PORTS:
                bind(sub, rel["ref"], True)
        visiting.discard(cap)

    for cap in man.get("requires", []):
        if cap in BUILTIN_PORTS:
            ports[cap] = {"builtin": True}
        else:
            bind(cap, consumer, True)
    for cap in man.get("optional", []):
        if cap in BUILTIN_PORTS:
            ports[cap] = {"builtin": True}
        else:
            bind(cap, consumer, False)
    reqs = {consumer: man.get("dependencies", [])}
    for p in ports.values():
        if p.get("ref"):
            rel = next(r for r in catalog if r["ref"] == p["ref"])
            reqs[p["ref"]] = rel["manifest"].get("dependencies", [])
    conflicts = dependency_conflicts(reqs, installed)
    if conflicts:
        raise ResolutionError("dependency conflict", details={"consumer": consumer, "conflicts": conflicts},
                              next_action="Use a separate trial/environment or adjust the component pins.")
    lock = {
        "lock": "eidos-run-lock/1",
        "component": {"ref": consumer, "id": man["id"], "release": man["release"], "digest": target["digest"],
                      "entry": man["entry"], "kind": man["kind"], "implements": man["implements"],
                      "effects": man["scientific_effects"], "isolation": man.get("isolation")},
        "environment": target.get("environment"),
        "ports": ports,
        "inputs": {k: {"artifact_id": v["artifact_id"], "digest": v["digest"], "schema": v.get("schema")}
                   for k, v in inputs.items()},
        "outputs": man.get("output") or {},
        "config": config,
        "grants": sorted(set(man.get("grants", []))),
        "profile_revision": profile.get("revision"),
    }
    lock["digest"] = "sha256:" + sha256_bytes(canonical_json(lock).encode())
    return lock


BUILTIN_PORTS = {"artifact.reader/1", "artifact.writer/1", "model.client/1", "checkpoint.io/1", "rng/1"}


def order_hooks(extensions: list[dict[str, Any]], adapter_phases: list[str]) -> list[dict[str, Any]]:
    """Validate and totally order training-extension hooks.

    Each extension: {"ref", "manifest"}; manifest declares phases, writes and optional
    ``compose`` (resources it composes additively with other writers) and ``after`` (refs).
    """
    order = {p: i for i, p in enumerate(PHASES)}
    supported = set(adapter_phases)
    writers: dict[tuple[str, str], list[str]] = {}
    composable: dict[tuple[str, str], bool] = {}
    for ext in extensions:
        man = ext["manifest"]
        for ph in man.get("phases", []):
            if ph not in supported:
                raise IncompatibleHook(
                    f"{ext['ref']} needs phase '{ph}', which the project adapter does not provide",
                    details={"consumer": ext["ref"], "phase": ph, "adapter_phases": sorted(supported)},
                    next_action="Implement the phase in the adapter or supply a whole training step.")
            for w in man.get("writes", []):
                writers.setdefault((ph, w), []).append(ext["ref"])
                composable[(ph, w)] = composable.get((ph, w), True) and w in (man.get("compose") or [])
    for (ph, res), refs in writers.items():
        if len(refs) > 1 and not composable[(ph, res)]:
            raise IncompatibleHook(f"{refs} all write {res} in phase {ph} with no defined composition",
                                   details={"phase": ph, "resource": res, "writers": refs})
    keyed = []
    for ext in extensions:
        first = min(order[p] for p in ext["manifest"].get("phases", []))
        keyed.append((first, len(ext["manifest"].get("after", [])), ext["ref"], ext))
    keyed.sort(key=lambda t: t[:3])
    placed = [k[3]["ref"] for k in keyed]
    for ext in extensions:
        for dep in ext["manifest"].get("after", []):
            if dep in placed and placed.index(dep) > placed.index(ext["ref"]):
                raise IncompatibleHook(f"{ext['ref']} must run after {dep} but phase order forbids it")
    return [{"ref": k[3]["ref"], "phases": k[3]["manifest"]["phases"], "position": i} for i, k in enumerate(keyed)]
