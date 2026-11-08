"""Claims, evidence plans, qualification/promotion and assessment submission."""

from __future__ import annotations

import json
import uuid

from eidos.application.handlers.investigations import confirm_scope, inv_scope
from eidos.application.handlers.runs import admit_job, resolve_lock, strongest_profile
from eidos.application.service import Ctx, command
from eidos.domain import evidence as ev
from eidos.domain import isolation, qualification
from eidos.domain.errors import Forbidden, Invalid, NotFound, QualificationMissing
from eidos_wire import canonical_json, sha256_bytes


@command("file_claim", "propose")
def file_claim(ctx: Ctx, p: dict) -> dict:
    body = ev.validate_claim({k: p.get(k) for k in ("statement", "kind", "scope", "assumptions", "alternatives",
                                                     "supporting", "contradicting", "fork_point")})
    body["author"] = ctx.principal.id
    body["investigation_id"] = p.get("investigation_id")
    cid = p.get("claim_id") or f"claim_{uuid.uuid4().hex[:12]}"
    existing = ctx.tx.get_record("claim", cid)
    if existing and p.get("supersedes_revision") != existing.revision:
        raise Invalid("revising a claim requires supersedes_revision equal to its current revision",
                      details={"observed_revision": existing.revision})
    if existing:
        body["superseded_revision"] = existing.revision
    rec = ctx.tx.put_record("claim", cid, ctx.project_id, body, created_by=ctx.principal.id, status="proposed")
    ctx.event("claim.filed", cid, {"revision": rec.revision, "kind": body["kind"]}, investigation_id=p.get("investigation_id"))
    return {"claim_id": cid, "revision": rec.revision}


def approval_for(ctx: Ctx, release_ref: str, scope: str) -> dict | None:
    for ap in ctx.tx.list_records(ctx.project_id, "approval", status="active"):
        if ap.body["release_ref"] == release_ref and scope in ap.body["scope"]:
            rel = ctx.tx.get_record("component_release", release_ref)
            if rel and rel.status == "active" and rel.body["digest"] == ap.body["release_digest"]:
                return {"approval_id": ap.id, **ap.body}
    return None


@command("draft_evidence_plan", "propose")
def draft_evidence_plan(ctx: Ctx, p: dict) -> dict:
    """Compile a planner's draft against the pinned charter. Success rules are copied from the
    charter; any threshold in the draft or template is rejected."""
    claim = ctx.tx.require("claim", p["claim_id"], p.get("claim_revision"))
    ch = ctx.tx.require("charter", p["charter_id"], p.get("charter_revision"))
    tmpl = ctx.tx.require("component_release", p["template_ref"])
    if tmpl.body["manifest"]["kind"] != "planning":
        raise Invalid(f"{tmpl.id} is not a planning (evidence-plan) component")
    draft = p.get("draft")
    if p.get("draft_artifact_id"):
        art = ctx.tx.get_artifact(p["draft_artifact_id"])
        if not art or not ctx.store.verify(art["digest"], art["size"]):
            raise NotFound("draft artifact not found or unverifiable")
        import json
        with open(ctx.store.path_for(art["digest"])) as fh:
            draft = json.load(fh)
    if not isinstance(draft, dict):
        raise Invalid("a draft (inline or draft_artifact_id) is required")
    plan = ev.compile_plan(draft=draft, claim=claim.body, claim_ref={"id": claim.id, "revision": claim.revision},
                           charter=ch.body, charter_ref={"id": ch.id, "revision": ch.revision, "digest": ch.digest},
                           template_ref={"ref": tmpl.id, "digest": tmpl.body["digest"]},
                           template_defaults=tmpl.body["manifest"].get("defaults"))
    plan["investigation_id"] = p.get("investigation_id")
    pid = p.get("plan_id") or f"plan_{uuid.uuid4().hex[:12]}"
    rec = ctx.tx.put_record("evidence_plan", pid, ctx.project_id, plan, created_by=ctx.principal.id, status="draft",
                            parent_id=claim.id)
    ctx.event("evidence_plan.drafted", pid, {"claim": claim.id, "obligations": plan["obligations"]},
              investigation_id=p.get("investigation_id"))
    return {"plan_id": pid, "revision": rec.revision, "obligations": plan["obligations"],
            "success_rule_ref": plan["success_rule_ref"]}


