"""Authorized read views. Every view is project-scoped and filtered by artifact access class.

Views that reveal evaluation results record an exposure for the reading principal, so human and
model disclosures enter the same record."""

from __future__ import annotations

import json
from typing import Any

from eidos.application.handlers.components import catalog
from eidos.application.handlers.investigations import confirm_scope, inv_scope
from eidos.application.service import Ctx, query
from eidos.domain import support
from eidos.domain.errors import Forbidden, NotFound

PREVIEW_BYTES = 16_384
RESTRICTED_CLASSES = {"sealed-labels": "sealed:read", "restricted": "restricted:read"}


def can_read_artifact(ctx: Ctx, art: dict) -> bool:
    need = RESTRICTED_CLASSES.get(art["access_class"])
    return need is None or ctx.principal.has(need)


def _artifact(ctx: Ctx, aid: str) -> dict:
    art = ctx.tx.get_artifact(aid)
    if not art or art["project_id"] != ctx.project_id:
        raise NotFound(f"artifact {aid} not found")
    if not can_read_artifact(ctx, art):
        raise Forbidden(f"artifact {aid} is {art['access_class']}; this principal may not read it")
    if art.get("producer_job"):  # outputs of evidence arms reveal outcomes (metadata, preview and download)
        job = ctx.tx.get_job(art["producer_job"])
        if job:
            _expose_job(ctx, job)
    return art


def _jsonable(row: dict) -> dict:
    return json.loads(json.dumps(row, default=str))


SENSITIVE_KINDS = {"assessment", "qualification", "evidence_plan", "reassessment_request"}
# Event types whose bodies carry evaluation outcomes.
SENSITIVE_EVENTS = ("assessment.", "qualification.", "evidence.ready", "protocol.promoted")


def _expose_record(ctx: Ctx, rec) -> None:
    """Reads that can reveal evaluation outcomes are exposures, whichever query path is used."""
    body = rec.body or {}
    if rec.kind in SENSITIVE_KINDS or (rec.kind == "claim" and (rec.labels or {}).get("outcome")):
        _expose(ctx, body.get("investigation_id"), f"{rec.kind}:{rec.id}@{rec.revision}",
                {"status": rec.status, "outcome": (rec.labels or {}).get("outcome")}, charter_ref=body.get("charter_ref"))


def _expose_job(ctx: Ctx, job: dict) -> None:
    if job.get("kind") == "evidence-arm" and job.get("terminal"):
        _expose(ctx, job.get("investigation_id"), f"job:{job['id']}", {"status": job["status"]})


@query("get_record")
def get_record(ctx: Ctx, p: dict) -> dict:
    rec = ctx.tx.require(p["kind"], p["id"], p.get("revision"))
    if rec.project_id != ctx.project_id:
        raise NotFound("not found")
    _expose_record(ctx, rec)
    return rec.view()


@query("list_records")
def list_records(ctx: Ctx, p: dict) -> list[dict]:
    recs = ctx.tx.list_records(ctx.project_id, p["kind"], status=p.get("status"), parent_id=p.get("parent_id"),
                               limit=min(int(p.get("limit", 100)), 1000))
    for r in recs:  # status alone can reveal an outcome (assessment status == outcome)
        _expose_record(ctx, r)
    if p.get("summary", True):
        return [{k: v for k, v in r.view().items() if k != "body"} | {"title": _title(r)} for r in recs]
    return [r.view() for r in recs]


def _title(r) -> str:
    b = r.body
    return str(b.get("statement") or b.get("question") or b.get("study_id") or b.get("purpose") or "")[:200]


@query("record_history")
def record_history(ctx: Ctx, p: dict) -> list[dict]:
    rec = ctx.tx.require(p["kind"], p["id"])
    if rec.project_id != ctx.project_id:
        raise NotFound("not found")
    _expose_record(ctx, rec)
    return [_jsonable(r) for r in ctx.tx.record_history(p["kind"], p["id"])]


