"""Finalization evidence admission, shared by the immutable context and every evidence read.

Project access alone does not authorize disclosure of charter-held-out data. Provenance follows recorded
lineage, job inputs/outputs and digest aliases to a fixed point, including jobs outside this investigation.
"""
from __future__ import annotations

import json
from typing import Any

from eidos.application.service import Ctx
from eidos.domain.errors import Forbidden


def excluded(ctx: Ctx, inv) -> set[str]:
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
            edges.extend([(art["producer_job"], art["id"]), (art["id"], art["producer_job"])])
        if art.get("access_class") != "project":
            blocked.add(art["id"])
    while True:
        before = len(blocked), len(digests)
        blocked.update(a["id"] for a in artifacts if a["digest"] in digests)
        blocked.update(dst for src, dst in edges if src in blocked)
        digests.update(a["digest"] for a in artifacts if a["id"] in blocked)
        if before == (len(blocked), len(digests)):
            return blocked


def allowlist(ctx: Ctx, inv, jobs: list[dict]) -> dict:
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


def check_read(ctx: Ctx, params: dict, kind: str, identifier: str) -> None:
    tid = params.get("finalization_turn_id")
    if not tid:
        return
    turn = ctx.tx.get_turn(tid)
    if not turn or turn["project_id"] != ctx.project_id:
        raise Forbidden("finalization evidence read requires its admitted turn")
    allowed = turn["spec"].get("evidence_allowlist") or {}
    if identifier not in allowed.get(kind, []):
        raise Forbidden("evidence is outside this finalization turn's allowlist")
    inv = ctx.tx.require("investigation", turn["investigation_id"])
    if identifier in excluded(ctx, inv):
        raise Forbidden("held-out evidence and its derivatives are isolated during finalization")


def safe_view(view: dict[str, Any], allowed: dict, *, blocked: set[str] | None = None) -> dict[str, Any]:
    """Project only admitted evidence into every context policy (and its stored source manifest).

    Free-form turn/receipt/claim histories can echo evidence results and are not recovery evidence. The exact
    immutable recovery snapshot supplies refusals and visible metrics separately.
    """
    out = dict(view)
    out["jobs"] = [j for j in view.get("jobs") or [] if j["id"] in allowed.get("jobs", [])]
    for key in ("turns", "events", "call_results", "evidence_plans", "claims", "assessment_requests"):
        out[key] = []
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
    inv["body"] = body
    out["investigation"] = inv
    base = out.get("baseline_record")
    if base and base.get("job_id") not in allowed.get("jobs", []):
        out["baseline_record"] = None
    return sanitize(out, denied)


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
        return {k: sanitize(v, blocked) for k, v in value.items()}
    return value