@command("resolve_plan_obligations", "propose")
def resolve_plan_obligations(ctx: Ctx, p: dict) -> dict:
    """Fill logistics (seed policy, analysis unit, assessor, arms) of a draft; thresholds stay fixed."""
    from eidos.domain.charter import reject_threshold_overrides
    plan = ctx.tx.require("evidence_plan", p["plan_id"], lock=True)
    if plan.status != "draft":
        raise Invalid(f"plan is {plan.status}")
    updates = p.get("updates") or {}
    reject_threshold_overrides("plan update", updates)
    allowed = {"seed_policy", "analysis_unit", "cluster_by", "assessor", "arms", "splits", "tuning",
               "required_isolation", "resource_reservation", "disclosures", "alternatives_unresolved"}
    bad = set(updates) - allowed
    if bad:
        raise Invalid(f"cannot update {sorted(bad)} on a plan")
    body = {**plan.body, **updates}
    body["obligations"] = [o for o in body["obligations"] if o not in updates and o not in p.get("resolved", [])]
    rec = ctx.tx.put_record("evidence_plan", plan.id, ctx.project_id, body, created_by=ctx.principal.id, status="draft",
                            expected_revision=ctx.expected_revision)
    return {"plan_id": plan.id, "revision": rec.revision, "obligations": body["obligations"]}


@command("freeze_evidence_plan", "propose")
def freeze_evidence_plan(ctx: Ctx, p: dict) -> dict:
    """Freeze a qualified plan. Requires approved (planted/null qualified) template and assessor
    releases; an investigator may freeze only when project policy allows automatic freezing."""
    if not ctx.principal.has("freeze_plan"):
        from eidos.application.handlers.projects import get_profile
        policy = get_profile(ctx).body.get("policy", {})
        if not (ctx.principal.has("freeze_plan:policy") and policy.get("auto_freeze_approved_templates")):
            raise Forbidden("freezing this plan requires the freeze_plan grant or an auto-freeze project policy")
    plan = ctx.tx.require("evidence_plan", p["plan_id"], lock=True)
    if plan.status != "draft":
        raise Invalid(f"plan is {plan.status}")
    if ctx.expected_revision is not None and plan.revision != ctx.expected_revision:
        from eidos.domain.errors import StaleRevision
        raise StaleRevision("plan revision changed", details={"observed_revision": plan.revision})
    kind = plan.body["claim_kind"]
    t_ok = approval_for(ctx, plan.body["template"]["ref"], kind) is not None
    a_ref = (plan.body.get("assessor") or {}).get("ref")
    a_ok = bool(a_ref) and approval_for(ctx, a_ref, kind) is not None
    ev.check_freezable(plan.body, template_approved=t_ok, assessor_approved=a_ok)
    _refuse_unsupported_sealed(ctx, plan.body)
    if not plan.body["arms"]:
        raise Invalid("a plan needs arms before freezing")
    body = dict(plan.body)
    body["assessor"] = {**body["assessor"], "digest": ctx.tx.require("component_release", a_ref).body["digest"]}
    body["frozen_by"] = ctx.principal.id
    rec = ctx.tx.put_record("evidence_plan", plan.id, ctx.project_id, body, created_by=ctx.principal.id, status="frozen")
    ctx.event("evidence_plan.frozen", plan.id, {"digest": rec.digest}, investigation_id=body.get("investigation_id"))
    return {"plan_id": plan.id, "revision": rec.revision, "plan_digest": rec.digest}


def _refuse_unsupported_sealed(ctx: Ctx, body: dict) -> None:
    """A plan requiring sealed (hostile-code) execution is refused on a lab whose strongest profile
    runs candidate code as the same OS user (macOS Seatbelt, or no sealed profile at all)."""
    strongest = strongest_profile(ctx)
    msg = isolation.sealed_refusal(body.get("required_isolation"), strongest)
    if msg:
        raise Invalid(msg, details={"strongest_available": strongest, **isolation.profile_traits(strongest),
                                    "required_isolation": body.get("required_isolation"),
                                    "supported_sealed_profiles": list(isolation.HOSTILE_CODE_PROFILES[:1])})


