"""ADR 0019, the failed-turn policy, on a Temporal development server (dev server is for tests only). A turn that ends
on an error never leaves the investigation active with nothing scheduled: a retryable failure gets at most two
automatic retry turns after a recorded, growing backoff; a non-retryable failure, or one that persists through the
retries, marks the investigation needs-input (code ``turn-failed``) and waits for operator news. Activities are in-test
fakes with the same names as the real ones; no model is called."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.exceptions import ActivityError, ApplicationError, RetryState, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeout
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, InvestigationWorkflow, turn_failure

temporal = pytest.mark.temporal
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")
TURN_FAILED_STATUS = "needs operator input (turn failed)"


@pytest.fixture(scope="module")
async def env():
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    e = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield e
    await e.shutdown()


class Scripted:
    """The scripted investigator activities of test_workflows.FakeInvestigator, whose ``decide`` fails per turn index. ``script`` maps a turn index (1-based, the coordinator's
    counter) to ``retryable`` (a retryable model-call error: the MODEL policy retries it, then gives up), ``fatal`` (a
    non-retryable error) or ``hang`` (heartbeats until the turn workflow is gone); other turns decide normally (one
    call, then wait for jobs). ``check_news`` with ``operator_only`` answers from ``operator``."""

    def __init__(self, script: dict[int, str] | None = None, stop_after: int = 1000):
        self.turns = 0
        self.stop_after = stop_after
        self.completed: list[dict] = []
        self.script = dict(script or {})
        self.started: list[int] = []
        self.marks: list[dict] = []
        self.retries: list[dict] = []
        self.decide_attempts: dict[int, int] = {}
        self.operator = False
        self.operator_checks = 0

    @property
    def turn_failed_marks(self) -> list[dict]:
        return [m for m in self.marks if m.get("code") == "turn-failed"]

    def completed_turn(self, index: int) -> dict:
        (c,) = [c for c in self.completed if c["turn_id"] == f"t{index}"]
        return c

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        if self.turns >= self.stop_after:
            return {"started": False, "reason": "max-turns-reached"}
        self.turns += 1
        self.started.append(a["index"])
        return {"started": True, "turn_id": f"t{a['index']}", "max_model_calls": 2}

    @activity.defn(name="decide")
    async def decide(self, a: dict) -> dict:
        index = int(a["turn_id"][1:])
        mode = self.script.get(index, "ok")
        self.decide_attempts[index] = self.decide_attempts.get(index, 0) + 1
        if mode == "retryable":
            raise ApplicationError("MODEL_CALL_FAILED: upstream 503 (overloaded)", type="MODEL_CALL_FAILED",
                                   non_retryable=False)
        if mode == "fatal":
            raise ApplicationError("MODEL_CALL_FAILED: authentication rejected", type="MODEL_CALL_FAILED",
                                   non_retryable=True)
        if mode == "hang":
            for _ in range(100):  # bounded: the turn workflow's execution timeout cuts it off first
                activity.heartbeat()
                await asyncio.sleep(0.1)
        return {"accepted": True, "decision_id": f"{a['turn_id']}-d{a['slot']}", "n_calls": 1,
                "control": {"action": "wait_for_jobs"} if a["slot"] == 1 else {}}

    @activity.defn(name="build_context")
    async def build_context(self, a: dict) -> dict:
        return {"context_artifact_id": "ctx"}

    @activity.defn(name="execute_decision")
    async def execute_decision(self, a: dict) -> dict:
        return {"summary": [{"tool": "record_note", "ok": True}]}

    @activity.defn(name="complete_turn")
    async def complete_turn(self, a: dict) -> dict:
        self.completed.append(a)
        return {"status": "completed"}

    @activity.defn(name="record_turn_retry")
    async def record_turn_retry(self, a: dict) -> dict:
        self.retries.append(a)
        return {}

    @activity.defn(name="check_news")
    async def check_news(self, a: dict) -> dict:
        if a.get("operator_only"):
            self.operator_checks += 1
            return {"news": self.operator}
        return {"news": True}

    @activity.defn(name="mark_needs_input")
    async def mark_needs_input(self, a: dict) -> dict:
        self.marks.append(a)
        return {"already": False}

    def acts(self):
        return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn,
                self.check_news, self.mark_needs_input, self.record_turn_retry]


async def until(predicate, timeout: float = 30.0, what: str = "condition", step: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@asynccontextmanager
async def running(env, fake: Scripted, **args):
    """The workflow and investigator workers plus a started coordinator; yields (handle, workflow id)."""
    wid = f"inv-ftp-{uuid.uuid4().hex[:6]}"
    full = {"investigation_id": wid, "project_id": "p", "idle_poll_seconds": 1, "failed_turn_backoff_seconds": 1, **args}
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        h = await env.client.start_workflow(InvestigationWorkflow.run, full, id=wid, task_queue=WORKFLOW_QUEUE)
        try:
            yield h, wid
        finally:
            try:
                await h.terminate()
            except Exception:  # already finished
                pass


def assert_failure(failure: dict, *, retryable: bool, retry_state: str, error_type: str = "MODEL_CALL_FAILED") -> None:
    assert failure["activity"] == "decide", failure
    assert failure["error_type"] == error_type, failure
    assert failure["retryable"] is retryable, failure
    assert failure["retry_state"] == retry_state, failure
    assert "MODEL_CALL_FAILED" in failure["message"], failure


@temporal
async def test_retryable_failure_records_a_retry_then_the_next_turn_runs(env):
    fake = Scripted({1: "retryable"}, stop_after=2)
    async with running(env, fake) as (h, wid):
        result = await asyncio.wait_for(h.result(), 60)
    assert result == {"status": "finished", "reason": "max-turns-reached", "turn_counter": 3}
    assert fake.started == [1, 2]
    assert fake.decide_attempts[1] == 3  # the MODEL policy retried the call before the turn failed
    assert fake.turn_failed_marks == []  # the only mark is the coordinator's own end-of-run surfacing
    (retry,) = fake.retries
    assert retry["investigation_id"] == wid and retry["project_id"] == "p"
    assert retry["turn_counter"] == 1 and retry["failed_turn_id"] == "t1"
    assert retry["attempt"] == 1 and retry["max_attempts"] == 2 and retry["delay_seconds"] == 1.0
    assert_failure(retry["failure"], retryable=True, retry_state="MAXIMUM_ATTEMPTS_REACHED")
    # The turn's own record was closed with the failure; the retry turn was an ordinary turn without one.
    c1, c2 = fake.completed_turn(1), fake.completed_turn(2)
    assert c1["stop_reason"] == "error"
    assert_failure(c1["failure"], retryable=True, retry_state="MAXIMUM_ATTEMPTS_REACHED")
    assert c2["stop_reason"] == "waiting" and "failure" not in c2


@temporal
async def test_non_retryable_failure_needs_the_operator_at_once(env):
    fake = Scripted({1: "fatal"}, stop_after=2)
    async with running(env, fake) as (h, _):
        await until(lambda: fake.turn_failed_marks, 20, "the turn-failed mark")
        (mark,) = fake.turn_failed_marks
        assert mark["turn_counter"] == 1
        assert mark["reason"].startswith("turn failed (non-retryable error): MODEL_CALL_FAILED")
        d = mark["details"]
        assert d["retryable"] is False and d["automatic_retries"] == 0 and d["max_automatic_retries"] == 2
        assert d["consecutive_failed_turns"] == 1 and d["turn_counter"] == 1 and d["turn_id"] == "t1"
        assert_failure(d["failure"], retryable=False, retry_state="NON_RETRYABLE_FAILURE")
        assert fake.decide_attempts[1] == 1  # not retried: Temporal stopped at the non-retryable error
        # No retry is recorded, and no turn starts while the operator has not acted (quiet polls ask for operator
        # news only).
        await until(lambda: fake.operator_checks >= 3, 15, "quiet operator polls")
        await h.signal("wake", 1)  # a job or evidence wake is not operator news
        await asyncio.sleep(2)
        assert fake.retries == [] and fake.started == [1]
        assert (await h.query("state"))["status"] == TURN_FAILED_STATUS
        # The operator acts: the next turn starts and runs normally.
        fake.operator = True
        result = await asyncio.wait_for(h.result(), 30)
    assert result["status"] == "finished" and fake.started == [1, 2]
    assert fake.completed_turn(2)["stop_reason"] == "waiting" and "failure" not in fake.completed_turn(2)
    assert len(fake.turn_failed_marks) == 1 and fake.retries == []


@temporal
async def test_persistent_retryable_failure_retries_twice_then_needs_the_operator(env):
    # Turns 1-3 fail with a retryable error; the operator's action then starts turn 4, which fails non-retryably:
    # the consecutive-failure count must have been reset by the operator action (1, not 4).
    fake = Scripted({1: "retryable", 2: "retryable", 3: "retryable", 4: "fatal"})
    async with running(env, fake) as (h, _):
        await until(lambda: fake.turn_failed_marks, 60, "the turn-failed mark after the retries")
        assert [r["attempt"] for r in fake.retries] == [1, 2]
        assert [r["max_attempts"] for r in fake.retries] == [2, 2]
        assert [r["delay_seconds"] for r in fake.retries] == [1.0, 2.0]  # base 1 s, doubling
        assert [r["turn_counter"] for r in fake.retries] == [1, 2]
        assert [r["failed_turn_id"] for r in fake.retries] == ["t1", "t2"]
        (mark,) = fake.turn_failed_marks
        assert mark["turn_counter"] == 3 and "still failing after 2 automatic retries" in mark["reason"]
        d = mark["details"]
        assert d["retryable"] is True and d["automatic_retries"] == 2 and d["max_automatic_retries"] == 2
        assert d["consecutive_failed_turns"] == 3 and d["turn_counter"] == 3 and d["turn_id"] == "t3"
        assert_failure(d["failure"], retryable=True, retry_state="MAXIMUM_ATTEMPTS_REACHED")
        # No fourth turn without operator news.
        await until(lambda: fake.operator_checks >= 2, 15, "quiet operator polls")
        await h.signal("wake", 1)
        await asyncio.sleep(2)
        assert fake.started == [1, 2, 3] and len(fake.retries) == 2
        assert (await h.query("state"))["status"] == TURN_FAILED_STATUS
        # The operator acts; turn 4 fails non-retryably and is a first failure again.
        fake.operator = True
        await until(lambda: len(fake.turn_failed_marks) == 2, 20, "the second turn-failed mark")
        assert fake.started == [1, 2, 3, 4] and len(fake.retries) == 2
        d2 = fake.turn_failed_marks[1]["details"]
        assert d2["retryable"] is False and d2["consecutive_failed_turns"] == 1 and d2["automatic_retries"] == 0
        assert d2["turn_id"] == "t4"


@temporal
async def test_a_successful_turn_between_failures_resets_the_count(env):
    # Failures at turns 1, 2 (attempts 1, 2), a good turn, then a failure again: attempt 1, not a third failure that
    # would need the operator.
    fake = Scripted({1: "retryable", 2: "retryable", 4: "retryable"}, stop_after=5)
    async with running(env, fake, failed_turn_backoff_seconds=0) as (h, _):
        result = await asyncio.wait_for(h.result(), 90)
    assert result["status"] == "finished" and fake.started == [1, 2, 3, 4, 5]
    assert [r["attempt"] for r in fake.retries] == [1, 2, 1]
    assert [r["turn_counter"] for r in fake.retries] == [1, 2, 4]
    assert [r["delay_seconds"] for r in fake.retries] == [0.0, 0.0, 0.0]
    assert fake.turn_failed_marks == []
    assert fake.completed_turn(3)["stop_reason"] == "waiting"


@temporal
async def test_backoff_ends_early_on_a_wake_signal(env):
    fake = Scripted({1: "retryable"}, stop_after=2)
    async with running(env, fake, failed_turn_backoff_seconds=3600) as (h, _):
        await until(lambda: fake.retries, 30, "the recorded retry")
        assert fake.retries[0]["delay_seconds"] == 900.0  # capped at 15 minutes
        await asyncio.sleep(2)
        assert fake.started == [1]  # the 15-minute backoff is in place
        assert "retrying after a failed turn (1/2) in 900s" in (await h.query("state"))["status"]
        t0 = time.monotonic()
        while 2 not in fake.started and time.monotonic() - t0 < 15:
            await h.signal("wake", 1)  # repeated: one sent while the retry is still being recorded would not count
            await asyncio.sleep(0.5)
        assert 2 in fake.started, "the wake did not end the backoff"
        assert time.monotonic() - t0 < 15
        result = await asyncio.wait_for(h.result(), 30)
    assert result["status"] == "finished" and fake.turn_failed_marks == [] and len(fake.retries) == 1


@temporal
async def test_turn_failed_wait_survives_continue_as_new(env):
    fake = Scripted({1: "fatal"})
    async with running(env, fake, history_limit=60) as (h, wid):
        await until(lambda: fake.turn_failed_marks, 20, "the turn-failed mark")
        first_run = (await env.client.get_workflow_handle(wid).describe()).run_id
        # Wakes are not operator news, but each one adds history to the wait: the coordinator must roll over.
        t0 = time.monotonic()
        current = first_run
        while current == first_run and time.monotonic() - t0 < 30:
            await h.signal("wake", 1)
            await asyncio.sleep(0.2)
            current = (await env.client.get_workflow_handle(wid).describe()).run_id
        assert current != first_run, "expected a Continue-As-New during the turn-failed wait"
        hist = await env.client.get_workflow_handle(wid, run_id=first_run).fetch_history()
        last = hist.events[-1]
        assert last.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
        carried = json.loads(
            last.workflow_execution_continued_as_new_event_attributes.input.payloads[0].data.decode())
        assert carried["needs_operator"] == "turn-failed" and carried["turn_counter"] == 1, carried
        # The new run keeps waiting: its status says so and no turn starts, however many wakes arrive.
        latest = env.client.get_workflow_handle(wid)
        status = ""
        for _ in range(50):
            status = (await latest.query("state"))["status"]
            if status == TURN_FAILED_STATUS:
                break
            await asyncio.sleep(0.2)
        assert status == TURN_FAILED_STATUS, status
        checks = fake.operator_checks
        for _ in range(8):
            await h.signal("wake", 1)
            await asyncio.sleep(0.25)
        await asyncio.sleep(2)
        assert fake.started == [1] and fake.operator_checks > checks
        assert len(fake.turn_failed_marks) == 1  # the rollover did not surface it a second time
        # Operator news ends the wait: the next turn starts, numbered after the failed one.
        fake.operator = True
        await until(lambda: 2 in fake.started, 15, "the turn after the operator acted")
        assert fake.started == [1, 2]


@temporal
async def test_turn_workflow_failure_is_a_retryable_turn_failure(env):
    # The turn workflow itself fails (its execution timeout) while its decision hangs: no stop reason and no failure
    # object come back from it, so the coordinator classifies it as retryable and goes through the same retry path.
    fake = Scripted({1: "hang"}, stop_after=2)
    async with running(env, fake, turn_timeout_seconds=4) as (h, _):
        result = await asyncio.wait_for(h.result(), 60)
    assert result["status"] == "finished" and fake.started == [1, 2]
    (retry,) = fake.retries
    assert retry["attempt"] == 1 and retry["max_attempts"] == 2 and retry["turn_counter"] == 1
    assert retry["failure"]["error_type"] == "turn-workflow-failed" and retry["failure"]["retryable"] is True
    assert retry["failure"]["message"]
    assert retry["failed_turn_id"] is None  # the turn workflow returned nothing, so no turn id is known
    assert fake.turn_failed_marks == []
    assert fake.completed_turn(2)["stop_reason"] == "waiting"


# --- turn_failure(): classification of an activity failure, without a server ----------------------------------------


def activity_error(cause: BaseException | None, retry_state: RetryState | None, activity_type: str = "decide"):
    exc = ActivityError("Activity task failed", scheduled_event_id=5, started_event_id=6, identity="worker@test",
                        activity_type=activity_type, activity_id="1", retry_state=retry_state)
    exc.__cause__ = cause  # what temporalio sets when it converts the failure proto
    return exc


def test_turn_failure_non_retryable_failure_state():
    exc = activity_error(ApplicationError("invalid model spec", type="MODEL_SPEC_INVALID", non_retryable=True),
                         RetryState.NON_RETRYABLE_FAILURE)
    f = turn_failure(exc)
    assert f == {"activity": "decide", "error_type": "MODEL_SPEC_INVALID", "retryable": False,
                 "retry_state": "NON_RETRYABLE_FAILURE", "message": "invalid model spec"}
    json.dumps(f)  # it travels in activity inputs and workflow results


def test_turn_failure_maximum_attempts_reached_is_retryable():
    exc = activity_error(ApplicationError("upstream 503", type="MODEL_CALL_FAILED"),
                         RetryState.MAXIMUM_ATTEMPTS_REACHED)
    f = turn_failure(exc)
    assert f == {"activity": "decide", "error_type": "MODEL_CALL_FAILED", "retryable": True,
                 "retry_state": "MAXIMUM_ATTEMPTS_REACHED", "message": "upstream 503"}


def test_turn_failure_timeout_is_retryable():
    cause = TemporalTimeout("activity StartToClose timeout", type=TimeoutType.START_TO_CLOSE,
                            last_heartbeat_details=[])
    f = turn_failure(activity_error(cause, RetryState.TIMEOUT, activity_type="execute_decision"))
    assert f["activity"] == "execute_decision" and f["error_type"] == "TimeoutError"
    assert f["retryable"] is True and f["retry_state"] == "TIMEOUT"
    assert f["message"] == "activity StartToClose timeout"


@pytest.mark.parametrize("state", [None, RetryState.MAXIMUM_ATTEMPTS_REACHED, RetryState.RETRY_POLICY_NOT_SET])
def test_turn_failure_application_error_marked_non_retryable_is_not_retryable_whatever_the_state(state):
    exc = activity_error(ApplicationError("payload refused", type="PAYLOAD_TOO_LARGE", non_retryable=True), state)
    f = turn_failure(exc)
    assert f["retryable"] is False and f["error_type"] == "PAYLOAD_TOO_LARGE"
    assert f["retry_state"] == (state.name if state is not None else None)


def test_turn_failure_without_a_typed_cause_or_any_cause():
    untyped = turn_failure(activity_error(ApplicationError("boom"), RetryState.MAXIMUM_ATTEMPTS_REACHED))
    assert untyped["error_type"] == "ApplicationError" and untyped["retryable"] is True
    bare = turn_failure(activity_error(None, None))
    assert bare["error_type"] == "ActivityError" and bare["retryable"] is True and bare["retry_state"] is None
    assert bare["message"] == "Activity task failed"


def test_turn_failure_message_is_bounded():
    exc = activity_error(ApplicationError("x" * 5000, type="MODEL_CALL_FAILED"), RetryState.MAXIMUM_ATTEMPTS_REACHED)
    assert len(turn_failure(exc)["message"]) == 300
