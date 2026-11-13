"""Investigations, steering, pause/resume and bounded investigator turns."""

from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from typing import Any

from eidos.application.handlers import exploration
from eidos.application.ports import Record
from eidos.application.service import EFFECT_ADMISSION, Ctx, command
from eidos.domain import charter as charter_rules
from eidos.domain import exploration as exploration_rules
from eidos.domain import turns as turn_rules
from eidos.domain.contracts import validate_config, validate_shape_schema
from eidos.domain.errors import BudgetExhausted, EffectNotAdmitted, Invalid, QuestionRevised, StaleRevision
from eidos.domain.redaction import redact_value

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
    investigator = {"runtime": "fixture", "model": None, "context_policy": "context-policy/3",
                    "max_calls_per_turn": 6, "max_model_calls_per_turn": 4, "max_turns": 40,
                    "finalization": "autonomous", **(p.get("investigator") or {})}
    if "conclusion_scope_schema" in investigator:
        problems = validate_shape_schema(investigator["conclusion_scope_schema"], "investigator.conclusion_scope_schema")
        if problems:
            raise Invalid("invalid conclusion scope schema", details={"problems": problems})
    if investigator["finalization"] not in ("autonomous", "operator"):
        raise Invalid("investigator.finalization must be autonomous or operator")
    if "model_usd" in limits and investigator.get("runtime") != "fixture" \
            and not (investigator.get("settings") or {}).get("price_per_mtok"):
        # Without prices no decision reserves or reports USD, so the limit would never apply (v1.0.0 review).
        raise Invalid("a model_usd limit needs investigator.settings.price_per_mtok ({\"input\": .., \"output\": ..} "
                      "USD per million tokens); without it the USD limit could not be enforced",
                      details={"limits": limits})
    body = {"question": p["question"], "model_artifact_id": p.get("model_artifact_id"),
            "environment": p.get("environment", "default"), "charter_ref": None, "limits": limits,
            "investigator": investigator, "steering": [], "confirmation_envelope": p.get("confirmation_envelope", {}),
            "notes": p.get("notes", ""), "inputs": dict(p.get("inputs") or {}),
            "task": p.get("task")}
    absolute_deadline = investigator.get("wall_deadline_unix")
    if investigator.get("wall_seconds") or absolute_deadline is not None:
        wall = float(investigator.get("wall_seconds") or 0)
        deadline = time.time() + wall
        if absolute_deadline is not None:
            try:
                absolute_deadline = float(absolute_deadline)
            except (TypeError, ValueError) as exc:
                raise Invalid("investigator.wall_deadline_unix must be a finite timestamp") from exc
            if not math.isfinite(absolute_deadline):
                raise Invalid("investigator.wall_deadline_unix must be a finite timestamp")
            if not math.isfinite(wall) or wall <= 0:
                raise Invalid("an absolute wall deadline requires positive finite investigator.wall_seconds")
            deadline = min(deadline, absolute_deadline)
        body["wall"] = {"wall_seconds": wall, "deadline_unix": deadline,
                        "conclude_threshold_seconds": turn_rules.conclude_threshold(
                            wall, investigator.get("conclude_threshold_seconds"))}
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
    _clear_needs_input(ctx, inv, inv.status, "steered")
    ctx.event("investigation.steered", inv.id, {"revision": rec.revision, "by": ctx.principal.id},
              investigation_id=inv.id, wake=True)
    return {"investigation_id": inv.id, "revision": rec.revision}


@command("revise_question", "steer")
def revise_question(ctx: Ctx, p: dict) -> dict:
    """The human changes the investigation's question mid-investigation (design, "Human changes the question").

    Policy (ADR 0010):
    - The change is a new investigation revision with an attributed question history entry. It is effective at
      the NEXT turn: a turn already running keeps the view at its horizon, and a decision it proposes after the
      change is recorded unapplied (``stale-revision``). It never sees the new question mid-turn.
    - Admitted work follows ``running_job_policy``: ``drain`` (default; jobs keep running and their results stay
      citable, labelled with the question revision they were admitted under), ``checkpoint-and-stop`` or
      ``cancel``. Under a stopping policy the remaining calls of a decision accepted before the change are
      refused (``QUESTION_REVISED``) instead of admitting new work for the old question.
    - The change is recorded with exposure: what the reviser could have seen (the ledger horizon, finished jobs,
      completed turns, assessments) is stored as an exposure event linked to the question revision.
    Success rules are not changed here; they need ``amend_study_charter``."""
    charter_rules.reject_threshold_overrides("question revision", p.get("structured") or {})
    question = str(p.get("question", "")).strip()
    if not question:
        raise Invalid("question is required")
    policy = p.get("running_job_policy", "drain")
    if policy not in RUNNING_JOB_POLICIES:
        raise Invalid(f"running_job_policy must be one of {RUNNING_JOB_POLICIES}")
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    if inv.status == "stopped":
        raise Invalid(f"investigation {inv.id} is stopped")
    if ctx.expected_revision is not None and inv.revision != ctx.expected_revision:
        raise StaleRevision(f"investigation at revision {inv.revision}", details={"observed_revision": inv.revision})
    if question == inv.body.get("question"):
        raise Invalid("the new question is identical to the current one")
    horizon = ctx.tx.horizon()
    jobs = ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id)
    finished = sorted(j["id"] for j in jobs if j["status"] in ("succeeded", "failed", "cancelled", "pruned"))
    live = [j for j in jobs if j["status"] in ("admitted", "queued", "running", "cancel-requested")]
    turns_done = [t["id"] for t in ctx.tx.list_turns(inv.id) if t["status"] != "open"]
    plans = {pl.id for pl in ctx.tx.list_records(ctx.project_id, "evidence_plan", limit=100000)
             if pl.body.get("investigation_id") == inv.id}
    assessments = sorted(a.id for a in ctx.tx.list_records(ctx.project_id, "assessment", limit=100000)
                         if a.body.get("plan_id") in plans)
    disclosed = {"horizon": horizon, "finished_jobs": finished, "completed_turns": turns_done,
                 "assessments": assessments, "kind": "question-revision"}
    study_id = None
    if inv.body.get("charter_ref"):
        ch = ctx.tx.get_record("charter", inv.body["charter_ref"]["id"])
        study_id = (ch.body.get("study_id") if ch else None)
    exposure_id = ctx.tx.add_exposure(ctx.project_id, study_id, ctx.principal.id, f"question:{inv.id}", disclosed)
    history = list(inv.body.get("question_history") or [])
    if not history:
        history.append({"question_revision": 1, "question": inv.body.get("question"), "by": inv.created_by})
    qrev = history[-1]["question_revision"] + 1
    entry = {"question_revision": qrev, "question": question[:4000], "previous": inv.body.get("question"),
             "by": ctx.principal.id, "reason": str(p.get("reason", ""))[:2000], "effective": "next-turn",
             "after_revision": inv.revision, "running_job_policy": policy, "exposure_id": exposure_id,
             "horizon": horizon}
    history.append(entry)
    body = dict(inv.body)
    body["question"] = question[:4000]
    body["question_history"] = history
    # Turns that began before this revision are stale (their spec carries an older study_revision).
    body["question_change"] = {"question_revision": qrev, "investigation_revision": inv.revision + 1,
                               "policy": policy}
    rec = ctx.tx.put_record("investigation", inv.id, ctx.project_id, body, created_by=ctx.principal.id,
                            status=inv.status)
    if rec.revision != inv.revision + 1:  # put_record appends exactly one revision under the row lock
        raise Invalid("investigation revision moved concurrently; retry")
    affected: list[str] = []
    for job in live:  # admitted under the previous question revision (recorded in the event and the history)
        if policy != "drain" and job["status"] != "cancel-requested":
            from eidos.application.handlers.runs import record_stop_policy
            record_stop_policy(ctx, job["id"], policy)
            ctx.tx.update_job(job["id"], cancel_requested=True)
            ctx.tx.enqueue("job.cancel", f"cancel:{job['id']}", {"job_id": job["id"], "policy": policy})
        affected.append(job["id"])
    if policy != "drain":  # ADR 0022 addendum R11: a plan's steps not yet admitted are ended like the jobs it had admitted
        from eidos.application.handlers.evidence import reconsider_plans
        reconsider_plans(ctx, inv.id, halt="the question was revised with a stopping policy")
    _clear_needs_input(ctx, inv, inv.status, "question-revised")
    ctx.event("investigation.question_revised", inv.id,
              {"question_revision": qrev, "revision": rec.revision, "by": ctx.principal.id, "policy": policy,
               "admitted_work": affected, "admitted_under_question_revision": qrev - 1,
               "exposure_id": exposure_id}, investigation_id=inv.id, wake=True)
    return {"investigation_id": inv.id, "revision": rec.revision, "question_revision": qrev,
            "running_job_policy": policy, "admitted_work": affected, "exposure_id": exposure_id,
            "effective": "next-turn"}


def _set_state(ctx: Ctx, p: dict, status: str, etype: str, *, controller_abstention: dict | None = None) -> dict:
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    policy = p.get("running_job_policy", "drain")
    if policy not in RUNNING_JOB_POLICIES:
        raise Invalid(f"running_job_policy must be one of {RUNNING_JOB_POLICIES}")
    if ctx.expected_revision is not None and inv.revision != ctx.expected_revision:
        raise StaleRevision(f"investigation at revision {inv.revision}", details={"observed_revision": inv.revision})
    body = dict(inv.body)
    body["control"] = {"status": status, "running_job_policy": policy, "by": ctx.principal.id}
    if status == "active":
        body.pop("controller_abstention", None)
    elif controller_abstention is not None:
        body["controller_abstention"] = controller_abstention
    rec = ctx.tx.put_record("investigation", inv.id, ctx.project_id, body, created_by=ctx.principal.id, status=status)
    _clear_needs_input(ctx, inv, status, etype.split(".")[-1])
    affected = []
    if status in ("paused", "stopped") and policy != "drain":
        for job in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id,
                                    statuses=["admitted", "queued", "running"]):
            from eidos.application.handlers.runs import record_stop_policy
            record_stop_policy(ctx, job["id"], policy)
            ctx.tx.update_job(job["id"], cancel_requested=True)
            ctx.tx.enqueue("job.cancel", f"cancel:{job['id']}", {"job_id": job["id"], "policy": policy})
            affected.append(job["id"])
    if status in ("stopped", "active"):  # ADR 0022 addendum R11: a plan's unadmitted steps end, or may continue
        from eidos.application.handlers.evidence import reconsider_plans
        reconsider_plans(ctx, inv.id, halt="the investigation was stopped" if status == "stopped" else None)
    ctx.event(etype, inv.id, {"policy": policy, "affected_jobs": affected}, investigation_id=inv.id, wake=True)
    return {"investigation_id": inv.id, "revision": rec.revision, "status": status, "running_job_policy": policy,
            "affected_jobs": affected}


@command("pause_investigation", "control")
def pause_investigation(ctx: Ctx, p: dict) -> dict:
    return _set_state(ctx, p, "paused", "investigation.paused")


def _clear_needs_input(ctx: Ctx, inv, status: str, action: str) -> None:
    if (inv.labels or {}).get("needs_input"):
        ctx.tx.set_status("investigation", inv.id, status, labels={"needs_input": None})
        ctx.event("investigation.input_received", inv.id, {"by": ctx.principal.id, "action": action,
                  "was": inv.labels["needs_input"]}, investigation_id=inv.id)


NEEDS_INPUT_CODES = ("refused-proposals", "coordinator-stopped", "turn-failed", "provider-unavailable", "finalization-failed", "runtime-bounded")
NEEDS_INPUT_DETAILS_BYTES = 2000


def _bounded_details(details: Any) -> dict:
    """Machine-readable details of a needs-input mark or a turn failure: a JSON object of bounded size."""
    if not isinstance(details, dict):
        return {}
    if len(json.dumps(details, default=str)) <= NEEDS_INPUT_DETAILS_BYTES:
        return details
    return {"truncated": True, "preview": json.dumps(details, default=str)[:NEEDS_INPUT_DETAILS_BYTES]}


