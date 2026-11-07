"""Study charters: success rules fixed before exploration, exposure-informed amendments.

Design §"The study charter precedes exploration" / §"Later changes are exposure-informed".
Charter bodies are JSON objects; these functions validate and derive revisions. Storage of the
immutable revision chain belongs to the command service.
"""

from __future__ import annotations

import copy
from typing import Any

from eidos.domain.errors import Invalid, ThresholdOverride
from eidos_wire import canonical_json, sha256_bytes

DIRECTIONS = ("increase", "decrease")
DECISION_STANDARDS = ("paired-difference-ci", "threshold")
# Fields whose change after exposure is a change of the success rule itself.
RULE_FIELDS = ("metric", "min_effect", "guardrails", "decision_standard", "comparison", "target")
CHARTER_RULE_FIELDS = ("success_rules", "metrics", "population", "comparator")
# Keys a plan/template may never set: they would select a success bar outside the charter.
OVERRIDE_KEYS = (
    "min_effect",
    "threshold",
    "success_rule",
    "success_rules",
    "decision_standard",
    "guardrails",
    "max_regression",
    "metric_direction",
)


def charter_digest(body: dict[str, Any]) -> str:
    return "sha256:" + sha256_bytes(canonical_json(body).encode())


def validate_charter(body: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized charter body or raise ``Invalid`` listing every problem."""
    problems: list[str] = []
    b = copy.deepcopy(body)
    for key in ("study_id", "question"):
        if not isinstance(b.get(key), str) or not b[key].strip():
            problems.append(f"'{key}' is required")
    b.setdefault("exploration_only", False)
    b.setdefault("known_exposures", [])
    b.setdefault("resource_limits", {})
    metrics = b.get("metrics") or {}
    if not isinstance(metrics, dict):
        problems.append("'metrics' must map metric identities to definitions")
        metrics = {}
    for name, m in metrics.items():
        if not isinstance(m, dict):
            problems.append(f"metric {name!r} must be an object")
            continue
        if m.get("direction") not in DIRECTIONS:
            problems.append(f"metric {name!r}: direction must be one of {DIRECTIONS}")
        if not m.get("denominator"):
            problems.append(f"metric {name!r}: denominator (unit of analysis) is required")
    rules = b.get("success_rules") or []
    if b["exploration_only"]:
        if rules:
            problems.append("an exploration-only charter cannot carry success rules")
    else:
        if not rules:
            problems.append("success_rules are required unless exploration_only is true")
        for key in ("population", "comparator"):
            if not b.get(key):
                problems.append(f"'{key}' is required for a charter with success rules")
    ids = set()
    for rule in rules:
        rid = rule.get("id")
        if not rid or rid in ids:
            problems.append(f"success rule id {rid!r} missing or duplicated")
        ids.add(rid)
        if rule.get("metric") not in metrics:
            problems.append(f"rule {rid!r}: metric {rule.get('metric')!r} is not defined in 'metrics'")
        if not isinstance(rule.get("min_effect"), (int, float)):
            problems.append(f"rule {rid!r}: numeric min_effect is required")
        std = rule.get("decision_standard") or {}
        if std.get("kind") not in DECISION_STANDARDS:
            problems.append(f"rule {rid!r}: decision_standard.kind must be one of {DECISION_STANDARDS}")
        if std.get("kind") == "paired-difference-ci":
            conf = std.get("confidence", 0.95)
            if not (0.5 < conf < 1):
                problems.append(f"rule {rid!r}: confidence must be in (0.5, 1)")
            std.setdefault("confidence", conf)
            comp = rule.get("comparison") or {}
            if not comp.get("baseline_arm_role") or not comp.get("candidate_arm_role"):
                problems.append(f"rule {rid!r}: comparison needs baseline_arm_role and candidate_arm_role")
        for g in rule.get("guardrails", []) or []:
            if g.get("metric") not in metrics or not isinstance(g.get("max_regression"), (int, float)):
                problems.append(f"rule {rid!r}: guardrail needs a defined metric and numeric max_regression")
    if problems:
        raise Invalid("invalid study charter", details={"problems": problems})
    return b


def find_rule(charter: dict[str, Any], rule_id: str) -> dict[str, Any]:
    for rule in charter.get("success_rules", []):
        if rule["id"] == rule_id:
            return rule
    raise Invalid(f"success rule {rule_id!r} not in charter", details={"rule": rule_id})


def amend_charter(
    previous: dict[str, Any],
    new_body: dict[str, Any],
    *,
    rationale: str,
    author: str,
    exposure_refs: list[str],
    exposures_since_charter: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create an amended charter body plus its amendment record.

    The amendment is exposure-informed whenever relevant results were seen (by anyone,
    including a human) before it was written. That status cannot be cleared by the caller and
    survives a new study ID.
    """
    if not rationale or not rationale.strip():
        raise Invalid("a charter amendment requires a rationale")
    body = validate_charter(new_body)
    changes = {}
    for key in sorted(set(previous) | set(body)):
        if key in ("amendment",):
            continue
        if canonical_json(previous.get(key)) != canonical_json(body.get(key)):
            changes[key] = {"before": previous.get(key), "after": body.get(key)}
    if not changes:
        raise Invalid("amendment changes nothing")
    rule_change = any(k in CHARTER_RULE_FIELDS for k in changes)
    exposure_informed = bool(exposure_refs) or exposures_since_charter > 0
    amendment = {
        "changes": changes,
        "rationale": rationale,
        "author": author,
        "exposure_refs": list(exposure_refs),
        "exposure_informed": exposure_informed,
        "changes_success_rule": rule_change,
        "previous_study_id": previous.get("study_id"),
    }
    # Inherit prior exposure state across a renamed study: a new study ID cannot erase it.
    if previous.get("study_id") != body.get("study_id"):
        amendment["study_id_changed"] = True
    lineage_flag = previous.get("exposure_informed_lineage", False) or (exposure_informed and rule_change)
    body["exposure_informed_lineage"] = bool(lineage_flag)
    return body, amendment


def reject_threshold_overrides(source: str, obj: Any, path: str = "") -> None:
    """Raise if a plan/template/steering payload tries to define or override a success bar."""
    if isinstance(obj, dict):
        if obj.get("allow_plan_threshold_override"):
            raise ThresholdOverride(f"{source} requests a threshold override", details={"path": path})
        for k, v in obj.items():
            if k in OVERRIDE_KEYS:
                raise ThresholdOverride(
                    f"{source} sets '{k}', which only the pinned charter may define",
                    details={"path": f"{path}.{k}".lstrip(".")},
                )
            reject_threshold_overrides(source, v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            reject_threshold_overrides(source, v, f"{path}[{i}]")