@query("list_events")
def list_events(ctx: Ctx, p: dict) -> dict:
    """Cursor-paginated events with a snapshot horizon; late events get later sequence numbers."""
    after = int(p.get("cursor") or 0)
    horizon = int(p.get("horizon") or ctx.tx.horizon())
    rows = ctx.tx.events_after(ctx.project_id, after, investigation_id=p.get("investigation_id"), upto=horizon,
                               limit=min(int(p.get("limit", 100)), 500), types=p.get("types"))
    nxt = rows[-1]["seq"] if rows else after
    for r in rows:
        if str(r["type"]).startswith(SENSITIVE_EVENTS):
            _expose(ctx, r.get("investigation_id"), f"event:{r['seq']}:{r['type']}:{r['subject']}",
                    {"type": r["type"]}, charter_ref=(r.get("body") or {}).get("charter_ref"))
    return {"events": [_jsonable(r) for r in rows], "next_cursor": nxt, "horizon": horizon,
            "more": bool(rows) and nxt < horizon}


@query("get_job")
def get_job(ctx: Ctx, p: dict) -> dict:
    job = ctx.tx.get_job(p["job_id"])
    if not job or job["project_id"] != ctx.project_id:
        raise NotFound(f"job {p['job_id']} not found")
    _expose_job(ctx, job)
    out = _jsonable(job)
    out["attempts"] = [_jsonable(a) for a in ctx.tx.list_attempts(job["id"])]
    out["outputs"] = [_jsonable(x) for x in ctx.tx.publications(job["id"])]
    if not p.get("include_lock", True):
        out.pop("lock", None)
    return out


@query("list_jobs")
def list_jobs(ctx: Ctx, p: dict) -> list[dict]:
    rows = ctx.tx.list_jobs(ctx.project_id, investigation_id=p.get("investigation_id"), statuses=p.get("statuses"))
    for j in rows:
        _expose_job(ctx, j)
    return [{"id": j["id"], "kind": j["kind"], "status": j["status"], "purpose": j["purpose"], "epoch": j["epoch"],
             "component": j["lock"].get("component", {}).get("ref"), "trial_id": j["trial_id"],
             "cancel_requested": j["cancel_requested"], "created_at": str(j["created_at"]),
             "terminal": _jsonable(j["terminal"]) if j["terminal"] else None} for j in rows]


@query("get_artifact", "artifact:read")
def get_artifact(ctx: Ctx, p: dict) -> dict:
    art = _artifact(ctx, p["artifact_id"])
    return _jsonable(art)


@query("artifact_preview", "artifact:read")
def artifact_preview(ctx: Ctx, p: dict) -> dict:
    """A bounded preview: the first bytes of text/JSON/CSV content, never arbitrary paths."""
    art = _artifact(ctx, p["artifact_id"])
    limit = min(int(p.get("max_bytes", PREVIEW_BYTES)), 65_536)
    if not ctx.store.has(art["digest"], art["size"]):
        return {"artifact_id": art["id"], "available": False, "reason": "bytes missing from store"}
    textual = art["media_type"].startswith("text/") or art["media_type"] in ("application/json", "application/x-ndjson",
                                                                             "application/vnd.eidos.source-bundle+json")
    if not textual:
        return {"artifact_id": art["id"], "available": True, "media_type": art["media_type"], "size": art["size"],
                "preview": None, "reason": "binary content; use a measurement component to inspect it"}
    with open(ctx.store.path_for(art["digest"]), "rb") as fh:
        data = fh.read(limit + 1)
    return {"artifact_id": art["id"], "available": True, "media_type": art["media_type"], "size": art["size"],
            "truncated": len(data) > limit, "preview": data[:limit].decode("utf-8", errors="replace")}