def _record_controller_abstention(ctx: Ctx, inv, *, code: str, reason: str, failure: Any,
                                  incumbent: dict | None, export_receipt: dict | None = None) -> dict | None:
    """Close the default run without writing an investigator conclusion."""
    if inv.status != "active" or inv.body["investigator"].get("finalization") == "operator":
        return inv.body.get("controller_abstention")
    abstention = {"schema": "controller-abstention/1", "author": "controller", "code": code,
                  "reason": reason, "failure": failure, "incumbent": incumbent,
                  "investigator_concluded": False, "export_receipt": export_receipt}
    _set_state(ctx, {"investigation_id": inv.id, "running_job_policy": "cancel"}, "stopped", "investigation.stopped",
               controller_abstention=abstention)
    ctx.event("investigation.controller_abstained", inv.id, abstention, investigation_id=inv.id)
    return abstention


@command("mark_needs_input", "turn")
def mark_needs_input(ctx: Ctx, p: dict) -> dict:
    """The coordinator surfaces an investigation it will not continue on its own: durable and visible (investigation
    label, event) until an operator steers, resumes or stops it. ``code`` says why, for clients and operators
    (``NEEDS_INPUT_CODES``: refused proposals after bounded retries, the coordinator stopped on a limit, or a turn
    failed with a non-retryable error or after its bounded automatic retries, ADR 0019); ``details`` is a bounded
    JSON object (for a failed turn: the turn, the error type and message, retryable, attempts)."""
    code = p.get("code") or ("refused-proposals" if int(p.get("refused_turns", 0) or 0) else "coordinator-stopped")
    if code not in NEEDS_INPUT_CODES:
        raise Invalid(f"code must be one of {NEEDS_INPUT_CODES}")
    if code in ("turn-failed", "provider-unavailable"):  # F5 (A2): a turn the failed child left open is closed and settled
        close_abandoned_turn(ctx, p["investigation_id"], (p.get("details") or {}).get("failure"))
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    if inv.body.get("controller_abstention"):
        return {"investigation_id": inv.id, "controller_abstention": inv.body["controller_abstention"]}
    if p.get("terminal"):
        if inv.status != "active":
            return {"investigation_id": inv.id, "already": True}
        if inv.body["investigator"].get("finalization") != "operator":
            details = p.get("details") or {}
            abstention = _record_controller_abstention(
                ctx, inv, code=code, reason=str(p.get("reason", ""))[:300],
                failure=_bounded_details(redact_value(details.get("failure") or details)),
                incumbent=exploration.final_incumbent(ctx, inv))
            return {"investigation_id": inv.id, "controller_abstention": abstention}
    if (inv.labels or {}).get("needs_input"):
        # F5 (A1): every needs-input wait gets its own cursor. An operator event that ended an earlier wait without
        # clearing the mark (e.g. a charter amendment) is behind it and never counts as news again.
        mark = {**inv.labels["needs_input"], "cursor": ctx.tx.horizon()}
        ctx.tx.set_status("investigation", inv.id, inv.status, labels={"needs_input": mark})
        return {"investigation_id": inv.id, "needs_input": mark, "already": True, "cursor": mark["cursor"]}
    from datetime import UTC, datetime
    mark = {"since": datetime.now(UTC).isoformat(), "code": code, "reason": str(p.get("reason", ""))[:300],
            "refused_turns": int(p.get("refused_turns", 0)), "details": _bounded_details(p.get("details"))}
    seq = ctx.event("investigation.needs_input", inv.id, mark, investigation_id=inv.id)
    # F5 (A1): the coordinator waits only for operator events after this cursor (the mark's own event), not after
    # the last turn's horizon, which does not move while turn workflows fail before creating a turn.
    mark = {**mark, "cursor": seq}
    ctx.tx.set_status("investigation", inv.id, inv.status, labels={"needs_input": mark})
    return {"investigation_id": inv.id, "needs_input": mark, "already": False, "cursor": seq}


@command("reconcile_finalization", "turn")
def reconcile_finalization(ctx: Ctx, p: dict) -> dict:
    """Recover an abandoned conclude-only child before ordinary retry/resumption (including a new coordinator).

    Pending recovery has no child to abandon. Running recovery is closed through the same atomic turn/receipt
    transaction as a normal failure. Finished recovery is returned idempotently after an activity/worker restart.
    """
    final = ctx.tx.get_record(FINALIZATION_KIND, p["investigation_id"])
    if final is None:
        return {"finalization": False}
    inv_id = p["investigation_id"]
    inv = ctx.tx.require("investigation", inv_id)
    if final.status == "finished" and inv.status == "active" and (inv.body.get("control") or {}).get("status") == "active":
        return {"finalization": False}  # explicit operator resume after the one-shot attempt
    turn_id = final.body.get("turn_id")
    if not turn_id and final.status != "finished":
        if not p.get("failure"):
            return {"finalization": False}  # pending admission, no failed child
        inv = ctx.tx.require("investigation", inv_id, lock=True)
        _fail_pending_finalization(ctx, inv, "child-failed-before-begin-turn", p["failure"])
    elif turn_id:
        turn = _turn(ctx, turn_id, lock=True)
        if turn["status"] == "open":
            failure = p.get("failure") or {"error_type": "coordinator-restarted", "retryable": False,
                                           "message": "active finalization child was abandoned"}
            close_abandoned_turn(ctx, inv_id, failure)
    receipt = ctx.tx.get_record("finalization_export_receipt", inv_id)
    body = receipt.body if receipt else {}
    return {"finalization": True, "turn_id": turn_id,
            "investigator_concluded": bool(body.get("investigator_concluded")), "stop_reason": body.get("stop_reason"),
            "export_receipt": {"kind": "finalization_export_receipt", "id": inv_id},
            **({"controller_abstention": body["controller_abstention"]} if body.get("controller_abstention") else {})}


@command("record_turn_retry", "turn")
def record_turn_retry(ctx: Ctx, p: dict) -> dict:
    """The coordinator schedules a bounded automatic retry after a turn failed with a retryable error (ADR 0019):
    recorded before the backoff starts, so the wait is never silent (event ``investigation.turn_retry_scheduled``).
    The retried turn is an ordinary turn: it reserves the turns budget and its decisions reserve model budget."""
    # F5 (A2): a turn workflow that failed (e.g. its execution timeout) can leave its turn open. The retry must be a
    # new turn that reserves the turns budget, not a resumption of the abandoned one: close and settle it first.
    closed = close_abandoned_turn(ctx, p["investigation_id"], p.get("failure"))
    inv = ctx.tx.require("investigation", p["investigation_id"])
    body = {"failed_turn_id": p.get("failed_turn_id") or closed, "attempt": int(p.get("attempt", 0)),
            "max_attempts": int(p.get("max_attempts", 0)), "delay_seconds": float(p.get("delay_seconds", 0)),
            "failure": _bounded_details(p.get("failure")), **({"closed_abandoned_turn": closed} if closed else {})}
    ctx.event("investigation.turn_retry_scheduled", inv.id, body, investigation_id=inv.id)
    return {"investigation_id": inv.id, **body}


@command("resume_investigation", "control")
def resume_investigation(ctx: Ctx, p: dict) -> dict:
    return _set_state(ctx, {**p, "running_job_policy": "drain"}, "active", "investigation.resumed")


@command("stop_investigation", "control")
def stop_investigation(ctx: Ctx, p: dict) -> dict:
    return _set_state(ctx, p, "stopped", "investigation.stopped")


# ---------------------------------------------------------------------------------------------
# Turns: TurnSpec committed before context; decisions accepted before effects.
# ---------------------------------------------------------------------------------------------

FINALIZATION_KIND = "investigation_finalization"
ORDINARY_FINALIZATION_KIND = "investigation_finalization_phase"


def _enter_finalization(ctx: Ctx, inv, trigger: str, runtime_outcome: Any = None) -> dict:
    prior = ctx.tx.get_record(FINALIZATION_KIND, inv.id)
    if prior:
        if prior.status == "finished":
            return {"enabled": False, "reason": "bounded finalization already completed"}
        return dict(prior.body)
    if inv.body["investigator"].get("finalization", "autonomous") == "operator":
        return {"enabled": False}
    refusals = [{"decision_id": d["id"], "turn_id": t["id"], "error": d["body"].get("error"),
                 "response_artifact_id": d["body"].get("response_artifact_id")}
                for t in ctx.tx.list_turns(inv.id) for d in ctx.tx.decisions(t["id"])
                if not d["accepted"] and d["body"].get("reason") == "invalid"]
    body = {"enabled": True, "mode": "conclude-only", "trigger": trigger, "runtime_outcome": runtime_outcome,
            "refusals": refusals, "turn_id": None, "status": "pending"}
    ctx.tx.put_record(FINALIZATION_KIND, inv.id, ctx.project_id, body, created_by=ctx.principal.id, status="pending")
    ctx.event("investigation.finalization_started", inv.id, body, investigation_id=inv.id)
    return body


def _ordinary_finalization(ctx: Ctx, inv, trigger: str) -> dict:
    prior = ctx.tx.get_record(ORDINARY_FINALIZATION_KIND, inv.id)
    if prior:
        return dict(prior.body)
    body = {"enabled": True, "mode": "ordinary", "trigger": trigger}
    ctx.tx.put_record(ORDINARY_FINALIZATION_KIND, inv.id, ctx.project_id, body,
                      created_by=ctx.principal.id, status="active")
    ctx.event("investigation.finalization_phase_started", inv.id, body, investigation_id=inv.id)
    return body


def _ordinary_token_envelope_fits(inv, budgets: dict) -> bool:
    budget = budgets.get("model_tokens")
    return budget is None or budget["limit_amount"] - budget["used"] - budget["reserved"] >= (
        exploration.decision_tokens(inv) or 0)


@command("enter_finalization", "turn")
def enter_finalization(ctx: Ctx, p: dict) -> dict:
    """Durably admit at most one conclude-only recovery in the original envelope (ADR 0027)."""
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    if inv.status != "active":
        return {"enabled": False, "reason": f"investigation is {inv.status}"}
    trigger = str(p.get("trigger") or "runtime-outcome")
    if trigger in ("exploration-boundary", "exploration-token-holdback"):
        wall = inv.body.get("wall")
        completion = turn_rules.completion_state(wall["deadline_unix"], time.time(), wall["conclude_threshold_seconds"]) \
            if wall else None
        budgets = ctx.tx.budgets(inv_scope(inv.id))
        has_reserve = all(budget["used"] + budget["reserved"] < budget["limit_amount"]
                          for key, budget in budgets.items() if key in ("model_tokens", "model_usd", "turns"))
        if has_reserve and _ordinary_token_envelope_fits(inv, budgets) and not (completion or {}).get("conclude_now"):
            return _ordinary_finalization(ctx, inv, trigger)
    return _enter_finalization(ctx, inv, trigger, p.get("runtime_outcome"))


def _fail_pending_finalization(ctx: Ctx, inv, reason: str, failure: dict | None = None) -> None:
    final = ctx.tx.get_record(FINALIZATION_KIND, inv.id)
    if final is None or final.status == "finished":
        return
    receipt = {"schema": "finalization-export-receipt/1", "investigation_id": inv.id,
               "investigator_concluded": False, "incumbent": exploration.final_incumbent(ctx, inv),
               "failure": _bounded_details(redact_value(failure)) if failure else {"kind": "finalization-not-admitted", "reason": reason},
               "provenance_limits": "Controller export only; no investigator conclusion or new confirmation."}
    _finish_finalization(ctx, inv, final, receipt, reason)


