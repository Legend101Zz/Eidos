"""Finalization evidence admission, shared by the immutable context and every evidence read.

Project access alone does not authorize disclosure of charter-held-out data. Provenance follows recorded
lineage, job inputs/outputs and digest aliases to a fixed point, including jobs outside this investigation.
"""
from __future__ import annotations

import json
from typing import Any

from eidos.application.service import Ctx
from eidos.domain.errors import Forbidden


def excluded(ctx: Ctx, inv, *, include_digests: bool = False) -> set[str]:
    ref = inv.body.get("charter_ref") or {}
    charter = ctx.tx.get_record("charter", ref["id"], ref.get("revision")) if ref.get("id") else None
    held = (charter.body.get("held_out") or {}) if charter else {}
    digests = {d.removeprefix("sha256:") for d in held.get("labels") or []}
    artifacts = ctx.tx.list_artifacts(ctx.project_id, limit=1_000_000)
    jobs = ctx.tx.list_jobs(ctx.project_id, limit=1_000_000)
    edges = [(e["src"], e["dst"]) for e in ctx.tx.lineage(ctx.project_id)]
    blocked: set[str] = set()
    for j in jobs:
        lock = j.get("lock") or {}
        # Evidence arms and held-out split attestations must not disclose confirmation observations.
        if lock.get("plan_id") or j.get("kind") == "evidence-arm" \
                or (lock.get("config") or {}).get("split") in (held.get("splits") or []):
            blocked.add(j["id"])
        for inp in (lock.get("inputs") or {}).values():
            if inp.get("digest") in digests:
                blocked.add(str(inp["artifact_id"]))
            edges.append((str(inp["artifact_id"]), j["id"]))
        component = (lock.get("component") or {}).get("ref")
        if component:
            edges.append((component, j["id"]))
    for art in artifacts:
        if art.get("producer_job"):
            edges.append((art["producer_job"], art["id"]))
        if art.get("access_class") != "project":
            blocked.add(art["id"])
    while True:
        before = len(blocked), len(digests)
        blocked.update(a["id"] for a in artifacts if a["digest"] in digests)
        blocked.update(dst for src, dst in edges if src in blocked)
        digests.update(a["digest"] for a in artifacts if a["id"] in blocked)
        if before == (len(blocked), len(digests)):
            return blocked | digests if include_digests else blocked


def allowlist(ctx: Ctx, inv, jobs: list[dict]) -> dict:
    from eidos.application.handlers import exploration
    from eidos.domain.assessment import candidate_lock_job_id

    by_id = {j["id"]: j for j in jobs}
    # Include safe support jobs as well as experiments: ordinary export/reproduction may need a build or
    # behavioral validation receipt. Evidence arms remain excluded by provenance below.
    for job in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id, limit=100000):
        by_id.setdefault(job["id"], exploration.view_job(job, ctx.tx.publications(job["id"])))
    jobs = list(by_id.values())
    lock_notes = [n for n in exploration._notes(ctx, inv.id) if n.get("kind") == "candidate_lock"]
    locked_id = candidate_lock_job_id(lock_notes[-1]) if lock_notes else None
    if locked_id and all(j["id"] != locked_id for j in jobs):
        locked = ctx.tx.get_job(locked_id)
        if locked and locked["project_id"] == ctx.project_id:
            jobs.append(exploration.view_job(locked, ctx.tx.publications(locked_id)))
    blocked = excluded(ctx, inv)
    safe = [j for j in jobs if j["id"] not in blocked]
    aids = {str(a) for j in safe for a in (j.get("inputs") or {}).values()}
    aids.update(str(o["artifact_id"]) for j in safe for o in j.get("outputs") or [])
    aids.update(str(a) for a in (inv.body.get("inputs") or {}).values() if isinstance(a, str))
    if inv.body.get("model_artifact_id"):
        aids.add(inv.body["model_artifact_id"])
    # Authored source bundles are relevant existing evidence even before their first execution.
    for t in ctx.tx.list_turns(inv.id):
        for d in ctx.tx.decisions(t["id"]):
            r = ctx.tx.get_record("decision_receipts", d["id"])
            for call in (r.body.get("receipts") or []) if r and d["accepted"] else []:
                result = call.get("result") or {}
                if call.get("ok") and call.get("tool") in ("author_source", "author_component"):
                    aid = result.get("artifact_id") or result.get("bundle_artifact_id")
                    if aid:
                        aids.add(str(aid))
    components = [r.id for r in ctx.tx.list_records(ctx.project_id, "component_release", status="active", limit=100000)
                  if r.id not in blocked]
    return {"jobs": sorted(j["id"] for j in safe), "artifacts": sorted(aids - blocked), "components": sorted(components)}