@query("search_components")
def search_components(ctx: Ctx, p: dict) -> dict:
    """Catalog entries are data: thousands of installed methods never become tool definitions."""
    q = str(p.get("text", "")).lower()
    cap = p.get("capability")
    eff = p.get("effect")
    rows = []
    cutoff = None
    if p.get("as_of_horizon") is not None:  # catalog snapshot as of a ledger horizon
        cutoff = ctx.tx.event_time(int(p["as_of_horizon"]))
    published = {r.id: r.created_at for r in ctx.tx.list_records(ctx.project_id, "component_release", limit=5000)}
    for r in catalog(ctx, include_inactive=bool(p.get("include_inactive"))):
        if cutoff is not None and published.get(r["ref"]) and published[r["ref"]] > cutoff:
            continue
        m = r["manifest"]
        hay = " ".join([r["ref"], m.get("implements", ""), " ".join(m.get("scientific_effects", [])),
                        str(m.get("summary", "")), m.get("kind", "")]).lower()
        if q and q not in hay:
            continue
        if cap and m.get("implements") != cap:
            continue
        if eff and eff not in m.get("scientific_effects", []):
            continue
        rows.append(catalog_row(r["ref"], m, r["status"], r.get("qualification"),
                                card_summary=None if m.get("summary") else _card_summary(ctx, m, r.get("bundle_digest"))))
    start = int(p.get("cursor") or 0)
    limit = min(int(p.get("limit", 20)), 100)
    page = rows[start:start + limit]
    return {"components": page, "total": len(rows), "next_cursor": start + limit if start + limit < len(rows) else None}


_CARD_CACHE: dict[str, str] = {}


def _card_summary(ctx: Ctx, m: dict, bundle_digest: str | None) -> str:
    """For a release whose manifest has no ``summary``: the method card's Question and Config paragraphs
    (built-in eidos.pytorch.* releases document their config only there). Cached per bundle digest."""
    if not bundle_digest or not m.get("card"):
        return ""
    if bundle_digest in _CARD_CACHE:
        return _CARD_CACHE[bundle_digest]
    text = ""
    try:
        with open(ctx.store.path_for(bundle_digest)) as fh:
            card = (json.load(fh).get("files") or {}).get(m["card"]) or ""
        paras = [x.strip().replace("\n", " ") for x in card.split("\n\n")]
        keep = [x for x in paras if x.startswith(("**Question.**", "**Config.**", "**Inputs.**", "**Outputs.**"))]
        text = " ".join(keep)
    except Exception:  # noqa: BLE001 - a missing or odd bundle just means no summary
        text = ""
    if len(_CARD_CACHE) > 2000:
        _CARD_CACHE.clear()
    _CARD_CACHE[bundle_digest] = text[:900]
    return _CARD_CACHE[bundle_digest]


def catalog_row(ref: str, m: dict, status: str, qualification: Any = None, *, card_summary: str | None = None) -> dict:
    """What the catalog (and so the investigator's context) shows about a release: its ports, including
    optional inputs, its config contract (with descriptions and examples) and a bounded summary (from the
    manifest, else from the method card)."""
    return {"ref": ref, "implements": m["implements"], "kind": m["kind"], "status": status,
            "effects": m["scientific_effects"], "summary": str(m.get("summary", ""))[:600] or (card_summary or ""),
            "input": m.get("input"), "optional_input": m.get("optional_inputs"), "output": m.get("output"),
            "qualification": qualification, **({"config_schema": m["config_schema"]} if m.get("config_schema") else {})}


@query("describe_component")
def describe_component(ctx: Ctx, p: dict) -> dict:
    rec = ctx.tx.require("component_release", p["component_ref"])
    approvals = [a.view() for a in ctx.tx.list_records(ctx.project_id, "approval") if a.body["release_ref"] == rec.id]
    quals = [q.view() for q in ctx.tx.list_records(ctx.project_id, "qualification", parent_id=rec.id)]
    return {**rec.view(), "approvals": approvals, "qualifications": quals}


