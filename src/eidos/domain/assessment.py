"""Assessment by default and evidence-derived claim state (ADR 0021).

Two sets of pure rules. The lab command ``assess_candidate`` (``application/handlers/assessment.py``) uses the first
to go from a locked candidate, a baseline and a charter rule to a frozen, launched evidence plan: it picks the
assessment path for the rule's metric and expands the plan draft. The conclusion path uses the second: the claim state
of a conclusion comes from recorded assessments, never from the status a model chooses.

The draft expansions below reproduce ``eidos.templates.improves-under-budget@1.2.0`` (its ``classification`` and
train/evaluate paths) so the command can answer in one transaction instead of waiting for a template job; a test keeps
them equal to the template's own output (``tests/components/test_assessment_pipeline_template.py``).
"""

from __future__ import annotations

import math
from typing import Any

from eidos.domain.errors import Invalid

ASSESSOR_REF = "eidos.assessors.paired-ci@1.2.0"
TEMPLATE_REF = "eidos.templates.improves-under-budget@1.2.0"
REPORT_REF = "eidos.task.paired-classification-report@1.0.0"
TRAIN = "eidos.pytorch.train"
EVALUATE = "eidos.pytorch.evaluate"
ROLES = ("baseline", "candidate")

PATH_CLASSIFICATION = "classification"
PATH_TRAIN_EVALUATE = "train-evaluate"
# The metric identity decides the path. A metric without a path has no independent assessor here and is refused.
METRIC_PATHS = {
    "classification-report/1:accuracy": PATH_CLASSIFICATION,
    "paired-report/1:both-correct-rate": PATH_TRAIN_EVALUATE,
    "paired-report/1:reference-accuracy": PATH_TRAIN_EVALUATE,
}
# Schemas the classification adapter reads by content (JSON records or columns, JSONL, CSV, NPZ). The second of each
# pair is what eidos.project.command publishes; the command registers a typed reference to the same bytes.
PREDICTION_SCHEMAS = ("class-predictions/1", "prediction-array/1")
LABEL_SCHEMAS = ("class-labels/1", "task-array/1")
CHECKPOINT_SCHEMAS = ("eidos-checkpoint/1", "model-file/1")

PROVENANCE_HELD_OUT = "held-out"
PROVENANCE_SELECTION = "selection"


def reason(code: str, message: str, **details: Any) -> dict[str, Any]:
    """One recorded reason a request was refused: a stable code, a sentence and what the caller can act on."""
    return {"code": code, "message": message, **({"details": details} if details else {})}


def seed_key(seed: Any) -> str | None:
    """Canonical text of a training seed, or None when it is not an integer or a non-empty string."""
    if isinstance(seed, bool) or not isinstance(seed, (int, str)) or str(seed).strip() == "":
        return None
    return str(seed).strip()


def seed_sort_key(seed: str) -> tuple[bool, int, str]:
    return (not seed.isdigit(), len(seed), seed)


def seed_value(seed: str) -> int | str:
    return int(seed) if seed.isdigit() else seed


# -- request validation --------------------------------------------------------------------------------------------
def metric_path(charter: dict[str, Any], rule_id: str) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    """(the charter rule, its assessment path, refusal reasons) for a rule id."""
    if charter.get("exploration_only"):
        return None, None, [reason("exploration-only-charter", "an exploration-only charter has no success rule to "
                                   "assess against; commit a charter with prospective success rules")]
    rule = next((r for r in charter.get("success_rules") or [] if r.get("id") == rule_id), None)
    if rule is None:
        return None, None, [reason("rule-not-found", f"success rule {rule_id!r} is not in the charter",
                                   available=[r.get("id") for r in charter.get("success_rules") or []])]
    identity = str((charter.get("metrics", {}).get(rule["metric"]) or {}).get("identity", ""))
    path = METRIC_PATHS.get(identity)
    if path is None:
        return rule, None, [reason("no-assessment-path", f"no assessment path exists for metric identity {identity!r}; "
                                   "supported identities are listed in details", supported=sorted(METRIC_PATHS))]
    return rule, path, []