def read_allowlist(ctx: Ctx, params: dict) -> dict | None:
    tid = params.get("finalization_turn_id") or params.get("evidence_turn_id")
    if not tid:
        return None
    turn = ctx.tx.get_turn(tid)
    if not turn or turn["project_id"] != ctx.project_id:
        raise Forbidden("evidence read requires its admitted turn")
    allowed = turn["spec"].get("evidence_allowlist") or {}
    inv = ctx.tx.require("investigation", turn["investigation_id"])
    if params.get("evidence_turn_id") and turn["spec"].get("evidence_allowlist") is None:
        # Exploration retains project evidence access except for held-out provenance and internal model traces.
        allowed = {
            "jobs": [job["id"] for job in ctx.tx.list_jobs(ctx.project_id, limit=1_000_000)],
            "artifacts": [art["id"] for art in ctx.tx.list_artifacts(ctx.project_id, limit=1_000_000)
                          if art.get("schema") not in {"investigation-view/1", "packed-context/1", "model-response/1"}],
            "components": [r.id for r in ctx.tx.list_records(ctx.project_id, "component_release", status="active", limit=100_000)],
        }
    if turn["spec"].get("finalization_phase") and not turn["spec"].get("finalization"):
        # Ordinary finalization can admit reproduction/export/confirmation work during this same turn.
        # Its durable jobs extend the relevant evidence set; recovery's snapshot stays fixed.
        from eidos.application.handlers import exploration
        allowed = allowlist(ctx, inv, exploration._jobs(ctx, inv.id))
    blocked = excluded(ctx, inv)
    return {kind: [identifier for identifier in values if identifier not in blocked]
            for kind, values in allowed.items()}


def check_read(ctx: Ctx, params: dict, kind: str, identifier: str) -> None:
    allowed = read_allowlist(ctx, params)
    if allowed is not None and identifier not in allowed.get(kind, []):
        raise Forbidden("evidence is outside the finalization allowlist or derives from held-out data")


