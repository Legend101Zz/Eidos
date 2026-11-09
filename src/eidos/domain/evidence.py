"""Claims, evidence plans and assessments (design §"Claims and evidence plans").

Two independent state axes are kept for every assessment: outcome (unassessed, protocol-passed,
protocol-failed, inconclusive) and evidence health (intact, affected, withdrawn); execution
conformance (conformant, deviated, incomplete) is a third field.
"""

from __future__ import annotations

import copy
from typing import Any

from eidos.domain.charter import charter_digest, find_rule, reject_threshold_overrides
from eidos.domain.errors import Invalid, QualificationMissing
from eidos.domain.stats import paired_difference
from eidos_wire import canonical_json, sha256_bytes

CLAIM_KINDS = ("improves", "intervention-effect", "decodable", "transfer", "mechanism", "exploratory")
OUTCOMES = ("unassessed", "protocol-passed", "protocol-failed", "inconclusive")
HEALTH = ("intact", "affected", "withdrawn")
CONFORMANCE = ("conformant", "deviated", "incomplete")
ARM_ROLES = ("baseline", "candidate", "control", "positive-control", "null-control")


def digest(obj: Any) -> str:
    return "sha256:" + sha256_bytes(canonical_json(obj).encode())


def validate_claim(body: dict[str, Any]) -> dict[str, Any]:
    problems = []
    if not body.get("statement"):
        problems.append("statement is required")
    if body.get("kind") not in CLAIM_KINDS:
        problems.append(f"kind must be one of {CLAIM_KINDS}")
    if not isinstance(body.get("scope"), dict) or not body["scope"]:
        problems.append("a non-empty scope (model/task/population) is required")
    if problems:
        raise Invalid("invalid claim", details={"problems": problems})
    b = copy.deepcopy(body)
    for k in ("assumptions", "alternatives", "supporting", "contradicting"):
        b.setdefault(k, [])
    return b


