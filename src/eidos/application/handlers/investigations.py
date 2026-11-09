"""Investigations, steering, pause/resume and bounded investigator turns."""

from __future__ import annotations

import uuid

from eidos.application.service import Ctx, command
from eidos.domain import charter as charter_rules
from eidos.domain import turns as turn_rules
from eidos.domain.errors import BudgetExhausted, Invalid, QuestionRevised, StaleRevision

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
    _clear_needs_input(ctx, inv, inv.status, "question-revised")
    ctx.event("investigation.question_revised", inv.id,
              {"question_revision": qrev, "revision": rec.revision, "by": ctx.principal.id, "policy": policy,
               "admitted_work": affected, "admitted_under_question_revision": qrev - 1,
               "exposure_id": exposure_id}, investigation_id=inv.id, wake=True)
    return {"investigation_id": inv.id, "revision": rec.revision, "question_revision": qrev,
            "running_job_policy": policy, "admitted_work": affected, "exposure_id": exposure_id,
            "effective": "next-turn"}


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


@command("mark_needs_input", "turn")
def mark_needs_input(ctx: Ctx, p: dict) -> dict:
    """The coordinator surfaces an investigation that keeps producing refused proposals: durable and visible
    (investigation label, event) until an operator steers, resumes or stops it."""
    inv = ctx.tx.require("investigation", p["investigation_id"], lock=True)
    if (inv.labels or {}).get("needs_input"):
        return {"investigation_id": inv.id, "needs_input": inv.labels["needs_input"], "already": True}
    from datetime import UTC, datetime
    mark = {"since": datetime.now(UTC).isoformat(), "reason": str(p.get("reason", ""))[:300],
            "refused_turns": int(p.get("refused_turns", 0))}
    ctx.tx.set_status("investigation", inv.id, inv.status, labels={"needs_input": mark})
    ctx.event("investigation.needs_input", inv.id, mark, investigation_id=inv.id)
    return {"investigation_id": inv.id, "needs_input": mark, "already": False}


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


@command("accept_decision", "turn")
def accept_decision(ctx: Ctx, p: dict) -> dict:
    """Persist a proposed decision. Exactly one decision is accepted per (turn, slot); a late or
    stale proposal is kept as an unapplied record with no authority to execute tools."""
    turn = _turn(ctx, p["turn_id"], lock=True)
    slot = int(p.get("slot", 0))
    existing = ctx.tx.accepted_decision(turn["id"], slot)
    did = f"dec_{uuid.uuid4().hex[:16]}"
    usage = p.get("usage") or {}
    if not p.get("usage_recorded"):  # recorded earlier by record_model_usage (first durable write)
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
        proposal = dict(p.get("proposal") or {})
        if isinstance(proposal.get("control"), dict):  # only the trusted runtime path may mark a bounded outcome
            ctl = {k: v for k, v in proposal["control"].items() if k != "bounded_outcome"}
            if p.get("bounded_outcome") and (p.get("usage") or {}).get("limit_reached"):
                ctl["bounded_outcome"] = str(p["bounded_outcome"])[:64]
            proposal["control"] = ctl
        decision = turn_rules.validate_decision(proposal, max_calls=turn["spec"]["limits"]["max_calls"])
        _check_component_contracts(ctx, decision["calls"])
        _check_something_to_wait_for(ctx, inv, decision)
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


def _check_component_contracts(ctx: Ctx, calls: list[dict]) -> None:
    """A decision is accepted only if each component invocation satisfies that release's declared
    configuration contract (so accepted means runnable, not merely well-formed)."""
    from eidos.domain.contracts import validate_config
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
        if schema:
            problems = validate_config(c["args"].get("config") or {}, schema)
            if problems:
                raise Invalid(f"call {i}: config violates {rel.id} contract", details={"call": i, "problems": problems,
                              "config_schema": schema})


