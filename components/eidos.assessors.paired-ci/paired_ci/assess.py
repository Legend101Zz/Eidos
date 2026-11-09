"""Protocol assessor: cluster-robust paired comparison of two arms under the plan's charter rule.

Runs in the LAB environment (it imports the lab's own ``eidos.domain`` code). It does not choose any
threshold: ``plan["success_rule"]`` is the rule materialised from the charter.
"""

from __future__ import annotations

import math
from typing import Any

from eidos.domain.evidence import evaluate_success_rule
from eidos_worker.errors import ContractError

# metric identity (normalised) -> per-pair function of (reference_correct, variant_correct)
UNIT_METRICS = {
    "both_correct_rate": lambda ref, var: float(ref and var),
    "both_correct": lambda ref, var: float(ref and var),
    "reference_accuracy": lambda ref, var: float(ref),
    "reference_correct": lambda ref, var: float(ref),
}


def normalise(metric: str) -> str:
    return metric.rsplit(":", 1)[-1].replace("-", "_")


def _pair_values(row: dict[str, Any]) -> tuple[bool, bool]:
    try:
        ref_score, var_score = float(row["reference_logit"]), float(row["variant_logit"])
        ref_label, var_label = int(float(row["reference_label"])), int(float(row["variant_label"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"pair {row.get('pair_id')!r}: unreadable row ({exc})") from exc
    if not (math.isfinite(ref_score) and math.isfinite(var_score)):
        raise ContractError(f"pair {row.get('pair_id')!r}: non-finite score")
    if ref_label not in (0, 1) or var_label not in (0, 1):
        raise ContractError(f"pair {row.get('pair_id')!r}: non-binary label")
    return (ref_score > 0) == bool(ref_label), (var_score > 0) == bool(var_label)


def per_unit_by_role(plan: dict[str, Any], evidence_arms: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    planned = {a["id"]: a.get("role") for a in plan.get("arms", [])}
    wanted = {plan["success_rule"]["metric"]} | {
        g["metric"] for g in plan["success_rule"].get("guardrails", [])
    }
    for m in wanted:
        if normalise(m) not in UNIT_METRICS:
            raise ContractError(
                f"cannot compute metric {m!r} from paired rows (known: {sorted(UNIT_METRICS)})"
            )
    by_role: dict[str, list[dict[str, Any]]] = {}
    seen: set = set()
    for arm_id, arm in sorted(evidence_arms.items()):
        if arm_id not in planned:
            raise ContractError(f"evidence arm {arm_id!r} is not in the frozen plan")
        if arm.get("role") != planned[arm_id]:
            raise ContractError(
                f"arm {arm_id!r}: evidence role {arm.get('role')!r} != planned role {planned[arm_id]!r}"
            )
        for row in arm.get("rows", []):
            ref_ok, var_ok = _pair_values(row)
            unit, cluster = str(row.get("pair_id")), str(row.get("cluster"))
            key = (arm["role"], cluster, unit)
            if key in seen:
                raise ContractError(
                    f"duplicate unit {unit!r} in cluster {cluster!r} for role {arm['role']!r}"
                )
            seen.add(key)
            record: dict[str, Any] = {"unit": unit, "cluster": cluster}
            for m in wanted:
                record[m] = UNIT_METRICS[normalise(m)](ref_ok, var_ok)
            by_role.setdefault(arm["role"], []).append(record)
    return by_role


def _finite(obj: Any, path: str, flagged: list[str]) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        flagged.append(path)
        return None
    if isinstance(obj, dict):
        return {k: _finite(v, f"{path}.{k}" if path else k, flagged) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v, f"{path}[{i}]", flagged) for i, v in enumerate(obj)]
    return obj


def run(inputs, config, *, reader, writer, context):
    evidence = reader.json(inputs["evidence"])
    if not isinstance(evidence, dict) or "plan" not in evidence or "arms" not in evidence:
        raise ContractError(
            "evidence must be {'plan': <frozen plan body>, 'arms': {arm_id: {'role', 'rows'}}}"
        )
    plan = evidence["plan"]
    for key in ("success_rule", "metric_definitions"):
        if key not in plan:
            raise ContractError(f"plan lacks {key!r}; it must be a compiled, frozen plan body")
    per_unit = per_unit_by_role(plan, evidence["arms"])
    result = evaluate_success_rule(plan, per_unit)
    flagged: list[str] = []
    result = _finite(result, "", flagged)
    comparison = plan["success_rule"].get("comparison") or {}
    clusters = sorted({r["cluster"] for rows in per_unit.values() for r in rows})
    evaluation = {
        **result,
        "unbounded_fields": flagged,
        "success_rule_ref": plan.get("success_rule_ref"),
        "analysis_unit": plan.get("analysis_unit"),
        "cluster_by": plan.get("cluster_by"),
        "units_by_role": {role: len(rows) for role, rows in sorted(per_unit.items())},
        "clusters": clusters,
        "roles": {
            "baseline": comparison.get("baseline_arm_role", "baseline"),
            "candidate": comparison.get("candidate_arm_role", "candidate"),
        },
        "assessor": "eidos.assessors.paired-ci@1.0.0",
    }
    writer.stage_json("evaluation", evaluation, schema="protocol-evaluation/1")
    effect = result.get("effect") or {}
    return {
        "outcome": result["outcome"],
        "units": effect.get("units", 0),
        "clusters": len(clusters),
        "metrics": {
            "estimate": effect.get("estimate"),
            "ci_low": effect.get("ci_low"),
            "ci_high": effect.get("ci_high"),
        },
    }