def compile_plan(
    *,
    draft: dict[str, Any],
    claim: dict[str, Any],
    claim_ref: dict[str, Any],
    charter: dict[str, Any],
    charter_ref: dict[str, Any],
    template_ref: dict[str, Any],
    template_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a planner's draft against the pinned charter.

    The success rule is *materialized from the charter*; any attempt by the draft or template
    to define a threshold, guardrail or decision standard is rejected.
    """
    reject_threshold_overrides("evidence-plan draft", draft)
    if template_defaults:
        reject_threshold_overrides("protocol template", template_defaults)
    if charter.get("exploration_only"):
        raise Invalid("an exploration-only charter has no success rule to assess against",
                      next_action="Commit or amend a charter with prospective success rules.")
    rule_id = draft.get("success_rule_id")
    if not rule_id:
        raise Invalid("draft must reference a charter success rule by success_rule_id")
    rule = find_rule(charter, rule_id)
    if charter_digest(charter) != charter_ref.get("digest"):
        raise Invalid("charter digest does not match the pinned revision")
    arms = draft.get("arms") or []
    problems = []
    roles = {a.get("role") for a in arms}
    for a in arms:
        if a.get("role") not in ARM_ROLES:
            problems.append(f"arm {a.get('id')!r}: role must be one of {ARM_ROLES}")
        if not a.get("id"):
            problems.append("every arm needs an id")
    comp = rule.get("comparison") or {}
    for need in (comp.get("baseline_arm_role"), comp.get("candidate_arm_role")):
        if need and need not in roles:
            problems.append(f"charter rule requires an arm with role {need!r}")
    if problems:
        raise Invalid("plan does not satisfy the charter", details={"problems": problems})
    plan = {
        "claim_ref": claim_ref,
        "claim_kind": claim["kind"],
        "scope": claim["scope"],
        "charter_ref": charter_ref,
        "success_rule_ref": f"{charter_ref['id']}@{charter_ref['revision']}#{rule_id}",
        "success_rule_source": "immutable-charter",
        "allow_plan_threshold_override": False,
        "success_rule": copy.deepcopy(rule),
        "metric_definitions": {m: charter["metrics"][m] for m in
                               {rule["metric"], *[g["metric"] for g in rule.get("guardrails", [])]}},
        "template": template_ref,
        "assessor": draft.get("assessor"),
        "arms": arms,
        "analysis_unit": draft.get("analysis_unit"),
        "cluster_by": draft.get("cluster_by"),
        "seed_policy": draft.get("seed_policy"),
        "splits": draft.get("splits"),
        "tuning": draft.get("tuning", {}),
        # No silent default: running confirmation on the same OS user must be an explicit choice.
        "required_isolation": draft.get("required_isolation"),
        "resource_reservation": draft.get("resource_reservation", {}),
        "obligations": list(draft.get("obligations", [])),
        "alternatives_unresolved": list(draft.get("alternatives_unresolved", [])),
        "disclosures": draft.get("disclosures", {}),
    }
    ri = plan["required_isolation"]
    if ri is not None and not isinstance(ri, dict):
        raise Invalid("required_isolation must be an object, e.g. {\"sealed\": true} or "
                      "{\"profile\": \"dev:same-user\", \"controls\": [], \"waiver\": \"<reason>\"}",
                      details={"got": ri})
    for key in ("analysis_unit", "seed_policy", "assessor", "required_isolation"):
        if not plan[key] and key not in plan["obligations"]:
            plan["obligations"].append(key)
    return plan


def check_freezable(plan: dict[str, Any], *, template_approved: bool, assessor_approved: bool) -> None:
    if plan.get("obligations"):
        raise Invalid("plan has unresolved obligations", details={"obligations": plan["obligations"]})
    if not template_approved:
        raise QualificationMissing("protocol template is not qualified and approved for this use")
    if not assessor_approved:
        raise QualificationMissing("assessor release is not qualified and approved for this use")


def check_conformance(plan: dict[str, Any], realized: dict[str, Any]) -> dict[str, Any]:
    """Compare a frozen plan with realized execution receipts.

    ``realized`` = {"arms": {arm_id: {"status", "receipt", "isolation"}}, "isolation_ok": bool,
    "isolation_findings": [...]}. Returns {"conformance", "findings"}. Execution success alone
    never implies conformance: every arm must have a succeeded attempt with a receipt, and the
    realized isolation must satisfy the plan.
    """
    findings: list[dict[str, Any]] = []
    status = "conformant"
    arms = realized.get("arms", {})
    for arm in plan["arms"]:
        r = arms.get(arm["id"])
        if not r or r.get("status") != "succeeded":
            findings.append({"arm": arm["id"], "finding": "missing or unsuccessful execution"})
            status = "incomplete"
            continue
        receipt = r.get("receipt") or {}
        want = arm.get("expected") or {}
        for key, expected in want.items():
            got = receipt.get(key)
            if got != expected:
                findings.append({"arm": arm["id"], "finding": f"realized {key}={got!r} differs from planned {expected!r}"})
                if status == "conformant":
                    status = "deviated"
    if not realized.get("isolation_ok", False):
        findings.extend(realized.get("isolation_findings", [{"finding": "required isolation not established"}]))
        if status == "conformant":
            status = "deviated"
    return {"conformance": status, "findings": findings}


def evaluate_success_rule(plan: dict[str, Any], per_unit: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Apply the plan's materialized charter rule to per-unit results.

    ``per_unit[arm_role] = [{"unit": id, "cluster": c, "<metric>": value, ...}]``. Units are paired
    by id between candidate and baseline.
    """
    rule = plan["success_rule"]
    metric = rule["metric"]
    mdef = plan["metric_definitions"][metric]
    sign = 1 if mdef["direction"] == "increase" else -1
    comp = rule.get("comparison") or {}
    cand = {(r.get("cluster"), r["unit"]): r for r in per_unit.get(comp.get("candidate_arm_role", "candidate"), [])}
    base = {(r.get("cluster"), r["unit"]): r for r in per_unit.get(comp.get("baseline_arm_role", "baseline"), [])}
    keys = sorted(set(cand) & set(base), key=lambda k: (str(k[0]), str(k[1])))
    if not keys:
        return {"outcome": "inconclusive", "reason": "no paired units", "effect": None, "guardrails": []}
    std = rule["decision_standard"]
    conf = std.get("confidence", 0.95)
    diffs = [sign * (float(cand[k][metric]) - float(base[k][metric])) for k in keys]
    clusters = [str(k[0]) for k in keys] if plan.get("cluster_by") else None
    eff = paired_difference(diffs, clusters, conf)
    min_eff = float(rule["min_effect"])
    if std["kind"] == "threshold":
        outcome = "protocol-passed" if eff["estimate"] >= min_eff else "protocol-failed"
    elif eff["ci_low"] >= min_eff:
        outcome = "protocol-passed"
    elif eff["ci_high"] < min_eff:
        outcome = "protocol-failed"
    else:
        outcome = "inconclusive"
    guards = []
    for g in rule.get("guardrails", []):
        gm = g["metric"]
        gsign = 1 if plan["metric_definitions"][gm]["direction"] == "increase" else -1
        gd = [gsign * (float(cand[k][gm]) - float(base[k][gm])) for k in keys if gm in cand[k] and gm in base[k]]
        if not gd:
            guards.append({"metric": gm, "status": "missing"})
            outcome = "inconclusive" if outcome == "protocol-passed" else outcome
            continue
        ge = paired_difference(gd, clusters, conf)
        ok = ge["ci_low"] >= -float(g["max_regression"])
        guards.append({"metric": gm, "status": "ok" if ok else "violated", **ge})
        if not ok and outcome == "protocol-passed":
            outcome = "protocol-failed" if ge["ci_high"] < -float(g["max_regression"]) else "inconclusive"
    return {"outcome": outcome, "effect": eff, "min_effect": min_eff, "guardrails": guards,
            "direction": mdef["direction"], "metric": metric}


def finalize_assessment(
    *, plan: dict[str, Any], plan_digest: str, evidence_set_digest: str, assessor_ref: dict[str, Any],
    conformance: dict[str, Any], evaluation: dict[str, Any],
) -> dict[str, Any]:
    """Combine conformance and the statistical evaluation. Non-conformance forces inconclusive."""
    outcome = evaluation["outcome"]
    if conformance["conformance"] != "conformant" and outcome == "protocol-passed":
        outcome = "inconclusive"
    if conformance["conformance"] == "incomplete":
        outcome = "inconclusive"
    return {
        "plan_digest": plan_digest,
        "evidence_set_digest": evidence_set_digest,
        "assessor": assessor_ref,
        "charter_ref": plan["charter_ref"],
        "success_rule_ref": plan["success_rule_ref"],
        "conformance": conformance["conformance"],
        "findings": conformance["findings"],
        "outcome": outcome,
        "evaluation": evaluation,
        "scope": plan["scope"],
        "remaining_alternatives": plan.get("alternatives_unresolved", []),
        "health": "intact",
    }