def _charge_model_usage(ctx: Ctx, inv_id: str, usage: dict, *, ref: str, reserved: dict | None = None) -> None:
    """Settle actual model usage against any per-decision reservation. Usage entries are written
    even for dimensions without a budget row, so unbudgeted spend is still on the ledger."""
    scope = inv_scope(inv_id)
    reserved = reserved or {}
    uncertain = bool(usage.get("uncertain"))
    detail = {k: usage.get(k) for k in ("provider", "model", "request_ids", "calls")}
    tokens = float(usage.get("input_tokens", 0) or 0) + float(usage.get("output_tokens", 0) or 0)
    usd = float(usage.get("usd") or 0)
    still_open = _open_by_ref(ctx, scope, ref) if reserved else {}
    for dim, actual in (("model_tokens", tokens), ("model_usd", usd)):
        # Never release more than is still open under this ref (a retry may already have released it).
        held = min(float(reserved.get(dim, 0)), still_open.get((dim, ref), 0.0))
        if actual or held:
            ctx.tx.settle(scope, dim, reserved=held, actual=actual, ref=ref, uncertain=uncertain, detail=detail)


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
    turn = _turn(ctx, p["turn_id"])
    scope = inv_scope(turn["investigation_id"])
    budgets = ctx.tx.budgets(scope)
    ref = f"model:{p['turn_id']}:{p.get('slot')}:{p.get('attempt')}"
    # Only one attempt of a decision slot runs at a time: a reservation left by an earlier attempt of
    # this slot belongs to a dead activity. Release it so the retry does not collide with it (a late
    # settlement from that attempt is capped at what is still open, so nothing is released twice).
    for (dim, old_ref), left in _open_by_ref(ctx, scope, f"model:{p['turn_id']}:{p.get('slot')}:").items():
        if old_ref != ref:
            ctx.tx.settle(scope, dim, reserved=left, actual=0, ref=old_ref, detail={"released_by": ref})
    out = {}
    for dim, amount in (p.get("estimate") or {}).items():
        if dim in ("model_tokens", "model_usd") and dim in budgets and float(amount) > 0:
            ctx.tx.reserve(scope, dim, float(amount), ref)
            out[dim] = float(amount)
    return {"reserved": out, "reservation_ref": f"model:{p['turn_id']}:{p.get('slot')}:{p.get('attempt')}"}


def release_turn_model_reservations(ctx: Ctx, turn: dict) -> dict:
    """Release model reservations of a turn that were never settled (e.g. the activity died)."""
    scope = inv_scope(turn["investigation_id"])
    released: dict[str, float] = {}
    # Release under each reservation's own ref (G7 re-check): a release booked under a separate ref
    # left the original ref looking open, so a late settlement from a zombie attempt released the
    # same amount again and freed budget held by other reservations.
    for (dim, ref), left in sorted(_open_by_ref(ctx, scope, f"model:{turn['id']}:").items()):
        ctx.tx.settle(scope, dim, reserved=left, actual=0, ref=ref, detail={"released_by": "complete_turn"})
        released[dim] = released.get(dim, 0.0) + left
    return released


@command("record_model_usage", "turn")
def record_model_usage(ctx: Ctx, p: dict) -> dict:
    """Record model usage that is not attached to an accepted decision (failed/timeout calls).
    A timeout after possible charge is recorded as possible usage with its request ID."""
    turn = _turn(ctx, p["turn_id"])
    _charge_model_usage(ctx, turn["investigation_id"], p.get("usage") or {},
                        ref=p.get("reservation_ref") or f"usage:{p['turn_id']}", reserved=p.get("reserved"))
    ctx.event("model.usage", p["turn_id"], {"usage": p.get("usage")}, investigation_id=turn["investigation_id"])
    return {"recorded": True}


@command("complete_turn", "turn")
def complete_turn(ctx: Ctx, p: dict) -> dict:
    turn = _turn(ctx, p["turn_id"], lock=True)
    if turn["status"] != "open":
        return {"turn_id": turn["id"], "status": turn["status"], "already_closed": True}
    reason = p.get("stop_reason", "calls-exhausted")
    if reason not in turn_rules.STOP_REASONS:
        raise Invalid(f"stop_reason must be one of {turn_rules.STOP_REASONS}")
    status = "cancelled" if reason == "cancelled" else ("failed" if reason == "error" else "completed")
    result = {"stop_reason": reason, "summary": str(p.get("summary", ""))[:4000], "receipts": p.get("receipts", []),
              "unfinished_handles": p.get("unfinished_handles", []), "control": p.get("control")}
    result["released_model_reservations"] = release_turn_model_reservations(ctx, turn)
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