def _finish_finalization(ctx: Ctx, inv, final, receipt: dict, reason: str) -> dict | None:
    abstention = None
    if not receipt["investigator_concluded"] and reason != "cancelled":
        abstention = _record_controller_abstention(
            ctx, inv, code="finalization-failed", reason=reason, failure=receipt["failure"],
            incumbent=receipt.get("incumbent"),
            export_receipt={"kind": "finalization_export_receipt", "id": inv.id})
        if abstention:
            receipt["controller_abstention"] = abstention
    ctx.tx.put_record("finalization_export_receipt", inv.id, ctx.project_id, receipt,
                      created_by=ctx.principal.id, status="recorded")
    ctx.tx.put_record(FINALIZATION_KIND, inv.id, ctx.project_id,
                      {**final.body, "status": "finished", "export_receipt_ref": inv.id},
                      created_by=ctx.principal.id, status="finished")
    ctx.event("investigation.finalization_finished", inv.id, receipt, investigation_id=inv.id)
    return abstention


def _finalization_context(ctx: Ctx, inv, state: dict, finalization: dict) -> dict:
    jobs = exploration._jobs(ctx, inv.id)
    incumbent = state.get("incumbent")
    job_id = (incumbent or {}).get("job_id")
    if job_id and not any(j["id"] == job_id for j in jobs):
        # A valid lock may name an existing job of this project, created before/outside the investigation.
        # Preserve that exact lock; never substitute a different recipe or invent selection evidence.
        locked = ctx.tx.get_job(job_id)
        if locked and locked["project_id"] == ctx.project_id:
            jobs.append(exploration.view_job(locked, ctx.tx.publications(job_id)))
    from eidos.application.handlers import finalization_evidence
    evidence = finalization_evidence.allowlist(ctx, inv, jobs)
    jobs = [{**j, "outputs": [o for o in j.get("outputs") or [] if o["artifact_id"] in evidence["artifacts"]]}
            for j in jobs if j["id"] in evidence["jobs"]]
    candidate = next((j for j in jobs if j["id"] == job_id), None)
    blocked = finalization_evidence.excluded(ctx, inv)
    incumbent = finalization_evidence.sanitize(incumbent, blocked)
    selection_rule = finalization_evidence.sanitize(state.get("rule"), blocked)
    finalization = {**finalization, "refusals": finalization_evidence.sanitize(finalization.get("refusals") or [], blocked)}
    return {**finalization, "evidence_allowlist": evidence, "locked_candidate": candidate, "incumbent": incumbent,
            "selection_rule": selection_rule,
            "metrics": [{"job_id": j["id"], "component": j["component"], "inputs": j["inputs"],
                         "config": j["config"], "receipt": j["receipt"], "outputs": j["outputs"]}
                        for j in jobs if j.get("receipt")],
            "provenance_limits": "Visible metrics are diagnostic unless matched by the fixed selection rule. "
                "Missing held-out evidence never passes assessment. Report reproduction exactly as recorded.",
            "instruction": "One finalization turn. Read existing evidence, then conclude or stop. No effects, "
                "new search, training, authoring, rule changes or candidate changes. Report the locked candidate "
                "with source and receipt identifiers and limitations, or honestly abstain if none is eligible."}


@command("begin_turn", "turn")
def begin_turn(ctx: Ctx, p: dict) -> dict:
    run = str(p.get("run_id") or "")  # the TurnWorkflow run that begins or resumes the turn (F5, ADR 0017 addendum)
    pre = ctx.tx.last_turn(p["investigation_id"])
    if pre and pre["status"] == "open":  # lock order (F2b): the turn row, then the investigation row
        _turn(ctx, pre["id"], lock=True)
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    stale = _fence_turn_ticket(ctx, inv.id, run, p.get("ticket"))
    if stale:  # F5c (R8 #1): a delayed begin_turn of an earlier turn workflow begins and resumes nothing
        return {"started": False, "reason": "superseded-run", **stale}
    if inv.status != "active":
        return {"started": False, "reason": f"investigation is {inv.status}"}
    last = ctx.tx.last_turn(inv.id)
    if last and last["status"] == "open":
        # A turn left open by a terminated child is resumed, never duplicated. The resuming run takes the turn's
        # authorization: every decision step of an earlier run is refused from now on (F5, C1).
        if not pre or pre["id"] != last["id"]:
            _turn(ctx, last["id"], lock=True)
        auth = authorize_turn_run(ctx, last, run)
        return {"started": True, "resumed": True, "turn_id": last["id"], "spec": last["spec"],
                **({"authorization": auth} if auth else {})}
    seq = (last["seq"] + 1) if last else 1
    cfg = inv.body["investigator"]
    if seq > int(cfg.get("max_turns", 40)):
        _fail_pending_finalization(ctx, inv, "max-turns-reached")
        return {"started": False, "reason": "max-turns-reached"}
    wall = inv.body.get("wall")
    completion = turn_rules.completion_state(wall["deadline_unix"], time.time(), wall["conclude_threshold_seconds"]) \
        if wall else None
    if completion and completion["remaining_seconds"] <= 0:
        _fail_pending_finalization(ctx, inv, turn_rules.WALL_REACHED)
        return {"started": False, "reason": turn_rules.WALL_REACHED}
    budgets = ctx.tx.budgets(inv_scope(inv.id))
    for dim, b in budgets.items():
        if dim in ("model_tokens", "model_usd") and b["used"] + b["reserved"] >= b["limit_amount"]:
            _fail_pending_finalization(ctx, inv, "budget-exhausted")
            return {"started": False, "reason": "budget-exhausted", "dimension": dim}
    finalization = ctx.tx.get_record(FINALIZATION_KIND, inv.id)
    if finalization and (finalization.body.get("turn_id") or finalization.status == "finished"):
        if finalization.status != "finished" or (inv.body.get("control") or {}).get("status") != "active":
            return {"started": False, "reason": "finalization-exhausted"}
        finalization = None  # explicit resume does not grant another bounded recovery
    if "turns" in budgets:
        remaining_turns = budgets["turns"]["limit_amount"] - budgets["turns"]["used"] - budgets["turns"]["reserved"]
        if remaining_turns < 1 and finalization:
            _fail_pending_finalization(ctx, inv, "turn-budget-exhausted")
            return {"started": False, "reason": "turn-budget-exhausted", "dimension": "turns"}
        ctx.tx.reserve(inv_scope(inv.id), "turns", 1, f"turn:{inv.id}:{seq}")
    tid = f"turn_{inv.id}_{seq}"
    spec = {
        "project_id": ctx.project_id, "investigation_id": inv.id, "turn_id": tid, "seq": seq,
        "study_revision": inv.revision, "charter_ref": inv.body.get("charter_ref"),
        "runtime": {"runtime": cfg["runtime"], "model": cfg.get("model"), "settings": cfg.get("settings", {}),
                    "runtime_release": p.get("runtime_release")},
        "context_policy": cfg.get("context_policy", "context-policy/1"),
        **({"conclusion_scope_schema": cfg["conclusion_scope_schema"]} if "conclusion_scope_schema" in cfg else {}),
        "limits": {"max_calls": int(cfg.get("max_calls_per_turn", 6)),
                   "max_model_calls": int(cfg.get("max_model_calls_per_turn", 4)),
                   "deadline_seconds": int(cfg.get("turn_deadline_seconds", 600))},
        "configured_decision_seconds": int(cfg.get("turn_deadline_seconds", 600)),
        **({"completion": completion} if completion else {}),
        "budgets": budgets,
        "tool_vocabulary": sorted(turn_rules.EFFECT_TOOLS) + list(turn_rules.CONTROL_TOOLS),
        "exposure_policy": p.get("exposure_policy", "development-feedback-allowed"),
    }
    if completion:
        spec["limits"]["deadline_seconds"] = turn_rules.turn_seconds(spec["limits"]["deadline_seconds"], completion)
    # ADR 0023: the incumbent, challenger rounds and the exploration allowance, as of the turn's start (immutable)
    state = exploration.exploration_state(ctx, inv, completion=completion, budgets=budgets)
    if state.get("phase") == "finalize":
        short_envelope = not _ordinary_token_envelope_fits(inv, budgets)
        recovery = _enter_finalization(ctx, inv, "ordinary-finalization-envelope-unavailable") if short_envelope else {}
        if not recovery.get("enabled"):
            spec["finalization_phase"] = _ordinary_finalization(ctx, inv, str(state.get("stop") or "exploration-ended"))
    if (completion or {}).get("conclude_now"):
        _enter_finalization(ctx, inv, "ordinary-finalization-time-exhausted")
    finalization = ctx.tx.get_record(FINALIZATION_KIND, inv.id)
    if finalization and finalization.status != "finished":
        final = {**finalization.body, "turn_id": tid, "status": "running"}
        ctx.tx.put_record(FINALIZATION_KIND, inv.id, ctx.project_id, final,
                          created_by=ctx.principal.id, status="running")
        spec["finalization"] = _finalization_context(ctx, inv, state, final)
        spec["limits"].update(max_calls=0, max_model_calls=1)
        spec["tool_vocabulary"] = ["conclude", "stop"]
        if completion:
            spec["completion"] = {**completion, "finalization": True}
            spec["limits"]["deadline_seconds"] = max(1, min(
                int(cfg.get("turn_deadline_seconds", 600)), int(completion["remaining_seconds"])))
    spec["exploration"] = exploration_rules.compact(state)
    if spec.get("finalization") or spec.get("finalization_phase"):
        from eidos.application.handlers import finalization_evidence
        spec["evidence_allowlist"] = (spec.get("finalization") or {}).get("evidence_allowlist") or \
            finalization_evidence.allowlist(ctx, inv, exploration._jobs(ctx, inv.id))
        spec["exploration"] = finalization_evidence.sanitize(spec["exploration"], finalization_evidence.excluded(ctx, inv))
    # R14 #4, #5: an exploratory turn ends where the hold-back begins, its decisions, retries and activities included,
    # so an open turn cannot spend the time kept for reproduction and export.
    until = exploration_rules.exploration_deadline_unix(state, completion)
    if until is not None and not finalization:
        spec["completion"] = {**spec["completion"], "exploration_deadline_unix": round(until, 3)}
        spec["limits"]["deadline_seconds"] = max(turn_rules.MIN_TURN_SECONDS,
                                                 min(int(spec["limits"]["deadline_seconds"]), int(until - time.time())))
    horizon = ctx.tx.horizon()
    spec["horizon"] = horizon
    ctx.tx.insert_turn({"id": tid, "investigation_id": inv.id, "project_id": ctx.project_id, "seq": seq,
                        "status": "open", "study_revision": inv.revision, "horizon": horizon, "spec": spec})
    ctx.event("turn.started", tid, {"seq": seq, "horizon": horizon}, investigation_id=inv.id)
    auth = authorize_turn_run(ctx, {"id": tid, "investigation_id": inv.id}, run)
    return {"started": True, "turn_id": tid, "spec": spec, **({"authorization": auth} if auth else {})}


# ---------------------------------------------------------------------------------------------
# Decision authorization (F5, ADR 0017 addendum "Turn and slot authorization"). Two durable, monotonic records,
# both read and written under the turn row lock:
#   * ``turn_authorization`` (per turn): the TurnWorkflow run that owns the turn and its generation. begin_turn sets
#     it; a resume by another run increments the generation, and every decision step of an earlier run is refused.
#   * ``decision_attempt`` (per turn slot): the execution and attempt that may decide the slot, and its epoch.
#     begin_decision advances it only for a newer attempt of the owning run (never back to an older attempt).
# A decision is executed only after a ``decision_handoff`` committed by the owning run (execute_decision's first
# step). An accepted decision that was never handed off before its run lost the turn is revoked: it never reached
# execution, and its acceptance may have committed after its workflow was terminated.
# ---------------------------------------------------------------------------------------------

