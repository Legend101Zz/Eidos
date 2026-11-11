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

from collections.abc import Callable
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

from eidos.domain.turns import WALL_REACHED, turn_boundary
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
WAIT_REASONS = ("waiting", "input-requested", "stale-revision", "calls-exhausted-waiting", "invalid-proposals", "runtime-bounded")
MAX_CONSECUTIVE_INVALID = 2
# ADR 0019: a failed turn (stop reason "error") never leaves the investigation active with nothing scheduled. A
# retryable failure gets at most MAX_FAILED_TURN_RETRIES automatic retry turns, after a recorded, growing backoff;
# a non-retryable failure, or one that persists through the retries, marks the investigation needs-input at once.
MAX_FAILED_TURN_RETRIES = 2
# ADR 0022: a batch waiting for its own jobs comes back to the workflow between activity slices. It looks again after a
# durable timer; the last slice, when the turn's own timeout is near, records what could not run instead of waiting.
BATCH_POLL_SECONDS = 2
BATCH_FINAL_MARGIN_SECONDS = 120
FAILED_TURN_BACKOFF_SECONDS = 60
MAX_FAILED_TURN_BACKOFF_SECONDS = 900
TERMINAL_REASONS = ("concluded", "budget-exhausted", "max-turns-reached", "stopped", "investigation is stopped",
                    "investigation is concluded", "finalization-exhausted", WALL_REACHED)


def turn_failure(exc: ActivityError) -> dict[str, Any]:
    """Why an activity failure ended a turn, for the coordinator's policy and the record (ADR 0019). Not retryable when
    Temporal stopped retrying because the error was marked non-retryable (an invalid model spec, an authentication
    or not-found error from the provider, a refused payload); retryable when its retries ran out or it timed out."""
    cause = exc.cause
    non_retryable = exc.retry_state == RetryState.NON_RETRYABLE_FAILURE or (
        isinstance(cause, ApplicationError) and cause.non_retryable)
    etype = cause.type if isinstance(cause, ApplicationError) and cause.type else type(cause or exc).__name__
    out = {"activity": exc.activity_type, "error_type": etype, "retryable": not non_retryable,
           "retry_state": exc.retry_state.name if exc.retry_state is not None else None,
           "message": str(getattr(cause, "message", None) or cause or exc)[:300]}
    details = cause.details if isinstance(cause, ApplicationError) else ()
    hint = details[0] if details and isinstance(details[0], dict) else {}
    if hint.get("kind"):  # ADR 0023: the provider failure's class and its Retry-After, for the turn retry
        out["provider"] = {k: hint.get(k) for k in ("kind", "status", "retry_after_seconds")}
    if hint.get("retry_stopped"):
        # R14 #5: the decide activity stopped retrying because the provider's delay cannot fit before the exploration
        # deadline. That is not a fault needing an operator: the turn retry waits until that deadline at most
        # (turn_retry_delay) and the next turn begins in the finalize phase, which may use the hold-back.
        out["retryable"] = True
        out["retry_stopped"] = str(hint["retry_stopped"])
    return out


