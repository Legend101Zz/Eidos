"""Protocol assessor: cluster-robust paired comparison of two arms under the plan's charter rule.

Runs in the LAB environment (it imports the lab's own ``eidos.domain`` code). It does not choose any
threshold: ``plan["success_rule"]`` is the rule materialised from the charter.
"""

from __future__ import annotations

import math
import re
from typing import Any

from eidos.domain.evidence import evaluate_success_rule
from eidos_worker.errors import ContractError

# metric identity (normalised) -> per-pair function of (reference_correct, variant_correct).
# Rows: paired-report/1 (pair_id, cluster, reference/variant logits and binary labels).
UNIT_METRICS = {
    "both_correct_rate": lambda ref, var: float(ref and var),
    "both_correct": lambda ref, var: float(ref and var),
    "reference_accuracy": lambda ref, var: float(ref),
    "reference_correct": lambda ref, var: float(ref),
}
# metric identity (normalised) -> per-example function of (label, prediction). Rows: classification
# pair rows from eidos.task.paired-classification-report (example_id, cluster, role, label, prediction).
# Ordinary (including multiclass) accuracy; ADR 0018.
EXAMPLE_METRICS = {
    "accuracy": lambda label, prediction: float(label == prediction),
}
FAMILIES = {"paired-report": UNIT_METRICS, "classification": EXAMPLE_METRICS}
ASSESSOR_REF = "eidos.assessors.paired-ci@1.1.0"


def normalise(metric: str) -> str:
    return metric.rsplit(":", 1)[-1].replace("-", "_")


def _family(metrics: set[str]) -> str:
    """All of a rule's metrics (target and guardrails) must be computable from one row family."""
    fams = set()
    for m in metrics:
        fam = next((f for f, table in FAMILIES.items() if normalise(m) in table), None)
        if fam is None:
            known = sorted(k for table in FAMILIES.values() for k in table)
            raise ContractError(f"cannot compute metric {m!r} from paired rows (known: {known})")
        fams.add(fam)
    if len(fams) > 1:
        raise ContractError(f"metrics {sorted(metrics)} need different row families {sorted(fams)}; "
                            "a rule and its guardrails must be computable from the same rows")
    return fams.pop()


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


_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+(\.0*)?")
_NON_FINITE = ("nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity")


def _class_value(row: dict[str, Any], field: str) -> str:
    """A label or prediction in canonical string form (integers, integral floats and strings spelling an
    integer as decimal integers); null, boolean, empty, non-integral or non-finite values are refused."""
    v = row.get(field)
    where = f"example {row.get('example_id')!r}: {field}"
    if v is None or isinstance(v, bool):
        raise ContractError(f"{where} is missing or not a class label ({v!r})")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v) or not v.is_integer():
            raise ContractError(f"{where} {v!r} is non-finite or not an integer class")
        return str(int(v))
    if isinstance(v, str) and v.strip() and v.strip().lower() not in _NON_FINITE:
        text = v.strip()
        if _INTEGER_TEXT.fullmatch(text):  # the same canonical form as eidos.task.paired-classification-report
            return str(int(text.split(".", 1)[0]))
        return text
    raise ContractError(f"{where} {v!r} is not a class label")


def _example_values(row: dict[str, Any], role: str) -> tuple[str, str, dict[str, str]]:
    if row.get("example_id") is None or isinstance(row.get("example_id"), bool) or str(row["example_id"]).strip() == "":
        raise ContractError(f"classification row without an example_id: {sorted(row)}")
    unit = _class_value(row, "example_id")  # ids and seeds use the same canonical form as labels
    if row.get("cluster") is None or isinstance(row.get("cluster"), bool) or str(row["cluster"]).strip() == "":
        raise ContractError(f"example {unit!r}: classification rows need a cluster (the training seed)")
    cluster = _class_value(row, "cluster")
    if row.get("role") is not None and row["role"] != role:
        raise ContractError(f"example {unit!r}: row role {row['role']!r} in an arm with role {role!r}")
    label, prediction = _class_value(row, "label"), _class_value(row, "prediction")
    return unit, cluster, {"label": label, "prediction": prediction}