def _fence_turn_ticket(ctx: Ctx, inv_id: str, run: str, ticket: Any) -> dict | None:
    """F5c (review R8 #1): fence turn acquisition by the coordinator's ordered ticket (ADR 0017 addendum).

    The coordinator gives every TurnWorkflow it starts a ticket from its workflow clock: durable (the child's input is
    in the history) and strictly increasing across its turn workflows, since each starts only after the previous one
    ended. The lab keeps the highest ticket that has begun or resumed a turn of the investigation (``turn_ticket``,
    under the investigation row lock, which every begin_turn holds) and refuses an older one, so a delayed begin_turn
    of a terminated run can never take a turn (or start a new one) after a newer run. Without the fence, the last
    begin_turn to commit won, whatever its run's age.

    Returns why ``run`` is refused, or None (and records its ticket). Callers without a run id (tests, tools) are not
    fenced. A run without a ticket (a turn workflow started before ``turn-ticket-v1``) is refused once a ticketed run
    has begun a turn; among unticketed runs the earlier rule applies (the documented limitation)."""
    if not run:
        return None
    rec = ctx.tx.get_record("turn_ticket", inv_id, lock=True)
    held = rec.body if rec is not None else None
    if ticket is None:
        return {"superseded": "unticketed-run", "current_ticket": held["ticket"]} if held else None
    ticket = int(ticket)
    if held is not None:
        if ticket < int(held["ticket"]):
            return {"superseded": "stale-ticket", "ticket": ticket, "current_ticket": int(held["ticket"])}
        if ticket == int(held["ticket"]):
            # The same ticket from another run: the turn workflow was reset by hand (Eidos never does). The run that
            # holds it keeps it; the order of two runs with one ticket cannot be known.
            return None if held.get("run") == run else {"superseded": "ticket-held-by-another-run", "ticket": ticket}
    ctx.tx.put_record("turn_ticket", inv_id, ctx.project_id, {"investigation_id": inv_id, "ticket": ticket, "run": run},
                      created_by=ctx.principal.id, status="active")
    return None


def _run_of(execution: str) -> str:
    return str(execution or "").split("/", 1)[0]


def authorize_turn_run(ctx: Ctx, turn: dict, run: str) -> dict | None:
    """Make ``run`` the owner of ``turn`` (caller holds the turn row lock). Callers without a run id (tests, tools)
    leave the authorization unchanged."""
    if not run:
        return None
    rec = ctx.tx.get_record("turn_authorization", turn["id"], lock=True)
    if rec is not None and rec.body.get("run") == run:
        return dict(rec.body)
    prev = dict(rec.body) if rec is not None else None
    body = {"turn_id": turn["id"], "run": run, "generation": int((prev or {}).get("generation") or 0) + 1,
            "previous_run": (prev or {}).get("run")}
    ctx.tx.put_record("turn_authorization", turn["id"], ctx.project_id, body, created_by=ctx.principal.id,
                      status="active")
    if prev is not None:
        revoked = _revoke_unhanded_decisions(ctx, turn, run)
        ctx.event("turn.resumed", turn["id"], {"generation": body["generation"], "revoked_decisions": revoked},
                  investigation_id=turn["investigation_id"])
    return body


def _execution_refused(ctx: Ctx, turn: dict, execution: str) -> str | None:
    """Why a decision step of ``execution`` may not act on ``turn``, or None. The caller holds the turn row lock."""
    if not execution:
        return None  # callers that do not identify themselves are checked as before (epoch, attempt)
    rec = ctx.tx.get_record("turn_authorization", turn["id"])
    if rec is None or not rec.body.get("run"):
        return None  # a turn begun without a run id (tests, tools): no owner to check against
    return None if _run_of(execution) == rec.body["run"] else "superseded-execution"


def _handed_off(ctx: Ctx, decision_id: str) -> bool:
    return ctx.tx.get_record("decision_handoff", decision_id) is not None


def _revoke_unhanded_decisions(ctx: Ctx, turn: dict, owner: str) -> list[str]:
    """Revoke the turn's accepted decisions that an earlier run accepted but never handed off (C1)."""
    out = []
    for d in ctx.tx.decisions(turn["id"]):
        if not d["accepted"] or decision_revoked(ctx, d["id"]) or _handed_off(ctx, d["id"]) \
                or ctx.tx.get_record("decision_receipts", d["id"]) is not None:
            continue
        origin = (d["body"] or {}).get("attempt") or {}
        if not origin.get("execution") or _run_of(origin["execution"]) == owner:
            continue
        _revoke(ctx, turn, d["id"], int(d["slot"]), "superseded-execution", origin,
                {"run": owner})
        out.append(d["id"])
    return out


def _revoke(ctx: Ctx, turn: dict, decision_id: str, slot: int, reason: str, origin: dict, by: dict) -> None:
    ctx.tx.put_record("decision_revocation", decision_id, ctx.project_id,
                      {"turn_id": turn["id"], "slot": slot, "reason": reason, "accepted_by": origin,
                       "revoked_by": by}, created_by=ctx.principal.id, status="revoked", parent_id=turn["id"])
    ctx.event("decision.revoked", decision_id, {"turn_id": turn["id"], "slot": slot, "reason": reason},
              investigation_id=turn["investigation_id"])


@command("hand_off_decision", "turn")
def hand_off_decision(ctx: Ctx, p: dict) -> dict:
    """The first step of executing an accepted decision (F5, C1): under the turn row lock, the owning run records
    that the decision reached execution. A run that no longer owns the turn, a revoked decision, a closed turn or an
    investigation that is no longer active (stopped, paused, concluded) is refused (``handed_off`` false) and nothing
    is executed. F5c (review R8 #2): the turn and investigation state is checked under their locks (turn row, then
    investigation row, the order stop/pause and accept_decision use) before granting a new handoff AND before
    returning an existing one, so a stop or a turn closure that commits first always wins. Each effect is checked
    again at its own admission (``admit_decision_effect``), so a stop that commits after the handoff refuses the
    decision's remaining effects. Idempotent otherwise: an existing handoff is returned."""
    dec = ctx.tx.get_decision(p["decision_id"])
    if not dec or not dec["accepted"]:
        raise Invalid("only an accepted decision can be handed off")
    refused = _decision_refused(ctx, dec, str(p.get("execution") or ""))
    if refused:
        return {"decision_id": dec["id"], "handed_off": False, "reason": refused}
    existing = ctx.tx.get_record("decision_handoff", dec["id"])
    if existing is not None:  # idempotent; the owner of a resumed turn continues a handed-off decision (recovery)
        return {"decision_id": dec["id"], "handed_off": True, "by": existing.body.get("run")}
    ctx.tx.put_record("decision_handoff", dec["id"], ctx.project_id,
                      {"turn_id": dec["turn_id"], "slot": dec["slot"], "run": _run_of(str(p.get("execution") or ""))},
                      created_by=ctx.principal.id, status="handed-off", parent_id=dec["turn_id"])
    return {"decision_id": dec["id"], "handed_off": True, "by": _run_of(str(p.get("execution") or ""))}


def _decision_refused(ctx: Ctx, dec: dict, execution: str) -> str | None:
    """Why ``execution`` may not execute (or continue executing) the accepted decision ``dec``, or None. Takes the turn
    row lock, then the investigation row lock (the order of stop/pause/resume, accept_decision and complete_turn), so
    the answer is atomic with respect to a stop, a pause and the turn's closing: whichever commits first wins."""
    turn = _turn(ctx, dec["turn_id"], lock=True)
    inv = ctx.tx.require("investigation", turn["investigation_id"], lock=True)
    # first: a run that lost the turn executes nothing, even a decision it had handed off (its retry)
    return _execution_refused(ctx, turn, execution) \
        or ("decision-revoked" if decision_revoked(ctx, dec["id"]) else None) \
        or (f"turn-{turn['status']}" if turn["status"] != "open" else None) \
        or (f"investigation-{inv.status}" if inv.status != "active" else None)


def admit_decision_effect(ctx: Ctx, admission: dict) -> None:
    """F5c (review R8 #2): the admission boundary of every effect of an accepted decision (``file_claim`` included).

    The investigator's execute_decision sends each effect command with ``admission`` = {decision_id, execution}; the
    command service calls this in the effect's own transaction, before the handler runs (a replay of an effect that
    already committed is returned as before). The effect is refused (``EFFECT_NOT_ADMITTED``, nothing written) unless
    the decision was handed off, is not revoked, its turn is open, the investigation is active and ``execution``'s run
    still owns the turn. The check holds the turn and investigation row locks until the effect commits, so a stop
    (or pause) either commits first and the effect is refused, or waits until the effect has committed."""
    did = str(admission.get("decision_id") or "")
    dec = ctx.tx.get_decision(did) if did else None
    if not dec or not dec["accepted"]:
        raise EffectNotAdmitted("not an effect of an accepted decision", details={"reason": "not-accepted",
                                                                                   "decision_id": did})
    _turn(ctx, dec["turn_id"])  # project access
    refused = _decision_refused(ctx, dec, str(admission.get("execution") or "")) \
        or (None if _handed_off(ctx, did) else "not-handed-off")
    if refused:
        raise EffectNotAdmitted(f"effect of decision {did} not admitted: {refused}",
                                details={"reason": refused, "decision_id": did})


if admit_decision_effect not in EFFECT_ADMISSION:
    EFFECT_ADMISSION.append(admit_decision_effect)


@command("record_turn_context", "turn")
def record_turn_context(ctx: Ctx, p: dict) -> dict:
    """Store the exact packed context and authorized source manifest used for this turn."""
    _turn(ctx, p["turn_id"])
    existing = ctx.tx.get_record("turn_context", p["turn_id"])
    if existing is not None:  # idempotent per turn: a retried build never replaces or conflicts
        return {"turn_id": p["turn_id"], "revision": existing.revision, "existing": True}
    body = {k: p.get(k) for k in ("context_artifact_id", "source_manifest_digest", "source_manifest_artifact_id",
                                  "policy", "selections", "omissions", "truncations", "catalog_snapshot_digest",
                                  "horizon", "chars")}
    rec = ctx.tx.put_record("turn_context", p["turn_id"], ctx.project_id, body, created_by=ctx.principal.id,
                            status="recorded")
    return {"turn_id": p["turn_id"], "revision": rec.revision}


@command("begin_decision", "turn")
def begin_decision(ctx: Ctx, p: dict) -> dict:
    """Start one attempt of a decision slot (F2c, ADR 0017): every decide activity attempt calls this first.

    Under the turn row lock it bumps the slot's attempt epoch, so a request of any earlier attempt that is still in
    flight (``reserve_model_budget``, ``accept_decision``) is refused from now on: that is where a cancelled or timed-out
    attempt is invalidated durably, at the acceptance boundary.

    An accepted decision already in the slot is returned for recovery, except one accepted by an earlier attempt of the
    SAME activity execution (``execution``: workflow run and activity id). Its result never reached the workflow, since
    Temporal retries only an attempt it failed (cancelled, timed out, or lost with its worker), so it has never been
    executed; its acceptance may have committed after Temporal gave up on it. It is revoked (a decision is never
    updated: a ``decision_revocation`` record), never recovered or executed, and the slot yields no decision
    (``revoked``). A decision accepted by another execution (a resumed turn after its workflow was terminated) is
    recovered as before: that run may have executed it; so is one with recorded receipts (it has run)."""
    turn = _turn(ctx, p["turn_id"], lock=True)
    slot = int(p.get("slot") or 0)
    execution, attempt = str(p.get("execution") or ""), int(p.get("attempt") or 0)
    rid = f"{turn['id']}:{slot}"
    rec = ctx.tx.get_record("decision_attempt", rid, lock=True)
    # F5 (C1): a run that no longer owns the turn (it was resumed by another) begins nothing.
    refused = _execution_refused(ctx, turn, execution)
    if refused:
        return {"turn_id": turn["id"], "slot": slot, "superseded": refused}
    # F5 (C3): the slot's authorization never moves back. A delayed begin of an EARLIER attempt of the same execution
    # (Temporal already gave up on it and started a later one) changes nothing and is told it is stale.
    if rec is not None and execution and rec.body.get("execution") == execution \
            and int(rec.body.get("attempt") or 0) > attempt:
        return {"turn_id": turn["id"], "slot": slot, "superseded": "stale-attempt",
                "current_attempt": int(rec.body.get("attempt") or 0)}
    epoch = int((rec.body if rec else {}).get("epoch") or 0) + 1
    ctx.tx.put_record("decision_attempt", rid, ctx.project_id,
                      {"turn_id": turn["id"], "slot": slot, "attempt": attempt, "execution": execution, "epoch": epoch,
                       "started_at": time.time()}, created_by=ctx.principal.id, status="started")
    out: dict = {"turn_id": turn["id"], "slot": slot, "epoch": epoch}
    existing = ctx.tx.accepted_decision(turn["id"], slot)
    if existing is None:
        return out
    if decision_revoked(ctx, existing["id"]):
        return {**out, "revoked": existing["id"]}
    origin = (existing["body"] or {}).get("attempt") or {}
    # never revoke what has run or was handed to execution (F5): that run may have committed effects
    executed = ctx.tx.get_record("decision_receipts", existing["id"]) is not None or _handed_off(ctx, existing["id"])
    if execution and origin.get("execution") == execution and int(origin.get("attempt") or 0) < attempt \
            and not executed:
        _revoke(ctx, turn, existing["id"], slot, "attempt-failed", origin, {"execution": execution, "attempt": attempt})
        return {**out, "revoked": existing["id"]}
    if execution and origin.get("execution") and _run_of(origin["execution"]) != _run_of(execution) \
            and not executed and ctx.tx.get_record("turn_authorization", turn["id"]) is not None:
        # F5 (C1): accepted by a run that lost the turn before handing it off: it never reached execution (its
        # acceptance may have committed after that workflow was terminated). Revoked, never recovered.
        _revoke(ctx, turn, existing["id"], slot, "superseded-execution", origin, {"execution": execution})
        return {**out, "revoked": existing["id"]}
    return {**out, "decision": {"id": existing["id"], "calls": existing["body"]["calls"],
                                "control": existing["body"].get("control")}}


