"""Study charters and exposure records."""

from __future__ import annotations

from eidos.application.service import Ctx, command
from eidos.domain import charter as rules
from eidos.domain.errors import Invalid


def charter_id(study_id: str) -> str:
    return f"charter:{study_id}"


@command("commit_study_charter", "define_study")
def commit_study_charter(ctx: Ctx, p: dict) -> dict:
    """Commit an immutable charter revision before task-specific exploration."""
    body = rules.validate_charter(p.get("charter") or {})
    cid = charter_id(body["study_id"])
    if ctx.tx.get_record("charter", cid):
        raise Invalid(f"{cid} already exists; use amend_study_charter", details={"charter_id": cid})
    body["committed_before_exploration"] = True
    rec = ctx.tx.put_record("charter", cid, ctx.project_id, body, created_by=ctx.principal.id, status="committed")
    ctx.event("charter.committed", cid, {"revision": 1, "digest": rules.charter_digest(body),
                                         "exploration_only": body["exploration_only"]})
    inv_id = p.get("investigation_id")
    if inv_id:
        from eidos.application.handlers.investigations import attach_charter
        attach_charter(ctx, inv_id, cid, rec.revision, rules.charter_digest(body))
    return {"charter_id": cid, "revision": rec.revision, "digest": rules.charter_digest(body)}


@command("amend_study_charter", "amend_charter")
def amend_study_charter(ctx: Ctx, p: dict) -> dict:
    """Create a new charter revision preserving prior values, rationale, author and exposures.

    If the amended body names a new study ID, a new charter record is created whose lineage and
    exposure-informed status point back to the original; the original rule and its assessments
    remain unchanged.
    """
    cid = p["charter_id"]
    prev = ctx.tx.require("charter", cid, lock=True)
    if ctx.expected_revision is not None and ctx.expected_revision != prev.revision:
        from eidos.domain.errors import StaleRevision
        raise StaleRevision(f"{cid} is at revision {prev.revision}", details={"observed_revision": prev.revision})
    seen = ctx.tx.exposures(ctx.project_id, prev.body["study_id"], since=prev.created_at)
    body, amendment = rules.amend_charter(prev.body, p.get("charter") or {}, rationale=p.get("rationale", ""),
                                          author=ctx.principal.id, exposure_refs=list(p.get("exposure_refs", [])),
                                          exposures_since_charter=len(seen))
    amendment["exposures_seen"] = [e["id"] for e in seen]
    amendment["previous"] = {"charter_id": cid, "revision": prev.revision, "digest": prev.digest}
    body["amendment"] = amendment
    body["committed_before_exploration"] = False
    new_cid = charter_id(body["study_id"])
    if new_cid != cid:
        if ctx.tx.get_record("charter", new_cid):
            raise Invalid(f"{new_cid} already exists")
        rec = ctx.tx.put_record("charter", new_cid, ctx.project_id, body, created_by=ctx.principal.id,
                                status="committed", parent_id=cid)
        ctx.tx.set_status("charter", cid, "superseded", labels={"superseded_by": new_cid})
        ctx.tx.add_lineage(ctx.project_id, f"{cid}@{prev.revision}", f"{new_cid}@{rec.revision}", "amended-as")
    else:
        rec = ctx.tx.put_record("charter", cid, ctx.project_id, body, created_by=ctx.principal.id, status="committed")
    ctx.event("charter.amended", rec.id, {"revision": rec.revision, "exposure_informed": amendment["exposure_informed"],
                                         "changes": sorted(amendment["changes"])})
    return {"charter_id": rec.id, "revision": rec.revision, "digest": rules.charter_digest(body),
            "exposure_informed": amendment["exposure_informed"],
            "exposure_informed_lineage": body["exposure_informed_lineage"]}


@command("record_exposure", "project:read")
def record_exposure(ctx: Ctx, p: dict) -> dict:
    """Record that a principal (human or model) saw evaluation information."""
    if not p.get("subject"):
        raise Invalid("subject is required")
    eid = ctx.tx.add_exposure(ctx.project_id, p.get("study_id"), ctx.principal.id, p["subject"], p.get("disclosed", {}))
    ctx.event("exposure.recorded", p["subject"], {"exposure_id": eid, "study_id": p.get("study_id")})
    return {"exposure_id": eid}
