"""Trusted-launcher commands: attempts with fencing epochs, verified publication, isolation receipts.

Only principals holding the ``launcher`` grant call these. Research workers never do: the
launcher verifies staged bytes, copies them into the content-addressed store and submits the
manifest with the attempt's epoch. A stale epoch can never publish.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eidos.application.service import Ctx, command
from eidos.boundary.wire import WireSchemaError, validate_wire
from eidos.domain.errors import ContentUnverifiable, Fenced, Invalid, NotFound
from eidos_wire import OutputManifest, WireError

TERMINAL = ("succeeded", "failed", "cancelled", "pruned")
RETRYABLE = ("infrastructure", "lost", "resource-transient")


def _job(ctx: Ctx, job_id: str) -> dict:
    job = ctx.tx.get_job(job_id, lock=True)
    if not job or job["project_id"] != ctx.project_id:
        raise NotFound(f"job {job_id} not found")
    return job


def _fence(job: dict, attempt_id: str, epoch: int) -> None:
    if job["epoch"] != epoch or job["current_attempt"] != attempt_id:
        raise Fenced(f"attempt {attempt_id} (epoch {epoch}) is not current for {job['id']}",
                     details={"current_epoch": job["epoch"], "current_attempt": job["current_attempt"]})


def _release_job_budget(ctx: Ctx, job: dict) -> None:
    scope = job["lock"].get("budget_scope")
    if scope:
        ctx.tx.settle(scope, "compute_seconds", reserved=float(job["lock"]["limits"]["wall_seconds"]), actual=0,
                      ref=job["id"])
        ctx.tx.settle(scope, "trials", reserved=1.0, actual=1.0, ref=job["id"])


def _terminal(ctx: Ctx, job: dict, status: str, info: dict) -> None:
    ctx.tx.update_job(job["id"], status=status, terminal=info)
    _release_job_budget(ctx, job)
    if job["trial_id"]:
        tr = ctx.tx.get_record("trial", job["trial_id"])
        if tr:
            ctx.tx.set_status("trial", tr.id, status)
    ctx.event(f"job.{status}", job["id"], {"kind": job["kind"], "category": info.get("error", {}).get("category")
                                            if info.get("error") else None},
              investigation_id=job["investigation_id"], wake=True)
    if job["kind"] == "evidence-arm":
        from eidos.application.handlers.evidence import maybe_request_assessment
        maybe_request_assessment(ctx, job["lock"]["plan_id"])
    if job["kind"] == "qualification" and status == "succeeded":
        ctx.tx.enqueue("qualification.finished", f"qualdone:{job['id']}",
                       {"job_id": job["id"], "project_id": ctx.project_id})


@command("start_attempt", "launcher")
def start_attempt(ctx: Ctx, p: dict) -> dict:
    job = _job(ctx, p["job_id"])
    if job["status"] in TERMINAL:
        return {"run": False, "status": job["status"]}
    if job["cancel_requested"]:
        _terminal(ctx, job, "cancelled", {"reason": "cancel requested before start", "by": "launcher"})
        return {"run": False, "status": "cancelled"}
    if job["current_attempt"]:
        prev = ctx.tx.get_attempt(job["current_attempt"], lock=True)
        if prev and prev["status"] in ("running", "starting"):
            ctx.tx.update_attempt(prev["id"], status="superseded", ended_at=datetime.now(UTC))
            ctx.event("attempt.superseded", prev["id"], {"epoch": prev["epoch"]}, investigation_id=job["investigation_id"])
    epoch = job["epoch"] + 1
    aid = f"{job['id']}_a{epoch}"
    lease = datetime.now(UTC) + timedelta(seconds=int(p.get("lease_seconds", 120)))
    ctx.tx.insert_attempt({"id": aid, "job_id": job["id"], "epoch": epoch, "status": "starting",
                           "launcher": ctx.principal.id, "backend_handle": p.get("backend_handle") or {},
                           "lease_expires_at": lease})
    ctx.tx.update_job(job["id"], status="running", epoch=epoch, current_attempt=aid)
    ctx.event("attempt.started", aid, {"job_id": job["id"], "epoch": epoch}, investigation_id=job["investigation_id"])
    from eidos.application.handlers.queries import RESTRICTED_CLASSES
    for port, inp in (job["lock"].get("inputs") or {}).items():
        art = ctx.tx.get_artifact(inp["artifact_id"])
        need = RESTRICTED_CLASSES.get(art["access_class"]) if art else None
        if need and not ctx.principal.has(need):  # this launcher may not hand restricted bytes to anyone
            _terminal(ctx, job, "failed", {"error": {"category": "isolation-unavailable",
                      "message": f"input {port} is {art['access_class']}; launcher {ctx.principal.id} lacks {need}"}})
            return {"run": False, "status": "failed"}
    inputs = {port: {**inp, "path": ctx.store.path_for(inp["digest"])} for port, inp in (job["lock"].get("inputs") or {}).items()}
    bundle = job["lock"].get("component", {}).get("bundle")
    if bundle:
        bundle = {**bundle, "path": ctx.store.path_for(bundle["digest"])}
    return {"run": True, "attempt_id": aid, "epoch": epoch, "lock": job["lock"], "inputs": inputs, "bundle": bundle,
            "investigation_id": job["investigation_id"], "kind": job["kind"]}


@command("record_attempt_handle", "launcher")
def record_attempt_handle(ctx: Ctx, p: dict) -> dict:
    job = _job(ctx, p["job_id"])
    _fence(job, p["attempt_id"], int(p["epoch"]))
    ctx.tx.update_attempt(p["attempt_id"], status="running", backend_handle=p.get("backend_handle") or {})
    return {"ok": True}


@command("heartbeat_attempt", "launcher")
def heartbeat_attempt(ctx: Ctx, p: dict) -> dict:
    job = _job(ctx, p["job_id"])
    _fence(job, p["attempt_id"], int(p["epoch"]))
    lease = datetime.now(UTC) + timedelta(seconds=int(p.get("lease_seconds", 120)))
    att = ctx.tx.get_attempt(p["attempt_id"])
    usage = {**(att["usage"] or {}), "progress": p.get("progress")}
    ctx.tx.update_attempt(p["attempt_id"], lease_expires_at=lease, usage=usage)
    control = ctx.tx.get_record("job_control", job["id"]) if job["cancel_requested"] else None
    return {"ok": True, "cancel_requested": job["cancel_requested"],
            "stop_policy": control.body["stop_policy"] if control else ("cancel" if job["cancel_requested"] else None)}


@command("publish_outputs", "launcher")
def publish_outputs(ctx: Ctx, p: dict) -> dict:
    """Publish a verified output manifest under the active epoch. Exactly one authoritative
    publication exists per (job, slot); duplicates and stale epochs leave a diagnostic event."""
    job = _job(ctx, p["job_id"])
    epoch = int(p["epoch"])
    # The published schema (schemas/eidos-wire/v1/output-manifest.schema.json), then the semantic rules.
    # A schema violation is refused only after the terminal/fence checks, so a stale attempt is still
    # reported as fenced (with its publication.rejected_stale event) whatever its manifest looks like.
    schema_error: WireSchemaError | None = None
    try:
        validate_wire("output-manifest", p["manifest"])
    except WireSchemaError as exc:
        schema_error = exc
    if schema_error is None:
        try:
            man = OutputManifest.from_dict(p["manifest"])
        except WireError as exc:
            raise Invalid(f"invalid output manifest: {exc}") from exc
        if man.job_id != job["id"] or man.attempt_id != p["attempt_id"] or man.epoch != epoch:
            raise Invalid("manifest identity does not match the attempt")
    if job["status"] in TERMINAL:
        raise Fenced(f"job {job['id']} is already {job['status']}; nothing more can be published")
    try:
        _fence(job, p["attempt_id"], epoch)
    except Fenced:
        ctx.event("publication.rejected_stale", job["id"], {"attempt_id": p["attempt_id"], "epoch": epoch,
                                                           "current_epoch": job["epoch"]},
                  investigation_id=job["investigation_id"])
        raise
    if schema_error is not None:
        raise Invalid(f"invalid output manifest: {schema_error}", details={"problems": schema_error.problems})
    # Storage can lag the database, and staged bytes can change or vanish between staging and publication.
    # Immediately before any reference is inserted, every output is verified again (size and a full rehash,
    # with a short bounded retry for lag). One unverifiable output refuses the whole publication: nothing is
    # published, and the refusal says which slots failed and why (missing, size-changed, digest-changed).
    unverifiable = []
    for out in man.outputs:
        reason = ctx.store.check_with_retry(out.digest, out.size)
        if reason is not None:
            unverifiable.append({"slot": out.slot, "digest": out.digest, "size": out.size, "reason": reason})
    if unverifiable:
        raise ContentUnverifiable(
            f"staged bytes for {[u['slot'] for u in unverifiable]} are missing or fail verification; nothing published",
            details={"unverifiable": unverifiable, "job_id": job["id"], "attempt_id": p["attempt_id"]})
    published, duplicates = [], []
    checkpoints = (man.receipt or {}).get("checkpoints", {})
    branch = (job["lock"].get("config") or {}).get("branch")
    for out in man.outputs:
        aid = f"art_{job['id'][4:]}_{out.slot}"[:120]
        existing = ctx.tx.get_artifact(aid)
        if existing is not None and (existing["digest"] != out.digest or existing["size"] != out.size
                                     or existing["producer_job"] != job["id"]):
            # Never point a publication at bytes this job did not produce (pre-registered id hijack).
            raise Invalid(f"artifact id {aid} is taken by different content; publication refused",
                          details={"slot": out.slot})
        meta = {}
        role = out.role
        declared = (job["lock"].get("outputs") or {}).get(out.slot)
        if role == "checkpoint" and declared != "eidos-checkpoint/1":
            role = "output"  # only slots the component declares as checkpoints can become branch parents
        if role == "checkpoint":
            ck = dict(checkpoints.get(out.slot) or {})
            # A checkpoint from a failed run is branchable only if its receipt vouches for it.
            ck.setdefault("complete", man.status == "succeeded")
            if man.status != "succeeded":
                ck["partial_from_failed_attempt"] = True
            if branch:
                parent = ctx.tx.get_artifact(job["lock"]["inputs"]["parent_checkpoint"]["artifact_id"])
                ck["trajectory_id"] = (parent["meta"].get("checkpoint") or {}).get("trajectory_id", parent["id"])
                ck["branch_of"] = parent["id"]
                ck["restore_class"] = branch.get("restore_class")
            else:
                ck.setdefault("trajectory_id", job["trial_id"] or job["id"])
            meta["checkpoint"] = ck
        if ctx.tx.get_artifact(aid) is None:
            ctx.tx.insert_artifact({"id": aid, "project_id": ctx.project_id, "digest": out.digest, "size": out.size,
                                    "media_type": out.media_type, "schema": out.schema, "role": role,
                                    "origin": "job-output", "producer_job": job["id"], "producer_attempt": p["attempt_id"],
                                    "access_class": job["lock"].get("output_access_class", "project"), "meta": meta})
        if ctx.tx.insert_publication(job["id"], out.slot, epoch, p["attempt_id"], aid):
            ctx.tx.add_lineage(ctx.project_id, job["id"], aid, f"output:{out.slot}")
            published.append({"slot": out.slot, "artifact_id": aid, "digest": out.digest, "role": role})
        else:
            duplicates.append(out.slot)
    if duplicates:
        ctx.event("publication.duplicate", job["id"], {"slots": duplicates, "attempt_id": p["attempt_id"]},
                  investigation_id=job["investigation_id"])
    ctx.event("evidence.ready", job["id"], {"outputs": published}, investigation_id=job["investigation_id"])
    return {"published": published, "duplicates": duplicates}


@command("finish_attempt", "launcher")
def finish_attempt(ctx: Ctx, p: dict) -> dict:
    """Close an attempt. Fenced-out attempts still record their actual resource usage."""
    job = _job(ctx, p["job_id"])
    epoch = int(p["epoch"])
    att = ctx.tx.get_attempt(p["attempt_id"], lock=True)
    if not att:
        raise NotFound("attempt not found")
    usage = p.get("usage") or {}
    if job["lock"].get("llm"):  # the attempt's LLM grant ends with it; unreported calls become uncertain spend
        from eidos.application.handlers.broker import reconcile_grant
        reconcile_grant(ctx, f"llmg_{att['id']}", reason="attempt-finished")
    scope = job["lock"].get("budget_scope")
    if scope and usage.get("compute_seconds"):
        ctx.tx.record_usage(scope, "compute_seconds", float(usage["compute_seconds"]), att["id"],
                            uncertain=bool(usage.get("uncertain")), detail={"job": job["id"], "epoch": epoch})
    status = p.get("status")
    if job["status"] in TERMINAL:  # a late report can never rewrite a terminal job (usage still recorded)
        if att["status"] not in TERMINAL + ("fenced", "lost"):
            ctx.tx.update_attempt(att["id"], status="fenced", ended_at=datetime.now(UTC), usage=usage)
        ctx.event("attempt.late_report", att["id"], {"job_status": job["status"], "reported": status},
                  investigation_id=job["investigation_id"])
        return {"fenced": True, "job_status": job["status"]}
    if job["epoch"] != epoch or job["current_attempt"] != att["id"]:
        ctx.tx.update_attempt(att["id"], status="fenced", ended_at=datetime.now(UTC), usage=usage)
        ctx.event("attempt.fenced", att["id"], {"usage": usage}, investigation_id=job["investigation_id"])
        return {"fenced": True, "job_status": job["status"]}
    ctx.tx.update_attempt(att["id"], status=status, ended_at=datetime.now(UTC), usage=usage, receipt=p.get("receipt") or {})
    error = p.get("error")
    if status == "succeeded":
        if not ctx.tx.publications(job["id"]) and job["lock"].get("outputs"):
            missing = sorted(job["lock"]["outputs"])
            _terminal(ctx, job, "failed", {"attempt_id": att["id"], "error": {"category": "contract",
                      "message": f"declared outputs {missing} were not published"}, "receipt": p.get("receipt")})
            return {"job_status": "failed"}
        _terminal(ctx, job, "succeeded", {"attempt_id": att["id"], "receipt": p.get("receipt") or {}})
        return {"job_status": "succeeded"}
    if status == "cancelled":
        _terminal(ctx, job, "cancelled", {"attempt_id": att["id"], "receipt": p.get("receipt") or {}, "error": error})
        return {"job_status": "cancelled"}
    category = (error or {}).get("category", "infrastructure")
    if category in RETRYABLE and not p.get("final", False) and not job["cancel_requested"]:
        ctx.tx.update_job(job["id"], status="queued")
        ctx.event("attempt.failed_retryable", att["id"], {"category": category}, investigation_id=job["investigation_id"])
        return {"job_status": "queued", "retry": True}
    _terminal(ctx, job, "failed", {"attempt_id": att["id"], "error": error or {}, "receipt": p.get("receipt") or {}})
    return {"job_status": "failed"}


@command("mark_job_outcome_unknown", "launcher")
def mark_job_outcome_unknown(ctx: Ctx, p: dict) -> dict:
    """A backend could not confirm whether work ran: keep possible spend reserved and visible."""
    job = _job(ctx, p["job_id"])
    if job["status"] in TERMINAL:
        return {"job_status": job["status"], "ignored": "job already terminal"}
    scope = job["lock"].get("budget_scope")
    if scope and p.get("possible_compute_seconds"):
        ctx.tx.record_usage(scope, "compute_seconds", float(p["possible_compute_seconds"]), job["id"], uncertain=True,
                            detail={"reason": p.get("reason")})
    ctx.tx.update_job(job["id"], status="reconciling")
    ctx.event("job.outcome_unknown", job["id"], {"reason": p.get("reason")}, investigation_id=job["investigation_id"])
    return {"job_status": "reconciling"}


@command("record_isolation_receipt", "launcher")
def record_isolation_receipt(ctx: Ctx, p: dict) -> dict:
    """Store the realized isolation receipt. The launcher identity is the authenticated caller,
    never a value supplied by candidate code."""
    job = _job(ctx, p["job_id"])
    _fence(job, p["attempt_id"], int(p["epoch"]))
    receipt = dict(p.get("receipt") or {})
    receipt["launcher"] = ctx.principal.id
    receipt["job_id"] = job["id"]
    receipt["job_profile"] = job["lock"].get("isolation_profile")
    # Derived by the service from the profile name, never taken from the launcher's payload.
    from eidos.domain.isolation import profile_traits
    traits = profile_traits(receipt.get("profile"))
    receipt["same_os_user"] = traits["same_os_user"]
    receipt["hostile_code_isolation"] = traits["hostile_code_isolation"]
    if ctx.tx.get_record("isolation_receipt", p["attempt_id"]) is not None:  # write-once per attempt
        raise Invalid(f"isolation receipt for {p['attempt_id']} already recorded")
    rec = ctx.tx.put_record("isolation_receipt", p["attempt_id"], ctx.project_id, receipt, created_by=ctx.principal.id,
                            status="recorded", parent_id=job["id"])
    return {"receipt_id": rec.id, "digest": rec.digest}


@command("abandon_unstarted_job", "launcher")
def abandon_unstarted_job(ctx: Ctx, p: dict) -> dict:
    """Retries exhausted before any attempt started: terminalize the job and release its reservation."""
    job = _job(ctx, p["job_id"])
    if job["status"] in TERMINAL or job["current_attempt"]:
        return {"job_status": job["status"], "ignored": True}
    _terminal(ctx, job, "failed", {"error": {"category": "infrastructure",
              "message": str(p.get("reason", "could not start any attempt"))[:1000]}})
    return {"job_status": "failed"}