def _check_classification_alignment(plan: dict[str, Any], by_role: dict[str, list[dict[str, Any]]],
                                     labels: dict[tuple[str, str, str], str], complete: dict[str, bool]) -> None:
    """Ordinary accuracy is paired by example: both arms must cover exactly the same (cluster, example)
    units with the same labels, and several training seeds need cluster_by (fail closed, ADR 0018).

    An arm whose execution is incomplete (a failed report job) may lack whole clusters, or all rows: those
    clusters are not paired, and the server's conformance check makes the assessment inconclusive. Within a
    cluster present in both arms the example ids must still match exactly."""
    comp = plan["success_rule"].get("comparison") or {}
    base_role = comp.get("baseline_arm_role", "baseline")
    cand_role = comp.get("candidate_arm_role", "candidate")
    keys = {role: {(r["cluster"], r["unit"]) for r in by_role.get(role, [])} for role in (base_role, cand_role)}
    if not all(complete.get(role, True) for role in (base_role, cand_role)):
        if not keys[base_role] or not keys[cand_role]:
            return  # nothing to pair: the domain reports "no paired units" (inconclusive)
        shared = {c for c, _ in keys[base_role]} & {c for c, _ in keys[cand_role]}
        keys = {role: {k for k in ks if k[0] in shared} for role, ks in keys.items()}
    for role, ks in keys.items():
        if not ks:
            raise ContractError(f"no classification rows for role {role!r}")
    only_base, only_cand = keys[base_role] - keys[cand_role], keys[cand_role] - keys[base_role]
    if only_base or only_cand:
        sample = lambda ks: [f"{c}/{u}" for c, u in sorted(ks)[:5]]  # noqa: E731
        raise ContractError(
            "baseline and candidate example ids do not match: "
            f"{len(only_base)} only in {base_role!r} (e.g. {sample(only_base)}), "
            f"{len(only_cand)} only in {cand_role!r} (e.g. {sample(only_cand)}); "
            "every training seed (cluster) must be evaluated on the same examples in both arms"
        )
    for (cluster, unit) in keys[base_role]:
        if labels[(base_role, cluster, unit)] != labels[(cand_role, cluster, unit)]:
            raise ContractError(f"example {cluster}/{unit}: the arms disagree on its label")
    clusters = {c for c, _ in keys[base_role]}
    if len(clusters) > 1 and not plan.get("cluster_by"):
        raise ContractError(
            f"rows come from {len(clusters)} training seeds but the plan has no cluster_by; "
            "treating examples from several trained models as independent would understate the variance"
        )


def per_unit_by_role(plan: dict[str, Any], evidence_arms: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    planned = {a["id"]: a.get("role") for a in plan.get("arms", [])}
    wanted = {plan["success_rule"]["metric"]} | {
        g["metric"] for g in plan["success_rule"].get("guardrails", [])
    }
    family = _family(wanted)
    by_role: dict[str, list[dict[str, Any]]] = {}
    labels: dict[tuple[str, str, str], str] = {}
    seen: set = set()
    for arm_id, arm in sorted(evidence_arms.items()):
        if arm_id not in planned:
            raise ContractError(f"evidence arm {arm_id!r} is not in the frozen plan")
        if arm.get("role") != planned[arm_id]:
            raise ContractError(
                f"arm {arm_id!r}: evidence role {arm.get('role')!r} != planned role {planned[arm_id]!r}"
            )
        for row in arm.get("rows", []):
            record: dict[str, Any]
            if family == "classification":
                unit, cluster, vals = _example_values(row, arm["role"])
                record = {"unit": unit, "cluster": cluster}
                for m in wanted:
                    record[m] = EXAMPLE_METRICS[normalise(m)](vals["label"], vals["prediction"])
            else:
                ref_ok, var_ok = _pair_values(row)
                unit, cluster = str(row.get("pair_id")), str(row.get("cluster"))
                record = {"unit": unit, "cluster": cluster}
                for m in wanted:
                    record[m] = UNIT_METRICS[normalise(m)](ref_ok, var_ok)
            key = (arm["role"], cluster, unit)
            if key in seen:
                raise ContractError(
                    f"duplicate unit {unit!r} in cluster {cluster!r} for role {arm['role']!r}"
                )
            seen.add(key)
            if family == "classification":
                labels[key] = vals["label"]
            by_role.setdefault(arm["role"], []).append(record)
    if family == "classification":
        # An arm counts as complete unless its evidence says otherwise (assess_plan passes each arm's status).
        complete = {arm.get("role"): arm.get("status", "succeeded") == "succeeded" for arm in evidence_arms.values()}
        _check_classification_alignment(plan, by_role, labels, complete)
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
        "assessor": ASSESSOR_REF,
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