def decision_revoked(ctx: Ctx, decision_id: str) -> bool:
    """Whether an accepted decision was revoked (F2c): it is never recovered or executed."""
    return ctx.tx.get_record("decision_revocation", decision_id) is not None


@command("record_runtime_outcome", "turn")
def record_runtime_outcome(ctx: Ctx, p: dict) -> dict:
    """A runtime bound is telemetry/control, never an invalid model proposal (ADR 0026)."""
    turn = _turn(ctx, p["turn_id"], lock=True)
    body = {k: p.get(k) for k in ("turn_id", "slot", "runtime_outcome", "response_artifact_id", "usage")}
    if p.get("phase_deadline") is True:
        body["phase_deadline"] = True
    if p.get("prompt_revision"):
        body["prompt_revision"] = p["prompt_revision"]
    report = (p.get("usage") or {}).get("structured_output") or {}
    cause = report.get("last_error_code")
    if (turn["spec"].get("finalization") and p["runtime_outcome"] == "structured-output-invalid"
            and p.get("response_artifact_id") and cause in turn_rules.AUTHORED_FINALIZATION_FAILURES):
        body["terminal_cause"] = cause
        body["problems"] = [redact_value(problem)[:300] for problem in (report.get("errors") or [])[-4:]
                            if isinstance(problem, str)]
    rid = f"{turn['id']}:{p['slot']}:{ctx.command_key}"
    ctx.tx.put_record("runtime_outcome", rid, ctx.project_id, body, created_by=ctx.principal.id,
                      status="recorded", parent_id=turn["id"])
    ctx.event("decision.runtime_bounded", turn["id"], body, investigation_id=turn["investigation_id"])
    return {"accepted": False, "reason": "runtime-bounded", "n_calls": 0,
            "runtime_outcome": p["runtime_outcome"], "response_artifact_id": p.get("response_artifact_id"),
            **({"runtime_record_id": rid} if body.get("terminal_cause") else {}),
            **({"phase_deadline": True} if body.get("phase_deadline") else {})}


@command("accept_decision", "turn")
def accept_decision(ctx: Ctx, p: dict) -> dict:
    """Persist a proposed decision. Exactly one decision is accepted per (turn, slot); a late or
    stale proposal is kept as an unapplied record with no authority to execute tools."""
    turn = _turn(ctx, p["turn_id"], lock=True)
    # Lock order (F2b): the turn row, then the investigation row. stop/pause/resume lock only the investigation row
    # and complete_turn takes the same order, so the status and revision checks below are atomic with respect to a
    # stop: a stop either commits first (and this decision is refused) or waits until it is accepted.
    inv = ctx.tx.require("investigation", turn["investigation_id"], lock=True)
    slot = int(p.get("slot", 0))
    existing = ctx.tx.accepted_decision(turn["id"], slot)
    did = f"dec_{uuid.uuid4().hex[:16]}"
    usage = p.get("usage") or {}
    if not p.get("usage_recorded"):  # recorded earlier by record_model_usage (first durable write)
        _charge_model_usage(ctx, turn["investigation_id"], usage, ref=did)
    if existing:
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"response_artifact_id": p.get("response_artifact_id"), "proposal": p.get("proposal"), "reason": "slot-taken",
                                                               "usage": usage})
        if decision_revoked(ctx, existing["id"]):  # a revoked decision is never handed out for execution (F2c)
            return {"accepted": False, "decision_id": did, "reason": "slot-revoked"}
        return {"accepted": False, "decision_id": existing["id"], "reason": "slot-already-accepted",
                "calls": existing["body"]["calls"], "control": existing["body"].get("control")}
    if turn["status"] != "open":
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"response_artifact_id": p.get("response_artifact_id"), "proposal": p.get("proposal"), "reason": "turn-closed"})
        return {"accepted": False, "decision_id": did, "reason": "turn-closed"}
    late = _execution_refused(ctx, turn, str(p.get("execution") or "")) if inv.status == "active" else None
    late = late or _late_decision(ctx, turn, inv, slot, p.get("attempt"), p.get("epoch"))
    if late:  # ADR 0017: a decision from a stopped investigation or a cancelled request is never applied
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"response_artifact_id": p.get("response_artifact_id"), "proposal": p.get("proposal"), "reason": late})
        ctx.event("decision.unapplied", did, {"reason": late}, investigation_id=inv.id)
        return {"accepted": False, "decision_id": did, "reason": late}
    if inv.revision != turn["study_revision"]:
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"response_artifact_id": p.get("response_artifact_id"), "proposal": p.get("proposal"), "reason": "stale-revision",
                                                               "observed_revision": inv.revision})
        ctx.event("decision.unapplied", did, {"reason": "stale-revision"}, investigation_id=inv.id)
        return {"accepted": False, "decision_id": did, "reason": "stale-revision", "observed_revision": inv.revision}
    try:
        proposal = dict(p.get("proposal") or {})
        if isinstance(proposal.get("control"), dict):  # only the trusted runtime path may mark a bounded outcome
            ctl = {k: v for k, v in proposal["control"].items() if k != "bounded_outcome"}
            if p.get("bounded_outcome") and (p.get("usage") or {}).get("limit_reached"):
                ctl["bounded_outcome"] = str(p["bounded_outcome"])[:64]
            proposal["control"] = ctl
        decision = turn_rules.validate_decision(proposal, max_calls=turn["spec"]["limits"]["max_calls"])
        for call in decision["calls"]:
            call.pop("macro", None)  # provenance stamps are lab-owned, never proposal arguments
        _check_conclusion_scope(inv, decision["control"])
        if turn["spec"].get("finalization") and (decision["calls"] or
                decision["control"].get("action") not in turn_rules.CONCLUDING_ACTIONS):
            raise Invalid("autonomous finalization permits read-only evidence and conclude/stop only")
        if turn["spec"].get("finalization"):
            candidate = turn["spec"]["finalization"].get("locked_candidate") or {}
            if candidate.get("status") != "succeeded" and (decision["control"].get("claim") or {}).get(
                    "disposition") in ("candidate", "confirmed"):
                raise Invalid("no eligible locked candidate: finalization must honestly abstain")
        from eidos.application.handlers import promotion
        from eidos.domain.promotion import expand
        if any(c["tool"] == "prepare_candidate" for c in decision["calls"]):
            decision["proposed_calls"] = copy.deepcopy(decision["calls"])
            decision["calls"] = expand(decision["calls"], promotion.expansion_view(ctx, inv, decision["calls"]),
                                       turn["spec"]["limits"], proposed_by=did)
        for call in decision["calls"]:
            if call["tool"] == "author_source":
                call["args"].pop("include_bundle", None)  # caller cannot choose the pin
                promotion.inclusion(ctx, call["args"])
        _check_component_contracts(ctx, decision["calls"])
        _check_conclusion_due(turn, decision)  # first: "nothing to wait for" would suggest running something
        _check_exploration_phase(turn, decision, live_stop=_live_exploration_stop(ctx, inv, turn), ctx=ctx)
        _check_resource_policy(ctx, decision)
        _check_something_to_wait_for(ctx, inv, decision)
    except Invalid as exc:
        ctx.tx.insert_decision(did, turn["id"], slot, False, {"response_artifact_id": p.get("response_artifact_id"), "proposal": p.get("proposal"), "reason": "invalid",
                                                               "error": exc.to_dict()})
        return {"accepted": False, "decision_id": did, "reason": "invalid", "error": exc.to_dict()}
    calls = turn_rules.keyed_calls(ctx.project_id, turn["id"], did, decision["calls"])
    body = {**decision, "calls": calls, "model": p.get("model_settings") or {}, "usage": usage,
            "response_artifact_id": p.get("response_artifact_id"), "prompt_revision": p.get("prompt_revision")}
    if p.get("epoch") is not None:  # which attempt accepted it: begin_decision revokes it if that attempt failed (F2c)
        body["attempt"] = {"execution": str(p.get("execution") or ""), "attempt": int(p.get("attempt") or 0),
                           "epoch": int(p["epoch"])}
    ctx.tx.insert_decision(did, turn["id"], slot, True, body)
    ctx.event("decision.accepted", did, {"turn_id": turn["id"], "slot": slot, "calls": [c["tool"] for c in calls],
                                         "control": decision["control"]}, investigation_id=inv.id)
    return {"accepted": True, "decision_id": did, "calls": calls, "control": decision["control"]}


def _check_conclusion_scope(inv: Record, control: dict) -> None:
    turn_rules.validate_conclusion_scope(control, inv.body["investigator"].get("conclusion_scope_schema"))


def _check_conclusion_due(turn: dict, decision: dict) -> None:
    """ADR 0020: once the conclusion's reserve has begun, a decision must conclude or stop. A runtime's bounded outcome
    (no usable decision within its limits) is not a choice to keep working, and is left to end the turn."""
    completion = turn["spec"].get("completion") or {}
    control = decision.get("control") or {}
    if completion.get("conclude_now") and control.get("action") not in turn_rules.CONCLUDING_ACTIONS \
            and not control.get("bounded_outcome"):
        raise Invalid("only the time reserved for the conclusion remains: this decision must conclude (or stop)",
                      details={"remaining_seconds": completion.get("remaining_seconds")},
                      next_action="Propose conclude with the best scoped result the recorded evidence supports.")


def _live_exploration_stop(ctx: Ctx, inv, turn: dict) -> str | None:
    """R14 #4: the hold-back boundary recomputed between decisions. An exploratory turn (phase ``explore`` at its start)
    whose wall allowance or model-token allowance has since run out admits no new challengers: the turn's start is not
    the only time the boundary is checked."""
    state = turn["spec"].get("exploration") or {}
    if state.get("phase") not in exploration_rules.EXPLORING_PHASES:  # before the first lock too (H2, R17 #3)
        return None
    until = (turn["spec"].get("completion") or {}).get("exploration_deadline_unix")
    if until is not None and time.time() >= float(until):
        return "wall-holdback"
    tokens = ctx.tx.budgets(inv_scope(inv.id)).get("model_tokens")
    if isinstance(tokens, dict) and tokens.get("limit_amount") is not None:
        left = exploration_rules.allowance(exploration_rules.policy(inv.body.get("investigator")), None, None,
                                           {"model_tokens": tokens})["tokens_left"]
        if left is not None and left <= 0:
            return "token-holdback"
    return None


