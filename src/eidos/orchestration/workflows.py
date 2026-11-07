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

WAIT_REASONS = ("waiting", "input-requested", "stale-revision", "calls-exhausted-waiting")
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

    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        inv = a["investigation_id"]
        counter = int(a.get("turn_counter", 0))
        history_limit = int(a.get("history_limit", 1500))
        turns_per_run = int(a.get("turns_per_run", 20))
        idle = int(a.get("idle_poll_seconds", 60))
        consecutive_errors = 0
        while True:
            if workflow.info().get_current_history_length() > history_limit or self._turns >= turns_per_run:
                self._status = "rolling-over"
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new({**a, "turn_counter": counter, "rollovers": int(a.get("rollovers", 0)) + 1})
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
            if not res.get("started") or reason in WAIT_REASONS or reason == "error":
                self._status = f"waiting ({reason})"
                try:
                    await workflow.wait_condition(lambda seen=seen: self._wakes > seen, timeout=timedelta(seconds=idle))
                except TimeoutError:
                    pass


@workflow.defn(name="TurnWorkflow")
class TurnWorkflow:
    @workflow.run
    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        b = await workflow.execute_activity("begin_turn", a, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        if not b.get("started"):
            return {"started": False, "reason": b.get("reason")}
        tid = b["turn_id"]
        base = {"turn_id": tid, "project_id": a["project_id"], "investigation_id": a["investigation_id"]}
        stop, control, receipts = "calls-exhausted", {}, []
        try:
            await workflow.execute_activity("build_context", base, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=120), retry_policy=SHORT)
            for slot in range(int(b.get("max_model_calls", 4))):
                d = await workflow.execute_activity("decide", {**base, "slot": slot}, task_queue=INVESTIGATOR_QUEUE,
                                                    start_to_close_timeout=timedelta(seconds=int(b.get("model_timeout", 300))),
                                                    retry_policy=MODEL)
                if not d.get("accepted"):
                    stop = "stale-revision" if d.get("reason") == "stale-revision" else "error"
                    break
                if d.get("n_calls"):
                    r = await workflow.execute_activity("execute_decision", {**base, "decision_id": d["decision_id"]},
                                                        task_queue=INVESTIGATOR_QUEUE,
                                                        start_to_close_timeout=timedelta(seconds=600), retry_policy=SHORT)
                    receipts.append({"decision_id": d["decision_id"], "summary": r.get("summary")})
                control = d.get("control") or {}
                action = control.get("action")
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
                                            retry_policy=SHORT)
        return {"started": True, "turn_id": tid, "stop_reason": stop, "control": control}


@workflow.defn(name="JobWorkflow")
class JobWorkflow:
    def __init__(self) -> None:
        self._cancel = False

    @workflow.signal(name="cancel")
    def cancel(self, policy: str = "cancel") -> None:
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