def _pipeline_steps(arm: dict) -> list[dict]:
    """Normalize an arm into executable steps. ``run`` = one step; ``runs[].jobs[]`` = per-seed
    pipelines whose inputs may reference earlier steps as ``"<step-id>.<slot>"``."""
    if arm.get("run"):
        return [{"key": f"{arm['id']}/run", "id": "run", "spec": arm["run"], "cluster": None}]
    steps = []
    for r in arm.get("runs") or []:
        for j in r.get("jobs") or []:
            steps.append({"key": f"{arm['id']}/{j['id']}", "id": j["id"], "spec": j, "cluster": r.get("cluster")})
    if not steps:
        raise Invalid(f"arm {arm['id']} has no run specification")
    return steps


def arm_jobs(plan_body: dict) -> dict[str, list[str]]:
    jobs = (plan_body.get("launch") or {}).get("jobs") or {}
    return {k: (v if isinstance(v, list) else [v]) for k, v in jobs.items()}


def _refs(step: dict, arm_steps: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """Map input ports to (step key, slot) for inputs that reference earlier pipeline steps."""
    out = {}
    for port, val in (step["spec"].get("inputs") or {}).items():
        if not isinstance(val, str):
            continue
        for sid in sorted(arm_steps, key=len, reverse=True):
            if val.startswith(sid + "."):
                out[port] = (arm_steps[sid]["key"], val[len(sid) + 1:])
                break
    return out


def _admit_step(ctx: Ctx, plan, arm: dict, step: dict, inputs: dict, scope, inv_id) -> str:
    spec = step["spec"]
    req = plan.body.get("required_isolation") or {}
    run = {"component_ref": spec.get("component_ref") or spec.get("component"), "inputs": inputs,
           "config": spec.get("config") or {}, "environment": spec.get("environment"), "seed": spec.get("seed"),
           "isolation_profile": req.get("profile", spec.get("isolation_profile")),
           "isolation_waiver": req.get("waiver")}
    lock = resolve_lock(ctx, run)
    lock.update({"plan_id": plan.id, "plan_digest": plan.digest, "arm": arm["id"], "arm_role": arm["role"],
                 "step": step["id"], "cluster": step.get("cluster"), "required_isolation": req})
    return admit_job(ctx, kind="evidence-arm", lock=lock, investigation_id=inv_id, purpose="confirmation",
                     wall_seconds=int(spec.get("wall_seconds", 900)), charter_check=False, scope=scope)


def _advance(ctx: Ctx, plan, launch: dict) -> dict:
    """Admit every pending step whose referenced steps succeeded; block those whose did not."""
    inv_id = plan.body.get("investigation_id")
    scope = launch.get("scope")
    progressed = True
    while progressed:
        progressed = False
        for arm in plan.body["arms"]:
            steps = _pipeline_steps(arm)
            by_id = {s["id"]: s for s in steps}
            for st in steps:
                if st["key"] in launch["steps"] or st["key"] in launch["blocked"]:
                    continue
                refs = _refs(st, by_id)
                ready, blocked, inputs = True, None, dict(st["spec"].get("inputs") or {})
                for port, (dep_key, slot) in refs.items():
                    if dep_key in launch["blocked"]:
                        blocked = f"depends on blocked step {dep_key}"
                        break
                    jid = launch["steps"].get(dep_key)
                    job = ctx.tx.get_job(jid) if jid else None
                    if not job or job["status"] not in ("succeeded", "failed", "cancelled", "pruned"):
                        ready = False
                        continue
                    if job["status"] != "succeeded":
                        blocked = f"predecessor {dep_key} ended {job['status']}"
                        break
                    pub = next((x for x in ctx.tx.publications(jid) if x["slot"] == slot), None)
                    if pub is None:
                        blocked = f"predecessor {dep_key} published no '{slot}'"
                        break
                    inputs[port] = pub["artifact_id"]
                if blocked:
                    launch["blocked"][st["key"]] = blocked
                    progressed = True
                elif ready:
                    jid = _admit_step(ctx, plan, arm, st, inputs, scope, inv_id)
                    launch["steps"][st["key"]] = jid
                    launch["jobs"].setdefault(arm["id"], []).append(jid)
                    progressed = True
    return launch


@command("launch_evidence_plan", "spend_compute")
def launch_evidence_plan(ctx: Ctx, p: dict) -> dict:
    plan = ctx.tx.require("evidence_plan", p["plan_id"], lock=True)
    if plan.status != "frozen":
        raise Invalid(f"only a frozen plan can be launched (plan is {plan.status})")
    _refuse_unsupported_sealed(ctx, plan.body)  # e.g. a plan frozen on Linux, lab restored on macOS
    inv_id = plan.body.get("investigation_id")
    scope = None
    if inv_id:
        scope = confirm_scope(inv_id) if ctx.tx.budgets(confirm_scope(inv_id)) else inv_scope(inv_id)
    for arm in plan.body["arms"]:
        _pipeline_steps(arm)  # validate shape before admitting anything
    launch = _advance(ctx, plan, {"jobs": {}, "steps": {}, "blocked": {}, "scope": scope, "by": ctx.principal.id})
    body = {**plan.body, "launch": launch}
    rec = ctx.tx.put_record("evidence_plan", plan.id, ctx.project_id, body, created_by=ctx.principal.id, status="executing")
    return {"plan_id": plan.id, "revision": rec.revision, "jobs": launch["jobs"], "pending_steps":
            sum(len(_pipeline_steps(a)) for a in plan.body["arms"]) - len(launch["steps"]) - len(launch["blocked"])}


def maybe_request_assessment(ctx: Ctx, plan_id: str) -> None:
    """Called when an evidence-arm job ends: advance pipelines; request assessment when done."""
    plan = ctx.tx.get_record("evidence_plan", plan_id, lock=True)
    if not plan or plan.status != "executing":
        return
    launch = plan.body["launch"]
    if "steps" in launch:
        launch = _advance(ctx, plan, json.loads(json.dumps(launch)))
        if launch != plan.body["launch"]:
            plan = ctx.tx.put_record("evidence_plan", plan.id, ctx.project_id, {**plan.body, "launch": launch},
                                     created_by=ctx.principal.id, status="executing")
        total = sum(len(_pipeline_steps(a)) for a in plan.body["arms"])
        if len(launch["steps"]) + len(launch["blocked"]) < total:
            return
    jobs = [ctx.tx.get_job(j) for js in arm_jobs(plan.body).values() for j in js]
    if all(j["status"] in ("succeeded", "failed", "cancelled", "pruned") for j in jobs):
        ctx.tx.set_status("evidence_plan", plan_id, "awaiting-assessment")
        ctx.tx.enqueue("assessment.request", f"assess:{plan_id}:{plan.revision}",
                       {"plan_id": plan_id, "project_id": ctx.project_id})


def _realized(ctx: Ctx, plan) -> dict:
    arms, iso_findings, iso_ok = {}, [], True
    req = plan.body.get("required_isolation") or {}
    sealed_needed = bool(req.get("sealed") or str(req.get("profile", "")).endswith("sealed") or req.get("controls"))
    # Trusted = holds the launcher grant and, for sealed requirements, the sealed:read grant too.
    trusted = {pr["id"] for pr in [ctx.tx.get_principal(r.body.get("launcher", "")) or {} for r in
                                   ctx.tx.list_records(ctx.project_id, "isolation_receipt")]
               if pr and "launcher" in (pr.get("scopes") or [])
               and (not sealed_needed or "sealed:read" in (pr.get("scopes") or []))}
    blocked = (plan.body["launch"].get("blocked") or {})
    waivers: list[dict] = []
    for arm_id, jids in arm_jobs(plan.body).items():
        statuses, receipt = [], {}
        for jid in jids:
            job = ctx.tx.get_job(jid)
            statuses.append(job["status"] if job else "missing")
            receipt = (job.get("terminal") or {}).get("receipt", {}) if job else {}
            att_receipt = ctx.tx.get_record("isolation_receipt", job["current_attempt"]) if job and job["current_attempt"] else None
            verdict = isolation.evaluate_receipt(req, att_receipt.body if att_receipt else None, trusted_launchers=trusted)
            if job and att_receipt and att_receipt.body.get("profile") != job["lock"].get("isolation_profile"):
                verdict = {"ok": False, "findings": verdict["findings"] + [{"finding": "receipt profile differs from the "
                           "job's locked profile", "job_profile": job["lock"].get("isolation_profile")}]}
            if not verdict["ok"]:
                iso_ok = False
                iso_findings.extend({**f, "arm": arm_id, "job": jid} for f in verdict["findings"])
        arm_blocked = [k for k in blocked if k.startswith(arm_id + "/")]
        ok = statuses and all(st == "succeeded" for st in statuses) and not arm_blocked
        choices = [ctx.tx.get_job(j)["lock"].get("isolation_choice") for j in jids]
        waived = [c for c in choices if c and c.get("waiver")]
        if waived:
            waivers.append({"arm": arm_id, "waivers": sorted({w["waiver"] for w in waived}),
                            "profiles": sorted({w["profile"] for w in waived})})
        arms[arm_id] = {"status": "succeeded" if ok else "incomplete", "receipt": receipt, "jobs": dict(zip(jids, statuses, strict=True)),
                        "blocked_steps": arm_blocked,
                        "isolation_profiles": sorted({(c or {}).get("profile", "unknown") for c in choices})}
    return {"arms": arms, "isolation_ok": iso_ok, "isolation_findings": iso_findings, "isolation_waivers": waivers}


@command("submit_assessment", "assess")
def submit_assessment(ctx: Ctx, p: dict) -> dict:
    """Commit an assessment. The service re-derives conformance and isolation eligibility from
    the ledger; the assessor's statistical evaluation cannot make a non-conformant plan pass.
    Deduplicated by (plan digest, evidence-set digest, assessor release)."""
    plan = ctx.tx.require("evidence_plan", p["plan_id"], lock=True)
    if plan.status not in ("awaiting-assessment", "executing", "assessed"):
        raise Invalid(f"plan is {plan.status}; it has not been executed under a frozen revision")
    frozen = next(r for r in reversed(ctx.tx.record_history("evidence_plan", plan.id)) if r["body"].get("frozen_by"))
    if p.get("plan_digest") not in (plan.digest, frozen["digest"]):
        raise Invalid("plan digest does not match the frozen plan")
    a_ref = p.get("assessor_ref")
    if a_ref != plan.body["assessor"]["ref"]:
        raise Forbidden("only the plan's pinned assessor release may assess it")
    if approval_for(ctx, a_ref, plan.body["claim_kind"]) is None:
        raise QualificationMissing("assessor release approval is missing or suspended")
    evidence_set = p.get("evidence_set") or {}
    es_digest = "sha256:" + sha256_bytes(canonical_json(evidence_set).encode())
    aid = "assess_" + sha256_bytes(canonical_json([frozen["digest"], es_digest, plan.body["assessor"]["digest"]]).encode())[:24]
    existing = ctx.tx.get_record("assessment", aid)
    if existing:
        return {"assessment_id": aid, "outcome": existing.body["outcome"], "deduplicated": True}
    realized = _realized(ctx, plan)
    conformance = ev.check_conformance(plan.body, realized)
    result = ev.finalize_assessment(plan=plan.body, plan_digest=frozen["digest"], evidence_set_digest=es_digest,
                                    assessor_ref=plan.body["assessor"], conformance=conformance,
                                    evaluation=p.get("evaluation") or {"outcome": "inconclusive"})
    deps = []
    for _arm_id, jids in plan.body["launch"]["jobs"].items():
        for jid in jids if isinstance(jids, list) else [jids]:
            deps.append(jid)
            job = ctx.tx.get_job(jid)
            for port in (job["lock"].get("ports") or {}).values():
                if port.get("ref"):
                    deps.append(port["ref"])
            deps.append(job["lock"]["component"]["ref"])
    for ids in evidence_set.values():
        deps.extend(ids)
    result["isolation_waivers"] = realized.get("isolation_waivers", [])  # always visible in the assessment
    result["assessor_isolation"] = (p.get("evaluation") or {}).get("assessor_isolation")
    result.update({"plan_id": plan.id, "claim_ref": plan.body["claim_ref"], "dependencies": sorted(set(deps)),
                   "assessed_by": ctx.principal.id, "realized": {k: {"status": v["status"], "isolation_profiles": v.get("isolation_profiles", []),
                                    "same_os_user": any(isolation.profile_traits(pr)["same_os_user"]
                                                        for pr in v.get("isolation_profiles", []))}
                                for k, v in realized["arms"].items()}})
    ctx.tx.put_record("assessment", aid, ctx.project_id, result, created_by=ctx.principal.id, status=result["outcome"],
                      parent_id=plan.body["claim_ref"]["id"], labels={"health": "intact"})
    for ids in evidence_set.values():
        for art in ids:
            ctx.tx.add_lineage(ctx.project_id, art, aid, "evaluated-by")
    for jids in arm_jobs(plan.body).values():
        for jid in jids:
            ctx.tx.add_lineage(ctx.project_id, jid, aid, "assessed-in")
    ctx.tx.set_status("evidence_plan", plan.id, "assessed", labels={"assessment": aid})
    claim_id = plan.body["claim_ref"]["id"]
    ctx.tx.put_record("justification", f"just:{aid}", ctx.project_id,
                      {"claim_id": claim_id, "requires": [aid], "kind": "assessment"},
                      created_by=ctx.principal.id, status="active", parent_id=claim_id)
    ctx.tx.set_status("claim", claim_id, "assessed", labels={"latest_assessment": aid, "outcome": result["outcome"]})
    ctx.event("assessment.committed", aid, {"plan": plan.id, "outcome": result["outcome"],
                                            "conformance": result["conformance"]},
              investigation_id=plan.body.get("investigation_id"), wake=True)
    return {"assessment_id": aid, "outcome": result["outcome"], "conformance": result["conformance"],
            "findings": result["findings"]}


@command("add_justification", "propose")
def add_justification(ctx: Ctx, p: dict) -> dict:
    claim = ctx.tx.require("claim", p["claim_id"])
    reqs = list(p.get("requires") or [])
    if not reqs:
        raise Invalid("a justification lists the dependencies it jointly requires")
    jid = p.get("justification_id") or f"just_{uuid.uuid4().hex[:10]}"
    ctx.tx.put_record("justification", jid, ctx.project_id, {"claim_id": claim.id, "requires": reqs,
                                                              "note": p.get("note", ""), "kind": p.get("kind", "manual")},
                      created_by=ctx.principal.id, status="active", parent_id=claim.id)
    return {"justification_id": jid}


@command("register_control_suite", "qualify")
def register_control_suite(ctx: Ctx, p: dict) -> dict:
    """Fix planted/null controls, expected outputs, repetitions and acceptance before qualifying."""
    suite = qualification.validate_control_suite(p.get("suite") or {})
    sid = p.get("suite_id") or f"suite:{suite.get('name', uuid.uuid4().hex[:8])}"
    if ctx.tx.get_record("control_suite", sid):
        raise Invalid(f"{sid} exists; control suites are immutable — register a new one")
    rec = ctx.tx.put_record("control_suite", sid, ctx.project_id, {**suite, "maintainer": ctx.principal.id},
                            created_by=ctx.principal.id, status="committed")
    return {"suite_id": sid, "suite_digest": rec.digest}


@command("qualify_protocol", "qualify")
def qualify_protocol(ctx: Ctx, p: dict) -> dict:
    """Admit an independent qualification job: run the target release on every committed control."""
    suite = ctx.tx.require("control_suite", p["suite_id"])
    rel = ctx.tx.require("component_release", p["release_ref"])
    runner = p.get("runner_ref", "eidos.qualification-runner@1.0.0")
    lock = resolve_lock(ctx, {"component_ref": runner, "inputs": p.get("inputs") or {},
                              "config": {"suite": suite.body, "suite_id": suite.id, "suite_digest": suite.digest,
                                         "target": {"ref": rel.id, "digest": rel.body["digest"],
                                                    "entry": rel.body["manifest"]["entry"],
                                                    "bundle_artifact_id": rel.body["bundle_artifact_id"]}}})
    lock["qualification"] = {"suite_id": suite.id, "suite_digest": suite.digest, "release_ref": rel.id,
                             "release_digest": rel.body["digest"]}
    lock["extra_bundles"] = {"target": {"artifact_id": rel.body["bundle_artifact_id"], "digest": rel.body["bundle_digest"]}}
    jid = admit_job(ctx, kind="qualification", lock=lock, investigation_id=None, purpose="qualification",
                    wall_seconds=int(p.get("wall_seconds", 600)), charter_check=False)
    return {"job_id": jid}


@command("submit_qualification", "qualify")
def submit_qualification(ctx: Ctx, p: dict) -> dict:
    suite = ctx.tx.require("control_suite", p["suite_id"])
    if p.get("suite_digest") != suite.digest:
        raise Invalid("results were produced against a different control-suite revision")
    rel = ctx.tx.require("component_release", p["release_ref"])
    if p.get("release_digest") != rel.body["digest"]:
        raise Invalid("results were produced for a different release digest")
    if p.get("job_id"):
        job = ctx.tx.get_job(p["job_id"])
        if not job or job["project_id"] != ctx.project_id or job["kind"] != "qualification" or job["status"] != "succeeded":
            raise Invalid("qualification job must have succeeded")
        if job["created_at"] < suite.created_at:
            raise Invalid("control suite must be committed before the qualification run")
    verdict = qualification.evaluate_qualification(suite.body, p.get("results") or {})
    qid = f"qual:{rel.id}:{uuid.uuid4().hex[:8]}"
    body = {"release_ref": rel.id, "release_digest": rel.body["digest"], "suite_id": suite.id, "suite_digest": suite.digest,
            "scope": suite.body["scope"], "status": verdict["status"], "per_control": verdict["per_control"],
            "environment": p.get("environment"), "job_id": p.get("job_id"), "independent": p.get("independent", True),
            "control_origin": suite.body.get("origin", "independent")}
    ctx.tx.put_record("qualification", qid, ctx.project_id, body, created_by=ctx.principal.id, status=verdict["status"],
                      parent_id=rel.id)
    ctx.event("qualification.recorded", qid, {"release": rel.id, "status": verdict["status"]})
    return {"qualification_id": qid, "status": verdict["status"], "per_control": verdict["per_control"]}


@command("promote_protocol", "promote_protocol")
def promote_protocol(ctx: Ctx, p: dict) -> dict:
    """Approve a template/assessor for specified uses. Checks executed qualification receipts; a
    signature with missing or failing receipts is rejected regardless of who signs."""
    rel = ctx.tx.require("component_release", p["release_ref"])
    if rel.body["manifest"]["kind"] not in ("planning", "assessment"):
        raise Invalid("only planning templates and assessors are promoted")
    quals = [q.body for q in ctx.tx.list_records(ctx.project_id, "qualification", parent_id=rel.id)]
    withdrawn = set()
    for w in ctx.tx.list_records(ctx.project_id, "withdrawal"):
        withdrawn.add(w.body.get("release_digest"))
        withdrawn.update(w.body.get("affected", []))
    if rel.status != "active":
        withdrawn.add(rel.body["digest"])
    q = qualification.check_promotion(release_digest=rel.body["digest"], requested_scope=list(p.get("scope") or []),
                                      qualifications=quals, withdrawn_refs=withdrawn)
    aid = f"approval:{rel.id}"
    rec = ctx.tx.put_record("approval", aid, ctx.project_id,
                            {"release_ref": rel.id, "release_digest": rel.body["digest"], "scope": list(p["scope"]),
                             "suite_digest": q["suite_digest"], "qualification_status": q["status"],
                             "approved_by": ctx.principal.id, "note": p.get("note", "")},
                            created_by=ctx.principal.id, status="active")
    ctx.event("protocol.promoted", rel.id, {"scope": p["scope"], "approval": aid})
    return {"approval_id": aid, "revision": rec.revision}