def check_seed_pairing(baseline: Any, candidate: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Normalize ``{seed: value}`` maps for both arms and require the same, valid seeds in each."""
    problems: list[dict[str, Any]] = []
    arms: dict[str, dict[str, Any]] = {}
    for role, spec in (("baseline", baseline), ("candidate", candidate)):
        if not isinstance(spec, dict) or not spec:
            problems.append(reason("seed-pairing", f"{role} must map each training seed to a job id"))
            continue
        arms[role] = {}
        for seed, value in spec.items():
            key = seed_key(seed)
            if key is None:
                problems.append(reason("seed-pairing", f"{role}: seed {seed!r} must be an integer or a non-empty string"))
            elif key in arms[role]:
                problems.append(reason("seed-pairing", f"{role}: seed {key} appears twice"))
            else:
                arms[role][key] = value
    if set(arms) == set(ROLES) and set(arms["baseline"]) != set(arms["candidate"]):
        problems.append(reason("seed-pairing", "paired seed clusters need the same training seeds in both arms",
                               baseline=sorted(arms["baseline"], key=seed_sort_key),
                               candidate=sorted(arms["candidate"], key=seed_sort_key)))
    return arms, problems


def fresh_initialization_problem(lock: dict[str, Any]) -> str | None:
    """Why a training job's lock shows it did not start from a fresh initialization, or None."""
    for port, binding in (lock.get("inputs") or {}).items():
        if port == "parent_checkpoint" or (binding or {}).get("schema") in CHECKPOINT_SCHEMAS:
            return f"it restored a checkpoint through input {port!r}"
    if (lock.get("config") or {}).get("branch"):
        return "it branched from a parent trial"
    return None


def job_seed(lock: dict[str, Any]) -> str | None:
    """The training seed a job recorded: the lock's own seed, else its config's."""
    config = lock.get("config") or {}
    return seed_key(lock.get("seed") if lock.get("seed") is not None else config.get("seed"))


def recipe_identity(lock: dict[str, Any]) -> dict[str, Any]:
    """What must match the locked candidate: component, input bytes and configuration, seed and timeout aside."""
    config = {k: v for k, v in (lock.get("config") or {}).items() if k not in ("seed", "timeout_seconds")}
    inputs = {port: (binding or {}).get("digest") for port, binding in (lock.get("inputs") or {}).items()}
    return {"component": (lock.get("component") or {}).get("ref"), "inputs": inputs, "config": config}


def check_provenance(declared: Any, *, charter: dict[str, Any], labels_digest: str | None = None,
                     split: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(the provenance recorded on the plan, refusal reasons).

    Selection data never becomes confirmation. Held-out data must be attested in the charter (committed by an operator
    before the investigation), by labels digest or by split name; an investigator's declaration alone is not enough.
    """
    if not isinstance(declared, dict) or declared.get("split") not in (PROVENANCE_HELD_OUT, PROVENANCE_SELECTION):
        return None, [reason("provenance-required", "data_provenance.split must be 'held-out' or 'selection'")]
    if declared["split"] == PROVENANCE_SELECTION:
        return None, [reason("selection-data", "these labels were used for selection, so they stay exploratory: a plan "
                             "over them cannot confirm anything. Conclude with the candidate submitted for confirmation "
                             "and name the held-out evaluation as an unfulfilled obligation")]
    attested = charter.get("held_out") or {}
    labels_ok = labels_digest is not None and labels_digest in (attested.get("labels") or [])
    split_ok = split is not None and split in (attested.get("splits") or [])
    if not labels_ok and not split_ok:
        what = f"labels digest {labels_digest}" if labels_digest else f"split {split!r}"
        return None, [reason("held-out-unattested", f"the charter does not attest {what} as held-out data; an operator "
                             "must commit it in the charter's held_out section before these data are used")]
    return {"split": PROVENANCE_HELD_OUT, "attested_by": "charter",
            **({"labels_digest": labels_digest} if labels_digest else {}), **({"evaluation_split": split} if split else {}),
            **({"note": str(declared["note"])[:500]} if declared.get("note") else {})}, []


# -- draft expansion (equals eidos.templates.improves-under-budget@1.2.0) -----------------------------------------
def _alternatives(request: dict[str, Any]) -> list[Any]:
    return list(request.get("alternatives_unresolved") or [
        "gain explained by extra computation rather than the candidate's mechanism",
        "gain specific to the evaluation population",
    ])


def classification_draft(request: dict[str, Any]) -> dict[str, Any]:
    """The template's ``classification`` draft from {claim, success_rule_id, assessor, required_isolation, tuning?,
    budget?, alternatives_unresolved?, classification: {labels, predictions: {role: {seed: artifact}}, classes?,
    id_field?}}. Seeds were checked by ``check_seed_pairing``."""
    spec = request["classification"]
    predictions, labels = spec["predictions"], spec["labels"]
    seeds = sorted(predictions["baseline"], key=seed_sort_key)
    extra = {k: spec[k] for k in ("classes", "id_field") if k in spec}
    arms = []
    for role in ROLES:
        runs = [{"seed": seed_value(seed), "cluster": seed,
                 "jobs": [{"id": f"{role}.s{seed}.report", "component": REPORT_REF,
                           "inputs": {"baseline": predictions["baseline"][seed],
                                      "candidate": predictions["candidate"][seed], "labels": labels},
                           "config": {"role": role, "cluster": seed, **extra}}]} for seed in seeds]
        recipe = request.get(role) or {}
        arms.append({"id": role, "role": role,
                     "description": recipe.get("description", f"{role} recipe (existing held-out predictions)"),
                     "runs": runs})
    several = len(seeds) > 1
    alternatives = _alternatives(request)
    if not several:
        alternatives.append("gain specific to these trained models: one training seed per arm, so training-seed "
                            "variance is not estimated")
    return {
        "success_rule_id": request["success_rule_id"],
        "assessor": request.get("assessor"),
        "arms": arms,
        "analysis_unit": "example",
        "cluster_by": "seed" if several else None,
        "seed_policy": {
            "kind": "independent-training-seeds",
            "seeds": [seed_value(s) for s in seeds],
            "shared_across_arms": True,
            "note": ("each seed is one trained model per arm; examples within a seed are clustered" if several
                     else "one trained model per arm; the interval reflects held-out example sampling only"),
        },
        "splits": {"evaluation": {"labels": labels, "population": "held-out examples"}, "tuning": None},
        "tuning": request.get("tuning", {}),
        "required_isolation": request.get("required_isolation"),
        "resource_reservation": {"matched_budget": request.get("budget") or {}, "arms": len(arms), "seeds": len(seeds)},
        "obligations": [],
        "alternatives_unresolved": alternatives,
        "disclosures": {
            "claim_statement": (request.get("claim") or {}).get("statement"),
            "evidence_path": "existing held-out predictions -> eidos.task.paired-classification-report -> "
                             "eidos.assessors.paired-ci (metric accuracy)",
            "predictions_predate_plan": "the predictions were produced before this plan was frozen; the plan pins "
                                        "their artifacts, and the charter rule was fixed before them",
            "seed_variance": "clustered by training seed" if several else "not estimated (one seed per arm)",
            "template": "eidos.templates.improves-under-budget",
        },
    }


TRAIN_EVALUATE_FIELDS = ("seeds", "evaluation", "baseline", "candidate", "common_train", "common_evaluate", "tuning", "budget")


def train_evaluate_draft(request: dict[str, Any]) -> dict[str, Any]:
    """The template's train/evaluate draft from {claim, success_rule_id, assessor, required_isolation, seeds: [int],
    evaluation, baseline, candidate, common_train?, common_evaluate?, tuning?, budget?}. Always from a fresh
    initialization: the command refuses a parent checkpoint."""
    seeds, evaluation = list(request["seeds"]), dict(request["evaluation"])

    def runs(arm_id: str, recipe: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for seed in seeds:
            prefix = f"{arm_id}.s{seed}"
            jobs: list[dict[str, Any]] = [{
                "id": f"{prefix}.train", "component": TRAIN, "inputs": {},
                "config": {**(request.get("common_train") or {}), **(recipe.get("train") or {}), "seed": seed}}]
            last = f"{prefix}.train"
            for i, post in enumerate(recipe.get("post") or []):
                job_id = f"{prefix}.post{i}"
                jobs.append({"id": job_id, "component": post["component"], "inputs": {"checkpoint": f"{last}.checkpoint"},
                             "config": post.get("config", {})})
                last = job_id
            jobs.append({"id": f"{prefix}.evaluate", "component": EVALUATE, "inputs": {"checkpoint": f"{last}.checkpoint"},
                         "config": {**(request.get("common_evaluate") or {}),
                                    "evaluation": {**evaluation, "cluster": str(seed)}}})
            out.append({"seed": seed, "cluster": str(seed), "jobs": jobs})
        return out

    baseline, candidate = dict(request.get("baseline") or {}), dict(request.get("candidate") or {})
    arms = [
        {"id": "baseline", "role": "baseline", "description": baseline.get("description", "reference recipe"),
         "runs": runs("baseline", baseline)},
        {"id": "candidate", "role": "candidate", "description": candidate.get("description", "candidate recipe"),
         "runs": runs("candidate", candidate)},
    ]
    return {
        "success_rule_id": request["success_rule_id"],
        "assessor": request.get("assessor"),
        "arms": arms,
        "analysis_unit": "pair",
        "cluster_by": "seed",
        "seed_policy": {"kind": "independent-training-seeds", "seeds": seeds, "shared_across_arms": True,
                        "stream_policy": "paired", "note": "each seed is one independent model; pairs within a seed are "
                                                           "clustered"},
        "splits": {"evaluation": evaluation, "tuning": None},
        "tuning": request.get("tuning", {}),
        "required_isolation": request.get("required_isolation"),
        "resource_reservation": {"matched_budget": request.get("budget") or {}, "arms": len(arms), "seeds": len(seeds)},
        "obligations": [],
        "alternatives_unresolved": _alternatives(request),
        "disclosures": {"claim_statement": (request.get("claim") or {}).get("statement"),
                        "budget_matching": "arms are planned with equal training budget",
                        "template": "eidos.templates.improves-under-budget"},
    }


# -- claim state ----------------------------------------------------------------------------------------------------
PROTOCOL_PASSED = "protocol-passed"
CANDIDATE_SUBMITTED = "candidate-submitted"
NOT_SUPPORTED = "not-supported"
ABSTAINED = "abstained"
NULL_REPORTED = "null-reported"
NO_CLAIM = "no-claim"
CLAIM_STATES = (PROTOCOL_PASSED, CANDIDATE_SUBMITTED, NOT_SUPPORTED, ABSTAINED, NULL_REPORTED, NO_CLAIM)
# What a model may ask for at conclusion. "confirmed" is honoured only with an assessor pass.
DISPOSITIONS = ("confirmed", "candidate", "null", "abstain")
CLAIM_STATE_SCHEMA = "conclusion-claim/1"
CONCLUSION_SCHEMA = "conclusion/2"
LABELS = {
    PROTOCOL_PASSED: "improvement confirmed by a conformant, healthy assessor pass, within the claim's scope",
    CANDIDATE_SUBMITTED: "candidate submitted for confirmation",
    NOT_SUPPORTED: "not supported: the pre-registered assessment failed",
    ABSTAINED: "the investigator abstained",
    NULL_REPORTED: "the investigator reported no improvement",
    NO_CLAIM: "no claim was made",
}
# eidos-answer/0.2 statuses a model already writes in scope.answer (arm 4), read when no disposition is given.
_LEGACY_STATUS = {"abstained": "abstain", "invalid": "abstain", "no_improvement_found": "null",
                  "improvement_claimed": "candidate", "inconclusive": "candidate"}


def validate_claim_block(block: Any) -> None:
    """Refuse a malformed ``control.claim`` before a decision is accepted."""
    if not isinstance(block, dict):
        raise Invalid("control.claim must be an object", details={"got": type(block).__name__})
    if block.get("disposition") not in (None, *DISPOSITIONS):
        raise Invalid(f"control.claim.disposition must be one of {DISPOSITIONS}", details={"got": block.get("disposition")})
    for key in ("point_estimate",):
        if block.get(key) is not None and not isinstance(block[key], dict):
            raise Invalid(f"control.claim.{key} must be an object", details={"got": type(block[key]).__name__})
    for key in ("evidence_refs", "obligations"):
        if block.get(key) is not None and not isinstance(block[key], list):
            raise Invalid(f"control.claim.{key} must be an array", details={"got": type(block[key]).__name__})


def lifecycle_state(outcome: str | None, conformance: str | None, health: str | None) -> str:
    """The state one assessment gives its claim. Only a conformant, healthy pass confirms; an intact failure does not
    support the claim; anything else leaves the candidate submitted, with the reason in its obligations."""
    if health in ("affected", "withdrawn"):
        return CANDIDATE_SUBMITTED
    if outcome == "protocol-passed" and conformance == "conformant":
        return PROTOCOL_PASSED
    if outcome == "protocol-failed":
        return NOT_SUPPORTED
    return CANDIDATE_SUBMITTED


def _scope_answer(control: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The conclusion's scope object and its eidos-answer block (empty when absent or malformed)."""
    scope = control["scope"] if isinstance(control.get("scope"), dict) else {}
    return scope, scope["answer"] if isinstance(scope.get("answer"), dict) else {}