@query("get_profile")
def get_profile_q(ctx: Ctx, p: dict) -> dict:
    return ctx.tx.require("profile", f"profile:{ctx.project_id}", p.get("revision")).view()


@query("get_investigation")
def get_investigation(ctx: Ctx, p: dict) -> dict:
    inv = ctx.tx.require("investigation", p["investigation_id"])
    if inv.project_id != ctx.project_id:
        raise NotFound("not found")
    turns = ctx.tx.list_turns(inv.id)
    jobs = ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id)
    for j in jobs:  # evidence-arm statuses reveal outcomes
        _expose_job(ctx, j)
    return {**inv.view(), "needs_input": (inv.labels or {}).get("needs_input"),
            "turns": [{"id": t["id"], "seq": t["seq"], "status": t["status"], "horizon": t["horizon"],
                       "stop_reason": (t["result"] or {}).get("stop_reason")} for t in turns],
            "jobs": [{"id": j["id"], "status": j["status"], "kind": j["kind"],
                      "component": j["lock"].get("component", {}).get("ref")} for j in jobs],
            "budgets": {"exploration": ctx.tx.budgets(inv_scope(inv.id)),
                        "confirmation": ctx.tx.budgets(confirm_scope(inv.id))}}


@query("get_turn")
def get_turn(ctx: Ctx, p: dict) -> dict:
    turn = ctx.tx.get_turn(p["turn_id"])
    if not turn or turn["project_id"] != ctx.project_id:
        raise NotFound("turn not found")
    ctxrec = ctx.tx.get_record("turn_context", turn["id"])
    return {**_jsonable(turn), "context": ctxrec.body if ctxrec else None,
            "decisions": [_jsonable(d) for d in ctx.tx.decisions(turn["id"])]}


@query("get_claim")
def get_claim(ctx: Ctx, p: dict) -> dict:
    claim = ctx.tx.require("claim", p["claim_id"], p.get("revision"))
    justs = [j.body | {"id": j.id} for j in ctx.tx.list_records(ctx.project_id, "justification", parent_id=claim.id)]
    affected: set[str] = set()
    for a in ctx.tx.list_records(ctx.project_id, "assessment", parent_id=claim.id):
        if a.labels.get("health") in ("affected", "withdrawn"):
            affected.add(a.id)
    for w in ctx.tx.list_records(ctx.project_id, "withdrawal"):
        affected.update(w.body.get("affected", []))
    assessments = [{"id": a.id, "outcome": a.body["outcome"], "conformance": a.body["conformance"],
                    "health": a.labels.get("health", "intact"), "plan_id": a.body.get("plan_id")}
                   for a in ctx.tx.list_records(ctx.project_id, "assessment", parent_id=claim.id)]
    _expose(ctx, claim.body.get("investigation_id"), f"claim:{claim.id}", {"assessments": [a["id"] for a in assessments]})
    return {**claim.view(), "assessments": assessments, "support": support.support_status(justs, affected)}


@query("get_assessment")
def get_assessment(ctx: Ctx, p: dict) -> dict:
    a = ctx.tx.require("assessment", p["assessment_id"])
    _expose(ctx, a.body.get("investigation_id"), f"assessment:{a.id}", {"outcome": a.body["outcome"]},
            charter_ref=a.body.get("charter_ref"))
    return {**a.view(), "health": a.labels.get("health", "intact")}


def _expose(ctx: Ctx, inv_id: str | None, subject: str, disclosed: dict, *, charter_ref: dict | None = None) -> None:
    study = None
    if inv_id:
        inv = ctx.tx.get_record("investigation", inv_id)
        if inv and inv.body.get("charter_ref"):
            charter_ref = inv.body["charter_ref"]
    if isinstance(charter_ref, dict) and str(charter_ref.get("id", "")).count(":"):
        study = str(charter_ref["id"]).split(":", 1)[1]
    ctx.tx.add_exposure(ctx.project_id, study, ctx.principal.id, subject, disclosed)


