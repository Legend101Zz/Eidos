"""Investigations, steering, pause/resume and bounded investigator turns."""

from __future__ import annotations

import uuid

from eidos.application.service import Ctx, command
from eidos.domain import charter as charter_rules
from eidos.domain import turns as turn_rules
from eidos.domain.errors import BudgetExhausted, Invalid, StaleRevision

RUNNING_JOB_POLICIES = ("drain", "checkpoint-and-stop", "cancel")
BUDGET_DIMS = ("model_tokens", "model_usd", "compute_seconds", "trials", "turns")


def inv_scope(inv_id: str) -> str:
    return f"inv:{inv_id}"


def confirm_scope(inv_id: str) -> str:
    return f"confirm:{inv_id}"


def attach_charter(ctx: Ctx, inv_id: str, charter_id: str, revision: int, digest: str) -> None:
    inv = ctx.tx.require("investigation", inv_id, lock=True)
    body = dict(inv.body)
    body["charter_ref"] = {"id": charter_id, "revision": revision, "digest": digest}
    ctx.tx.put_record("investigation", inv_id, ctx.project_id, body, created_by=ctx.principal.id, status=inv.status)
    ctx.event("investigation.charter_attached", inv_id, {"charter_id": charter_id, "revision": revision},
              investigation_id=inv_id, wake=True)


@command("start_investigation", "investigate")
def start_investigation(ctx: Ctx, p: dict) -> dict:
    """Create a durable investigation and ask the relay to start its coordinator workflow."""
    if not p.get("question"):
        raise Invalid("question is required")
    inv_id = p.get("investigation_id") or f"inv_{uuid.uuid4().hex[:12]}"
    if ctx.tx.get_record("investigation", inv_id):
        raise Invalid(f"investigation {inv_id} already exists")
    limits = dict(p.get("limits") or {})
    investigator = {"runtime": "fixture", "model": None, "context_policy": "context-policy/1",
                    "max_calls_per_turn": 6, "max_model_calls_per_turn": 4, "max_turns": 40,
                    **(p.get("investigator") or {})}
    body = {"question": p["question"], "model_artifact_id": p.get("model_artifact_id"),
            "environment": p.get("environment", "default"), "charter_ref": None, "limits": limits,
            "investigator": investigator, "steering": [], "confirmation_envelope": p.get("confirmation_envelope", {}),
            "notes": p.get("notes", ""), "inputs": dict(p.get("inputs") or {}),
            "task": p.get("task")}
    charter_id = p.get("charter_id")
    if charter_id:
        ch = ctx.tx.require("charter", charter_id)
        body["charter_ref"] = {"id": ch.id, "revision": ch.revision, "digest": ch.digest}
    rec = ctx.tx.put_record("investigation", inv_id, ctx.project_id, body, created_by=ctx.principal.id, status="active")
    for dim in BUDGET_DIMS:
        if dim in limits:
            ctx.tx.ensure_budget(inv_scope(inv_id), ctx.project_id, dim, float(limits[dim]))
    for dim, v in (p.get("confirmation_envelope") or {}).items():
        ctx.tx.ensure_budget(confirm_scope(inv_id), ctx.project_id, dim, float(v))
    seq = ctx.event("investigation.started", inv_id, {"question": p["question"]}, investigation_id=inv_id)
    ctx.tx.enqueue("investigation.start", f"start:{inv_id}", {"investigation_id": inv_id, "project_id": ctx.project_id}, seq)
    return {"investigation_id": inv_id, "revision": rec.revision}


@command("steer_investigation", "steer")
def steer_investigation(ctx: Ctx, p: dict) -> dict:
    """Append an attributed instruction effective at the next turn. Steering cannot change success
    rules: those require ``amend_study_charter``."""
    charter_rules.reject_threshold_overrides("steering", p.get("structured") or {})
    text_ = str(p.get("instruction", "")).strip()
    if not text_:
        raise Invalid("instruction is required")
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    body = dict(inv.body)
    entry = {"by": ctx.principal.id, "instruction": text_[:4000], "structured": p.get("structured") or {},
             "effective": "next-turn", "after_revision": inv.revision}
    body["steering"] = [*body.get("steering", []), entry]
    rec = ctx.tx.put_record("investigation", inv.id, ctx.project_id, body, created_by=ctx.principal.id,
                            status=inv.status, expected_revision=ctx.expected_revision)
    ctx.event("investigation.steered", inv.id, {"revision": rec.revision, "by": ctx.principal.id},
              investigation_id=inv.id, wake=True)
    return {"investigation_id": inv.id, "revision": rec.revision}


