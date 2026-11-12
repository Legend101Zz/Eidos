"""F5c (review R8 #3): the refused-proposal needs-input wait counts only operator events after the mark's cursor, on a
Temporal development server (dev server is for tests only).

After ``invalid_turns > MAX_INVALID_RETRY_TURNS`` the coordinator calls ``mark_needs_input`` and waits for operator
news. That activity returns the ``cursor`` of the mark. The wait used to discard it, so its operator-only ``check_news``
counted from the last turn's horizon, and an operator event between that horizon and the mark ended the wait at once.
Now the cursor is kept in the workflow input (``operator_cursor``), passed as ``after`` to every operator-only check of
the wait, carried through Continue-As-New, and used by the post-rollover wait (``needs-input-survives-rollover-v1``).

Activities are in-test fakes with the same names as the real ones; no model is called. The fake ledger is a list of
operator event seqs and the fake ``check_news`` reports news only for events past ``max(horizon, after)``, which is the
real activity's rule (see test_f5_failed_turn_cursor.py for the real activity against PostgreSQL)."""

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
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, InvestigationWorkflow

temporal = pytest.mark.temporal
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")
REFUSED_STATUS = "needs operator input (repeated refused proposals)"

HORIZON = 10  # the last turn's horizon: the old wait counted operator events after this
STALE_EVENT = 40  # an operator event after the horizon but before the mark
CURSOR = 50  # the seq of the first needs-input mark's own event
LATER_EVENT = 60  # an operator event after the first mark
SECOND_CURSOR = 70  # the seq of the second needs-input mark (past the later event)


@pytest.fixture(scope="module")
async def env():
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    e = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield e
    await e.shutdown()


class Refusing:
    """Scripted investigator activities whose every decision is refused, so each turn workflow ends on
    ``invalid-proposals`` (two refused slots) and the coordinator retries twice, then marks the investigation
    needs-input (``refused_turns`` 3) and waits for operator news.

    ``mark_needs_input`` returns the next cursor of ``cursors`` (the last one repeats) and records its input and the
    run it ran in. ``check_news`` records each call (input, run id, number of marks made before it) and, for an
    operator-only call, reports news only if an event of ``operator_events`` has a seq past ``max(horizon, after)``;
    any other call reports news."""

    def __init__(self, cursors: tuple[int, ...] = (CURSOR, SECOND_CURSOR), operator_events: tuple[int, ...] = (STALE_EVENT,)):
        self.cursors = list(cursors)
        self.operator_events = list(operator_events)
        self.horizon = HORIZON
        self.begun: list[int] = []
        self.completed: list[dict] = []
        self.marks: list[dict] = []
        self.mark_runs: list[str] = []
        self.news_calls: list[dict] = []

    def waits(self, n: int) -> list[dict]:
        """The check_news calls made while the n-th mark was the latest one (1-based): the n-th wait's checks."""
        return [c for c in self.news_calls if c["marks"] == n]

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        self.begun.append(a["index"])
        return {"started": True, "turn_id": f"t{a['index']}", "max_model_calls": 2}

    @activity.defn(name="build_context")
    async def build_context(self, a: dict) -> dict:
        return {"context_artifact_id": "ctx"}

    @activity.defn(name="decide")
    async def decide(self, a: dict) -> dict:
        return {"accepted": False, "reason": "invalid", "decision_id": f"{a['turn_id']}-d{a['slot']}"}

    @activity.defn(name="execute_decision")
    async def execute_decision(self, a: dict) -> dict:
        return {"summary": []}

    @activity.defn(name="complete_turn")
    async def complete_turn(self, a: dict) -> dict:
        self.completed.append(a)
        return {"status": "completed"}

    @activity.defn(name="enter_finalization")
    async def enter_finalization(self, a: dict) -> dict:
        return {"enabled": False}  # these regressions exercise the operator finalization policy

    @activity.defn(name="reconcile_finalization")
    async def reconcile_finalization(self, a: dict) -> dict:
        return {"finalization": False}  # these fixtures have no autonomous recovery admission

    @activity.defn(name="record_turn_retry")
    async def record_turn_retry(self, a: dict) -> dict:
        return {}

    @activity.defn(name="check_news")
    async def check_news(self, a: dict) -> dict:
        self.news_calls.append({"input": dict(a), "run": activity.info().workflow_run_id, "marks": len(self.marks)})
        if not a.get("operator_only"):
            return {"news": True}
        floor = max(self.horizon, int(a.get("after") or 0))
        return {"news": any(seq > floor for seq in self.operator_events)}

    @activity.defn(name="mark_needs_input")
    async def mark_needs_input(self, a: dict) -> dict:
        self.marks.append(a)  # no await between the two appends: a reader sees both or neither
        self.mark_runs.append(activity.info().workflow_run_id)
        return {"already": False, "cursor": self.cursors[min(len(self.marks), len(self.cursors)) - 1]}

    def acts(self):
        return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn, self.enter_finalization,
                self.reconcile_finalization, self.check_news, self.mark_needs_input, self.record_turn_retry]