def _finishes_pending_round(call: dict, calls: list[dict], pending: set[str], memo: dict[int, bool]) -> bool:
    """R14 #6: a job-starting call that reads an output of a pending challenger (or of an earlier call of the same
    decision that does), e.g. scoring it: it finishes a round that was already admitted, it is not a new challenger."""
    for v in ((call.get("args") or {}).get("inputs") or {}).values():
        if isinstance(v, str) and v in pending:
            return True
        if isinstance(v, dict) and isinstance(v.get(turn_rules.REF_KEY), str):
            m = re.match(r"call\[(\d+)\]", v[turn_rules.REF_KEY])
            i = int(m.group(1)) if m else -1
            if 0 <= i < len(calls) and calls[i]["tool"] in turn_rules.JOB_TOOLS:
                if i not in memo:
                    memo[i] = _finishes_pending_round(calls[i], calls, pending, memo)
                if memo[i]:
                    return True
    return False


def _job_effect_intent(ctx: Ctx | None, call: dict, calls: list[dict]) -> tuple[str | None, dict, str]:
    """The purpose and input bindings the command will actually use for a job."""
    args = call["args"]
    if call["tool"] == "run_component":
        return (args.get("purpose", "open-search"), call,
                "run_component args.purpose" if "purpose" in args else "run_component default")
    if call["tool"] == "author_component":
        return "build", call, "component validation command"
    trial_id = args.get("trial_id")
    if isinstance(trial_id, dict):
        ref = turn_rules.parse_ref(trial_id.get(turn_rules.REF_KEY, ""))
        if ref and ref[1] == "trial_id" and ref[0] < len(calls):
            plan = calls[ref[0]]
            if plan["tool"] in ("plan_trial", "plan_branch"):
                return plan["args"].get("purpose", "open-search"), plan, "same-decision trial plan"
    elif ctx is not None and isinstance(trial_id, str):
        trial = ctx.tx.get_record("trial", trial_id)
        if trial is not None and trial.project_id == ctx.project_id:
            inputs = {port: binding.get("artifact_id") for port, binding in
                      (trial.body["lock"].get("inputs") or {}).items()}
            return trial.body.get("purpose"), {"args": {"inputs": inputs}}, "stored trial plan"
    return None, call, "unresolved trial plan"


def _check_exploration_phase(turn: dict, decision: dict, live_stop: str | None = None, *, ctx: Ctx | None = None) -> None:
    """Once exploration stops, a decision's headline cannot authorize new challenger effects."""
    state = dict(turn["spec"].get("exploration") or {})
    if live_stop and state.get("phase") in exploration_rules.EXPLORING_PHASES:
        state.update(phase="finalize", stop=live_stop, **({} if state.get("incumbent") else {"candidate": "none"}))
    if state.get("phase") != "finalize":
        return
    from eidos.application.handlers.runs import PURPOSES

    headline = decision.get("purpose", "open-search")
    challenger = exploration_rules.REFUSED_WHEN_FINALIZING
    pending = set(state.get("pending_outputs") or [])
    memo: dict[int, bool] = {}
    calls = decision["calls"]
    refused: list[dict[str, Any]] = []
    for i, call in enumerate(calls):
        if call["tool"] not in turn_rules.JOB_TOOLS:
            continue
        purpose, bindings, source = _job_effect_intent(ctx, call, calls)
        conflicts = headline not in challenger and purpose in challenger
        needs_pending = headline in challenger or purpose in challenger
        if purpose not in PURPOSES or conflicts or (needs_pending and not (
                pending and _finishes_pending_round(bindings, calls, pending, memo))):
            refused.append({"call": i, "purpose": purpose, "source": source})
    if refused:
        message, next_action = exploration_rules.stopped_message(state)
        if headline in exploration_rules.FINALIZE_PURPOSES:
            next_action += (f" For a direct {headline} run, set run_component args.purpose to {headline} explicitly. "
                            "A trial launch uses the purpose stored in its plan.")
        raise Invalid(message,
                      details={"stop": state.get("stop"), "calls": [item["call"] for item in refused], "purpose": headline,
                               "effect_purposes": refused,
                               "incumbent": state.get("incumbent"), "allowance": state.get("allowance"),
                               **({"candidate": "none"} if not state.get("incumbent") else {})},
                      next_action=next_action)


def _late_decision(ctx: Ctx, turn: dict, inv, slot: int, attempt: int | None, epoch: int | None = None) -> str | None:
    """Why a proposed decision may no longer be applied (ADR 0017), or None:

    * ``investigation-<status>``: the investigation is no longer active (stopped, paused, concluded);
    * ``superseded-attempt``: a later attempt of the same decision slot has started (``begin_decision`` bumped the
      slot's epoch), so this one was cancelled or timed out and its request only finished late;
    * ``decision-deadline-passed``: the attempt began (lab clock) more than the turn deadline ago, the decide
      activity's own timeout, so Temporal has already given up on it.

    Callers that carry the epoch from ``begin_decision`` (the decide activity, F2c) are checked against it; callers
    that report only their attempt number keep the earlier attempt-number check; others are checked as before. The
    caller holds the turn row lock, which ``begin_decision`` also takes, so the check and the epoch bump are ordered."""
    if inv.status != "active":
        return f"investigation-{inv.status}"
    if attempt is None and epoch is None:
        return None
    rec = ctx.tx.get_record("decision_attempt", f"{turn['id']}:{slot}")
    if rec is None:
        return "superseded-attempt" if epoch is not None else None
    if epoch is not None:
        if int(epoch) != int(rec.body.get("epoch") or 0):
            return "superseded-attempt"
    elif int(attempt or 0) < int(rec.body.get("attempt", 0)):
        return "superseded-attempt"
    current = epoch is not None or int(attempt or 0) == int(rec.body.get("attempt", 0))
    deadline = float((turn["spec"].get("limits") or {}).get("deadline_seconds") or 0)
    if current and deadline and time.time() > float(rec.body.get("started_at", 0)) + deadline:
        return "decision-deadline-passed"
    return None


def _note_attempt(ctx: Ctx, turn: dict, slot: int, attempt: int) -> None:
    """Record that ``attempt`` of a decision slot has started (lab clock), unless a later one already has (callers
    that do not use ``begin_decision``)."""
    rid = f"{turn['id']}:{slot}"
    rec = ctx.tx.get_record("decision_attempt", rid, lock=True)
    if rec is not None and int(rec.body.get("attempt", 0)) >= attempt:
        return
    ctx.tx.put_record("decision_attempt", rid, ctx.project_id,
                      {**(rec.body if rec else {}), "turn_id": turn["id"], "slot": slot, "attempt": attempt,
                       "started_at": time.time()}, created_by=ctx.principal.id, status="started")


def _check_something_to_wait_for(ctx: Ctx, inv, decision: dict) -> None:
    """``wait_for_jobs`` with nothing outstanding would park the investigation until the max-idle timer
    (no job, evidence or assessment event will arrive). Refuse it as an invalid proposal; the refusal is
    fed back within the turn and counts against the turn's invalid-proposal limit."""
    control = decision.get("control") or {}
    implicit_wait = not control.get("action") and not decision["calls"]  # the turn ends "waiting" on it too
    if control.get("action") != "wait_for_jobs" and not implicit_wait:
        return
    # A bounded no-effect outcome (request budget spent) is checked like any wait (Mac request 12): with nothing
    # outstanding it would park the investigation, so it takes the refused-proposal path (bounded, then surfaces).
    if decision["calls"]:
        # Refusing would also drop this decision's effects (e.g. a publish). Their events wake the loop
        # (see check_news NEWS_TYPES), so only a pure wait with nothing outstanding is refused.
        return
    idle = ("succeeded", "failed", "cancelled", "pruned", "planned")  # planned trials run only when launched
    if any(j["status"] not in idle for j in ctx.tx.list_jobs(ctx.project_id, investigation_id=inv.id, limit=100000)):
        return
    executing = [r for r in ctx.tx.list_records(ctx.project_id, "evidence_plan", status="executing")
                 if r.body.get("investigation_id") == inv.id]
    if executing:  # evidence arms still pending or an assessment still to be committed
        return
    raise Invalid("nothing to wait for: no jobs or evidence plans are outstanding for this investigation; run "
                  "something, request input, or conclude", details={"control": "wait_for_jobs", "outstanding": 0})


def _environment_names(ctx: Ctx) -> list[str]:
    from eidos.application.handlers.projects import get_profile
    try:
        return sorted(get_profile(ctx).body.get("environments") or {})
    except Exception:  # noqa: BLE001
        return []


def _check_resource_policy(ctx: Ctx, decision: dict) -> None:
    """ADR 0022: jobs of a decision run one at a time unless the decision asks for concurrency AND the project's policy
    allows that much. A model cannot grant it itself."""
    from eidos.application.handlers.projects import checked_concurrency
    checked_concurrency(ctx, (decision.get("resource_policy") or {}).get(turn_rules.MAX_CONCURRENT_JOBS_KEY))


def _check_component_contracts(ctx: Ctx, calls: list[dict]) -> None:
    """A decision is accepted only if each component invocation satisfies that release's declared
    configuration contract (so accepted means runnable, not merely well-formed)."""
    for i, c in enumerate(calls):
        if c["tool"] in ("author_component", "run_component", "plan_trial", "plan_branch") \
                and c["args"].get("environment") is not None or c["tool"] == "author_component":
            env = c["args"].get("environment", "default")
            from eidos.application.handlers.components import env_for
            if not isinstance(env, str):
                env_names = _environment_names(ctx)
                raise Invalid(f"call {i} ({c['tool']}): environment must be a registered environment name string, "
                              f"not {type(env).__name__}; available: {env_names}",
                              details={"call": i, "available": env_names})
            try:
                env_for(ctx, env)
            except Invalid as exc:
                raise Invalid(f"call {i} ({c['tool']}): environment {env!r} is not registered; available: "
                              f"{_environment_names(ctx)}", details={"call": i, "available": _environment_names(ctx)}) from exc
        if c["tool"] not in ("run_component", "plan_trial", "plan_branch"):
            continue
        rel = ctx.tx.get_record("component_release", c["args"].get("component_ref", ""))
        if rel is None:
            raise Invalid(f"call {i}: unknown component {c['args'].get('component_ref')!r}",
                          details={"call": i, "tool": c["tool"]})
        schema = rel.body["manifest"].get("config_schema")
        config = c["args"].get("config") or {}
        # A config holding a reference has its final value only at execution; resolve_lock checks the contract then.
        if schema and not any(True for _ in turn_rules.iter_refs(config)):
            problems = validate_config(config, schema)
            if problems:
                raise Invalid(f"call {i}: config violates {rel.id} contract", details={"call": i, "problems": problems,
                              "config_schema": schema})
    _check_output_refs(ctx, calls)


def _check_output_refs(ctx: Ctx, calls: list[dict]) -> None:
    """ADR 0022: ``call[i].outputs.<slot>`` must name an output the referenced component declares, when it declares them
    (a ``dynamic_outputs`` component, or a trial launched by id, is resolved at execution)."""
    for i, c in enumerate(calls):
        for path, text in turn_rules.iter_refs(c["args"]):
            target, field = turn_rules.parse_ref(text) or (-1, "")
            if not field.startswith("outputs."):
                continue
            source = calls[target]
            if source["tool"] != "run_component":
                continue
            rel = ctx.tx.get_record("component_release", source["args"]["component_ref"])
            manifest = rel.body["manifest"] if rel is not None else {}
            declared = manifest.get("output") or {}
            slot = field.split(".", 1)[1]
            if not manifest.get("dynamic_outputs") and slot not in declared:
                raise Invalid(f"call {i} ({c['tool']}): {path}: {text!r} names no output of "
                              f"{source['args']['component_ref']}; it declares {sorted(declared)}",
                              details={"call": i, "path": path, "ref": text, "declared": sorted(declared)})


STRUCTURED_OUTPUT_DETAIL_BYTES = 16_384