def _set_state(ctx: Ctx, p: dict, status: str, etype: str) -> dict:
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    policy = p.get("running_job_policy", "drain")
    if policy not in RUNNING_JOB_POLICIES:
        raise Invalid(f"running_job_policy must be one of {RUNNING_JOB_POLICIES}")
    if ctx.expected_revision is not None and inv.revision != ctx.expected_revision:
        raise StaleRevision(f"investigation at revision {inv.revision}", details={"observed_revision": inv.revision})
    body = dict(inv.body)
    body["control"] = {"status": status, "running_job_policy": policy, "by": ctx.principal.id}
    rec = ctx.tx.put_record("investigation", inv.id, ctx.project_id, body, created_by=ctx.principal.id, status=status)
    affected = []
    if status in ("paused", "stopped") and policy != "drain":
        for job in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id,
                                    statuses=["admitted", "queued", "running"]):
            ctx.tx.update_job(job["id"], cancel_requested=True)
            ctx.tx.enqueue("job.cancel", f"cancel:{job['id']}", {"job_id": job["id"], "policy": policy})
            affected.append(job["id"])
    ctx.event(etype, inv.id, {"policy": policy, "affected_jobs": affected}, investigation_id=inv.id, wake=True)
    return {"investigation_id": inv.id, "revision": rec.revision, "status": status, "running_job_policy": policy,
            "affected_jobs": affected}


@command("pause_investigation", "control")
def pause_investigation(ctx: Ctx, p: dict) -> dict:
    return _set_state(ctx, p, "paused", "investigation.paused")


@command("resume_investigation", "control")
def resume_investigation(ctx: Ctx, p: dict) -> dict:
    return _set_state(ctx, {**p, "running_job_policy": "drain"}, "active", "investigation.resumed")


@command("stop_investigation", "control")
def stop_investigation(ctx: Ctx, p: dict) -> dict:
    return _set_state(ctx, p, "stopped", "investigation.stopped")


# ---------------------------------------------------------------------------------------------
# Turns: TurnSpec committed before context; decisions accepted before effects.
# ---------------------------------------------------------------------------------------------

@command("begin_turn", "turn")
def begin_turn(ctx: Ctx, p: dict) -> dict:
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    if inv.status != "active":
        return {"started": False, "reason": f"investigation is {inv.status}"}
    last = ctx.tx.last_turn(inv.id)
    if last and last["status"] == "open":
        # A turn left open by a terminated child is resumed, never duplicated.
        return {"started": True, "resumed": True, "turn_id": last["id"], "spec": last["spec"]}
    seq = (last["seq"] + 1) if last else 1
    cfg = inv.body["investigator"]
    if seq > int(cfg.get("max_turns", 40)):
        return {"started": False, "reason": "max-turns-reached"}
    budgets = ctx.tx.budgets(inv_scope(inv.id))
    for dim, b in budgets.items():
        if dim in ("model_tokens", "model_usd") and b["used"] + b["reserved"] >= b["limit_amount"]:
            return {"started": False, "reason": "budget-exhausted", "dimension": dim}
    if "turns" in budgets:
        ctx.tx.reserve(inv_scope(inv.id), "turns", 1, f"turn:{inv.id}:{seq}")
    tid = f"turn_{inv.id}_{seq}"
    spec = {
        "project_id": ctx.project_id, "investigation_id": inv.id, "turn_id": tid, "seq": seq,
        "study_revision": inv.revision, "charter_ref": inv.body.get("charter_ref"),
        "runtime": {"runtime": cfg["runtime"], "model": cfg.get("model"), "settings": cfg.get("settings", {}),
                    "runtime_release": p.get("runtime_release")},
        "context_policy": cfg.get("context_policy", "context-policy/1"),
        "limits": {"max_calls": int(cfg.get("max_calls_per_turn", 6)),
                   "max_model_calls": int(cfg.get("max_model_calls_per_turn", 4)),
                   "deadline_seconds": int(cfg.get("turn_deadline_seconds", 600))},
        "budgets": budgets,
        "tool_vocabulary": sorted(turn_rules.EFFECT_TOOLS) + list(turn_rules.CONTROL_TOOLS),
        "exposure_policy": p.get("exposure_policy", "development-feedback-allowed"),
    }
    horizon = ctx.tx.horizon()
    spec["horizon"] = horizon
    ctx.tx.insert_turn({"id": tid, "investigation_id": inv.id, "project_id": ctx.project_id, "seq": seq,
                        "status": "open", "study_revision": inv.revision, "horizon": horizon, "spec": spec})
    ctx.event("turn.started", tid, {"seq": seq, "horizon": horizon}, investigation_id=inv.id)
    return {"started": True, "turn_id": tid, "spec": spec}