def turn_retry_delay(base_delay: float, failure: dict[str, Any], completion: dict[str, Any], now: float,
                     jitter: float) -> float:
    """ADR 0023: the backoff before a retry turn. At least the provider's Retry-After (when the failure carried one),
    with ``jitter`` (a fraction in [0, 1), from the workflow's deterministic random) of up to 20 % added, and never past
    the start of the conclusion's reserve: the retry spends exploration time only."""
    after = float(((failure.get("provider") or {}).get("retry_after_seconds")) or 0.0)
    delay = max(float(base_delay), after) * (1.0 + 0.2 * jitter)
    delay = min(delay, float(MAX_FAILED_TURN_BACKOFF_SECONDS))
    if completion:
        # R14 #5: an exploratory turn's retry never waits into the reproduction hold-back; when the requested delay
        # cannot fit, the backoff ends at the exploration deadline and the next turn begins in the finalize phase.
        boundary = float(completion["deadline_unix"]) - float(completion["conclude_threshold_seconds"])
        if completion.get("exploration_deadline_unix") is not None:
            boundary = min(boundary, float(completion["exploration_deadline_unix"]))
        delay = min(delay, boundary - now)
    return max(0.0, delay)


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

    @staticmethod
    def _wall_idle(idle: int, completion: dict[str, Any]) -> int:
        """The poll interval, shortened so that a quiet wait ends when the conclusion's reserve begins (ADR 0020)."""
        if not completion:
            return idle
        boundary = turn_boundary(completion) if workflow.patched("exploration-boundary-finalization-v1") else (
            float(completion["deadline_unix"]) - float(completion["conclude_threshold_seconds"]))
        until = boundary - workflow.now().timestamp()
        return max(1, min(idle, int(until) + 1))

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

    async def _surface_end(self, a: dict, inv: str, counter: int, reason: str) -> bool:
        """The coordinator stops on its own (budget or turn limit, repeated failures) while the investigation is
        still active: record that it needs the operator (visible label and event). The reconciler then leaves it
        alone instead of restarting a coordinator that would only stop again (G5 live run)."""
        if not workflow.patched("terminal-outcome-needs-input-v1"):
            return False
        terminal = workflow.patched("terminal-controller-abstention-v1")
        marked = await workflow.execute_activity(
            "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                 "reason": f"coordinator stopped: {reason}" if terminal else
                                           f"coordinator stopped: {reason}; steer or resume to continue (limits are fixed at start), or stop",
                                 **({"terminal": True} if terminal else {})},
            task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        return bool((marked or {}).get("controller_abstention"))

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
                                 history_limit: int, idle: int) -> str:
        """ADR 0019. A retryable failure within MAX_FAILED_TURN_RETRIES: record the retry (ledger event), back off
        (base x 2^(n-1), capped; a wake can end the backoff early), then start the next ordinary turn. A terminal
        failure closes a default run with a controller abstention or waits for the operator before resuming."""
        failure = dict(res.get("failure") or {"error_type": "unknown", "retryable": True})
        retryable = bool(failure.get("retryable", True))
        quota = failure.get("error_type") == "PROVIDER_QUOTA_EXHAUSTED" and workflow.patched("provider-unavailable-v1")
        if retryable and not quota and failed_turns <= MAX_FAILED_TURN_RETRIES:
            base = float(a.get("failed_turn_backoff_seconds", FAILED_TURN_BACKOFF_SECONDS))
            delay = min(base * 2 ** (failed_turns - 1), float(MAX_FAILED_TURN_BACKOFF_SECONDS))
            completion = res.get("completion") or {}
            if workflow.patched("turn-retry-after-v1"):
                # ADR 0023: honour the provider's Retry-After with jitter, within the exploration time left
                delay = turn_retry_delay(delay, failure, completion, workflow.now().timestamp(), workflow.random().random())
            elif completion:  # ADR 0020: a retry's backoff never outlives the wall time
                delay = max(0.0, min(delay, float(completion["deadline_unix"]) - workflow.now().timestamp()))
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
            return "retry"
        why = "provider quota exhausted" if quota else "non-retryable error" if not retryable else \
            f"still failing after {MAX_FAILED_TURN_RETRIES} automatic retries"
        details = {"turn_counter": counter, "turn_id": res.get("turn_id"), "failure": failure, "retryable": retryable,
                   "consecutive_failed_turns": failed_turns, "automatic_retries": failed_turns - 1,
                   "max_automatic_retries": MAX_FAILED_TURN_RETRIES}
        # ADR 0023: an exhausted provider quota is recorded as the provider being unavailable, at once, not retried
        code = "provider-unavailable" if quota else "turn-failed"
        terminal = workflow.patched("failed-turn-controller-abstention-v1")
        marked = await workflow.execute_activity(
            "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                 "code": code, "details": details,
                                 **({"terminal": True} if terminal else {}),
                                 "reason": f"turn failed ({why}): {failure.get('error_type')}: "
                                           f"{str(failure.get('message', ''))[:200]}; steer, resume or stop"},
            task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        if (marked or {}).get("controller_abstention"):
            return "controller-abstained"
        cursor = (marked or {}).get("cursor")
        await self._wait_for_operator({**{k: v for k, v in a.items() if k != "operator_cursor"},
                                       "turn_counter": counter, "needs_operator": "turn-failed",
                                       **({"operator_cursor": int(cursor)} if cursor else {})}, inv,
                                      history_limit, idle, "needs operator input (turn failed)")
        return "operator-resumed"

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
        if workflow.patched("finalization-child-recovery-v2"):
            recovered = await workflow.execute_activity(
                "reconcile_finalization", {"investigation_id": inv, "project_id": a["project_id"],
                                            "turn_counter": counter},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            if recovered.get("controller_abstention"):
                return {"status": "abstained", "reason": "controller-abstained", "turn_counter": counter}
            if recovered.get("stop_reason") == "cancelled":
                return {"status": "finished", "reason": "cancelled", "turn_counter": counter}
            if recovered.get("finalization"):
                return {"status": "finished" if recovered.get("investigator_concluded") else "needs_input",
                        "reason": "concluded" if recovered.get("investigator_concluded") else "finalization-failed",
                        "turn_counter": counter}
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
                a = {**{k: v for k, v in a.items() if k != "operator_cursor"}, "invalid_turns": 0}
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
            child: dict[str, Any] = {"investigation_id": inv, "project_id": a["project_id"], "index": counter}
            if workflow.patched("turn-ticket-v1"):
                # F5c (review R8 #1, ADR 0017 addendum): an ordered ticket for this turn workflow, issued by the
                # coordinator (the only one running for the investigation) from its workflow clock, which is durable
                # (the child's input is in the history) and strictly increasing across its turn workflows: each is
                # started only after the previous one ended, and across Continue-As-New, reset or a restarted
                # coordinator. A delayed begin_turn of an earlier, terminated turn workflow carries an older ticket,
                # and the lab never lets it take the turn back from a newer one.
                # Strictly increasing even if two starts read the same clock value: never below the last ticket + 1
                # (carried through Continue-As-New as ``last_ticket``).
                ticket = max(int(workflow.now().timestamp() * 1_000_000), int(a.get("last_ticket") or 0) + 1)
                a = {**a, "last_ticket": ticket}
                child["ticket"] = ticket
            try:
                res = await workflow.execute_child_workflow(
                    "TurnWorkflow", child,
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
            if res.get("stop_reason") == "error" and workflow.patched("finalization-child-recovery-v2"):
                # The child may have failed before returning its finalization flag. Read the durable admission,
                # close any abandoned recovery turn and its export receipt before the generic retry policy.
                recovered = await workflow.execute_activity(
                    "reconcile_finalization", {"investigation_id": inv, "project_id": a["project_id"],
                                                "turn_counter": counter, "failure": res.get("failure")},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                if recovered.get("controller_abstention"):
                    res["controller_abstention"] = recovered["controller_abstention"]
                if recovered.get("finalization"):
                    res["finalization"] = True
                    if recovered.get("investigator_concluded"):
                        res["stop_reason"] = "concluded"  # closure committed before the child's failure
                    elif recovered.get("stop_reason") == "cancelled":
                        res["stop_reason"] = "cancelled"
            reason = res.get("stop_reason") or res.get("reason") or ""
            if res.get("controller_abstention"):
                self._status = "controller abstained"
                return {"status": "abstained", "reason": "controller-abstained", "turn_counter": counter}
            if res.get("finalization") and reason not in ("concluded", "cancelled"):
                self._status = "needs operator input (finalization failed)"
                await workflow.execute_activity(
                    "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"],
                                         "turn_counter": counter, "code": "finalization-failed",
                                         "reason": "bounded autonomous finalization failed",
                                         "details": {"failure": res.get("failure") or {"reason": reason},
                                                     "export_receipt": {"kind": "finalization_export_receipt", "id": inv}}},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                return {"status": "needs_input", "reason": "finalization-failed", "turn_counter": counter}
            if reason in TERMINAL_REASONS or reason.startswith("investigation is stopped") or reason == "max-turns-reached":
                self._status = "finished"
                if reason in ("budget-exhausted", "max-turns-reached", WALL_REACHED) and await self._surface_end(a, inv, counter, reason):
                    self._status = "controller abstained"
                    return {"status": "abstained", "reason": "controller-abstained", "turn_counter": counter}
                return {"status": "finished", "reason": reason, "turn_counter": counter}
            if reason != "error":
                failed_turns = 0
            elif workflow.patched("failed-turn-policy-v1"):
                # Histories recorded before ADR 0019 waited for ledger news after a failed turn (up to the max-idle
                # timer, with nothing scheduled and nothing visible: pilot D12-r3).
                failed_turns += 1
                invalid_turns = 0
                failure_action = await self._after_failed_turn(a, inv, counter, failed_turns, res, history_limit, idle)
                if failure_action == "controller-abstained":
                    self._status = "controller abstained"
                    return {"status": "abstained", "reason": "controller-abstained", "turn_counter": counter}
                if failure_action == "operator-resumed":
                    failed_turns = 0
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
            if workflow.patched("bounded-autonomous-finalization-v1") and (needs_input or reason == "runtime-bounded"):
                final = await workflow.execute_activity(
                    "enter_finalization", {"investigation_id": inv, "project_id": a["project_id"],
                                           "turn_counter": counter, "trigger": "refused-proposals" if needs_input else reason,
                                           "runtime_outcome": res.get("runtime_outcome")},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                if final.get("enabled"):
                    invalid_turns = 0
                    self._status = "finalizing"
                    continue
                needs_input = True
            if needs_input and workflow.patched("needs-input-visible-v1"):
                terminal = workflow.patched("refused-turn-controller-abstention-v1")
                marked = await workflow.execute_activity(
                    "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"], "turn_counter": counter,
                                         "refused_turns": invalid_turns, **({"terminal": True} if terminal else {}),
                                         "reason": "runtime exhausted after bounded recovery" if reason == "runtime-bounded"
                                                   else "repeated refused proposals after bounded retries",
                                         **({"code": "runtime-bounded", "details": {"runtime_outcome": res.get("runtime_outcome")}}
                                            if reason == "runtime-bounded" else {})},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                if (marked or {}).get("controller_abstention"):
                    self._status = "controller abstained"
                    return {"status": "abstained", "reason": "controller-abstained", "turn_counter": counter}
                # F5c (review R8 #3): this wait, like the failed-turn wait (A1), counts only operator events after the
                # mark's cursor, not after the last turn's horizon (an event between the two would end it at once).
                # The cursor is kept in the workflow input, so a Continue-As-New during the wait carries it too.
                cursor = (marked or {}).get("cursor")
                a = {k: v for k, v in a.items() if k != "operator_cursor"}
                if cursor:
                    a["operator_cursor"] = int(cursor)
            completion = res.get("completion") or {}
            if completion and reason == "deadline" and workflow.patched("exploration-boundary-finalization-v1") \
                    and (completion.get("exploration_deadline_unix") is not None
                         or not workflow.patched("exploration-boundary-only-v3")):
                # Only exploratory turns cross this boundary. Ordinary finalization ends at the separate
                # conclusion reserve and starts its fresh conclusion turn through the existing path below.
                # Wait only to the true exploration boundary, never through the reproduction/export hold-back.
                left = turn_boundary(completion) - workflow.now().timestamp()
                if left > 0:
                    await workflow.sleep(left)
                final = await workflow.execute_activity(
                    "enter_finalization", {"investigation_id": inv, "project_id": a["project_id"],
                                           "turn_counter": counter, "trigger": "exploration-boundary"},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                if final.get("enabled") or not workflow.patched("ordinary-finalization-v2"):
                    self._status = "finalizing"
                    continue
                marked = await workflow.execute_activity(
                    "mark_needs_input", {"investigation_id": inv, "project_id": a["project_id"],
                                         "turn_counter": counter, "code": "runtime-bounded",
                                         "reason": "exploration boundary reached; autonomous recovery disabled"},
                    task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
                await self._wait_for_operator({**a, "turn_counter": counter, "needs_operator": "turn-failed",
                                              **({"operator_cursor": marked["cursor"]} if marked.get("cursor") else {})},
                                             inv, history_limit, idle, "needs operator input (exploration boundary)")
                continue
            if completion and reason == "deadline":
                # A retry that would have crossed the boundary ended a little before it: the fresh turn starts there.
                left = float(completion["deadline_unix"]) - float(completion["conclude_threshold_seconds"]) - workflow.now().timestamp()
                if left > 0:
                    await workflow.sleep(left)
                self._status = "concluding (wall time reserved for the conclusion)"
                continue
            if completion and reason != "error" and float(completion["deadline_unix"]) - float(
                    completion["conclude_threshold_seconds"]) <= workflow.now().timestamp():
                # ADR 0020: inside the conclusion's reserve there is nothing to wait for. Start the next turn at once;
                # it is told to conclude, and begin_turn ends the investigation at the wall deadline.
                self._status = "concluding (wall time reserved for the conclusion)"
                continue
            if not res.get("started") or reason in WAIT_REASONS or reason == "error":
                self._status = "needs operator input (repeated refused proposals)" if needs_input else f"waiting ({reason})"
                waited_since = workflow.now()
                max_idle = int(a.get("max_idle_turn_seconds", 6 * 3600))
                quiet_news = False
                while True:
                    try:
                        # Signals add history while waiting; leave the wait (and roll over at the loop
                        # top) once the history crosses its limit, so a wake storm cannot grow it.
                        await self._wait_for_wake_or_full(seen, history_limit, self._wall_idle(idle, completion))
                        if not needs_input or workflow.info().get_current_history_length() > history_limit:
                            break
                        seen = self._wakes  # job/evidence wakes do not end a needs-input wait; operator news does
                    except TimeoutError:
                        pass
                    if not workflow.patched("idle-poll-needs-news-v1"):
                        break  # histories recorded before this change started a turn on every timeout
                    if completion and (turn_boundary(completion) if workflow.patched("exploration-boundary-finalization-v1")
                                       else float(completion["deadline_unix"]) - float(completion["conclude_threshold_seconds"])) <= workflow.now().timestamp():
                        break  # ADR 0020: the conclusion's reserve has begun; the next turn concludes
                    # A quiet poll costs no model call: start a turn only if something happened.
                    news = await workflow.execute_activity(
                        "check_news", {"investigation_id": inv, "project_id": a["project_id"],
                                       **({"operator_only": True} if needs_input else {}),
                                       **({"after": int(a["operator_cursor"])}
                                          if needs_input and a.get("operator_cursor") else {})},
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
    def _turn_seconds_left(self) -> float | None:
        info = workflow.info()
        if info.execution_timeout is None:
            return None
        return (info.start_time + info.execution_timeout - workflow.now()).total_seconds()

    async def _drive_batch(self, base: dict, decision_id: str, r: dict, boundary: float | None,
                           bounded: Callable[[], dict[str, Any]]) -> dict:
        """ADR 0022: re-run a batch that is waiting for its own jobs until it has finished. Each run re-admits nothing
        (committed calls replay by command key) and goes on where the last stopped; near the turn's timeout, or the
        conclusion reserve's start (ADR 0020), it is told to record the calls that cannot start (the jobs it admitted keep
        running, independently of the turn)."""
        while r.get("pending"):
            left = self._turn_seconds_left()
            if boundary is not None:  # the reserve's start, which the batch may not wait through
                to_boundary = boundary - workflow.now().timestamp()
                left = to_boundary if left is None else min(left, to_boundary)
            final = left is not None and left < BATCH_FINAL_MARGIN_SECONDS
            if not final:
                await workflow.sleep(BATCH_POLL_SECONDS)
            r = await workflow.execute_activity(
                "execute_decision", {**base, "decision_id": decision_id, "continuation": r.get("continuation"),
                                     **({"final": True} if final else {})},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=600),
                heartbeat_timeout=HB, retry_policy=SHORT, **bounded())
        return r

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
        runtime_outcome = None
        failed_waits = 0  # accepted waits whose calls left nothing outstanding
        # ADR 0020 addendum R10: everything in this turn, every retry included, ends at its boundary. A schedule-to-close
        # timeout covers all attempts of one activity; the check before each slot covers the slots between them.
        boundary = turn_boundary(b["completion"]) if b.get("completion") else None

        def bounded() -> dict[str, Any]:
            if boundary is None:
                return {}
            return {"schedule_to_close_timeout": timedelta(seconds=max(1.0, boundary - workflow.now().timestamp()))}

        try:
            await workflow.execute_activity("build_context", base, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=120),
                                            heartbeat_timeout=HB, retry_policy=SHORT, **bounded())
            for slot in range(int(b.get("max_model_calls", 4))):
                if boundary is not None and workflow.now().timestamp() >= boundary:
                    stop = "deadline"
                    break
                d = await workflow.execute_activity("decide", {**base, "slot": slot}, task_queue=INVESTIGATOR_QUEUE,
                                                    start_to_close_timeout=timedelta(seconds=int(b.get("model_timeout", 300))),
                                                    heartbeat_timeout=HB, retry_policy=MODEL, **bounded())
                if d.get("runtime_outcome"):
                    runtime_outcome = d["runtime_outcome"]
                    stop = "runtime-bounded"
                    break
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
                                                        heartbeat_timeout=HB, retry_policy=SHORT, **bounded())
                    if r.get("pending") and workflow.patched("dependent-batch-v1"):
                        r = await self._drive_batch(base, d["decision_id"], r, boundary, bounded)
                    receipts.append({"decision_id": d["decision_id"], "summary": r.get("summary")})
                    if (r.get("stopped") or r.get("skipped")) and workflow.patched("refused-execution-cancels-v1"):
                        # F5d (review R9): execution refused (a stop, pause, turn closure or a run that lost the turn
                        # committed first) is a cancellation: the decision's control (e.g. conclude) is never applied.
                        # Older histories went on to the decision's control; complete_turn refuses it too.
                        stop = "cancelled"
                        break
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
            if stop == "error" and boundary is not None and (
                    exc.retry_state == RetryState.TIMEOUT or workflow.now().timestamp() >= boundary):
                # ADR 0020 addendum R10: the boundary ended this activity (a retry that would cross it counts). That is
                # not a failed turn: the coordinator starts a fresh one, which sees the wall time then left.
                stop = "deadline"
            if stop == "error":
                failure = turn_failure(exc)
        finally:
            done = await workflow.execute_activity("complete_turn", {**base, "stop_reason": stop, "control": control,
                                                                     "receipts": receipts[-20:],
                                                                     **({"failure": failure} if failure else {}),
                                                                     **({"runtime_outcome": runtime_outcome} if runtime_outcome else {})},
                                                   task_queue=INVESTIGATOR_QUEUE,
                                                   start_to_close_timeout=timedelta(seconds=60),
                                                   heartbeat_timeout=HB, retry_policy=SHORT)
            if stop == "concluded" and (done or {}).get("status") == "cancelled" \
                    and workflow.patched("refused-execution-cancels-v1"):
                # F5d (review R9): a stop or pause committed after execution but before completion; the lab refused
                # the conclusion and closed the turn as cancelled, and the coordinator sees the same.
                stop = "cancelled"
        return {"started": True, "turn_id": tid, "stop_reason": stop, "control": control,
                **({"controller_abstention": done["controller_abstention"]} if (done or {}).get("controller_abstention") else {}),
                **({"finalization": True} if b.get("finalization") else {}),
                **({"runtime_outcome": runtime_outcome} if runtime_outcome else {}),
                **({"failure": failure} if failure else {}),
                **({"completion": b["completion"]} if b.get("completion") else {})}


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