@query("compare_trials")
def compare_trials(ctx: Ctx, p: dict) -> dict:
    """Side-by-side receipts for jobs, with eligibility for matched comparison (same metric
    identity, completed, and whether they share a training trajectory)."""
    rows = []
    for jid in p.get("job_ids", []):
        job = ctx.tx.get_job(jid)
        if not job or job["project_id"] != ctx.project_id:
            raise NotFound(f"job {jid} not found")
        receipt = (job.get("terminal") or {}).get("receipt", {})
        branch_rec = ctx.tx.get_record("branch", f"branch:{job['trial_id']}") if job["trial_id"] else None
        branch = branch_rec.body if branch_rec else (job["lock"].get("config") or {}).get("branch")
        rows.append({"job_id": jid, "status": job["status"], "component": job["lock"]["component"]["ref"],
                     "metrics": receipt.get("metrics", {}), "realized": {k: receipt.get(k) for k in
                                                                          ("examples", "steps", "device", "seconds")},
                     "independence_unit": (receipt.get("independence_unit") or (branch or {}).get("independence_unit")
                                           or (branch or {}).get("parent", {}).get("trajectory_id")
                                           or job["trial_id"])})
    units = [r["independence_unit"] for r in rows]
    _expose(ctx, p.get("investigation_id"), "comparison:" + ",".join(p.get("job_ids", [])), {"jobs": p.get("job_ids")})
    return {"rows": rows, "all_succeeded": all(r["status"] == "succeeded" for r in rows),
            "shared_training_history": len(set(units)) < len(units),
            "note": "rows sharing an independence unit are not independent replications"}


@query("budgets")
def budgets(ctx: Ctx, p: dict) -> dict:
    scope = p.get("scope") or (inv_scope(p["investigation_id"]) if p.get("investigation_id") else f"project:{ctx.project_id}")
    owner = ctx.tx.budget_owner(scope)
    if owner is not None and owner != ctx.project_id:
        raise NotFound(f"budget scope {scope} not found")
    entries = ctx.tx.budget_entries(scope) if p.get("entries") else []
    return {"scope": scope, "budgets": ctx.tx.budgets(scope), "entries": [_jsonable(e) for e in entries[-200:]]}


@query("assessment_inputs", "assess")
def assessment_inputs(ctx: Ctx, p: dict) -> dict:
    """What the assessor may read: the frozen plan, each arm's receipts and output artifacts."""
    plan = ctx.tx.require("evidence_plan", p["plan_id"])
    from eidos.application.handlers.evidence import arm_jobs
    arms = {}
    for arm_id, jids in arm_jobs(plan.body).items():
        jobs = [ctx.tx.get_job(j) for j in jids]
        statuses = [j["status"] for j in jobs]
        arms[arm_id] = {"job_ids": jids, "status": "succeeded" if statuses and all(x == "succeeded" for x in statuses)
                        else "incomplete", "receipts": [(j.get("terminal") or {}).get("receipt", {}) for j in jobs],
                        "outputs": [_jsonable(o) for j in jids for o in ctx.tx.publications(j)]}
    return {"plan_id": plan.id, "plan_digest": plan.digest, "plan": plan.body, "arms": arms}


