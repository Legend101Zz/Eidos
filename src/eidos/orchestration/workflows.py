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
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, ChildWorkflowError

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
TERMINAL_REASONS = ("concluded", "budget-exhausted", "max-turns-reached", "stopped", "investigation is stopped",
                    "investigation is concluded")


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

    async def _wait_for_operator(self, a: dict, inv: str, history_limit: int, idle: int) -> None:
        self._status = "needs operator input (repeated refused proposals)"
        while True:
            seen = self._wakes
            try:
                await workflow.wait_condition(lambda seen=seen: self._wakes > seen
                                              or workflow.info().get_current_history_length() > history_limit,
                                              timeout=timedelta(seconds=idle))
            except TimeoutError:
                pass
            if workflow.info().get_current_history_length() > history_limit:
                workflow.continue_as_new({**a, "rollovers": int(a.get("rollovers", 0)) + 1})
            news = await workflow.execute_activity(
                "check_news", {"investigation_id": inv, "project_id": a["project_id"], "operator_only": True},
                task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            if news.get("news"):
                return

    async def _resume_quiet_wait(self, a: dict, inv: str, history_limit: int, idle: int) -> None:
        """Continue a quiet wait that rolled over (Continue-As-New) without news: no model turn
        until a wake, ledger news or the max-idle timer, which counts from the original wait start."""
        rw = a["resume_wait"]
        since = float(rw["since"])
        max_idle = int(a.get("max_idle_turn_seconds", 6 * 3600))
        self._status = f"waiting ({rw.get('reason') or 'quiet'})"
        while True:
            seen = self._wakes
            try:
                await workflow.wait_condition(lambda seen=seen: self._wakes > seen
                                              or workflow.info().get_current_history_length() > history_limit,
                                              timeout=timedelta(seconds=idle))
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

    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        inv = a["investigation_id"]
        counter = int(a.get("turn_counter", 0))
        history_limit = int(a.get("history_limit", 1500))
        turns_per_run = int(a.get("turns_per_run", 20))
        idle = int(a.get("idle_poll_seconds", 60))
        consecutive_errors = 0
        invalid_turns = int(a.get("invalid_turns", 0))  # consecutive turns ended on the invalid-proposal limit
        while True:
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
                                          "invalid_turns": invalid_turns})
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
            except ChildWorkflowError:
                consecutive_errors += 1
                res = {"started": True, "stop_reason": "error"}
                if consecutive_errors >= 3:
                    self._status = "failed"
                    return {"status": "failed", "reason": "three consecutive turn failures", "turn_counter": counter}
            reason = res.get("stop_reason") or res.get("reason") or ""
            if reason in TERMINAL_REASONS or reason.startswith("investigation is stopped") or reason == "max-turns-reached":
                self._status = "finished"
                return {"status": "finished", "reason": reason, "turn_counter": counter}
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
                        await workflow.wait_condition(
                            lambda seen=seen: self._wakes > seen
                            or workflow.info().get_current_history_length() > history_limit,
                            timeout=timedelta(seconds=idle))
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
        stop, control, receipts = "calls-exhausted", {}, []
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
                    stop = {"stale-revision": "stale-revision", "budget-exhausted": "budget-exhausted"}.get(
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
        finally:
            await workflow.execute_activity("complete_turn", {**base, "stop_reason": stop, "control": control,
                                                              "receipts": receipts[-20:]},
                                            task_queue=INVESTIGATOR_QUEUE, start_to_close_timeout=timedelta(seconds=60),
                                            heartbeat_timeout=HB, retry_policy=SHORT)
        return {"started": True, "turn_id": tid, "stop_reason": stop, "control": control}


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