def _structured_output_detail(report: Any) -> dict:
    """Cap the ledger copy, including escaped previews, at 16 KiB of serialized JSON."""
    if not isinstance(report, dict):
        return {}
    encoded = json.dumps(report, default=str, ensure_ascii=True)
    if len(encoded) <= STRUCTURED_OUTPUT_DETAIL_BYTES:
        return report
    # encoded is ASCII without literal control characters: re-encoding its preview can at most double it.
    return {"truncated": True, "preview": encoded[:(STRUCTURED_OUTPUT_DETAIL_BYTES - 64) // 2]}


def _charge_model_usage(ctx: Ctx, inv_id: str, usage: dict, *, ref: str, reserved: dict | None = None) -> None:
    """Settle actual model usage against any per-decision reservation. Usage entries are written
    even for dimensions without a budget row, so unbudgeted spend is still on the ledger.

    F2c: the tokens and dollars ``usage`` reports are those of completed responses (certain unless ``uncertain``).
    A request abandoned in flight (including a recovered finalization timeout) may still be billed: its estimate
    (``in_flight_estimate``: the request's admitted input estimate plus output cap, priced) is charged as uncertain
    ``possible-usage``, never as zero; without one, the rest of the reservation is. If the reservation was already
    reconciled as provisional uncertain usage (the turn closed or a retry began first), that provisional charge is
    reversed and replaced by what is reported here, so a late settlement never charges twice."""
    scope = inv_scope(inv_id)
    reserved = reserved or {}
    uncertain = bool(usage.get("uncertain"))
    detail = {k: usage.get(k) for k in ("provider", "model", "request_ids", "calls")}
    # ADRs 0023/0042: retain attempt timing, failure and structured finalization evidence on ledger entries too.
    for k in ("attempt", "elapsed_seconds", "failure", "phase", "runtime_outcome"):
        if usage.get(k) is not None:
            detail[k] = usage[k]
    if usage.get("structured_output") is not None:
        detail["structured_output"] = _structured_output_detail(usage["structured_output"])
    if usage.get("aborted"):  # ADR 0017: why the decision ended and what was abandoned in flight
        detail["aborted"] = usage["aborted"]
    if usage.get("reasoning_tokens") is not None:  # already inside output_tokens (ADR 0016); on the ledger too
        detail["reasoning_tokens"] = usage["reasoning_tokens"]
    tokens = float(usage.get("input_tokens", 0) or 0) + float(usage.get("output_tokens", 0) or 0)
    usd = float(usage.get("usd") or 0)
    in_flight = bool((usage.get("aborted") or {}).get("in_flight") or usage.get("abandoned_requests"))
    est = usage.get("in_flight_estimate")
    estimate: dict[str, float | None] = {"model_tokens": None, "model_usd": None}
    if isinstance(est, dict):
        estimate["model_tokens"] = float(est.get("input_tokens") or 0) + float(est.get("output_tokens") or 0)
        estimate["model_usd"] = float(est["usd"]) if est.get("usd") is not None else None
    still_open = _open_by_ref(ctx, scope, ref) if reserved else {}
    provisional = _provisional_by_ref(ctx, scope, ref)
    # Completed responses reported their usage: with a request abandoned in flight, the uncertainty is that request's.
    known_uncertain = uncertain and not in_flight
    for dim, actual in (("model_tokens", tokens), ("model_usd", usd)):
        back = provisional.get(dim, 0.0)
        if back:
            ctx.tx.reverse_possible_usage(scope, dim, back, ref, detail={"reverses": "provisional",
                                                                         "replaced_by": "settlement"})
        # Never release more than is still open under this ref (a retry may already have released it).
        held = min(float(reserved.get(dim, 0)), still_open.get((dim, ref), 0.0))
        if not in_flight:
            if actual or held:
                ctx.tx.settle(scope, dim, reserved=held, actual=actual, ref=ref, uncertain=known_uncertain,
                              detail=detail)
            continue
        on_known = min(held, actual)
        if actual:
            ctx.tx.settle(scope, dim, reserved=on_known, actual=actual, ref=ref, uncertain=known_uncertain,
                          detail=detail)
        rest = held - on_known
        guess = estimate[dim]
        if guess is None:  # no per-request estimate: all this ref still held (reserved or provisional) may be billed
            guess = max(held + back - actual, 0.0)
        if guess or rest:
            ctx.tx.settle(scope, dim, reserved=rest, actual=guess, ref=ref, uncertain=True,
                          detail={**detail, "in_flight_estimate": True})


def _provisional_by_ref(ctx: Ctx, scope: str, ref: str) -> dict[str, float]:
    """Per dimension, provisional uncertain usage still charged under exactly ``ref`` (reconciled reservations not yet
    replaced by a settlement)."""
    acc: dict[str, float] = {}
    for e in ctx.tx.budget_entries(scope):
        if e["ref"] != ref:
            continue
        if e["kind"] == "possible-usage" and (e.get("detail") or {}).get("provisional"):
            acc[e["dimension"]] = acc.get(e["dimension"], 0.0) + float(e["amount"])
        elif e["kind"] == "correction" and (e.get("detail") or {}).get("reverses") == "provisional":
            acc[e["dimension"]] = acc.get(e["dimension"], 0.0) + float(e["amount"])  # negative
    return {k: v for k, v in acc.items() if v > 1e-12}


def reconcile_open_model_reservations(ctx: Ctx, scope: str, prefix: str, *, by: str,
                                      keep: str | None = None) -> dict[str, float]:
    """Settle every model reservation still open under ``prefix`` (except ``keep``) as provisional uncertain usage,
    per dimension the whole open amount (F2c). An open reservation means its decide attempt neither settled nor
    failed cleanly: it was cancelled, timed out or died, possibly with a request at the provider, whose bill is
    unknown. The whole open amount is the conservative bound (the token envelope keeps a decision inside its
    reservation). Settled refs are no longer open, so this is idempotent; a late settlement of the same ref reverses
    the provisional charge and books what it reports instead (``_charge_model_usage``). The caller holds the turn
    row lock, which every settlement of the turn's reservations takes."""
    charged: dict[str, float] = {}
    for (dim, ref), left in sorted(_open_by_ref(ctx, scope, prefix).items()):
        if ref == keep:
            continue
        ctx.tx.settle(scope, dim, reserved=left, actual=left, ref=ref, uncertain=True,
                      detail={"provisional": True, "reconciled_by": by})
        charged[dim] = charged.get(dim, 0.0) + left
    return charged


def _open_by_ref(ctx: Ctx, scope: str, prefix: str) -> dict[tuple[str, str], float]:
    """Open (reserved minus settled) model reservations per (dimension, exact ref) under ``prefix``."""
    acc: dict[tuple[str, str], float] = {}
    for e in ctx.tx.budget_entries(scope):
        if not str(e["ref"]).startswith(prefix):
            continue
        k = (e["dimension"], e["ref"])
        if e["kind"] == "reserve":
            acc[k] = acc.get(k, 0.0) + float(e["amount"])
        elif e["kind"] in ("release", "usage", "possible-usage"):
            acc[k] = acc.get(k, 0.0) - float(e["amount"])
    return {k: v for k, v in acc.items() if v > 0}


@command("reserve_model_budget", "turn")
def reserve_model_budget(ctx: Ctx, p: dict) -> dict:
    """Reserve one decision's maximum model envelope before its requests are sent. Raises
    BUDGET_EXHAUSTED (no model call) when the investigation's remaining budget cannot cover it."""
    # The turn row lock serializes every read-then-settle of this turn's model reservations (reserve_model_budget,
    # record_model_usage, complete_turn): two transactions never both see the same reservation open (F2b).
    turn = _turn(ctx, p["turn_id"], lock=True)
    scope = inv_scope(turn["investigation_id"])
    budgets = ctx.tx.budgets(scope)
    # F5 (C2): the reservation ref carries the slot epoch, unique per begin_decision across executions (a resumed
    # turn's execution starts again at attempt 1), so reconciliation and late settlements never touch another
    # execution's reservation. Callers without begin_decision keep the attempt-only ref.
    ref = f"model:{p['turn_id']}:{p.get('slot')}:{p.get('attempt')}" + (
        f":e{int(p['epoch'])}" if p.get("epoch") is not None else "")
    refused = _execution_refused(ctx, turn, str(p.get("execution") or ""))
    if refused:  # F5 (C1): a run that lost the turn reserves nothing
        raise Invalid("superseded decision execution: the turn was resumed by another run",
                      details={"reason": refused})
    if p.get("epoch") is not None:  # F2c: a superseded attempt (a later one began) reserves nothing
        rec = ctx.tx.get_record("decision_attempt", f"{turn['id']}:{int(p.get('slot') or 0)}")
        if rec is None or int(rec.body.get("epoch") or 0) != int(p["epoch"]):
            raise Invalid("superseded decision attempt: a later attempt of this decision slot has begun",
                          details={"reason": "superseded-attempt", "epoch": p["epoch"]})
    # Only one attempt of a decision slot runs at a time: a reservation left open by an earlier attempt of this slot
    # belongs to an attempt that was cancelled, timed out or died, possibly with a request in flight. It is settled as
    # provisional uncertain usage (F2c), not released as zero; a late settlement from that attempt replaces it.
    reconcile_open_model_reservations(ctx, scope, f"model:{p['turn_id']}:{p.get('slot')}:", by=ref, keep=ref)
    if p.get("attempt") is not None and p.get("epoch") is None:  # callers without begin_decision
        _note_attempt(ctx, turn, int(p.get("slot") or 0), int(p["attempt"]))
    tokens = float((p.get("estimate") or {}).get("model_tokens") or 0)
    if tokens > 0 and not turn["spec"].get("finalization") \
            and (turn["spec"].get("exploration") or {}).get("phase") in exploration_rules.EXPLORING_PHASES \
            and isinstance(budgets.get("model_tokens"), dict):
        # R14 #4: an exploratory decision's reservation must fit in what is left before the token hold-back, as of now
        # (other decisions of the turn have reserved and spent since it began); the hold-back is for finalization.
        inv = ctx.tx.require("investigation", turn["investigation_id"])
        left = exploration_rules.allowance(exploration_rules.policy(inv.body.get("investigator")), None, None,
                                           {"model_tokens": budgets["model_tokens"]})["tokens_left"]
        if left is not None and tokens > left:
            raise BudgetExhausted("the exploration allowance cannot cover this decision: the rest of the model-token "
                                  "budget is held back for reproduction, export and the conclusion",
                                  details={"scope": scope, "reason": "exploration-holdback", "estimate": tokens,
                                           "tokens_left": left})
    out = {}
    for dim, amount in (p.get("estimate") or {}).items():
        if dim in ("model_tokens", "model_usd") and dim in budgets and float(amount) > 0:
            ctx.tx.reserve(scope, dim, float(amount), ref)
            out[dim] = float(amount)
    return {"reserved": out, "reservation_ref": ref}


def release_turn_model_reservations(ctx: Ctx, turn: dict) -> dict:
    """Reconcile model reservations of a closing turn that were never settled (its decide attempt was cancelled,
    timed out or died): each is charged as provisional uncertain usage, not released as zero (F2c). The caller
    holds the turn row lock (complete_turn), which serializes this with record_model_usage and reserve_model_budget.
    Charged under each reservation's own ref (G7 re-check), so a late settlement finds and replaces it."""
    return reconcile_open_model_reservations(ctx, inv_scope(turn["investigation_id"]), f"model:{turn['id']}:",
                                             by="complete_turn")


@command("record_model_usage", "turn")
def record_model_usage(ctx: Ctx, p: dict) -> dict:
    """Record model usage that is not attached to an accepted decision (failed/timeout calls).
    A timeout after possible charge is recorded as possible usage with its request ID.

    The turn row is locked before the reservation is read (F2b): a concurrent complete_turn, a retry's
    reserve_model_budget or another settlement of the same reservation waits, then sees it settled, so it is
    released once."""
    turn = _turn(ctx, p["turn_id"], lock=True)
    ref = p.get("reservation_ref") or f"usage:{p['turn_id']}"
    if p.get("reserved") and not str(ref).startswith(f"model:{turn['id']}:"):
        # Only this turn's reservations are serialized by its lock; settling another turn's would bypass it.
        raise Invalid("reservation_ref must be a model reservation of this turn", details={"reservation_ref": ref})
    _charge_model_usage(ctx, turn["investigation_id"], p.get("usage") or {}, ref=ref, reserved=p.get("reserved"))
    ctx.event("model.usage", p["turn_id"], {"usage": p.get("usage")}, investigation_id=turn["investigation_id"])
    return {"recorded": True}


@command("complete_turn", "turn")
def complete_turn(ctx: Ctx, p: dict) -> dict:
    turn = _turn(ctx, p["turn_id"], lock=True)
    if turn["status"] != "open":
        receipt = ctx.tx.get_record("finalization_export_receipt", turn["investigation_id"])
        abstention = receipt.body.get("controller_abstention") if receipt and receipt.body.get("turn_id") == turn["id"] else None
        refused = (turn.get("result") or {}).get("control_refused")
        if not abstention and refused == turn_rules.WALL_REACHED:
            inv = ctx.tx.require("investigation", turn["investigation_id"])
            abstention = inv.body.get("controller_abstention")
        return {"turn_id": turn["id"], "status": turn["status"], "already_closed": True,
                **({"control_refused": refused} if refused == turn_rules.WALL_REACHED else {}),
                **({"controller_abstention": abstention} if abstention else {})}
    refused = _execution_refused(ctx, turn, str(p.get("execution") or ""))
    if refused:  # F5 (C1): a run that lost the turn to a resuming run never closes it under the new owner
        return {"turn_id": turn["id"], "status": turn["status"], "superseded": refused}
    return _close_turn(ctx, turn, p)


def close_abandoned_turn(ctx: Ctx, inv_id: str, failure: Any) -> str | None:
    """Close the investigation's last turn if it is still open, as failed (F5, A2). Called by the coordinator's
    failed-turn policy (ADR 0019) before an automatic retry or a needs-input wait: the turn's workflow failed (e.g.
    its execution timeout) without completing it. Its open model reservations are charged as uncertain usage and its
    turns reservation is settled, like any closing turn; a late acceptance from the abandoned workflow then finds the
    turn closed and is refused. Lock order: the turn row (here), then the investigation row (callers)."""
    last = ctx.tx.last_turn(inv_id)
    if not last or last["status"] != "open":
        return None
    turn = _turn(ctx, last["id"], lock=True)
    if turn["status"] != "open":
        return None
    why = {**_bounded_details(failure), "abandoned": True} if isinstance(failure, dict) else \
        {"error_type": "turn-workflow-failed", "retryable": True, "abandoned": True}
    _close_turn(ctx, turn, {"stop_reason": "error", "summary": "turn workflow failed; turn closed by the coordinator",
                            "failure": why})
    return turn["id"]


def _close_turn(ctx: Ctx, turn: dict, p: dict) -> dict:
    """Close an open turn (the caller holds its row lock): result, reservations, turns budget, event."""
    reason = p.get("stop_reason", "calls-exhausted")
    if reason not in turn_rules.STOP_REASONS:
        raise Invalid(f"stop_reason must be one of {turn_rules.STOP_REASONS}")
    control = p.get("control") or {}
    inv_id = turn["investigation_id"]
    control_refused = None
    if control.get("action") in ("conclude", "stop"):
        # F5d (review R9): the conclusion is applied only to an investigation that is still active, checked under its
        # row lock (lock order: the turn row, held by the caller, then the investigation row). A stop or pause that
        # committed after the decision was accepted (before or after its effects ran) always wins: the turn closes as
        # cancelled and the operator's state is never overwritten with ``concluded``.
        inv = ctx.tx.require("investigation", inv_id, lock=True)
        if inv.status != "active":
            control_refused = f"investigation-{inv.status}"
            if reason == "concluded":
                reason = "cancelled"
        elif reason == "concluded":
            _check_conclusion_scope(inv, control)
    uncertain = release_turn_model_reservations(ctx, turn)
    budgets = ctx.tx.budgets(inv_scope(inv_id))
    if "turns" in budgets:
        ctx.tx.settle(inv_scope(inv_id), "turns", reserved=1, actual=1, ref=turn["id"])
    summary = str(p.get("summary", ""))[:4000]
    if reason == "concluded" and control.get("action") in ("conclude", "stop") and not control_refused:
        from eidos.application.handlers.assessment import conclusion_claim_state
        from eidos.domain.assessment import CONCLUSION_SCHEMA
        # ADR 0021: the claim state comes from recorded evidence and assessor state.
        claim_state = conclusion_claim_state(ctx, inv, control)
        body = {**inv.body, "conclusion": {
            "schema": CONCLUSION_SCHEMA, "turn_id": turn["id"], "summary": control.get("summary") or summary,
            "scope": control.get("scope"), "action": control["action"], "claim_state": claim_state,
            "incumbent": exploration.final_incumbent(ctx, inv),
        }}
        # Check under the investigation lock immediately before the durable conclusion write.
        wall = inv.body.get("wall")
        if wall and time.time() >= wall["deadline_unix"]:
            control_refused = turn_rules.WALL_REACHED
            reason = "deadline"
        else:
            ctx.tx.put_record("investigation", inv_id, ctx.project_id, body, created_by=ctx.principal.id,
                              status="concluded")
            ctx.event("assessment.claim_state", inv_id, {k: claim_state.get(k) for k in (
                "state", "confirmed", "claim_id", "assessment_id", "requested_disposition")}, investigation_id=inv_id)
    late_wall = control_refused == turn_rules.WALL_REACHED
    status = "cancelled" if reason == "cancelled" else ("failed" if reason == "error" else "completed")
    result = {"stop_reason": reason, "summary": summary, "receipts": p.get("receipts", []),
              "unfinished_handles": p.get("unfinished_handles", []), "control": p.get("control"),
              "uncertain_model_reservations": uncertain,
              **({"control_refused": control_refused} if control_refused else {})}
    if p.get("runtime_outcome"):
        result["runtime_outcome"] = p["runtime_outcome"]
    failure = _bounded_details(redact_value(p.get("failure"))) if reason == "error" else {}
    if failure:
        result["failure"] = failure
    ctx.tx.update_turn(turn["id"], status=status, result=result)
    abstention = None
    if turn["spec"].get("finalization"):
        inv = ctx.tx.require("investigation", inv_id, lock=True)
        final = ctx.tx.require(FINALIZATION_KIND, inv_id)
        runtime_record = ctx.tx.get_record("runtime_outcome", p["runtime_record_id"]) if p.get("runtime_record_id") else None
        if (runtime_record and runtime_record.body.get("turn_id") == turn["id"]
                and runtime_record.body.get("terminal_cause") in turn_rules.AUTHORED_FINALIZATION_FAILURES
                and reason == "runtime-bounded" and not late_wall):
            failure = {"kind": "investigator-output-invalid", "reason": runtime_record.body["terminal_cause"],
                       "runtime_record_id": runtime_record.id, "problems": runtime_record.body["problems"]}
        receipt = {"schema": "finalization-export-receipt/1", "investigation_id": inv_id,
                   "turn_id": turn["id"], "locked_candidate": turn["spec"]["finalization"].get("locked_candidate"),
                   **{k: turn["spec"]["finalization"].get(k) for k in (
                       "metrics", "selection_rule", "refusals", "runtime_outcome")},
                   "remaining_budget": ctx.tx.budgets(inv_scope(inv_id)),
                   "finalization_refusals": [{"decision_id": d["id"], "error": d["body"].get("error"),
                                               "response_artifact_id": d["body"].get("response_artifact_id")}
                                              for d in ctx.tx.decisions(turn["id"]) if not d["accepted"]],
                   "finalization_runtime_outcome": p.get("runtime_outcome"),
                   "incumbent": turn["spec"]["finalization"].get("incumbent"),
                   "provenance_limits": turn["spec"]["finalization"]["provenance_limits"],
                   "investigator_concluded": reason == "concluded" and not control_refused,
                   "stop_reason": reason, "failure": failure or ({"kind": "finalization-incomplete",
                                                                   "reason": turn_rules.WALL_REACHED if late_wall else reason}
                                                                 if reason != "concluded" else None)}
        abstention = _finish_finalization(ctx, inv, final, receipt, turn_rules.WALL_REACHED if late_wall else reason)
    elif late_wall:
        abstention = _record_controller_abstention(
            ctx, inv, code="coordinator-stopped", reason=turn_rules.WALL_REACHED,
            failure={"error_type": turn_rules.WALL_REACHED}, incumbent=exploration.final_incumbent(ctx, inv))
    ctx.event("turn.completed", turn["id"], {"stop_reason": reason, "status": status,
                                             **({"failure": failure} if failure else {}),
                                             **({"control_refused": control_refused} if control_refused else {})},
              investigation_id=inv_id)
    return {"turn_id": turn["id"], "status": status,
            **({"control_refused": control_refused} if late_wall else {}),
            **({"controller_abstention": abstention} if abstention else {})}


def _turn(ctx: Ctx, tid: str, *, lock: bool = False) -> dict:
    """A turn of THIS project (turn ids are global; project access is checked here)."""
    turn = ctx.tx.get_turn(tid, lock=lock)
    if not turn or turn["project_id"] != ctx.project_id:
        raise Invalid("unknown turn")
    return turn


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
    change = inv.body.get("question_change") or {}
    if change.get("policy") in ("cancel", "checkpoint-and-stop") and ctx.principal.has("turn") \
            and not ctx.principal.has("steer"):
        last = ctx.tx.last_turn(inv.id)
        if last and last["status"] == "open" and int(last["study_revision"]) < int(change["investigation_revision"]):
            raise QuestionRevised(f"the question of {inv_id} was revised (question revision "
                                  f"{change['question_revision']}, policy {change['policy']}) after turn "
                                  f"{last['id']} began; its remaining calls admit no new work",
                                  details={"turn_id": last["id"], **change})
    return inv.body["charter_ref"]


def ensure_budget_left(ctx: Ctx, scope: str, dim: str, amount: float) -> None:
    b = ctx.tx.budgets(scope).get(dim)
    if b and b["reserved"] + b["used"] + amount > b["limit_amount"]:
        raise BudgetExhausted(f"{dim} budget exhausted for {scope}", details={"scope": scope, **b})


@command("record_decision_receipts", "turn")
def record_decision_receipts(ctx: Ctx, p: dict) -> dict:
    """Store the ordered receipts of an accepted decision's calls (audit + next-slot context)."""
    dec = ctx.tx.get_decision(p["decision_id"])
    if dec:
        _turn(ctx, dec["turn_id"])
    if not dec or not dec["accepted"]:
        raise Invalid("receipts can only be recorded for an accepted decision")
    if decision_revoked(ctx, dec["id"]):  # F2c: a revoked decision is never executed
        raise Invalid("receipts cannot be recorded for a revoked decision")
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
    incumbent = None
    if inv_id and body.get("kind") in exploration.NOTE_KINDS:  # ADR 0023: rule fixed, re-lock only on better evidence
        incumbent = exploration.check_note(ctx, inv_id, body)
        if incumbent is not None:
            if prior := incumbent.get("reaffirmed_lock"):
                return {**prior, "reaffirmed": True}
            body["incumbent"] = incumbent
    ctx.tx.put_record("note", nid, ctx.project_id, body, created_by=ctx.principal.id, status=p.get("status") or "open",
                      parent_id=inv_id)
    ctx.event("note.recorded", nid, {"kind": p.get("kind")}, investigation_id=inv_id)
    if incumbent is not None and inv_id:
        exploration.recorded(ctx, nid, inv_id, incumbent)
    if inv_id and body.get("kind") == "baseline":  # R14 #7: an explicit baseline designation
        exploration.designate_baseline(ctx, inv_id, body)
    return {"note_id": nid, **({"incumbent": {k: incumbent.get(k) for k in ("version", "basis", "evidence")}}
                               if incumbent else {})}