@command("record_turn_context", "turn")
def record_turn_context(ctx: Ctx, p: dict) -> dict:
    """Store the exact packed context and authorized source manifest used for this turn."""
    turn = ctx.tx.get_turn(p["turn_id"])
    if not turn:
        raise Invalid("unknown turn")
    body = {k: p.get(k) for k in ("context_artifact_id", "source_manifest_digest", "source_manifest_artifact_id",
                                  "policy", "selections", "omissions", "truncations", "catalog_snapshot_digest",
                                  "horizon", "chars")}
    rec = ctx.tx.put_record("turn_context", p["turn_id"], ctx.project_id, body, created_by=ctx.principal.id,
                            status="recorded")
    return {"turn_id": p["turn_id"], "revision": rec.revision}


@command("accept_decision", "turn")
def accept_decision(ctx: Ctx, p: dict) -> dict:
    """Persist a proposed decision. Exactly one decision is accepted per (turn, slot); a late or
    stale proposal is kept as an unapplied record with no authority to execute tools."""
    turn = ctx.tx.get_turn(p["turn_id"], lock=True)
    if not turn:
        raise Invalid("unknown turn")
    slot = int(p.get("slot", 0))
    existing = ctx.tx.accepted_decision(turn["id"], slot)
    did = f"dec_{uuid.uuid4().hex[:16]}"
    usage = p.get("usage") or {}
    _charge_model_usage(ctx, turn["investigation_id"], usage, ref=did)
    if existing:
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"proposal": p.get("proposal"), "reason": "slot-taken",
                                                               "usage": usage})
        return {"accepted": False, "decision_id": existing["id"], "reason": "slot-already-accepted",
                "calls": existing["body"]["calls"], "control": existing["body"].get("control")}
    inv = ctx.tx.require("investigation", turn["investigation_id"])
    if turn["status"] != "open":
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"proposal": p.get("proposal"), "reason": "turn-closed"})
        return {"accepted": False, "decision_id": did, "reason": "turn-closed"}
    if inv.revision != turn["study_revision"]:
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"proposal": p.get("proposal"), "reason": "stale-revision",
                                                               "observed_revision": inv.revision})
        ctx.event("decision.unapplied", did, {"reason": "stale-revision"}, investigation_id=inv.id)
        return {"accepted": False, "decision_id": did, "reason": "stale-revision", "observed_revision": inv.revision}
    try:
        decision = turn_rules.validate_decision(p.get("proposal") or {}, max_calls=turn["spec"]["limits"]["max_calls"])
    except Invalid as exc:
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"proposal": p.get("proposal"), "reason": "invalid",
                                                               "error": exc.to_dict()})
        return {"accepted": False, "decision_id": did, "reason": "invalid", "error": exc.to_dict()}
    calls = turn_rules.keyed_calls(ctx.project_id, turn["id"], did, decision["calls"])
    body = {**decision, "calls": calls, "model": p.get("model_settings") or {}, "usage": usage,
            "response_artifact_id": p.get("response_artifact_id"), "prompt_revision": p.get("prompt_revision")}
    ctx.tx.insert_decision(did, turn["id"], slot, True, body)
    ctx.event("decision.accepted", did, {"turn_id": turn["id"], "slot": slot, "calls": [c["tool"] for c in calls],
                                         "control": decision["control"]}, investigation_id=inv.id)
    return {"accepted": True, "decision_id": did, "calls": calls, "control": decision["control"]}


def _charge_model_usage(ctx: Ctx, inv_id: str, usage: dict, *, ref: str) -> None:
    scope = inv_scope(inv_id)
    budgets = ctx.tx.budgets(scope)
    uncertain = bool(usage.get("uncertain"))
    detail = {k: usage.get(k) for k in ("provider", "model", "request_ids", "calls")}
    tokens = float(usage.get("input_tokens", 0)) + float(usage.get("output_tokens", 0))
    if tokens and "model_tokens" in budgets:
        ctx.tx.record_usage(scope, "model_tokens", tokens, ref, uncertain=uncertain, detail=detail)
    if usage.get("usd") and "model_usd" in budgets:
        ctx.tx.record_usage(scope, "model_usd", float(usage["usd"]), ref, uncertain=uncertain, detail=detail)


@command("record_model_usage", "turn")
def record_model_usage(ctx: Ctx, p: dict) -> dict:
    """Record model usage that is not attached to an accepted decision (failed/timeout calls).
    A timeout after possible charge is recorded as possible usage with its request ID."""
    turn = ctx.tx.get_turn(p["turn_id"])
    if not turn:
        raise Invalid("unknown turn")
    _charge_model_usage(ctx, turn["investigation_id"], p.get("usage") or {}, ref=f"usage:{p['turn_id']}")
    ctx.event("model.usage", p["turn_id"], {"usage": p.get("usage")}, investigation_id=turn["investigation_id"])
    return {"recorded": True}