def safe_view(view: dict[str, Any], allowed: dict, *, blocked: set[str] | None = None, ordinary: bool = False) -> dict[str, Any]:
    """Project only admitted evidence into every context policy (and its stored source manifest).

    Free-form turn/receipt/claim histories can echo evidence results and are not recovery evidence. The exact
    immutable recovery snapshot supplies refusals and visible metrics separately.
    """
    out = dict(view)
    out["jobs"] = [{**j, **({"outputs": [o for o in j["outputs"]
                                         if o["artifact_id"] in allowed.get("artifacts", [])]} if "outputs" in j else {})}
                   for j in view.get("jobs") or [] if j["id"] in allowed.get("jobs", [])]
    for key in ("turns", "events", "call_results", "evidence_plans", "claims", "assessment_requests"):
        out[key] = []
    if ordinary:
        # Keep the handles and prospective obligations needed to finish already-admitted confirmation work,
        # without its result payloads. Recovery does not need these effect handles.
        out["evidence_plans"] = [{k: plan.get(k) for k in (
            "plan_id", "status", "claim", "success_rule_ref", "obligations")}
            for plan in view.get("evidence_plans") or []]
        out["assessment_requests"] = [{k: request.get(k) for k in ("request_id", "status", "plan_id", "path")}
                                      for request in view.get("assessment_requests") or []]
        out["call_results"] = [{"turn": call.get("turn"), "tool": call.get("tool"), "ok": call.get("ok"),
                                "result": {k: v for k, v in (call.get("result") or {}).items()
                                           if k.endswith("_id") and isinstance(v, str)}}
                               for call in view.get("call_results") or [] if isinstance(call.get("result"), dict)]
    out["created_artifacts"] = [a for a in view.get("created_artifacts") or []
                                if a.get("artifact_id") in allowed.get("artifacts", [])]
    # Preserve the lock/rule without carrying a held-out provenance reference or an echoed derived value.
    denied = blocked or set()
    for key in ("pinned_notes",):
        out[key] = [n for n in view.get(key) or [] if not any(x in json.dumps(n, default=str) for x in denied)]
    inv = dict(out["investigation"])
    body = dict(inv["body"])
    body["inputs"] = {k: a for k, a in (body.get("inputs") or {}).items() if a in allowed.get("artifacts", [])}
    if body.get("model_artifact_id") not in allowed.get("artifacts", []):
        body.pop("model_artifact_id", None)
    inv["body"] = {k: sanitize(v, denied) for k, v in body.items()}
    out["investigation"] = inv
    base = out.get("baseline_record")
    if base and base.get("job_id") not in allowed.get("jobs", []):
        out["baseline_record"] = None
    return sanitize(out, denied)



def model_projection(view: dict, catalog: list, blocked: set[str]) -> tuple[dict, list]:
    """Return the model-facing projection of a recorded view and catalog."""
    if not blocked:
        return view, catalog
    projected = {**view}
    inv = view["investigation"]
    inv_body = {k: sanitize(v, blocked) for k, v in inv["body"].items()}
    projected["investigation"] = {**inv, "body": inv_body}
    if charter := view.get("charter"):
        held = charter.get("held_out") or {}
        projected["charter"] = {**charter, "held_out": {
            "labels": bool(held.get("labels")), "splits": bool(held.get("splits"))}}
        if held.get("labels") or held.get("splits"):
            _hide_assessment_outcomes(projected)
    return sanitize(projected, blocked), sanitize(catalog, blocked)


def _hide_assessment_outcomes(view: dict) -> None:
    """Preserve prospective handles and completion without sending assessor outcomes to the model."""
    state = view.get("claim_state") or {}
    plans = view.get("evidence_plans") or []
    assessed = any(plan.get("status") == "assessed" for plan in plans) or bool(state.get("assessments_considered"))
    view["claim_state"] = {"assessment_result_withheld": True} if assessed else {}
    view["investigation"]["body"].pop("conclusion", None)
    view["evidence_plans"] = [{k: plan.get(k) for k in (
        "plan_id", "status", "claim", "success_rule_ref", "obligations")} for plan in plans]
    view["assessment_requests"] = [{k: request.get(k) for k in ("request_id", "status", "plan_id", "path")}
                                   for request in view.get("assessment_requests") or []]
    view["claims"] = [{k: claim.get(k) for k in ("id", "statement")} for claim in view.get("claims") or []]
    view["events"] = [event for event in view.get("events") or [] if not str(event.get("type")).startswith("assessment.")]


def sanitize(value: Any, blocked: set[str]) -> Any:
    """Drop records that directly name excluded provenance; recursively sanitize their containers."""
    def denied(text: str) -> bool:
        return any(identifier in text for identifier in blocked)

    if isinstance(value, str):
        return None if denied(value) else value
    if isinstance(value, list):
        return [clean for item in value if (clean := sanitize(item, blocked)) is not None]
    if isinstance(value, dict):
        if any(isinstance(v, str) and denied(v) for v in value.values()):
            return None
        return {k: sanitize(v, blocked) for k, v in value.items() if not (isinstance(k, str) and denied(k))}
    return value