async def until(predicate, timeout: float = 30.0, what: str = "condition", step: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@asynccontextmanager
async def running(env, fake: Refusing, **args):
    """The workflow and investigator workers plus a started coordinator; yields (handle, workflow id)."""
    wid = f"inv-f5c-{uuid.uuid4().hex[:6]}"
    full = {"investigation_id": wid, "project_id": "p", "idle_poll_seconds": 1, **args}
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


def assert_after(calls: list[dict], cursor: int, wid: str) -> None:
    """Every check of a refused-proposal wait is operator-only and counts from the wait's own cursor."""
    assert calls, "no operator check ran"
    for c in calls:
        assert c["input"].get("operator_only") is True, c
        assert c["input"].get("after") == cursor, c
        assert c["input"]["investigation_id"] == wid and c["input"]["project_id"] == "p", c


@temporal
async def test_an_operator_event_behind_the_mark_does_not_end_the_refused_proposal_wait(env):
    fake = Refusing()
    async with running(env, fake) as (h, wid):
        await until(lambda: fake.marks, 30, "the refused-proposals needs-input mark")
        # The turn and its two bounded retries, all ended on refused proposals, then one mark.
        assert fake.begun == [1, 2, 3]
        assert {c["stop_reason"] for c in fake.completed} == {"invalid-proposals"} and len(fake.completed) == 3
        (mark,) = fake.marks
        assert mark["investigation_id"] == wid and mark["turn_counter"] == 3 and mark["refused_turns"] == 3
        # The operator event at seq 40 is after the last turn's horizon (10) and before the mark (cursor 50): the old
        # wait counted it from the horizon and ended at once. Quiet polls must go on, counting after the cursor.
        await until(lambda: len(fake.waits(1)) >= 3, 20, "quiet operator polls of the wait")
        assert fake.begun == [1, 2, 3] and len(fake.marks) == 1
        assert (await h.query("state"))["status"] == REFUSED_STATUS
        # A job or evidence wake is not operator news either: the check it triggers still sees nothing past the cursor.
        await h.signal("wake", 1)
        await asyncio.sleep(2)
        assert fake.begun == [1, 2, 3] and len(fake.marks) == 1
        assert_after(fake.waits(1), CURSOR, wid)
        # An operator event after the cursor ends the wait: the next turn starts.
        fake.operator_events.append(LATER_EVENT)
        await h.signal("wake", 1)  # ask for a check now instead of at the next poll
        await until(lambda: 4 in fake.begun, 20, "the turn after the operator event past the cursor")
        assert_after(fake.waits(1), CURSOR, wid)  # the check that saw seq 60 carried the cursor as well
        # That turn ends on refused proposals again: a second mark with its own, later cursor. The new wait counts after
        # it, so neither the old stale event (40) nor the one that ended the previous wait (60) ends it.
        await until(lambda: len(fake.marks) == 2, 30, "the second needs-input mark")
        await until(lambda: len(fake.waits(2)) >= 2, 20, "quiet operator polls of the second wait")
        assert_after(fake.waits(2), SECOND_CURSOR, wid)
        begun = list(fake.begun)
        await asyncio.sleep(2)
        assert fake.begun == begun and len(fake.marks) == 2
        assert (await h.query("state"))["status"] == REFUSED_STATUS


@temporal
async def test_the_refused_proposal_cursor_survives_continue_as_new_during_the_wait(env):
    fake = Refusing()
    async with running(env, fake, history_limit=80) as (h, wid):
        await until(lambda: fake.marks, 30, "the refused-proposals needs-input mark")
        mark_run = fake.mark_runs[0]

        def after_rollover() -> list[dict]:
            return [c for c in fake.waits(1) if c["run"] != mark_run]

        # Quiet polls grow the history past the limit: the run that made the mark continues as new, and the wait goes on
        # in the next run. The operator event at seq 40 (behind the mark's cursor 50) still does not end it.
        await until(lambda: len(after_rollover()) >= 2, 60, "operator checks of the wait after the rollover")
        assert fake.begun == [1, 2, 3] and len(fake.marks) == 1
        # The rollover carried the cursor (and the refused-turn count that makes the next run keep waiting).
        old = await env.client.get_workflow_handle(wid, run_id=mark_run).fetch_history()
        last = old.events[-1]
        assert last.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
        carried = json.loads(last.workflow_execution_continued_as_new_event_attributes.input.payloads[0].data.decode())
        assert carried["operator_cursor"] == CURSOR and carried["invalid_turns"] == 3, carried
        assert carried["turn_counter"] == 3 and carried["rollovers"] >= 1, carried
        # The new run says it is waiting for the operator, and its checks carry the cursor like the first run's.
        latest = env.client.get_workflow_handle(wid)
        status = ""
        for _ in range(50):
            status = (await latest.query("state"))["status"]
            if status == REFUSED_STATUS:
                break
            await asyncio.sleep(0.2)
        assert status == REFUSED_STATUS, status
        assert len({c["run"] for c in fake.waits(1)}) >= 2
        assert_after(fake.waits(1), CURSOR, wid)  # before and after the rollover
        # Wakes are not operator news: no turn starts however many arrive.
        for _ in range(3):
            await h.signal("wake", 1)
            await asyncio.sleep(0.3)
        await asyncio.sleep(2)
        assert fake.begun == [1, 2, 3] and len(fake.marks) == 1
        assert_after(fake.waits(1), CURSOR, wid)
        # An operator event after the cursor ends the wait of the new run, and the next turns start.
        fake.operator_events.append(LATER_EVENT)
        await h.signal("wake", 1)
        await until(lambda: 4 in fake.begun, 20, "the turn after the operator event past the cursor")
        assert_after(fake.waits(1), CURSOR, wid)
        # The cursor was dropped with that wait: the next needs-input mark brings its own, and the wait counts after it.
        await until(lambda: len(fake.marks) == 2, 30, "the second needs-input mark")
        await until(lambda: len(fake.waits(2)) >= 2, 20, "quiet operator polls of the second wait")
        assert_after(fake.waits(2), SECOND_CURSOR, wid)
        begun = list(fake.begun)
        await asyncio.sleep(2)
        assert fake.begun == begun and len(fake.marks) == 2
