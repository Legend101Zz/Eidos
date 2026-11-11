"""Deterministic Temporal workflows. All side effects (LLM calls, commands, processes) are
activities, addressed by name so workflow code imports no adapters.

* ``InvestigationWorkflow`` — long-lived coordinator. Starts one bounded ``TurnWorkflow`` child
  at a time, sleeps on durable wake signals between turns, and rolls itself forward with
  Continue-As-New before history thresholds, carrying only compact counters. The ledger (not the
  signal) is the durable inbox: a turn reconstructs context from events up to its horizon.
* ``TurnWorkflow`` — one bounded decision turn: begin → context → (decide → execute)* → complete.
* ``JobWorkflow`` — one admitted job; its lifetime is independent of turns and coordinators.
* ``AssessmentWorkflow`` — runs the pinned assessor for an executed evidence plan.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
    RetryState,
)

from eidos.orchestration.names import ASSESSOR_QUEUE, INVESTIGATOR_QUEUE, WORKFLOW_QUEUE

NON_RETRYABLE = ["PAYLOAD_TOO_LARGE", "FORBIDDEN", "INVALID", "NonRetryable"]
SHORT = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=30), maximum_attempts=5,
                    non_retryable_error_types=NON_RETRYABLE)
MODEL = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=60), maximum_attempts=3,
                    non_retryable_error_types=NON_RETRYABLE)

HB = timedelta(seconds=30)  # investigator activities heartbeat every 5 s; a killed worker is noticed in ~30 s
MAX_INVALID_RETRY_TURNS = 2
WAIT_REASONS = ("waiting", "input-requested", "stale-revision", "calls-exhausted-waiting", "invalid-proposals")
MAX_CONSECUTIVE_INVALID = 2
# ADR 0019: a failed turn (stop reason "error") never leaves the investigation active with nothing scheduled. A
# retryable failure gets at most MAX_FAILED_TURN_RETRIES automatic retry turns, after a recorded, growing backoff;
# a non-retryable failure, or one that persists through the retries, marks the investigation needs-input at once.
MAX_FAILED_TURN_RETRIES = 2
FAILED_TURN_BACKOFF_SECONDS = 60
MAX_FAILED_TURN_BACKOFF_SECONDS = 900
TERMINAL_REASONS = ("concluded", "budget-exhausted", "max-turns-reached", "stopped", "investigation is stopped",
                    "investigation is concluded")


def turn_failure(exc: ActivityError) -> dict[str, Any]:
    """Why an activity failure ended a turn, for the coordinator's policy and the record (ADR 0019). Not retryable when
    Temporal stopped retrying because the error was marked non-retryable (an invalid model spec, an authentication
    or not-found error from the provider, a refused payload); retryable when its retries ran out or it timed out."""
    cause = exc.cause
    non_retryable = exc.retry_state == RetryState.NON_RETRYABLE_FAILURE or (
        isinstance(cause, ApplicationError) and cause.non_retryable)
    etype = cause.type if isinstance(cause, ApplicationError) and cause.type else type(cause or exc).__name__
    return {"activity": exc.activity_type, "error_type": etype, "retryable": not non_retryable,
            "retry_state": exc.retry_state.name if exc.retry_state is not None else None,
            "message": str(getattr(cause, "message", None) or cause or exc)[:300]}


@workflow.defn(name="InvestigationWorkflow")
class InvestigationWorkflow:
    def __init__(self) -> None:
        self._wakes = 0
        self._last_seq = 0
        self._turns = 0
        self._status = "starting"

    @workflow.signal(name="wake")
    def wake(self, seq: int = 0) -> None:
        self._wakes += 1
        self._last_seq = max(self._last_seq, int(seq or 0))

    @workflow.query(name="state")
    def state(self) -> dict:
        return {"status": self._status, "turns_this_run": self._turns, "wakes": self._wakes,
                "history_length": workflow.info().get_current_history_length()}

    async def _wait_for_wake_or_full(self, seen: int, history_limit: int, idle: int) -> None:
        """Block until a wake past ``seen`` arrives or the history crosses its limit; ``TimeoutError`` after ``idle`` s.
        ``seen`` is bound as a parameter, so a later reassignment in the caller's loop cannot change the condition."""
        await workflow.wait_condition(
            lambda: self._wakes > seen or workflow.info().get_current_history_length() > history_limit,
            timeout=timedelta(seconds=idle))

    async def _wait_for_operator(self, a: dict, inv: str, history_limit: int, idle: int,
                                 status: str = "needs operator input (repeated refused proposals)") -> None:
        self._status = status
        while True:
            seen = self._wakes
            try:
                await self._wait_for_wake_or_full(seen, history_limit, idle)
            except TimeoutError:
                pass
            if workflow.info().get_current_history_length() > history_limit:
                workflow.continue_as_new({**a, "rollovers": int(a.get("rollovers", 0)) + 1})
            # F5 (A1): only operator events after the needs-input mark's cursor end the wait. The last turn's horizon
            # does not move while turn workflows fail before creating a turn, so an event behind the mark would be
            # news on every check and every wait would end at once (and reset the failed-turn counter).
            news = await workflow.execute_activity(
                "check_news", {"investigation_id": inv, "project_id": a["project_id"], "operator_only": True,
                               **({"after": int(a["operator_cursor"])} if a.get("operator_cursor") else {})},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            if news.get("news"):
                return

    async def _surface_end(self, a: dict, inv: str, counter: int, reason: str) -> None:
        """The coordinator stops on its own (budget or turn limit, repeated failures) while the investigation is
        still active: record that it needs the operator (visible label and event). The reconciler then leaves it
        alone instead of restarting a coordinator that would only stop again (G5 live run)."""
        if workflow.patched("terminal-outcome-needs-input-v1"):
            await workflow.execute_activity(
                "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                     "reason": f"coordinator stopped: {reason}; steer or resume to continue (limits are fixed at start), or stop"},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)

    async def _resume_quiet_wait(self, a: dict, inv: str, history_limit: int, idle: int) -> None:
        """Continue a quiet wait that rolled over (Continue-As-New) without news: no model turn
        until a wake, ledger news or the max-idle timer, which counts from the original wait start."""
        rw = a["resume_wait"]
        since = float(rw["since"])
        max_idle = int(a.get("max_idle_turn_seconds", 6 * 3600))
        self._status = f"waiting ({rw.get('reason') or 'quiet'})"
        seen = self._wakes  # once: a wake during check_news is still seen by the next wait
        while True:
            try:
                await self._wait_for_wake_or_full(seen, history_limit, idle)
            except TimeoutError:
                pass
            if self._wakes > seen:
                return
            if workflow.info().get_current_history_length() > history_limit:
                self._status = "rolling-over"
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new({**a, "rollovers": int(a.get("rollovers", 0)) + 1})
            news = await workflow.execute_activity(
                "check_news", {"investigation_id": inv, "project_id": a["project_id"]},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            if news.get("news") or workflow.now().timestamp() - since > max_idle:
                return

    async def _after_failed_turn(self, a: dict, inv: str, counter: int, failed_turns: int, res: dict,
                                 history_limit: int, idle: int) -> bool:
        """ADR 0019. A retryable failure within MAX_FAILED_TURN_RETRIES: record the retry (ledger event), back off
        (base x 2^(n-1), capped; a wake, e.g. a stop or new evidence, ends the backoff early) and return False: the
        caller starts the next turn, an ordinary turn that reserves the turns and model budgets. Otherwise mark the
        investigation needs-input with code ``turn-failed`` and machine-readable details, wait for the operator and
        return True."""
        failure = dict(res.get("failure") or {"error_type": "unknown", "retryable": True})
        retryable = bool(failure.get("retryable", True))
        if retryable and failed_turns <= MAX_FAILED_TURN_RETRIES:
            base = float(a.get("failed_turn_backoff_seconds", FAILED_TURN_BACKOFF_SECONDS))
            delay = min(base * 2 ** (failed_turns - 1), float(MAX_FAILED_TURN_BACKOFF_SECONDS))
            self._status = f"retrying after a failed turn ({failed_turns}/{MAX_FAILED_TURN_RETRIES}) in {delay:g}s"
            await workflow.execute_activity(
                "record_turn_retry", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                      "failed_turn_id": res.get("turn_id"), "attempt": failed_turns,
                                      "max_attempts": MAX_FAILED_TURN_RETRIES, "delay_seconds": delay,
                                      "failure": failure},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            seen = self._wakes
            if delay > 0:
                try:
                    await workflow.wait_condition(lambda: self._wakes > seen, timeout=timedelta(seconds=delay))
                except TimeoutError:
                    pass
            return False
        why = "non-retryable error" if not retryable else \
            f"still failing after {MAX_FAILED_TURN_RETRIES} automatic retries"
        details = {"turn_counter": counter, "turn_id": res.get("turn_id"), "failure": failure, "retryable": retryable,
                   "consecutive_failed_turns": failed_turns, "automatic_retries": failed_turns - 1,
                   "max_automatic_retries": MAX_FAILED_TURN_RETRIES}
        marked = await workflow.execute_activity(
            "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                 "code": "turn-failed", "details": details,
                                 "reason": f"turn failed ({why}): {failure.get('error_type')}: "
                                           f"{str(failure.get('message', ''))[:200]}; steer, resume or stop"},
            task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        cursor = (marked or {}).get("cursor")
        await self._wait_for_operator({**a, "turn_counter": counter, "needs_operator": "turn-failed",
                                       **({"operator_cursor": int(cursor)} if cursor else {})}, inv,
                                      history_limit, idle, "needs operator input (turn failed)")
        return True

    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        inv = a["investigation_id"]
        counter = int(a.get("turn_counter", 0))
        history_limit = int(a.get("history_limit", 1500))
        turns_per_run = int(a.get("turns_per_run", 20))
        idle = int(a.get("idle_poll_seconds", 60))
        consecutive_errors = 0
        invalid_turns = int(a.get("invalid_turns", 0))  # consecutive turns ended on the invalid-proposal limit
        failed_turns = int(a.get("failed_turns", 0))  # consecutive failed turns (ADR 0019)
        while True:
            if a.get("needs_operator") == "turn-failed" and self._turns == 0:
                # A rollover while a failed turn waits for the operator (ADR 0019): keep waiting, no turn without news.
                await self._wait_for_operator(a, inv, history_limit, idle, "needs operator input (turn failed)")
                a = {k: v for k, v in a.items() if k not in ("needs_operator", "operator_cursor")}
                failed_turns = 0
            if a.get("resume_wait") and self._turns == 0 and workflow.patched("quiet-rollover-keeps-waiting-v1"):
                # Review 2: a rollover during a quiet wait used to start a model turn with no news and
                # reset the max-idle timer. Keep waiting instead, from the original wait start.
                await self._resume_quiet_wait(a, inv, history_limit, idle)
                a = {k: v for k, v in a.items() if k != "resume_wait"}
            if invalid_turns > MAX_INVALID_RETRY_TURNS and a.get("invalid_turns") and self._turns == 0 \
                    and workflow.patched("needs-input-survives-rollover-v1"):
                # A rollover while waiting for operator input: keep waiting (no turn without operator news).
                await self._wait_for_operator(a, inv, history_limit, idle)
                a = {**a, "invalid_turns": 0}
                invalid_turns = 0
            if workflow.info().get_current_history_length() > history_limit or self._turns >= turns_per_run:
                self._status = "rolling-over"
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new({**a, "turn_counter": counter, "rollovers": int(a.get("rollovers", 0)) + 1,
                                          "invalid_turns": invalid_turns,
                                          **({"failed_turns": failed_turns} if failed_turns else {})})
            counter += 1
            self._turns += 1
            seen = self._wakes
            self._status = f"turn {counter}"
            try:
                res = await workflow.execute_child_workflow(
                    "TurnWorkflow", {"investigation_id": inv, "project_id": a["project_id"], "index": counter},
                    id=f"{inv}-turn-{counter}", task_queue=WORKFLOW_QUEUE,
                    execution_timeout=timedelta(seconds=int(a.get("turn_timeout_seconds", 1800))))
                consecutive_errors = 0
            except ChildWorkflowError as exc:
                consecutive_errors += 1
                res = {"started": True, "stop_reason": "error"}
                if workflow.patched("failed-turn-policy-v1"):
                    # The turn workflow itself failed (e.g. its execution timeout): a retryable turn failure, under
                    # the same bounded policy as a turn that ended on an error (ADR 0019).
                    res["failure"] = {"error_type": "turn-workflow-failed", "retryable": True,
                                      "message": str(exc.cause or exc)[:300]}
                elif consecutive_errors >= 3:
                    self._status = "failed"
                    await self._surface_end(a, inv, counter, "three consecutive turn failures")
                    return {"status": "failed", "reason": "three consecutive turn failures", "turn_counter": counter}
            reason = res.get("stop_reason") or res.get("reason") or ""
            if reason in TERMINAL_REASONS or reason.startswith("investigation is stopped") or reason == "max-turns-reached":
                self._status = "finished"
                if reason in ("budget-exhausted", "max-turns-reached"):
                    await self._surface_end(a, inv, counter, reason)
                return {"status": "finished", "reason": reason, "turn_counter": counter}
            if reason != "error":
                failed_turns = 0
            elif workflow.patched("failed-turn-policy-v1"):
                # Histories recorded before ADR 0019 waited for ledger news after a failed turn (up to the max-idle
                # timer, with nothing scheduled and nothing visible: pilot D12-r3).
                failed_turns += 1
                invalid_turns = 0
                if await self._after_failed_turn(a, inv, counter, failed_turns, res, history_limit, idle):
                    failed_turns = 0  # the operator acted: start afresh
                continue
            needs_input = False
            if workflow.patched("invalid-turn-retry-v1"):
                # A turn that ended on refused proposals gets a bounded, immediate retry with a fresh view
                # (the refusals are in the ledger the next context is built from); after that it surfaces
                # to the operator instead of idling until the max-idle timer.
                invalid_turns = invalid_turns + 1 if reason == "invalid-proposals" else 0
                if 0 < invalid_turns <= MAX_INVALID_RETRY_TURNS:
                    self._status = f"retrying after refused proposals ({invalid_turns}/{MAX_INVALID_RETRY_TURNS})"
                    continue
                needs_input = invalid_turns > MAX_INVALID_RETRY_TURNS
            if needs_input and workflow.patched("needs-input-visible-v1"):
                await workflow.execute_activity(
                    "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                         "refused_turns": invalid_turns,
                                         "reason": "repeated refused proposals after bounded retries"},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            if not res.get("started") or reason in WAIT_REASONS or reason == "error":
                self._status = "needs operator input (repeated refused proposals)" if needs_input else f"waiting ({reason})"
                waited_since = workflow.now()
                max_idle = int(a.get("max_idle_turn_seconds", 6 * 3600))
                quiet_news = False
                while True:
                    try:
                        # Signals add history while waiting; leave the wait (and roll over at the loop
                        # top) once the history crosses its limit, so a wake storm cannot grow it.
                        await self._wait_for_wake_or_full(seen, history_limit, idle)
                        if not needs_input or workflow.info().get_current_history_length() > history_limit:
                            break
                        seen = self._wakes  # job/evidence wakes do not end a needs-input wait; operator news does
                    except TimeoutError:
                        pass
                    if not workflow.patched("idle-poll-needs-news-v1"):
                        break  # histories recorded before this change started a turn on every timeout
                    # A quiet poll costs no model call: start a turn only if something happened.
                    news = await workflow.execute_activity(
                        "check_news", {"investigation_id": inv, "project_id": a["project_id"],
                                       **({"operator_only": True} if needs_input else {})},
                        task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                    quiet_news = bool(news.get("news"))
                    if news.get("news") or workflow.info().get_current_history_length() > history_limit \
                            or (not needs_input and (workflow.now() - waited_since).total_seconds() > max_idle):
                        break
                if not needs_input and not quiet_news and self._wakes == seen \
                        and workflow.info().get_current_history_length() > history_limit \
                        and (workflow.now() - waited_since).total_seconds() <= max_idle \
                        and workflow.patched("quiet-rollover-keeps-waiting-v1"):
                    self._status = "rolling-over"
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    workflow.continue_as_new({**a, "turn_counter": counter, "rollovers": int(a.get("rollovers", 0)) + 1,
                                              "invalid_turns": invalid_turns,
                                              "resume_wait": {"since": waited_since.timestamp(), "reason": reason}})


@workflow.defn(name="TurnWorkflow")
class TurnWorkflow:
    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        b = await workflow.execute_activity("begin_turn", a, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=60),
                                            heartbeat_timeout=HB, retry_policy=SHORT)
        if not b.get("started"):
            return {"started": False, "reason": b.get("reason")}
        tid = b["turn_id"]
        base = {"turn_id": tid, "project_id": a["project_id"], "investigation_id": a["investigation_id"]}
        stop = "calls-exhausted"
        control: dict[str, Any] = {}
        receipts: list[dict[str, Any]] = []
        failure: dict[str, Any] = {}
        invalid_run = 0
        failed_waits = 0  # accepted waits whose calls left nothing outstanding
        try:
            await workflow.execute_activity("build_context", base, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=120),
                                            heartbeat_timeout=HB, retry_policy=SHORT)
            for slot in range(int(b.get("max_model_calls", 4))):
                d = await workflow.execute_activity("decide", {**base, "slot": slot}, task_queue=INVESTIGATOR_QUEUE,
                                                    start_to_close_timeout=timedelta(seconds=int(b.get("model_timeout", 300))),
                                                    heartbeat_timeout=HB, retry_policy=MODEL)
                if not d.get("accepted"):
                    # Versioned change: histories recorded before it ended the turn on any rejection.
                    if d.get("reason") == "invalid" and workflow.patched("invalid-proposal-feedback-v1"):
                        invalid_run += 1
                        if invalid_run >= MAX_CONSECUTIVE_INVALID:
                            stop = "invalid-proposals"  # waits like other wait reasons; no hot loop
                            break
                        continue  # the next slot sees why the proposal was refused
                    if str(d.get("reason") or "").startswith("superseded:"):
                        # F5 (ADR 0017 addendum): this run lost the turn to a resuming run, or the attempt is stale;
                        # it stops without effects (the lab refuses its completion too). Older histories never
                        # carry this reason.
                        stop = "cancelled"
                        break
                    if d.get("reason") == "attempt-revoked":
                        # F2c: the slot's decision came from a failed attempt and was revoked, never executed; the next
                        # slot decides afresh. Older histories never carry this reason.
                        continue
                    # A decision aborted because the investigation stopped or paused ends the turn as cancelled
                    # (ADR 0017); older histories never carry these reasons.
                    stop = {"stale-revision": "stale-revision", "budget-exhausted": "budget-exhausted",
                            "investigation-stopped": "cancelled", "investigation-paused": "cancelled"}.get(
                        d.get("reason"), "error")
                    break
                invalid_run = 0
                if d.get("n_calls"):
                    r = await workflow.execute_activity("execute_decision", {**base, "decision_id": d["decision_id"]},
                                                        task_queue=INVESTIGATOR_QUEUE,
                                                        start_to_close_timeout=timedelta(seconds=600),
                                                        heartbeat_timeout=HB, retry_policy=SHORT)
                    receipts.append({"decision_id": d["decision_id"], "summary": r.get("summary")})
                control = d.get("control") or {}
                action = control.get("action")
                if action == "wait_for_jobs" and d.get("n_calls") and r.get("nothing_outstanding") \
                        and workflow.patched("failed-calls-no-wait-v1"):
                    # Nothing to wait for after the calls ran (all failed or launched nothing): the next slot sees
                    # the failed receipts; repeated, it ends like refused proposals (bounded retry, then surface).
                    failed_waits += 1
                    if failed_waits >= MAX_CONSECUTIVE_INVALID or slot == int(b.get("max_model_calls", 4)) - 1:
                        stop = "invalid-proposals"
                        break
                    continue
                if action == "wait_for_jobs":
                    stop = "waiting"
                    break
                if action == "request_input":
                    stop = "input-requested"
                    break
                if action in ("conclude", "stop"):
                    stop = "concluded"
                    break
                if d.get("budget_exhausted"):
                    stop = "budget-exhausted"
                    break
                if not d.get("n_calls"):
                    stop = "waiting"
                    break
        except CancelledError:
            stop = "cancelled"
            raise
        except ActivityError as exc:
            cause = exc.cause
            stop = "budget-exhausted" if isinstance(cause, ApplicationError) and cause.type == "BUDGET_EXHAUSTED" else "error"
            if stop == "error":
                failure = turn_failure(exc)
        finally:
            await workflow.execute_activity("complete_turn", {**base, "stop_reason": stop, "control": control,
                                                              "receipts": receipts[-20:],
                                                              **({"failure": failure} if failure else {})},
                                            task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60),
                                            heartbeat_timeout=HB, retry_policy=SHORT)
        return {"started": True, "turn_id": tid, "stop_reason": stop, "control": control,
                **({"failure": failure} if failure else {})}


@workflow.defn(name="JobWorkflow")
class JobWorkflow:
    def __init__(self) -> None:
        self._cancel = False

    @workflow.signal(name="cancel")
    def cancel(self, policy: str = "cancel") -> None:
        # checkpoint-and-stop is delivered to the worker through the lab (heartbeat response), so the
        # attempt can checkpoint and publish; only a hard cancel interrupts the activity.
        if policy != "checkpoint-and-stop":
            self._cancel = True

    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        wall = int(a.get("wall_seconds", 900))
        handle = workflow.start_activity(
            "run_attempt", a, task_queue=a["queue"], start_to_close_timeout=timedelta(seconds=wall + 600),
            heartbeat_timeout=timedelta(seconds=int(a.get("heartbeat_timeout", 60))),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=int(a.get("max_attempts", 3)),
                                     non_retryable_error_types=NON_RETRYABLE),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)
        await workflow.wait_condition(lambda: self._cancel or handle.done())
        if self._cancel and not handle.done():
            handle.cancel()
        try:
            result = await handle
        except ActivityError as exc:
            result = {"job_status": "failed-after-retries", "error": str(exc.cause or exc)[:500]}
            await workflow.execute_activity("fail_job", {**a, "error": result["error"]}, task_queue=a["queue"],
                                            start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        if a.get("kind") == "qualification" and result.get("job_status") == "succeeded":
            await workflow.execute_activity("submit_qualification_results", a, task_queue=ASSESSOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=120), retry_policy=SHORT)
        return result


@workflow.defn(name="AssessmentWorkflow")
class AssessmentWorkflow:
    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        return await workflow.execute_activity("assess_plan", a, task_queue=ASSESSOR_QUEUE,
                                               start_to_close_timeout=timedelta(seconds=900), retry_policy=SHORT)


ALL_WORKFLOWS = [InvestigationWorkflow, TurnWorkflow, JobWorkflow, AssessmentWorkflow]
