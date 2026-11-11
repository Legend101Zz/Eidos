"""The assessment pipeline command and the claim state of a conclusion (ADR 0021).

``assess_candidate`` is one transaction from "locked candidate + baseline + charter rule" to a frozen, launched
evidence plan, or to a recorded refusal listing every reason. A refused request is committed (kind
``assessment_request``, status ``refused``), never raised, because raising would roll the record back; the caller gets
a receipt with ``status: refused`` and its reasons.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from eidos.application.handlers.evidence import (
    approval_for,
    draft_evidence_plan,
    file_claim,
    freeze_evidence_plan,
    launch_evidence_plan,
    typed_reference,
)
from eidos.application.service import Ctx, command
from eidos.domain import assessment as rules
from eidos.domain.errors import DomainError, Forbidden, Invalid

REQUEST_FIELDS = ("success_rule_id", "charter_id", "claim_id", "baseline", "candidate", "seeds", "labels", "predictions_slot",
                  "data_provenance", "required_isolation", "environment", "train_evaluate")


# A refusal for one of these leaves the candidate exploratory: it is still submitted for confirmation at the conclusion.
EXPLORATORY_REASONS = ("selection-data", "selection-exposed", "provenance-required", "held-out-unattested")


class _StepBlocked(Invalid):
    """A plan step was refused admission at launch; the whole pipeline rolls back and records the reason."""


def _record(ctx: Ctx, rid: str, p: dict, status: str, **fields: Any) -> dict:
    inv_id = p.get("investigation_id")
    body = {"investigation_id": inv_id, "status": status, "requested_by": ctx.principal.id,
            "request": {k: p.get(k) for k in REQUEST_FIELDS if p.get(k) is not None}, **fields}
    ctx.tx.put_record("assessment_request", rid, ctx.project_id, body, created_by=ctx.principal.id, status=status,
                      parent_id=inv_id)
    ctx.event(f"assessment_request.{status}", rid,
              {"plan_id": fields.get("plan_id"), "reasons": [r["code"] for r in fields.get("reasons") or []]},
              investigation_id=inv_id)
    return {"request_id": rid, "status": status, **fields}


def _refuse(ctx: Ctx, rid: str, p: dict, reasons: list[dict[str, Any]], **fields: Any) -> dict:
    exploratory = any(r["code"] in EXPLORATORY_REASONS for r in reasons)
    return _record(ctx, rid, p, "refused", reasons=reasons, exploratory=exploratory, **fields)


def _charter(ctx: Ctx, p: dict, inv: Any, reasons: list[dict[str, Any]]) -> Any:
    """The charter this request runs under: the investigation's pinned charter when it has one."""
    pinned = (inv.body.get("charter_ref") or {}) if inv else {}
    given = p.get("charter_id")
    if inv and not pinned:
        reasons.append(rules.reason("charter-required", f"investigation {inv.id} has no committed study charter"))
        return None
    if pinned and given and given != pinned["id"]:
        reasons.append(rules.reason("charter-mismatch", f"charter {given!r} is not this investigation's charter",
                                    charter_id=pinned["id"]))
        return None
    cid = given or pinned.get("id")
    if not cid:
        reasons.append(rules.reason("charter-required", "charter_id is required outside an investigation"))
        return None
    rec = ctx.tx.get_record("charter", cid, pinned.get("revision") if pinned else p.get("charter_revision"))
    if rec is None or rec.project_id != ctx.project_id:
        reasons.append(rules.reason("charter-not-found", f"charter {cid!r} was not found in this project"))
        return None
    return rec


def _claim_problem(ctx: Ctx, p: dict) -> dict[str, Any] | None:
    if p.get("claim_id"):
        rec = ctx.tx.get_record("claim", p["claim_id"])
        if rec is None or rec.project_id != ctx.project_id:
            return rules.reason("claim-not-found", f"claim {p['claim_id']!r} was not found in this project")
        if rec.body.get("kind") != "improves":
            return rules.reason("claim-kind", f"this pipeline assesses 'improves' claims, not {rec.body.get('kind')!r}")
        return None
    claim = p.get("claim")
    if not isinstance(claim, dict) or not claim.get("statement") or not isinstance(claim.get("scope"), dict) \
            or not claim["scope"]:
        return rules.reason("claim-required", "give claim_id, or claim {statement, scope} (a non-empty scope object)")
    if claim.get("kind", "improves") != "improves":
        return rules.reason("claim-kind", "this pipeline assesses 'improves' claims")
    return None


