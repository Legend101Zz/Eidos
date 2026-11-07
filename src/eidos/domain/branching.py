"""Checkpoint branch contract (design §"Checkpoint branching and repairability")."""

from __future__ import annotations

from typing import Any

from eidos.domain.errors import Invalid

ACTIONS = ("copy", "transform", "init", "drop")
RESTORE_CLASSES = ("exact", "numerically-equivalent", "warm-start", "inference-only", "unrestorable")
STREAM_POLICIES = ("paired", "independent")


def validate_branch(parent: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Validate a branch request against an immutable parent checkpoint manifest.

    parent: {"checkpoint_id", "trial_id", "step", "complete", "state_entries": [...],
             "cumulative_cost": {...}, "trajectory_id", "restore_class"}
    request: {"intervention": {"component_ref"?, "kind", "config"}, "migration": {entry: action|{action,..}},
              "new_state": [...], "stream_policy", "seed", "additional_budget": {...}, "claimed_restore_class"?}
    Returns the normalized branch intent with derived restore class and cost accounting.
    """
    problems = []
    if not parent.get("complete"):
        raise Invalid("a partially written checkpoint cannot be a branch parent",
                      details={"checkpoint": parent.get("checkpoint_id")})
    entries = list(parent.get("state_entries") or [])
    migration = request.get("migration") or {}
    norm: dict[str, dict[str, Any]] = {}
    for name, spec in migration.items():
        spec = {"action": spec} if isinstance(spec, str) else dict(spec)
        if spec.get("action") not in ACTIONS:
            problems.append(f"state {name!r}: action must be one of {ACTIONS}")
        if spec.get("action") == "transform" and not spec.get("mapping"):
            problems.append(f"state {name!r}: transform requires an explicit mapping")
        norm[name] = spec
    undeclared = [e for e in entries if e not in norm]
    if undeclared:
        problems.append(f"undeclared parent state: {undeclared} (copy, transform, init or drop each one)")
    unknown = [n for n, s in norm.items() if n not in entries and s.get("action") != "init"]
    if unknown:
        problems.append(f"migration names state absent from the parent: {unknown}")
    if request.get("stream_policy") not in STREAM_POLICIES:
        problems.append(f"stream_policy must be one of {STREAM_POLICIES}")
    if request.get("stream_policy") == "independent" and request.get("seed") is None:
        problems.append("an independent stream needs an explicit branch seed")
    intervention = request.get("intervention") or {"kind": "none"}
    if problems:
        raise Invalid("invalid branch request", details={"problems": problems})
    no_change = intervention.get("kind") == "none" and all(s["action"] == "copy" for s in norm.values())
    structural = intervention.get("kind") in ("change-structure",)
    if no_change:
        derived = parent.get("restore_class", "exact")
    elif structural or any(s["action"] in ("transform", "init", "drop") for s in norm.values()):
        derived = "warm-start"
    else:
        derived = "warm-start" if intervention.get("kind") in ("change-weights", "change-learning") else derived_default(parent)
    claimed = request.get("claimed_restore_class")
    if claimed and claimed != derived and RESTORE_CLASSES.index(claimed) < RESTORE_CLASSES.index(derived):
        raise Invalid(f"claimed restore class {claimed!r} is stronger than the migration supports ({derived!r})",
                      details={"claimed": claimed, "derived": derived})
    inherited = dict(parent.get("cumulative_cost") or {})
    additional = dict(request.get("additional_budget") or {})
    return {
        "parent": {k: parent.get(k) for k in ("checkpoint_id", "trial_id", "step", "trajectory_id")},
        "intervention": intervention,
        "migration": norm,
        "stream_policy": request["stream_policy"],
        "seed": request.get("seed"),
        "restore_class": derived,
        "no_change_continuation": no_change,
        "cost": {"inherited": inherited, "additional_budget": additional},
        # Siblings from one parent share training history: not independent replications.
        "independence_unit": parent.get("trajectory_id") or parent.get("trial_id"),
        "required_fixtures": ["no-change-restore", "intervention"] if not no_change else ["no-change-restore"],
    }


def derived_default(parent: dict[str, Any]) -> str:
    return parent.get("restore_class", "exact")


def total_cost(branch_costs: list[dict[str, Any]], parent_cost: dict[str, float]) -> dict[str, Any]:
    """Aggregate research cost: ancestry charged once, each child's additional cost added."""
    agg = dict(parent_cost)
    for c in branch_costs:
        for k, v in c.items():
            agg[k] = agg.get(k, 0) + v
    return {"inherited_once": parent_cost, "children_additional": branch_costs, "total": agg}
