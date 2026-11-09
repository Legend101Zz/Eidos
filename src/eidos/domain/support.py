"""Support justifications and withdrawal impact (design §"Exposure and support are durable records").

A claim may have several independently sufficient justifications. Each justification lists the
observations, assessments, component releases and assumptions it jointly requires. A withdrawal
marks dependencies affected; an unaffected sufficient justification keeps the claim supported.
"Unsupported" means current support is inadequate, not that the statement is false.
"""

from __future__ import annotations

from typing import Any


def matches_condition(run: dict[str, Any], condition: dict[str, Any]) -> bool:
    """Affected-condition predicate: every key path in ``condition`` equals the run's value.

    An empty condition affects every use of the withdrawn release.
    """
    for path, expected in condition.items():
        cur: Any = run
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        if cur != expected:
            return False
    return True


def affected_refs(
    withdrawal: dict[str, Any], runs: list[dict[str, Any]], edges: list[tuple[str, str]]
) -> set[str]:
    """Traverse typed dependencies from a withdrawn release.

    ``runs`` are job records ({"ref", "component_ref", "config", ...}); ``edges`` are (src, dst)
    lineage/support edges meaning "dst depends on src".
    """
    release = withdrawal["release_ref"]
    cond = withdrawal.get("condition") or {}
    seeds = {release}
    for run in runs:
        if run.get("component_ref") == release and matches_condition(run, cond):
            seeds.add(run["ref"])
    children: dict[str, list[str]] = {}
    for src, dst in edges:
        children.setdefault(src, []).append(dst)
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        node = stack.pop()
        for nxt in children.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def support_status(justifications: list[dict[str, Any]], affected: set[str]) -> dict[str, Any]:
    per = []
    for j in justifications:
        hit = sorted(set(j.get("requires", [])) & affected)
        per.append({"id": j["id"], "status": "affected" if hit else "intact", "affected_dependencies": hit})
    intact = [p for p in per if p["status"] == "intact"]
    if not justifications:
        status = "unsupported"
    elif intact:
        status = "supported" if len(intact) == len(per) else "supported-with-affected-justifications"
    else:
        status = "unsupported-pending-reassessment"
    shared: dict[str, int] = {}
    for j in justifications:
        for dep in j.get("requires", []):
            shared[dep] = shared.get(dep, 0) + 1
    return {"status": status, "justifications": per,
            "shared_dependencies": sorted(d for d, n in shared.items() if n > 1)}