@command("complete_turn", "turn")
def complete_turn(ctx: Ctx, p: dict) -> dict:
    turn = ctx.tx.get_turn(p["turn_id"], lock=True)
    if not turn:
        raise Invalid("unknown turn")
    if turn["status"] != "open":
        return {"turn_id": turn["id"], "status": turn["status"], "already_closed": True}
    reason = p.get("stop_reason", "calls-exhausted")
    if reason not in turn_rules.STOP_REASONS:
        raise Invalid(f"stop_reason must be one of {turn_rules.STOP_REASONS}")
    status = "cancelled" if reason == "cancelled" else ("failed" if reason == "error" else "completed")
    result = {"stop_reason": reason, "summary": str(p.get("summary", ""))[:4000], "receipts": p.get("receipts", []),
              "unfinished_handles": p.get("unfinished_handles", []), "control": p.get("control")}
    ctx.tx.update_turn(turn["id"], status=status, result=result)
    inv_id = turn["investigation_id"]
    budgets = ctx.tx.budgets(inv_scope(inv_id))
    if "turns" in budgets:
        ctx.tx.settle(inv_scope(inv_id), "turns", reserved=1, actual=1, ref=turn["id"])
    control = p.get("control") or {}
    if control.get("action") in ("conclude", "stop"):
        inv = ctx.tx.require("investigation", inv_id, lock=True)
        body = dict(inv.body)
        body["conclusion"] = {"turn_id": turn["id"], "summary": control.get("summary") or result["summary"],
                              "scope": control.get("scope"), "action": control["action"]}
        ctx.tx.put_record("investigation", inv_id, ctx.project_id, body, created_by=ctx.principal.id,
                          status="concluded")
    ctx.event("turn.completed", turn["id"], {"stop_reason": reason, "status": status}, investigation_id=inv_id)
    return {"turn_id": turn["id"], "status": status}


def check_exploration_allowed(ctx: Ctx, inv_id: str | None) -> dict | None:
    """Exploratory admission for an investigation requires a committed charter."""
    if not inv_id:
        return None
    from eidos.domain.errors import CharterRequired
    inv = ctx.tx.require("investigation", inv_id)
    if inv.status not in ("active",):
        raise Invalid(f"investigation {inv_id} is {inv.status}; no new work is admitted")
    if not inv.body.get("charter_ref"):
        raise CharterRequired(f"investigation {inv_id} has no committed study charter")
    return inv.body["charter_ref"]


def ensure_budget_left(ctx: Ctx, scope: str, dim: str, amount: float) -> None:
    b = ctx.tx.budgets(scope).get(dim)
    if b and b["reserved"] + b["used"] + amount > b["limit_amount"]:
        raise BudgetExhausted(f"{dim} budget exhausted for {scope}", details={"scope": scope, **b})


@command("record_decision_receipts", "turn")
def record_decision_receipts(ctx: Ctx, p: dict) -> dict:
    """Store the ordered receipts of an accepted decision's calls (audit + next-slot context)."""
    dec = ctx.tx.get_decision(p["decision_id"])
    if not dec or not dec["accepted"]:
        raise Invalid("receipts can only be recorded for an accepted decision")
    rec = ctx.tx.put_record("decision_receipts", dec["id"], ctx.project_id,
                            {"turn_id": dec["turn_id"], "receipts": p.get("receipts", [])},
                            created_by=ctx.principal.id, status="recorded")
    return {"decision_id": dec["id"], "revision": rec.revision}


@command("record_note", "propose")
def record_note(ctx: Ctx, p: dict) -> dict:
    """Investigation notebook entry: hypotheses, disconfirming predictions, observations with
    artifact references. Summaries are views; numbers should cite ledger rows or artifacts."""
    inv_id = p.get("investigation_id")
    if inv_id:
        ctx.tx.require("investigation", inv_id)
    nid = f"note_{uuid.uuid4().hex[:12]}"
    body = {k: p.get(k) for k in ("investigation_id", "kind", "text", "hypotheses", "evidence_refs", "prediction",
                                  "falsifier", "status")}
    body["text"] = str(body.get("text") or "")[:4000]
    ctx.tx.put_record("note", nid, ctx.project_id, body, created_by=ctx.principal.id, status=p.get("status") or "open",
                      parent_id=inv_id)
    ctx.event("note.recorded", nid, {"kind": p.get("kind")}, investigation_id=inv_id)
    return {"note_id": nid}