def _candidate_lock(ctx: Ctx, inv_id: str) -> dict[str, Any] | None:
    """The job named by the investigation's latest ``candidate_lock`` note; ``{}`` when no note names a job."""
    notes = [n for n in ctx.tx.list_records(ctx.project_id, "note", parent_id=inv_id, limit=100000)
             if n.body.get("kind") == "candidate_lock"]
    for note in sorted(notes, key=lambda n: n.created_at, reverse=True):
        try:
            jid = json.loads(note.body.get("text") or "")["job_id"]
        except (ValueError, KeyError, TypeError):
            continue
        job = ctx.tx.get_job(jid)
        if job and job["project_id"] == ctx.project_id:
            return job
    return None


def _recipe_jobs(ctx: Ctx, p: dict, seeds: list[str], slot: str, inv_id: str | None,
                 reasons: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The recorded jobs whose recipes the plan will re-run, ``{role: job}``, after checking each and that the requested
    seeds are new for that recipe. The jobs themselves are never evidence: their results are already visible."""
    locked = _candidate_lock(ctx, inv_id) if inv_id else None
    if inv_id and locked is None:
        reasons.append(rules.reason("candidate-not-locked", "no candidate_lock note names a job; record_note kind "
                                    "'candidate_lock', text {\"job_id\": \"<selected training job>\"}, before assessing"))
    recipes: dict[str, dict[str, Any]] = {}
    known = [j["lock"] for j in ctx.tx.list_jobs(ctx.project_id, limit=100000)]
    for role in rules.ROLES:
        job_id = rules.recipe_job_id(p.get(role))
        if job_id is None:
            reasons.append(rules.reason("observed-evidence", f"{role} must name the recorded job whose recipe the plan "
                                        "will re-run, as {\"job_id\": ...}. Already-observed jobs are never admissible as "
                                        "confirmation: seeds go in 'seeds', and fresh arms run after the plan is frozen"))
            continue
        job = ctx.tx.get_job(job_id)
        if not job or job["project_id"] != ctx.project_id:
            reasons.append(rules.reason("job-invalid", f"{role}: job {job_id!r} was not found"))
            continue
        if job["status"] != "succeeded":
            reasons.append(rules.reason("job-invalid", f"{role}: job {job_id} is {job['status']}, not succeeded"))
            continue
        problem = rules.fresh_initialization_problem(job["lock"])
        if problem:
            reasons.append(rules.reason("not-fresh-initialization", f"{role}: the recipe of job {job_id} is not a fresh "
                                        f"initialization: {problem}"))
        if role == "candidate" and locked is not None \
                and rules.recipe_identity(job["lock"]) != rules.recipe_identity(locked["lock"]):
            reasons.append(rules.reason("candidate-not-locked", f"job {job_id} is not a run of the locked candidate "
                                        f"recipe (job {locked['id']})"))
        pub = next((x for x in ctx.tx.publications(job["id"]) if x["slot"] == slot), None)
        if pub is None:
            reasons.append(rules.reason("output-schema", f"{role}: job {job_id} published no {slot!r} output",
                                        published=[x["slot"] for x in ctx.tx.publications(job["id"])]))
        elif pub.get("schema") not in rules.PREDICTION_SCHEMAS:
            reasons.append(rules.reason("output-schema", f"{role}: output {slot!r} has schema {pub.get('schema')!r}; the "
                                        f"classification adapter reads {list(rules.PREDICTION_SCHEMAS)}"))
        repeated = rules.seeds_already_run(seeds, rules.recipe_identity(job["lock"]), known)
        if repeated:
            reasons.append(rules.reason("seed-already-observed", f"{role}: this recipe already ran with seeds {repeated}, "
                                        "so their results were visible before this plan; choose seeds it has not run",
                                        seeds=repeated))
        recipes[role] = job
    return recipes


def _release(ctx: Ctx, ref: str) -> Any:
    rec = ctx.tx.get_record("component_release", ref)
    return rec if rec is not None and rec.status == "active" else None


def _active_named(ctx: Ctx, name: str) -> list[Any]:
    return [r for r in ctx.tx.list_records(ctx.project_id, "component_release", status="active", limit=100000)
            if r.body.get("manifest", {}).get("id") == name]


def _check_protocols(ctx: Ctx, reasons: list[dict[str, Any]]) -> None:
    for ref, what in ((rules.TEMPLATE_REF, "template"), (rules.ASSESSOR_REF, "assessor")):
        if approval_for(ctx, ref, "improves") is None:
            reasons.append(rules.reason("protocol-not-approved", f"the {what} {ref} has no active approval for "
                                        "'improves' claims; qualify and promote it first", release=ref))


def _classification(ctx: Ctx, p: dict, charter: Any, inv_id: str | None, reasons: list[dict[str, Any]]) -> dict | None:
    """Validate the classification path. Returns what the expansion needs, or None after adding reasons."""
    slot = str(p.get("predictions_slot") or "predictions")
    seeds, problems = rules.check_confirmation_seeds(p.get("seeds"))
    reasons.extend(problems)
    recipes = _recipe_jobs(ctx, p, seeds, slot, inv_id, reasons)
    report = _release(ctx, rules.REPORT_REF)
    if report is None:
        reasons.append(rules.reason("output-schema", f"{rules.REPORT_REF} is not an active release here; install the "
                                    "built-in components", release=rules.REPORT_REF))
    elif (report.body["manifest"].get("output") or {}).get("predictions") != "classification-pair-rows/1":
        reasons.append(rules.reason("output-schema", f"{rules.REPORT_REF} does not output 'predictions' rows the "
                                    "assessor reads"))
    labels = ctx.tx.get_artifact(str(p.get("labels") or ""))
    if labels is None or labels["project_id"] != ctx.project_id:
        reasons.append(rules.reason("output-schema", f"labels artifact {p.get('labels')!r} was not found"))
        labels = None
    elif labels.get("schema") not in rules.LABEL_SCHEMAS:
        reasons.append(rules.reason("output-schema", f"labels artifact has schema {labels.get('schema')!r}; expected "
                                    f"one of {list(rules.LABEL_SCHEMAS)}"))
    provenance, problems = rules.check_provenance(p.get("data_provenance"), charter=charter.body,
                                                  labels_digest=labels["digest"] if labels else None)
    reasons.extend(problems)
    if labels is not None and inv_id and not any(r["code"] == "selection-data" for r in reasons):
        used = [j["id"] for j in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv_id, limit=100000)
                if j.get("purpose") != "confirmation"
                and any((i or {}).get("digest") == labels["digest"] for i in (j["lock"].get("inputs") or {}).values())]
        if used:
            reasons.append(rules.reason("selection-exposed", "these labels were an input to exploratory jobs of this "
                                        "investigation, so they are selection data and cannot confirm", jobs=used[:10]))
    if reasons:
        return None
    return {"labels": labels, "seeds": seeds, "slot": slot, "recipes": recipes, "provenance": provenance}


def _pin_release(ctx: Ctx, name: str) -> str | None:
    """The newest active release of a component the plan names without a version."""
    released = _active_named(ctx, name)
    return max(released, key=lambda r: r.created_at).id if released else None


def _check_locked_recipe(ctx: Ctx, spec: dict, inv_id: str, train_ref: str | None, reasons: list[dict[str, Any]]) -> None:
    """The submitted train/evaluate candidate must be the recipe the investigation locked."""
    locked = _candidate_lock(ctx, inv_id)
    if locked is None:
        reasons.append(rules.reason("candidate-not-locked", "no candidate_lock note names a job; record_note kind "
                                    "'candidate_lock', text {\"job_id\": \"<selected training job>\"}, before assessing"))
        return
    candidate = spec.get("candidate")
    if isinstance(candidate, dict) and candidate.get("post"):
        reasons.append(rules.reason("candidate-not-locked", "a candidate_lock names one training job, so a candidate "
                                    "with post steps cannot be compared with it; assess the locked training recipe"))
    recorded = rules.recipe_identity(locked["lock"])
    submitted = rules.train_recipe_identity(spec, "candidate", train_ref)
    if submitted != recorded:
        reasons.append(rules.reason("candidate-not-locked", f"train_evaluate.candidate is not the locked candidate recipe "
                                    f"(job {locked['id']})", submitted=submitted, recorded=recorded))


def _train_evaluate(ctx: Ctx, p: dict, charter: Any, inv_id: str | None, reasons: list[dict[str, Any]]) -> dict | None:
    spec = p.get("train_evaluate")
    if not isinstance(spec, dict):
        reasons.append(rules.reason("request-invalid", "this rule's metric uses the train/evaluate path: give "
                                    "train_evaluate {seeds, baseline, candidate, evaluation, common_train?, common_evaluate?}"))
        return None
    seeds = spec.get("seeds")
    if not (isinstance(seeds, list) and seeds and all(type(s) is int for s in seeds) and len(set(seeds)) == len(seeds)):
        reasons.append(rules.reason("seed-pairing", "train_evaluate.seeds must be a non-empty list of distinct integers "
                                    "(both arms use the same seeds)"))
    if spec.get("parent"):
        reasons.append(rules.reason("not-fresh-initialization", "train_evaluate.parent is not supported: this path "
                                    "requires fresh initializations; use draft_evidence_plan for branch comparisons"))
    if not isinstance(spec.get("candidate"), dict) or not spec.get("candidate"):
        reasons.append(rules.reason("request-invalid", "train_evaluate.candidate (the recipe under test) is required"))
    evaluation = spec.get("evaluation")
    split = evaluation.get("split") if isinstance(evaluation, dict) else None
    if not split:
        reasons.append(rules.reason("output-schema", "train_evaluate.evaluation.split is required"))
    for name in (rules.TRAIN, rules.EVALUATE):
        released = _active_named(ctx, name)
        if not released:
            reasons.append(rules.reason("output-schema", f"no active release of {name} is installed"))
        elif name == rules.EVALUATE and not any("predictions" in (r.body["manifest"].get("output") or {}) for r in released):
            reasons.append(rules.reason("output-schema", f"{name} publishes no 'predictions' output for the assessor"))
    provenance, problems = rules.check_provenance(p.get("data_provenance"), charter=charter.body,
                                                  split=str(split) if split else None)
    reasons.extend(problems)
    train_ref = _pin_release(ctx, rules.TRAIN)
    if inv_id:
        _check_locked_recipe(ctx, spec, inv_id, train_ref, reasons)
    if isinstance(seeds, list) and train_ref:
        known = [j["lock"] for j in ctx.tx.list_jobs(ctx.project_id, limit=100000)]
        for role in rules.ROLES:
            identity = rules.train_recipe_identity(spec, role, train_ref)
            repeated = rules.seeds_already_run([str(s) for s in seeds], identity, known)
            if repeated:
                reasons.append(rules.reason("seed-already-observed", f"{role}: this recipe already ran with seeds "
                                            f"{repeated}, so their results were visible before this plan; choose seeds "
                                            "it has not run", seeds=repeated))
    if reasons:
        return None
    return {"provenance": provenance, "pinned": {rules.TRAIN: train_ref, rules.EVALUATE: _pin_release(ctx, rules.EVALUATE)}}


def _draft(ctx: Ctx, p: dict, path: str, checked: dict, rid: str, env: str | None) -> dict[str, Any]:
    base = {"claim": p.get("claim") or {"statement": (ctx.tx.require("claim", p["claim_id"]).body or {}).get("statement")},
            "success_rule_id": p["success_rule_id"], "assessor": {"ref": rules.ASSESSOR_REF},
            "required_isolation": p["required_isolation"], "tuning": p.get("tuning", {}),
            "budget": p.get("budget") or {}, "alternatives_unresolved": p.get("alternatives_unresolved")}
    if path == rules.PATH_CLASSIFICATION:
        draft = rules.fresh_classification_draft({
            **base, "baseline": p.get("baseline_recipe"), "candidate": p.get("candidate_recipe"),
            "classification": {
                "labels": typed_reference(ctx, checked["labels"]["id"], "class-labels/1"), "seeds": checked["seeds"],
                "slot": checked["slot"], "environment": env,
                "recipes": {role: job["lock"] for role, job in checked["recipes"].items()},
                **{k: p[k] for k in ("classes", "id_field") if k in p}}})
    else:
        spec = {k: v for k, v in copy.deepcopy(p["train_evaluate"]).items() if k in rules.TRAIN_EVALUATE_FIELDS}
        draft = rules.train_evaluate_draft({**base, **spec})
        for arm in draft["arms"]:
            for run in arm["runs"]:
                for job in run["jobs"]:
                    job["component"] = checked["pinned"].get(job["component"], job["component"])
                    if env:
                        job["environment"] = env
    draft["data_provenance"] = checked["provenance"]
    draft["disclosures"]["data_provenance"] = checked["provenance"]
    draft["disclosures"]["pipeline"] = {"request_id": rid, "path": path}
    draft["disclosures"]["expanded_by"] = ("assess_candidate: fresh arms run after the freeze (ADR 0021)"
                                           if path == rules.PATH_CLASSIFICATION
                                           else "assess_candidate (equals the template's own expansion; ADR 0021)")
    return draft


@command("assess_candidate", "propose")
def assess_candidate(ctx: Ctx, p: dict) -> dict:
    """Default pipeline: from a locked candidate, a baseline and a charter rule to a frozen, launched evidence plan.

    The rule's metric selects the path: ordinary accuracy uses eidos.task.paired-classification-report and
    eidos.assessors.paired-ci@1.2.0, the paired-report metrics use the train/evaluate path. On both, the candidate,
    baseline, seeds, data and rule are frozen first and fresh arms run after, so no confirming result exists when the
    plan is frozen: recorded jobs only name recipes. Output schemas, seeds, fresh initialization, the charter id and
    data provenance are validated first; selection data stays exploratory. Every failure is returned as a recorded
    refusal with its reasons."""
    if not ctx.principal.has("spend_compute"):
        raise Forbidden("assess_candidate launches a plan and needs the spend_compute grant")
    rid = p.get("request_id") or f"assessreq_{uuid.uuid4().hex[:12]}"
    reasons: list[dict[str, Any]] = []
    inv_id = p.get("investigation_id")
    inv = ctx.tx.require("investigation", inv_id) if inv_id else None
    if not p.get("success_rule_id"):
        return _refuse(ctx, rid, p, [rules.reason("rule-not-found", "success_rule_id is required")])
    charter = _charter(ctx, p, inv, reasons)
    if charter is None:
        return _refuse(ctx, rid, p, reasons)
    _rule, path, problems = rules.metric_path(charter.body, str(p["success_rule_id"]))
    if problems or path is None:
        return _refuse(ctx, rid, p, problems)
    claim_problem = _claim_problem(ctx, p)
    if claim_problem:
        reasons.append(claim_problem)
    if not p.get("required_isolation"):
        reasons.append(rules.reason("isolation-required", "required_isolation is required, e.g. {\"profile\": "
                                    "\"dev:same-user\", \"controls\": [], \"waiver\": \"<reason>\"} or {\"sealed\": true}"))
    _check_protocols(ctx, reasons)
    checked = (_classification(ctx, p, charter, inv_id, reasons) if path == rules.PATH_CLASSIFICATION
               else _train_evaluate(ctx, p, charter, inv_id, reasons))
    if reasons or checked is None:
        return _refuse(ctx, rid, p, reasons, path=path)
    n_events, expected = len(ctx.emitted), ctx.expected_revision
    ctx.expected_revision = None
    env = p.get("environment") or (inv.body.get("environment") if inv else None)
    try:
        with ctx.tx.savepoint():
            claim_id = p.get("claim_id")
            if not claim_id:
                claim = p["claim"]
                claim_id = file_claim(ctx, {**{k: claim.get(k) for k in ("statement", "scope", "assumptions", "alternatives",
                                                                       "supporting", "contradicting")},
                                            "kind": "improves", "investigation_id": inv_id})["claim_id"]
            draft = _draft(ctx, p, path, checked, rid, env)
            plan = draft_evidence_plan(ctx, {"claim_id": claim_id, "charter_id": charter.id, "charter_revision": charter.revision,
                                             "template_ref": rules.TEMPLATE_REF, "draft": draft, "investigation_id": inv_id})
            if plan["obligations"]:
                raise Invalid("plan has unresolved obligations", details={"obligations": plan["obligations"]})
            frozen = freeze_evidence_plan(ctx, {"plan_id": plan["plan_id"]})
            launched = launch_evidence_plan(ctx, {"plan_id": plan["plan_id"]})
            blocked = ctx.tx.require("evidence_plan", plan["plan_id"]).body["launch"]["blocked"]
            if blocked:
                raise _StepBlocked("a plan step was refused admission at launch", details={"blocked": blocked})
    except DomainError as exc:
        del ctx.emitted[n_events:]
        ctx.expected_revision = expected
        code = "step-blocked" if isinstance(exc, _StepBlocked) else str((exc.details or {}).get("reason_code") or "plan-refused")
        return _refuse(ctx, rid, p, [rules.reason(code, f"{exc.code}: {exc.message}"[:500], **(exc.details or {}))], path=path)
    ctx.expected_revision = expected
    return _record(ctx, rid, p, "launched", path=path, claim_id=claim_id, plan_id=plan["plan_id"],
                   plan_digest=frozen["plan_digest"], jobs=launched["jobs"], pending_steps=launched["pending_steps"],
                   template=rules.TEMPLATE_REF, assessor=rules.ASSESSOR_REF, data_provenance=checked["provenance"])


# -- claim state of a conclusion ---------------------------------------------------------------------------------
def _assessment_view(a: Any) -> dict[str, Any]:
    ref = a.body.get("claim_ref") or {}
    return {"id": a.id, "claim_id": ref.get("id"), "claim_revision": ref.get("revision"), "plan_id": a.body.get("plan_id"),
            "outcome": a.body.get("outcome"), "conformance": a.body.get("conformance"),
            "health": (a.labels or {}).get("health", "intact"), "evaluation": a.body.get("evaluation") or {},
            "success_rule_ref": a.body.get("success_rule_ref"), "scope": a.body.get("scope"),
            "remaining_alternatives": a.body.get("remaining_alternatives") or []}


def conclusion_claim_state(ctx: Ctx, inv: Any, control: dict[str, Any]) -> dict[str, Any]:
    """Build the claim state of a conclusion from the investigation's recorded claims, plans and assessments."""
    records = sorted((c for c in ctx.tx.list_records(ctx.project_id, "claim", limit=100000)
                      if c.body.get("investigation_id") == inv.id), key=lambda c: c.created_at)
    claims = [{"id": c.id, "revision": c.revision, "statement": c.body.get("statement"), "scope": c.body.get("scope")}
              for c in records]
    claim_ids = {c["id"] for c in claims}
    plans = [{"plan_id": pl.id, "status": pl.status, "obligations": pl.body.get("obligations") or [],
              "claim_id": (pl.body.get("claim_ref") or {}).get("id"), "data_provenance": pl.body.get("data_provenance"),
              "confirmation": pl.body.get("confirmation")}
             for pl in ctx.tx.list_records(ctx.project_id, "evidence_plan", limit=100000)
             if pl.body.get("investigation_id") == inv.id]
    plan_ids = {p["plan_id"] for p in plans}
    assessments = sorted((a for a in ctx.tx.list_records(ctx.project_id, "assessment", limit=100000)
                          if (a.body.get("claim_ref") or {}).get("id") in claim_ids or a.body.get("plan_id") in plan_ids),
                         key=lambda a: a.created_at)
    ref = inv.body.get("charter_ref") or {}
    charter = ctx.tx.get_record("charter", ref["id"], ref.get("revision")) if ref.get("id") else None
    return rules.derive_claim_state(control=control, claims=claims, assessments=[_assessment_view(a) for a in assessments],
                                    plans=plans, charter=charter.body if charter else None)