@query("investigation_view")
def investigation_view(ctx: Ctx, p: dict) -> dict:
    """Authorized source view at a ledger horizon, used by the context builder. Nothing after the
    horizon is returned, so replays cannot see the future."""
    inv = ctx.tx.require("investigation", p["investigation_id"])
    horizon = int(p["horizon"])
    # Recent events for display (a window ending at the horizon); job visibility and the record cutoff are
    # computed separately so a long project history cannot truncate them (the window used to start at seq 0).
    events = ctx.tx.events_after(ctx.project_id, max(0, horizon - 5000), investigation_id=inv.id, upto=horizon,
                                 limit=5000)
    finished = {e["subject"] for e in ctx.tx.events_after(
        ctx.project_id, 0, investigation_id=inv.id, upto=horizon, limit=1_000_000,
        types=["job.succeeded", "job.failed", "job.cancelled", "job.pruned", "job.outcome_unknown"])}
    # Records as of the horizon: pick the latest revision whose creating event is <= horizon.
    hist = ctx.tx.record_history("investigation", inv.id)
    cutoff_ts = ctx.tx.event_time(horizon) if horizon else None
    inv_body = hist[0]["body"]
    for h in hist:
        if cutoff_ts is None or h["created_at"] <= cutoff_ts:
            inv_body = h["body"]
    jobs = []
    for j in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id):
        if cutoff_ts is not None and j["created_at"] > cutoff_ts:
            continue
        terminal_visible = j["id"] in finished
        if terminal_visible:
            _expose_job(ctx, j)
        outputs = [o for o in ctx.tx.publications(j["id"]) if cutoff_ts is None or o["created_at"] <= cutoff_ts]
        jobs.append({"id": j["id"], "component": j["lock"].get("component", {}).get("ref"), "purpose": j["purpose"],
                     "status": j["status"] if terminal_visible else "running-or-queued",
                     "config": j["lock"].get("config"), "inputs": {k: v["artifact_id"] for k, v in (j["lock"].get("inputs") or {}).items()},
                     "receipt": (j["terminal"] or {}).get("receipt") if terminal_visible else None,
                     "error": (j["terminal"] or {}).get("error") if terminal_visible else None,
                     "outputs": [{"slot": o["slot"], "artifact_id": o["artifact_id"], "role": o["role"],
                                  "schema": o["schema"], "media_type": o["media_type"], "size": o["size"]} for o in outputs]})
    charter = None
    if inv_body.get("charter_ref"):
        ch = ctx.tx.get_record("charter", inv_body["charter_ref"]["id"], inv_body["charter_ref"]["revision"])
        charter = ch.body if ch else None
    turns = [t for t in ctx.tx.list_turns(inv.id) if t["horizon"] <= horizon]
    claims = [c for c in ctx.tx.list_records(ctx.project_id, "claim") if c.body.get("investigation_id") == inv.id
              and (cutoff_ts is None or c.created_at <= cutoff_ts)]
    for c in claims:
        _expose_record(ctx, c)
    try:
        from eidos.application.handlers.projects import get_profile
        environments = sorted(get_profile(ctx).body.get("environments") or {})
    except Exception:  # noqa: BLE001 - a project without a profile simply has none
        environments = []
    # Artifacts the investigator itself created (e.g. author_source bundles), with whether a job has used them.
    # Their ids otherwise only travel inside events, which the context does not carry (Mac request 11).
    created, call_results = [], []
    for t in turns:
        for d in ctx.tx.decisions(t["id"]):
            if not d["accepted"]:
                continue
            rec = ctx.tx.get_record("decision_receipts", d["id"])
            if rec is None or (cutoff_ts is not None and rec.created_at > cutoff_ts):
                continue
            for r in rec.body.get("receipts") or []:
                aid = ((r.get("result") or {}).get("artifact_id") or (r.get("result") or {}).get("bundle_artifact_id"))
                if r.get("ok") and r.get("tool") in ("author_source", "author_component") and aid:
                    created.append({"artifact_id": aid, "tool": r["tool"], "turn": t["seq"],
                                    "used_by": [j["id"] for j in jobs if aid in (j.get("inputs") or {}).values()]})
                # The ids an effect call returned (trial_id, plan_id, validation job, export id, ...) and its errors:
                # a later call needs them, and they otherwise travel only inside events (G5 live run).
                call_results.append({"turn": t["seq"], "tool": r.get("tool"), "ok": bool(r.get("ok")),
                                     **({"result": _compact_result(r.get("result"))} if r.get("ok") else
                                        {"code": r.get("code"), "error": _compact_result(r.get("error"))})})
    plans = []
    assessments = {a.body.get("plan_id"): a for a in ctx.tx.list_records(ctx.project_id, "assessment", limit=100000)
                   if cutoff_ts is None or a.created_at <= cutoff_ts}
    for pl in ctx.tx.list_records(ctx.project_id, "evidence_plan", limit=100000):
        if pl.body.get("investigation_id") != inv.id or (cutoff_ts is not None and pl.created_at > cutoff_ts):
            continue
        _expose_record(ctx, pl)
        asm = assessments.get(pl.id)
        if asm is not None:
            _expose_record(ctx, asm)
        launch = pl.body.get("launch") or {}
        plans.append({"plan_id": pl.id, "status": pl.status, "claim": (pl.body.get("claim_ref") or {}).get("id"),
                      "success_rule_ref": pl.body.get("success_rule_ref"), "obligations": pl.body.get("obligations"),
                      "jobs": launch.get("jobs"), "blocked": launch.get("blocked"),
                      "assessment": None if asm is None else {"assessment_id": asm.id, "outcome": asm.body.get("outcome"),
                                                              "conformance": asm.body.get("conformance")}})
    approved = sorted({(ap.body["release_ref"], tuple(ap.body.get("scope") or ()))
                       for ap in ctx.tx.list_records(ctx.project_id, "approval", status="active", limit=100000)})
    return {"investigation": {"id": inv.id, "status": inv.status, "body": inv_body}, "horizon": horizon,
            "environments": environments, "created_artifacts": created[-50:], "call_results": call_results[-30:],
            "evidence_plans": plans[-10:],
            "approved_protocols": [{"release_ref": r, "scope": list(sc)} for r, sc in approved],
            "charter": charter, "jobs": jobs,
            "events": [{"seq": e["seq"], "type": e["type"], "subject": e["subject"]} for e in events[-200:]],
            "turns": [{"id": t["id"], "seq": t["seq"], "status": t["status"], "result": t["result"]} for t in turns],
            "claims": [{"id": c.id, "statement": c.body["statement"], "status": c.status, "labels": c.labels} for c in claims],
            "budgets": ctx.tx.budgets(inv_scope(inv.id))}


