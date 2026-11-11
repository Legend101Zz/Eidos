"""Investigator-worker activities. Credentials: an ``investigator`` principal (exploration and
proposals) for effectful calls and a ``workflow`` principal for turn bookkeeping. No database.

Recovery rules implemented here:
* ``decide``: each activity attempt is a new model response → new decision identity. If a prior
  attempt already got its decision accepted for this slot, the service returns that decision
  and records the new response as unaccepted (its usage is still charged).
* ``execute_decision``: re-executes calls of the *persisted* decision with their recorded
  command keys, so a retry after a committed effect returns the existing receipt.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import random
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from eidos.batches import ACTIVE_JOB_STATUSES, BatchRunner
from eidos.client import LabClient, LabError
from eidos.domain.redaction import redact_value
from eidos.domain.turns import (
    COMPUTE_TOOLS,
    EFFECT_TOOLS,
    MAX_CONCURRENT_JOBS_KEY,
    PLAN_TOOLS,
    WORKFLOW_PAYLOAD_LIMIT,
    check_payload,
)
from eidos.investigator.context import POLICY_V1, pack
from eidos.investigator.runtime import (
    DecisionAbort,
    DecisionAborted,
    DecisionRequest,
    ModelCallFailed,
    build_runtime,
    output_cap,
)
from eidos_wire import command_key


@contextmanager
def _heartbeating(every: float = 5.0):
    """Heartbeat while an activity body runs so Temporal notices a killed worker within the heartbeat
    timeout (30 s in TurnWorkflow) instead of waiting for the 10-minute start-to-close timeout."""
    ctx = contextvars.copy_context()
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            try:
                activity.heartbeat()
            except Exception:  # noqa: BLE001 - the activity is gone; nothing to heartbeat
                return

    threading.Thread(target=lambda: ctx.run(loop), daemon=True).start()
    try:
        yield
    finally:
        stop.set()


# ADR 0022: how long one execute_decision activity waits for a batch's jobs before handing the wait back to the
# TurnWorkflow (a durable timer, then another activity), and how often it looks at them meanwhile.
BATCH_WAIT_SLICE_SECONDS = 45.0
BATCH_POLL_SECONDS = 0.5


# ADR 0017: a decision is aborted (its in-flight provider request abandoned) when the investigation stops or pauses,
# when Temporal cancels the activity, and shortly before the activity's own timeout (the turn deadline), leaving this
# margin to record its usage and its no-effect outcome before Temporal gives up on the attempt.
ABORT_WATCH_SECONDS = 1.0
DEADLINE_MARGIN_SECONDS = 15.0


def _admitted_compute(receipt: dict) -> bool:
    """Did this receipt admit compute? (``BatchRunner.admitted_compute`` with the investigator's tools.)"""
    return bool(receipt["ok"]) and receipt["tool"] in COMPUTE_TOOLS and (receipt.get("result") or {}).get("status") != "refused"


def decision_deadline(started: float, deadline_seconds: float) -> float | None:
    """When (a ``time.monotonic`` reading) a decision that started at ``started`` is aborted: the turn deadline (the
    decide activity's start-to-close timeout) less a margin of 10% of it, at most ``DEADLINE_MARGIN_SECONDS``."""
    if not deadline_seconds or deadline_seconds <= 0:
        return None
    return started + deadline_seconds - min(DEADLINE_MARGIN_SECONDS, 0.1 * deadline_seconds)


@contextmanager
def _watching(abort: DecisionAbort, check: Callable[[], str | None], every: float = ABORT_WATCH_SECONDS):
    """While the body runs, set ``abort`` as soon as Temporal cancels the activity or ``check`` returns a reason
    (polled every ``every`` seconds; a failing check is skipped, never fatal)."""
    ctx = contextvars.copy_context()
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            try:
                if activity.is_cancelled():
                    abort.trigger("activity-cancelled")
                    return
            except Exception:  # noqa: BLE001 - outside an activity context (tests): only the check applies
                pass
            try:
                reason = check()
            except Exception:  # noqa: BLE001 - the lab may be briefly unreachable; the deadline still applies
                reason = None
            if reason:
                abort.trigger(reason)
                return

    threading.Thread(target=lambda: ctx.run(loop), daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def _activity_cancelled() -> bool:
    """Whether Temporal has cancelled the running activity (False outside an activity context, as in unit tests)."""
    try:
        return bool(activity.is_cancelled())
    except Exception:  # noqa: BLE001
        return False


def _abort_reason(abort: DecisionAbort) -> str | None:
    """Why a decision whose response has arrived must not be proposed: a Temporal cancellation (checked directly,
    since the watcher stops at its first reason, which may be the deadline) or a stop/pause the watcher saw. The
    deadline alone does not stop a decision that has its response: the lab checks the attempt's deadline itself."""
    if _activity_cancelled():
        abort.trigger("activity-cancelled")
        return "activity-cancelled"
    return abort.reason if abort.reason not in (None, "decision-deadline") else None


# ADR 0023: provider resilience refines the one retry owner (Temporal's MODEL retry policy and the coordinator's turn
# retry); nothing here retries by itself. A transient failure with a provider Retry-After sets the next activity attempt's
# delay (with jitter), bounded by the exploration time left before the conclusion's reserve; quota exhaustion stops at
# once (non-retryable ``PROVIDER_QUOTA_EXHAUSTED``) and the coordinator records the provider as unavailable.
QUOTA_ERROR_TYPE = "PROVIDER_QUOTA_EXHAUSTED"
RETRY_JITTER_FRACTION = 0.2
MAX_RETRY_DELAY_SECONDS = 300.0


def retry_delay(failure: dict[str, Any], completion: dict[str, Any] | None, *, now: float | None = None,
                rng: Callable[[], float] = random.random) -> float | None:
    """The delay before the next attempt of a decision that failed with ``failure``: the provider's Retry-After plus up
    to ``RETRY_JITTER_FRACTION`` of it as jitter, at most ``MAX_RETRY_DELAY_SECONDS`` and never past the start of the
    conclusion's reserve (``completion``, ADR 0020); 0 when no time is left there (the turn's schedule-to-close bound
    then ends it as a deadline). None without a Retry-After: Temporal's own backoff applies."""
    after = failure.get("retry_after_seconds")
    if after is None:
        return None
    delay = min(float(after) * (1.0 + RETRY_JITTER_FRACTION * rng()), MAX_RETRY_DELAY_SECONDS)
    if completion:
        delay = min(delay, max(0.0, retry_boundary(completion) - (time.time() if now is None else now)))
    return round(delay, 3)


def retry_boundary(completion: dict[str, Any]) -> float:
    """R14 #5: the time a retry may not wait past. For an exploratory turn the start of the reproduction hold-back
    (``exploration_deadline_unix``, set by begin_turn), else the start of the conclusion's reserve."""
    reserve = float(completion["deadline_unix"]) - float(completion["conclude_threshold_seconds"])
    if completion.get("exploration_deadline_unix") is not None:
        return min(float(completion["exploration_deadline_unix"]), reserve)
    return reserve


def retry_fits(failure: dict[str, Any], completion: dict[str, Any] | None, *, now: float | None = None) -> bool:
    """R14 #5: whether the provider's requested delay (Retry-After) fits before the retry boundary. When it does not,
    retrying sooner than asked is pointless and waiting would spend the hold-back: the retry stops."""
    after = failure.get("retry_after_seconds")
    if after is None or not completion:
        return True
    return float(after) <= retry_boundary(completion) - (time.time() if now is None else now)


def model_failure_error(exc: ModelCallFailed, completion: dict[str, Any] | None) -> ApplicationError:
    """The activity error for a failed decision (ADR 0023): quota exhaustion is non-retryable and typed
    ``PROVIDER_QUOTA_EXHAUSTED``; a transient failure carries the provider's Retry-After as the next retry's delay, and
    is not retried when that delay cannot fit before the exploration deadline (R14 #5)."""
    failure = dict(exc.failure or {})
    if failure.get("kind") == "quota-exhausted":
        return ApplicationError(f"provider quota exhausted: {exc}"[:500], failure, type=QUOTA_ERROR_TYPE,
                                non_retryable=True)
    if exc.retryable and not retry_fits(failure, completion):
        failure["retry_stopped"] = "retry-after-exceeds-exploration-time"
        return ApplicationError(str(exc), failure, type="MODEL_CALL_FAILED", non_retryable=True)
    delay = retry_delay(failure, completion) if exc.retryable else None
    if delay is not None:
        failure["next_retry_delay_seconds"] = delay
    return ApplicationError(str(exc), failure, type="MODEL_CALL_FAILED", non_retryable=not exc.retryable,
                            next_retry_delay=timedelta(seconds=delay) if delay else None)


def _attempt_identity() -> tuple[int, str]:
    """This decide attempt's number and its activity execution (workflow run and activity id): attempts of one
    execution are Temporal retries of each other (F2c)."""
    info = activity.info()
    return int(info.attempt), f"{getattr(info, 'workflow_run_id', '')}/{getattr(info, 'activity_id', '')}"


def _hb(fn):
    @functools.wraps(fn)
    def wrapper(self, a):
        with _heartbeating():
            return fn(self, a)
    return wrapper


def _app_error(exc: LabError) -> ApplicationError:
    return ApplicationError(str(exc), exc.error, type=exc.code or "LAB_ERROR",
                            non_retryable=exc.status in (400, 403, 404, 409, 413, 422))


def bounded(result: dict) -> dict:
    try:
        check_payload(result, limit=WORKFLOW_PAYLOAD_LIMIT)
        return result
    except Exception:
        # The reduced result must itself be small: a long ``reason`` or ``control`` used to be kept whole, so an
        # oversized result still exceeded Temporal's payload limit and failed the activity (ADR 0019 made such failed
        # turns visible; before, they looked like waits).
        out = {k: result[k] for k in ("accepted", "decision_id", "n_calls") if k in result}
        if isinstance(result.get("reason"), str):
            out["reason"] = result["reason"][:300]
        control = result.get("control")
        if isinstance(control, dict):
            try:
                check_payload(control, limit=WORKFLOW_PAYLOAD_LIMIT // 4)
                out["control"] = control
            except Exception:  # noqa: BLE001 - keep only what the workflow branches on
                out["control"] = {k: str(control[k])[:300] for k in ("action", "summary") if control.get(k)}
        return out | {"truncated": True}


def decision_usd_envelope(est_tokens: float, price: dict, cfg: dict) -> float:
    """Worst-case USD for one decision: at most max_requests_per_decision x the per-request output cap of the token
    envelope can be output; the rest is input. Pricing every token at the output rate (the old bound) reserved
    about 3x too much and ended a real run with a third of its budget unused (G5 live run 2).

    Consistent with the runtime's admission rule (ADR 0016): the runtime sends the same cap (``output_cap``: the
    configured ``max_output_tokens`` or the default) with every request and starts a request only if its input
    estimate plus that cap fits what is left of ``max_tokens_per_decision`` (``est_tokens`` here), so the decision's
    tokens stay inside this reservation."""
    # Same defaults as the runtime (8 requests per decision, output_cap).
    out_cap = float(cfg.get("max_requests_per_decision", 8)) * float(output_cap(cfg))
    out_tokens = min(est_tokens, out_cap)
    in_price = float(price.get("input", max(price.values())))
    out_price = float(price.get("output", max(price.values())))
    return ((est_tokens - out_tokens) * in_price + out_tokens * out_price) / 1e6


class InvestigatorActivities:
    def __init__(self, investigator: LabClient, workflow: LabClient, *, runtime_overrides: dict | None = None,
                 batch_wait_slice: float = BATCH_WAIT_SLICE_SECONDS, batch_poll: float = BATCH_POLL_SECONDS):
        self.inv = investigator
        self.wf = workflow
        self.runtime_overrides = runtime_overrides or {}
        self.batch_wait_slice = batch_wait_slice
        self.batch_poll = batch_poll

    # -- turn lifecycle ----------------------------------------------------------------------
    @activity.defn(name="begin_turn")
    @_hb
    def begin_turn(self, a: dict) -> dict:
        try:
            # F5 (C1): the TurnWorkflow run that begins or resumes the turn becomes its owner
            r = self.wf.command("begin_turn", {"investigation_id": a["investigation_id"],
                                               "runtime_release": "eidos-investigator/1",
                                               "run_id": getattr(activity.info(), "workflow_run_id", "") or "",
                                               # F5c: the coordinator's ordered ticket for this turn workflow (ADR 0017 addendum)
                                               **({"ticket": int(a["ticket"])} if a.get("ticket") is not None
                                                  else {})},
                                project=a["project_id"],
                                # per run (F5): a new run of the same index must take the turn, not replay the receipt
                                key=f"begin:{a['investigation_id']}:{a['index']}:"
                                    f"{getattr(activity.info(), 'workflow_run_id', '') or ''}")["result"]
        except LabError as exc:
            raise _app_error(exc) from exc
        if not r.get("started"):
            return {"started": False, "reason": r.get("reason")}
        lim = r["spec"]["limits"]
        return {"started": True, "turn_id": r["turn_id"], "max_model_calls": lim["max_model_calls"],
                "finalization": bool(r["spec"].get("finalization")),
                "resumed": r.get("resumed", False), "model_timeout": lim.get("deadline_seconds", 300),
                **({"completion": r["spec"]["completion"]} if r["spec"].get("completion") else {})}

    @activity.defn(name="build_context")
    @_hb
    def build_context(self, a: dict) -> dict:
        p = a["project_id"]
        turn = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)
        if turn.get("context"):
            return {"context_artifact_id": turn["context"]["context_artifact_id"], "reused": True}
        view = self.inv.query("investigation_view", {"investigation_id": a["investigation_id"], "horizon": turn["horizon"]},
                              project=p)
        view["budgets"] = turn["spec"].get("budgets")  # immutable snapshot taken at begin_turn
        if turn["spec"].get("completion"):
            view["completion"] = turn["spec"]["completion"]  # likewise (ADR 0020)
        if turn["spec"].get("exploration"):
            view["exploration"] = turn["spec"]["exploration"]
        if turn["spec"].get("finalization"):
            view["finalization"] = turn["spec"]["finalization"]  # likewise (ADR 0023)
        catalog = self.inv.query("search_components", {"limit": 100, "as_of_horizon": turn["horizon"]}, project=p)["components"]
        packed = pack(view, catalog, policy=turn["spec"].get("context_policy") or POLICY_V1)
        if turn["spec"].get("finalization"):
            # Exact recovery evidence is supplied independently of context-policy windowing.
            packed["text"] += "\n\n## Bounded autonomous finalization\n" + json.dumps(
                {**turn["spec"]["finalization"], "remaining_budget": view["budgets"],
                 "remaining_wall": view.get("completion")}, sort_keys=True, default=str)
        # The source manifest includes the catalog snapshot so offline replays see exactly what was
        # installed at the horizon, not components published later.
        src_bytes = json.dumps({**view, "catalog": catalog}, sort_keys=True, default=str).encode()
        digest = hashlib.sha256(src_bytes + packed["text"].encode()).hexdigest()[:24]
        src = self.inv.upload(p, data=src_bytes, media_type="application/json",
                              schema="investigation-view/1", role="context", name=f"source-{a['turn_id']}",
                              key=f"src:{a['turn_id']}:{digest}")
        ctx_art = self.inv.upload(p, data=packed["text"].encode(), media_type="text/plain", schema="packed-context/1",
                                  role="context", name=f"context-{a['turn_id']}", key=f"ctx:{a['turn_id']}:{digest}")
        self.wf.command("record_turn_context", {
            "turn_id": a["turn_id"], "context_artifact_id": ctx_art["result"]["artifact_id"],
            "source_manifest_artifact_id": src["result"]["artifact_id"],
            "source_manifest_digest": packed["source_manifest_digest"], "policy": packed["policy"],
            "selections": packed["selections"], "omissions": packed["omissions"], "truncations": packed["truncations"],
            "horizon": turn["horizon"], "chars": packed["chars"]}, project=p, key=f"ctxrec:{a['turn_id']}:{digest}")
        rec = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)["context"]  # first write wins
        return {"context_artifact_id": rec["context_artifact_id"], "chars": rec.get("chars")}

    def _turn_so_far(self, p: str, turn: dict) -> list[dict]:
        out = []
        for d in turn["decisions"]:
            if d.get("revoked"):  # never executed (F2c): not part of what this turn did
                continue
            if not d["accepted"]:
                if (d["body"] or {}).get("reason") == "invalid":  # feedback: why the last proposal was refused
                    out.append({"slot": d["slot"], "rejected": True, "error": d["body"].get("error")})
                continue
            try:
                rec = self.wf.query("get_record", {"kind": "decision_receipts", "id": d["id"]}, project=p)["body"]["receipts"]
            except LabError:
                rec = []
            out.append({"slot": d["slot"], "calls": [{"tool": c["tool"], "args": c["args"]} for c in d["body"]["calls"]],
                        "control": d["body"].get("control"), "receipts": rec})
        return out

    @activity.defn(name="decide")
    @_hb
    def decide(self, a: dict) -> dict:
        started = time.monotonic()  # the activity's start-to-close timeout counts from about here
        p = a["project_id"]
        attempt, execution = _attempt_identity()
        # F2c: begin this attempt first. The lab bumps the slot's epoch under the turn lock, so an earlier attempt's
        # request still in flight (reserve, accept) is refused from now on, and it revokes a decision an earlier
        # attempt of this same activity got accepted: that attempt failed (Temporal retried it), so its decision
        # never reached the workflow and may have been accepted after Temporal gave up on it.
        try:
            b = self.wf.command("begin_decision", {"turn_id": a["turn_id"], "slot": a["slot"], "attempt": attempt,
                                "execution": execution}, project=p,
                                key=f"begin-dec:{a['turn_id']}:{a['slot']}:{execution}:{attempt}")["result"]
        except LabError as exc:
            raise _app_error(exc) from exc
        if b.get("superseded"):  # F5: this run lost the turn (C1) or this attempt is stale (C3): nothing to do
            return {"accepted": False, "decision_id": None, "reason": f"superseded:{b['superseded']}", "n_calls": 0}
        if b.get("decision"):  # handed off by another run of this turn (a resumed turn): never regenerate it
            d = b["decision"]
            return {"accepted": True, "decision_id": d["id"], "n_calls": len(d["calls"]), "control": d.get("control"),
                    "reason": "recovered"}
        if b.get("revoked"):  # the slot's decision was revoked: it yields nothing, the turn goes on to its next slot
            return {"accepted": False, "decision_id": b["revoked"], "reason": "attempt-revoked", "n_calls": 0}
        epoch = b["epoch"]
        turn = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)
        ctxrec = turn["context"]
        with tempfile.NamedTemporaryFile() as tmp:
            self.inv.download(p, ctxrec["context_artifact_id"], tmp.name)
            context_text = open(tmp.name, encoding="utf-8").read()
        with tempfile.NamedTemporaryFile() as tmp:
            self.inv.download(p, ctxrec["source_manifest_artifact_id"], tmp.name)
            view = json.load(open(tmp.name))
        cfg = {**turn["spec"]["runtime"].get("settings", {}), "runtime": turn["spec"]["runtime"]["runtime"],
               "model": turn["spec"]["runtime"].get("model"), **self.runtime_overrides}
        if turn["spec"].get("finalization"):
            # Recovery shares the original envelope; shrink its reservation to the remaining budget.
            envelope = int(cfg.get("max_tokens_per_decision", 0 if cfg.get("runtime") == "fixture" else 120_000))
            for dim, budget in (turn["spec"].get("budgets") or {}).items():
                left = max(0.0, budget["limit_amount"] - budget["used"] - budget["reserved"])
                if dim == "model_tokens":
                    envelope = min(envelope, int(left))
                if dim == "model_usd" and cfg.get("price_per_mtok"):
                    rate = max(float(v) for v in cfg["price_per_mtok"].values())
                    if rate > 0:
                        envelope = min(envelope, int(left * 1e6 / rate))
            cfg["max_tokens_per_decision"] = envelope
        runtime = build_runtime(cfg)
        # Reserve this decision's maximum envelope before any request (checked inside the turn,
        # not only between turns). Failure paths settle the reservation via record_model_usage.
        # The runtime admits a model request only if its worst case (input estimate + output cap) fits what is left
        # of the same max_tokens_per_decision, so the decision's tokens stay inside this reservation (ADR 0016).
        # A fixture runtime makes no model requests: reserve nothing unless an envelope is configured.
        default_env = 0 if cfg.get("runtime", "fixture") == "fixture" else 120_000
        est_tokens = float(cfg.get("max_tokens_per_decision", default_env))
        price = cfg.get("price_per_mtok") or {}
        estimate = {"model_tokens": est_tokens}
        if price:
            estimate["model_usd"] = decision_usd_envelope(est_tokens, price, cfg)
        try:
            rr = self.wf.command("reserve_model_budget", {"turn_id": a["turn_id"], "slot": a["slot"],
                                 "attempt": attempt, "epoch": epoch, "estimate": estimate, "execution": execution},
                                 project=p,
                                 key=f"reserve:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")["result"]
            reserved, reservation_ref = rr["reserved"], rr.get("reservation_ref")
        except LabError as exc:
            if exc.code == "BUDGET_EXHAUSTED":
                if (exc.error.get("details") or {}).get("reason") == "exploration-holdback":
                    return {"accepted": False, "reason": "runtime-bounded", "n_calls": 0,
                            "runtime_outcome": "exploration-exhausted"}
                return {"accepted": False, "reason": "budget-exhausted", "n_calls": 0}
            raise
        limits = dict(turn["spec"]["limits"])
        if turn["spec"].get("completion"):
            from eidos.domain.turns import turn_boundary
            limits["deadline_seconds"] = min(float(limits.get("deadline_seconds") or 600),
                                               max(0.01, turn_boundary(turn["spec"]["completion"]) - time.time()))
        abort = DecisionAbort(decision_deadline(started, float(limits.get("deadline_seconds") or 0)))
        req = DecisionRequest(turn_id=a["turn_id"], slot=a["slot"], context=context_text,
                              turn_so_far=self._turn_so_far(p, turn), limits=limits, settings=cfg,
                              read_tools=self._read_tools(p, finalization=bool(turn["spec"].get("finalization"))), view=view, abort=abort)

        def aborted() -> dict:
            if abort.reason == "activity-cancelled" or _activity_cancelled():
                raise ApplicationError("decision aborted: activity-cancelled", type="DECISION_ABORTED",
                                       non_retryable=True)
            return {"accepted": False, "reason": abort.reason, "n_calls": 0}

        # F2b: cancellation and the investigation status are monitored until the decision is accepted, not only while
        # the provider request runs: a stop, pause or Temporal cancellation that arrives after the last response (while
        # usage is recorded or the response uploaded) still prevents acceptance. The lab enforces the stop at the
        # durable boundary itself (accept_decision locks the investigation row); a cancellation is durable once the
        # cancelled TurnWorkflow closes the turn, which accept_decision refuses under the turn row lock.
        with _watching(abort, lambda: self._not_active(p, a["investigation_id"]),
                       float(cfg.get("abort_poll_seconds", ABORT_WATCH_SECONDS))):
            try:
                res = runtime.decide(req)
            except DecisionAborted as exc:
                # Stopped, paused or cancelled: nothing is proposed. Usage is what the responses reported; uncertain
                # when a request was abandoned in flight (its bill is unknown); the reservation is settled against it.
                usage = {**exc.possible_usage, **({"phase": "finalization"} if turn["spec"].get("finalization") else {}), "uncertain": bool(exc.in_flight or exc.possible_usage.get("uncertain"))}
                self.wf.command("record_model_usage", {"turn_id": a["turn_id"], "usage": usage, "reserved": reserved,
                                "reservation_ref": reservation_ref}, project=p,
                                key=f"usage-abort:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")
                if exc.reason == "activity-cancelled":
                    raise ApplicationError(str(exc), type="DECISION_ABORTED", non_retryable=True) from exc
                return {"accepted": False, "reason": exc.reason, "n_calls": 0}
            except ModelCallFailed as exc:
                # ADR 0023: every attempt's usage, its uncertainty, its elapsed time and why it failed are recorded
                error = model_failure_error(exc, turn["spec"].get("completion"))
                self.wf.command("record_model_usage", {"turn_id": a["turn_id"], "usage": {**exc.possible_usage,
                                **({"phase": "finalization"} if turn["spec"].get("finalization") else {}),
                                "uncertain": True, "error": str(exc)[:500], "attempt": attempt,
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                                "failure": error.details[0] if error.details else exc.failure},
                                "reserved": reserved, "reservation_ref": reservation_ref}, project=p,
                                key=f"usage-fail:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")
                raise error from exc
            # Spend is the first durable write: a crash before acceptance must not lose billed requests.
            # ADR 0023: the ledger's entry for this attempt also carries its number and elapsed time (a copy: the
            # decision's own usage is what the runtime reported)
            recorded = {**res.usage, **({"phase": "finalization"} if turn["spec"].get("finalization") else {}), "attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3)}
            self.wf.command("record_model_usage", {"turn_id": a["turn_id"], "usage": recorded, "reserved": reserved,
                                                   "reservation_ref": reservation_ref},
                            project=p, key=f"usage:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")
            if _abort_reason(abort):  # stopped, paused or cancelled after the last response: never proposed (ADR 0017)
                return aborted()
            resp = self.inv.upload(p, data=json.dumps(res.raw_response, default=str, sort_keys=True).encode(),
                                   media_type="application/json", schema="model-response/1", role="context",
                                   name=f"response-{a['turn_id']}-{a['slot']}-{attempt}",
                                   key=f"resp:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")
            if _abort_reason(abort):  # last check before the durable acceptance boundary
                return aborted()
            if res.runtime_outcome:
                return self.wf.command("record_runtime_outcome", {
                    "turn_id": a["turn_id"], "slot": a["slot"], "runtime_outcome": res.runtime_outcome,
                    "response_artifact_id": resp["result"]["artifact_id"], "usage": res.usage},
                    project=p, key=f"runtime:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")["result"]
            try:
                r = self.wf.command("accept_decision", {"turn_id": a["turn_id"], "slot": a["slot"],
                                    "proposal": res.proposal, "usage": res.usage, "usage_recorded": True,
                                    "model_settings": res.model_settings, "prompt_revision": res.prompt_revision,
                                    "bounded_outcome": ((res.proposal or {}).get("control") or {}).get("bounded_outcome"),
                                    "response_artifact_id": resp["result"]["artifact_id"], "attempt": attempt,
                                    "epoch": epoch, "execution": execution},
                                    project=p, key=f"accept:{a['turn_id']}:{a['slot']}:{attempt}:{epoch}")["result"]
            except LabError as exc:
                raise _app_error(exc) from exc
        return bounded({"accepted": r["accepted"], "decision_id": r["decision_id"], "n_calls": len(r.get("calls") or []),
                        "control": r.get("control"), "reason": r.get("reason")})

    def _not_active(self, p: str, investigation_id: str) -> str | None:
        """``investigation-<status>`` once the investigation is no longer active (stopped, paused, concluded)."""
        status = self.wf.query("get_record", {"kind": "investigation", "id": investigation_id}, project=p).get("status")
        return None if status in (None, "active") else f"investigation-{status}"

    def _read_tools(self, p: str, *, finalization: bool = False) -> dict:
        tools: dict[str, Any] = {
            "inspect_evidence": lambda artifact_id, max_bytes=8192: self.inv.query(
                "artifact_preview", {"artifact_id": artifact_id, "max_bytes": max_bytes}, project=p),
            "search_components": lambda text="", capability=None: self.inv.query(
                "search_components", {"text": text, "capability": capability, "limit": 20}, project=p),
            "describe_component": lambda component_ref: self.inv.query(
                "describe_component", {"component_ref": component_ref}, project=p),
            "get_job": lambda job_id: redact_value(self.inv.query("get_job", {"job_id": job_id, "include_lock": False}, project=p)),
        }
        return {k: v for k, v in tools.items() if not finalization or k != "search_components"}

    # -- effects (ADR 0022: a decision's calls run in order; dependent calls wait for what they name) ----------
    @activity.defn(name="execute_decision")
    @_hb
    def execute_decision(self, a: dict) -> dict:
        p = a["project_id"]
        turn = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)
        dec = next(d for d in turn["decisions"] if d["id"] == a["decision_id"] and d["accepted"])
        if dec.get("revoked"):  # F2c: a revoked decision (its attempt failed) is never executed
            return {"summary": [], "skipped": "decision-revoked"}
        if turn["status"] != "open":
            # F2b: a decision accepted just before its turn was closed (the TurnWorkflow was cancelled while the decide
            # activity was accepting it) is never executed: the turn's closing is the durable cancellation.
            return {"summary": [], "skipped": f"turn-{turn['status']}"}
        # F5 (C1): the durable handoff, under the turn lock, by the run that owns the turn. A run that lost the turn
        # (resumed by another) executes nothing; once handed off, a resumed run recovers the decision instead.
        info = activity.info()
        continuation = int(a.get("continuation") or 0)  # ADR 0022: a later slice of a batch waiting for its jobs
        h = self.wf.command("hand_off_decision", {"decision_id": dec["id"], "execution":
                            f"{getattr(info, 'workflow_run_id', '')}/{getattr(info, 'activity_id', '')}"},
                            # per attempt: a retry is re-checked (a run that lost the turn meanwhile is refused)
                            project=p, key=f"handoff:{dec['id']}:{getattr(info, 'workflow_run_id', '')}:"
                                          f"{getattr(info, 'attempt', 1)}:{continuation}")["result"]
        if not h.get("handed_off"):
            return {"summary": [], "skipped": h.get("reason") or "not-handed-off"}
        # F5c (review R8 #2): every effect is admitted against this decision and run in its own transaction, so a stop,
        # pause or turn closure that commits after the handoff refuses the remaining effects (EFFECT_NOT_ADMITTED).
        admission = {"decision_id": dec["id"],
                     "execution": f"{getattr(info, 'workflow_run_id', '')}/{getattr(info, 'activity_id', '')}"}
        inv_id = a["investigation_id"]
        batch = BatchRunner(lambda name, params: self.wf.query(name, params, project=p),
                            max_concurrent=int((dec["body"].get("resource_policy") or {}).get(MAX_CONCURRENT_JOBS_KEY) or 1),
                            slice_ends=time.monotonic() + self.batch_wait_slice, final=bool(a.get("final")),
                            poll=self.batch_poll, deadline_message="the turn ran out of time waiting for this batch's jobs",
                            interrupted=lambda: "activity-cancelled" if _activity_cancelled() else self._not_active(p, inv_id))
        receipts: list[dict] = []
        for call in dec["body"]["calls"]:
            blocked = batch.gate(call, receipts)
            if blocked == "pending":  # ADR 0022: committed calls replay by key when the workflow runs this again
                return {"pending": True, "next_call": call["index"], "continuation": continuation + 1}
            if isinstance(blocked, str):
                # a stop, pause or turn closure seen while waiting: as if the next effect had been refused
                receipts.append({"tool": call["tool"], "index": call["index"], "ok": False, "code": "EFFECT_NOT_ADMITTED",
                                 "error": {"message": f"not started: {blocked}", "details": {"reason": blocked}}})
                return self._finish_decision(p, a, dec, receipts, stopped=blocked)
            r = blocked or self._execute_call(p, a["investigation_id"], turn["id"], dec["id"], call, admission,
                                              receipts=receipts, batch=batch)
            if r.get("code") == "EFFECT_NOT_ADMITTED":
                why = str(((r.get("error") or {}).get("details") or {}).get("reason") or "not-admitted")
                if why == "superseded-execution":
                    # the run that now owns the turn continues this handed-off decision (its receipts are its own)
                    return {"summary": [], "skipped": why}
                receipts.append(r)  # the effects before it ran; this one and the rest were refused
                return self._finish_decision(p, a, dec, receipts, stopped=why)
            receipts.append(r)
            batch.record(r)
        return self._finish_decision(p, a, dec, receipts)

    def _finish_decision(self, p: str, a: dict, dec: dict, receipts: list[dict], stopped: str | None = None) -> dict:
        # Replay-invariant record: a retry after committed effects yields identical receipts.
        stable = [{k: v for k, v in r.items() if k not in ("replayed", "_job")} for r in receipts]
        # F5c: receipts of a decision stopped at a refused effect have their own key (per refused call), so a retry
        # that completes the decision after a resume records its full receipts instead of conflicting with them.
        key = f"receipts:{dec['id']}" + (f":stopped:{receipts[-1].get('index')}" if stopped else "")
        self.wf.command("record_decision_receipts", {"decision_id": dec["id"], "receipts": stable}, project=p,
                        key=key)
        summary = [{"tool": r["tool"], "ok": r["ok"], "replayed": r.get("replayed"), "code": r.get("code")} for r in receipts]
        out: dict[str, Any] = {"summary": summary}
        if stopped:
            out["stopped"] = stopped
            return bounded(out)
        # A wait after effects that created nothing to wait for (every effect call failed, or none launched work)
        # would park the investigation: report it so the turn treats it like a refused proposal (ADR 0007/0008).
        if (dec["body"].get("control") or {}).get("action") == "wait_for_jobs" and receipts:
            active = self.wf.query("list_jobs", {"investigation_id": a["investigation_id"],
                                                 "statuses": ACTIVE_JOB_STATUSES}, project=p)
            # ADR 0022: a batch's own jobs may all have finished already, sequentially. Their events are the news the
            # wait is for; only a decision that started nothing leaves nothing to wait for.
            if not active and not any(_admitted_compute(r) for r in receipts):
                out["nothing_outstanding"] = True
                out["failed_calls"] = [{"tool": r["tool"], "code": r.get("code"),
                                        "message": str((r.get("error") or {}).get("message", ""))[:300]}
                                       for r in receipts if not r["ok"]]
        return bounded(out)

    def _execute_call(self, p: str, inv: str, turn_id: str, dec_id: str, call: dict,
                      admission: dict | None = None, *, receipts: list[dict] | None = None,
                      batch: BatchRunner | None = None) -> dict:
        key = call["command_key"]
        assert key == command_key(p, turn_id, dec_id, call["index"]), "persisted command key mismatch"
        provenance: dict[str, Any] = {}
        args = dict(call["args"])
        if call.get("depends_on") and receipts is not None and batch is not None:
            resolved = batch.resolve(call, receipts)
            if isinstance(resolved, dict):
                return resolved  # REF_UNRESOLVED: recorded, not run
            args, provenance = resolved
        if batch is not None and call["tool"] in PLAN_TOOLS:  # the decision's checked allowance, not the model's own args
            args[MAX_CONCURRENT_JOBS_KEY] = batch.max_concurrent
        result = self._run_effect(p, inv, turn_id, dec_id, call, args, admission)
        return {**result, **provenance} if provenance else result

    def _run_effect(self, p: str, inv: str, turn_id: str, dec_id: str, call: dict, args: dict,
                    admission: dict | None) -> dict:
        tool, idx = call["tool"], call["index"]
        key = call["command_key"]
        adm: dict[str, Any] = {"admission": admission} if admission else {}
        args.setdefault("investigation_id", inv)
        try:
            if tool == "author_source":  # the whole-trainer path: candidate project files, no build
                bundle = json.dumps({"files": args.get("files") or {}}, sort_keys=True).encode()
                up = self.inv.upload(p, data=bundle, media_type="application/vnd.eidos.source-bundle+json",
                                     schema="source-bundle/1", role="source", name="authored-source", key=key,
                                     **adm)
                return {"tool": tool, "index": idx, "ok": True, "replayed": up.get("replayed"),
                        "result": {"artifact_id": up["result"]["artifact_id"],
                                   "use_as": "run_component inputs.source (e.g. eidos.project.command)"}}
            if tool == "author_component":
                bundle = json.dumps({"files": args.get("files") or {}}, sort_keys=True).encode()
                up = self.inv.upload(p, data=bundle, media_type="application/vnd.eidos.source-bundle+json",
                                     schema="source-bundle/1", role="source", name="authored-component",
                                     key=command_key(p, turn_id, dec_id, idx, 1),
                                     **adm)
                aid = up["result"]["artifact_id"]
                r = self.inv.command("validate_component", {"bundle_artifact_id": aid, "investigation_id": inv,
                                     "environment": args.get("environment", "default")}, project=p, key=key,
                                     **adm)
                r["result"]["bundle_artifact_id"] = aid
            else:
                r = self.inv.command(EFFECT_TOOLS[tool], args, project=p, key=key,
                                     **adm)
            return {"tool": tool, "index": idx, "ok": True, "replayed": r.get("replayed"), "result": r["result"]}
        except LabError as exc:
            return {"tool": tool, "index": idx, "ok": False, "code": exc.code, "error": exc.error}

    OPERATOR_TYPES = ["investigation.steered", "investigation.question_revised", "investigation.release_withdrawn",
                      "investigation.charter_attached", "investigation.resumed",
                      "investigation.paused", "investigation.stopped", "charter.amended"]
    NEWS_TYPES = ["job.succeeded", "job.failed", "job.cancelled", "job.pruned", "evidence.ready", "assessment.committed",
                  "investigation.steered", "investigation.question_revised", "investigation.release_withdrawn",
                  "investigation.charter_attached", "investigation.resumed",
                  "investigation.paused", "investigation.stopped", "component.published", "component.withdrawn",
                  "charter.amended", "exposure.recorded",
                  # effects of the investigator's own calls that need a fresh view to act on
                  "claim.filed", "model.exported", "component.retired", "evidence_plan.frozen", "evidence_plan.drafted"]

    @activity.defn(name="check_news")
    def check_news(self, a: dict) -> dict:
        """Cheap ledger check (no model call): has anything relevant happened since the last turn's horizon?"""
        inv = self.wf.query("get_investigation", {"investigation_id": a["investigation_id"]}, project=a["project_id"])
        if inv["status"] != "active":
            return {"news": True, "reason": f"investigation is {inv['status']}"}
        horizon = max((t["horizon"] for t in inv["turns"]), default=0)
        # F5 (A1): a needs-input wait passes the mark's cursor; only events after both count.
        cursor = max(horizon, int(a.get("after") or 0))
        types = self.OPERATOR_TYPES if a.get("operator_only") else self.NEWS_TYPES
        ev = self.wf.query("list_events", {"investigation_id": a["investigation_id"], "cursor": cursor,
                                           "types": types, "limit": 1}, project=a["project_id"])
        return {"news": bool(ev["events"]), "after_horizon": horizon, "after": cursor}

    @activity.defn(name="mark_needs_input")
    def mark_needs_input(self, a: dict) -> dict:
        return self.wf.command("mark_needs_input", {"investigation_id": a["investigation_id"], "reason": a.get("reason"),
                               "refused_turns": a.get("refused_turns", 0),
                               **{k: a[k] for k in ("code", "details") if a.get(k)}}, project=a["project_id"],
                               key=f"needs-input:{a['investigation_id']}:{a.get('turn_counter')}:"
                                   f"{activity.info().workflow_run_id}")["result"]

    @activity.defn(name="enter_finalization")
    def enter_finalization(self, a: dict) -> dict:
        return self.wf.command("enter_finalization", a, project=a["project_id"],
                               key=f"finalize:{a['investigation_id']}:{a.get('turn_counter')}")["result"]

    @activity.defn(name="record_turn_retry")
    def record_turn_retry(self, a: dict) -> dict:
        """ADR 0019: a bounded automatic retry after a retryable turn failure is on the ledger before its backoff."""
        return self.wf.command("record_turn_retry", {k: a.get(k) for k in (
            "investigation_id", "failed_turn_id", "attempt", "max_attempts", "delay_seconds", "failure")},
            project=a["project_id"], key=f"turn-retry:{a['investigation_id']}:{a.get('turn_counter')}")["result"]

    @activity.defn(name="complete_turn")
    @_hb
    def complete_turn(self, a: dict) -> dict:
        r = self.wf.command("complete_turn", {"turn_id": a["turn_id"], "stop_reason": a["stop_reason"],
                            "control": a.get("control"), "receipts": a.get("receipts", []),
                            "execution": f"{getattr(activity.info(), 'workflow_run_id', '') or ''}/complete",
                            **({"failure": a["failure"]} if a.get("failure") else {}),
                            **({"runtime_outcome": a["runtime_outcome"]} if a.get("runtime_outcome") else {})},
                            project=a["project_id"],
                            # per run (F5): a completion refused for a run that lost the turn is not the owner's receipt
                            key=f"complete:{a['turn_id']}:{getattr(activity.info(), 'workflow_run_id', '') or ''}")
        return {"status": r["result"].get("status")}