def _requested(control: dict[str, Any], block: dict[str, Any]) -> str | None:
    if block.get("disposition"):
        return str(block["disposition"])
    scope, answer = _scope_answer(control)
    status = answer.get("status")
    if isinstance(status, str) and status in _LEGACY_STATUS:
        return _LEGACY_STATUS[status]
    if scope.get("claim") == "no_positive_claim":
        return "null"
    return None


def _number(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _bound(x: Any) -> float | None:
    """An interval bound for JSON: an unbounded side is recorded as null, never as infinity."""
    return _number(x)


def _effect_estimate(assessment: dict[str, Any], confidence: float | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    evaluation = assessment.get("evaluation") or {}
    effect = evaluation.get("effect")
    if not isinstance(effect, dict) or _number(effect.get("estimate")) is None:
        return None, None
    estimate = {"metric": evaluation.get("metric"), "value": _number(effect.get("estimate")),
                "direction": evaluation.get("direction"), "source": "assessor"}
    uncertainty = {"kind": "confidence-interval", "low": _bound(effect.get("ci_low")), "high": _bound(effect.get("ci_high")),
                   "confidence": confidence, "method": effect.get("method") or evaluation.get("interval"),
                   "source": "assessor"}
    return estimate, uncertainty


def _reported_estimate(control: dict[str, Any], block: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    """(point estimate, uncertainty) the investigator reported, marked as such; never used to set the state."""
    pe = block.get("point_estimate")
    if isinstance(pe, dict):
        value = _number(pe.get("value"))
        if value is None and _number(pe.get("gain")) is not None:
            value = _number(pe["gain"])
        if value is None and _number(pe.get("baseline")) is not None and _number(pe.get("candidate")) is not None:
            value = _number(pe["candidate"]) - _number(pe["baseline"])  # type: ignore[operator]
        estimate = {**pe, "value": value, "source": "investigator"}
        return estimate, block.get("uncertainty")
    scope, answer = _scope_answer(control)
    for m in answer.get("measurements") or []:
        if not isinstance(m, dict) or _number(m.get("baseline")) is None or _number(m.get("candidate")) is None:
            continue
        sign = -1 if m.get("direction") == "lower_is_better" else 1
        value = sign * (_number(m["candidate"]) - _number(m["baseline"]))  # type: ignore[operator]
        estimate = {"metric": m.get("metric"), "split": m.get("split"), "denominator": m.get("denominator"),
                    "baseline": m.get("baseline"), "candidate": m.get("candidate"), "value": value, "source": "investigator"}
        return estimate, m.get("uncertainty_method")
    return None, None


def _population(claim_scope: Any, charter: dict[str, Any] | None, block: dict[str, Any]) -> Any:
    """Authoritative population: the charter's, then the claim's scope, then what the investigator reported."""
    if charter and charter.get("population"):
        return charter["population"]
    if isinstance(claim_scope, dict) and claim_scope.get("population"):
        return claim_scope["population"]
    return block.get("population")


def derive_claim_state(*, control: dict[str, Any], claims: list[dict[str, Any]], assessments: list[dict[str, Any]],
                       plans: list[dict[str, Any]], charter: dict[str, Any] | None) -> dict[str, Any]:
    """The conclusion's claim state from recorded evidence.

    ``claims``: this investigation's claims, oldest first ({id, revision, statement, scope, created_at?}).
    ``assessments``: their assessment records ({id, claim_id, claim_revision, plan_id, outcome, conformance, health,
    evaluation, success_rule_ref, scope, remaining_alternatives, confidence, created_at}), oldest first.
    ``plans``: this investigation's evidence plans ({plan_id, status, obligations, claim_id, data_provenance}).

    The model may abstain or report a null. It cannot confirm: without a conformant, healthy pass on the claim's
    current revision, a requested confirmation becomes a submitted candidate (or "not supported" after a failure) and
    the request is recorded as downgraded.
    """
    block: dict[str, Any] = control["claim"] if isinstance(control.get("claim"), dict) else {}
    requested = _requested(control, block)
    by_id = {c["id"]: c for c in claims}
    designated = block.get("claim_id")
    target = by_id.get(str(designated)) if designated else None
    current = [a for a in assessments if target and a.get("claim_id") == target["id"]
               and a.get("claim_revision") == target["revision"]]
    if target is None and not designated:
        # Most recently assessed claim, then the most recently filed one.
        for a in reversed(assessments):
            c = by_id.get(str(a.get("claim_id")))
            if c and a.get("claim_revision") == c["revision"]:
                target = c
                break
        target = target or (claims[-1] if claims else None)
        current = [a for a in assessments if target and a.get("claim_id") == target["id"]
                   and a.get("claim_revision") == target["revision"]]
    considered = [{k: a.get(k) for k in ("id", "plan_id", "claim_id", "outcome", "conformance", "health")} for a in assessments]
    states = [lifecycle_state(a.get("outcome"), a.get("conformance"), a.get("health")) for a in current]
    # An intact failure on this revision outweighs a pass on the same claim: re-running until a pass is not evidence.
    def latest(wanted: str) -> dict[str, Any]:
        return next(a for a, st in reversed(list(zip(current, states, strict=True))) if st == wanted)

    chosen: dict[str, Any] | None
    if NOT_SUPPORTED in states:
        evidence, chosen = NOT_SUPPORTED, latest(NOT_SUPPORTED)
    elif PROTOCOL_PASSED in states:
        evidence, chosen = PROTOCOL_PASSED, latest(PROTOCOL_PASSED)
    else:
        evidence, chosen = CANDIDATE_SUBMITTED, current[-1] if current else None
    # A selection-only plan never confirms, whatever the numbers (it could not be frozen, but keep the rule explicit).
    selection_only = False
    plan_of = {p.get("plan_id"): p for p in plans}
    if evidence == PROTOCOL_PASSED and chosen is not None and ((plan_of.get(chosen.get("plan_id")) or {})
                                                               .get("data_provenance") or {}).get("split") == PROVENANCE_SELECTION:
        evidence, selection_only = CANDIDATE_SUBMITTED, True

    downgraded: dict[str, Any] = {}
    if requested == "abstain":
        state = ABSTAINED
    elif requested == "null":
        state = NOT_SUPPORTED if evidence == NOT_SUPPORTED else NULL_REPORTED
    elif evidence != CANDIDATE_SUBMITTED or current:
        state = evidence
    elif requested in ("candidate", "confirmed") or target is not None:
        state = CANDIDATE_SUBMITTED
    else:
        state = NO_CLAIM
    if requested == "confirmed" and state != PROTOCOL_PASSED:
        downgraded = {"downgraded_from": "confirmed",
                      "downgrade_reason": "a model cannot claim confirmation without a conformant, healthy assessor pass"}

    confidence = (((charter or {}).get("success_rules") or [{}])[0].get("decision_standard") or {}).get("confidence")
    if chosen is not None:
        estimate, uncertainty = _effect_estimate(chosen, confidence)
    else:
        estimate, uncertainty = None, None
    if estimate is None and state in (CANDIDATE_SUBMITTED, NULL_REPORTED, ABSTAINED):
        estimate, uncertainty = _reported_estimate(control, block)

    obligations: list[Any] = []
    if state == CANDIDATE_SUBMITTED:
        pending = [p for p in plans if (p.get("claim_id") == (target or {}).get("id") or not p.get("claim_id"))
                   and p.get("status") in ("draft", "frozen", "executing", "awaiting-assessment", "assessment-blocked")]
        if chosen is None:
            obligations.append("no conformant, healthy assessor pass on evaluator-owned held-out data")
            obligations.extend(f"evidence plan {p['plan_id']} is {p['status']}; its assessment has not been recorded"
                               for p in pending)
        else:
            health, conformance, outcome = chosen.get("health"), chosen.get("conformance"), chosen.get("outcome")
            if selection_only:
                obligations.append(f"assessment {chosen.get('id')} ran over selection data, which stays exploratory and "
                                   "cannot confirm the claim")
            elif health != "intact":
                obligations.append(f"assessment {chosen.get('id')} evidence health is {health}: reassess under a corrected release")
            elif outcome == "inconclusive":
                detail = (chosen.get("evaluation") or {}).get("reason")
                obligations.append(f"assessment {chosen.get('id')} was inconclusive ({conformance}"
                                   + (f": {detail}" if detail else "") + "); it did not confirm the claim")
            else:
                obligations.append(f"assessment {chosen.get('id')} did not pass conformantly (outcome {outcome}, "
                                   f"conformance {conformance})")
        for p in plans:
            if p.get("obligations") and (not target or p.get("claim_id") == target.get("id")):
                obligations.append(f"evidence plan {p.get('plan_id')} has unresolved obligations: {p.get('obligations')}")
        obligations.append("independent confirmation on evaluator-owned held-out data with fresh training seeds")
    obligations.extend(block.get("obligations") or [])
    obligations = list(dict.fromkeys(str(o) for o in obligations))

    claim_scope = (chosen or {}).get("scope") or (target or {}).get("scope")
    out: dict[str, Any] = {
        "schema": CLAIM_STATE_SCHEMA,
        "state": state,
        "confirmed": state == PROTOCOL_PASSED,
        "label": LABELS[state],
        "requested_disposition": requested,
        "claim_id": (target or {}).get("id"),
        "claim_revision": (target or {}).get("revision"),
        "statement": (target or {}).get("statement"),
        "assessment_id": (chosen or {}).get("id"),
        "plan_id": (chosen or {}).get("plan_id"),
        "success_rule_ref": (chosen or {}).get("success_rule_ref"),
        "scope": claim_scope,
        "point_estimate": estimate,
        "uncertainty": uncertainty,
        "population": _population(claim_scope, charter, block),
        "unfulfilled_obligations": obligations,
        "evidence_refs": [str(x) for x in block.get("evidence_refs") or []],
        "assessments_considered": considered,
        **downgraded,
    }
    if state == PROTOCOL_PASSED and chosen is not None:
        out["remaining_alternatives"] = chosen.get("remaining_alternatives") or []
    if designated and target is None:
        out["unfulfilled_obligations"] = [f"designated claim {designated} is not a claim of this investigation",
                                          *out["unfulfilled_obligations"]]
    return out