def _compact_result(x, limit: int = 600):
    """A call result or error for the context: drop bulky locks, keep ids and messages, cap the size."""
    import json as _json
    if isinstance(x, dict):
        x = {k: v for k, v in x.items() if k not in ("lock", "manifest", "estimated_cost")}
    text = _json.dumps(x, sort_keys=True, default=str)
    return x if len(text) <= limit else text[:limit] + "...(truncated)"


@query("list_projects", "project:read", project_scoped=False)
def list_projects(ctx: Ctx, p: dict) -> list[dict]:
    return [_jsonable(r) for r in ctx.tx.list_projects() if ctx.principal.can_access(r["id"])]


@query("project_summary")
def project_summary(ctx: Ctx, p: dict) -> dict:
    kinds = ("investigation", "charter", "claim", "evidence_plan", "assessment", "component_release", "export",
             "branch", "withdrawal", "approval", "qualification")
    counts = {k: len(ctx.tx.list_records(ctx.project_id, k, limit=100000)) for k in kinds}
    jobs = ctx.tx.list_jobs(ctx.project_id, limit=100000)
    by_status: dict[str, int] = {}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    needs_input = [r.id for r in ctx.tx.list_records(ctx.project_id, "investigation", limit=100000)
                   if (r.labels or {}).get("needs_input")]
    return {"project_id": ctx.project_id, "records": counts, "jobs": by_status, "investigations_needing_input": needs_input,
            "budgets": ctx.tx.budgets(f"project:{ctx.project_id}"), "horizon": ctx.tx.horizon()}


def jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))
