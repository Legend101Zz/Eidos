"""ADR 0022 on a Temporal development server (dev server is for tests only): ``TurnWorkflow`` and a dependent batch that is
still waiting for its own jobs.

``execute_decision`` returns ``{"pending": True, "next_call": i, "continuation": n}`` when its slice ended before the jobs
its later calls depend on had finished (committed calls replay by command key). The workflow (patch
``dependent-batch-v1``) then waits a durable timer and runs the activity again for the same decision with
``continuation`` set, until it returns a summary. Near the turn's own execution timeout it sends ``final: True`` instead of
waiting, so the activity records what could not run (``BATCH_DEADLINE``) and the turn can still be completed.

Activities are in-test fakes with the same names as the real ones, so the tests see only the workflow's behaviour: how the
activity is called, and what the turn does with its eventual result (see tests/unit/test_dependent_batches.py for the
activity itself). The shape of the fixture follows test_workflows.py and test_f5d_refused_execution.py: a ``temporal`` CLI
is needed (``EIDOS_TEMPORAL_CLI``, default ``/opt/temporal/temporal``) and the ``env`` fixture skips when it is missing."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, TurnWorkflow

pytestmark = pytest.mark.temporal
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")

CONCLUDE = {"action": "conclude", "summary": "done", "scope": {"m": 1}}
BASE = {"turn_id": "t1", "project_id": "p", "investigation_id": "i", "decision_id": "t1-d0"}
DONE = [{"tool": "author_source", "ok": True, "replayed": True, "code": None},
        {"tool": "run_component", "ok": True, "replayed": True, "code": None},
        {"tool": "run_component", "ok": True, "replayed": False, "code": None}]
STOPPED = [{"tool": "author_source", "ok": True, "replayed": True, "code": None},
           {"tool": "run_component", "ok": False, "replayed": None, "code": "EFFECT_NOT_ADMITTED"}]


@pytest.fixture(scope="module")
async def env():
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    e = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield e
    await e.shutdown()


class FakeBatchLab:
    """One turn, one decision of three calls (source, train, score) with the control ``control``.

    ``execute_decision`` answers ``pending`` for its first ``pending_slices`` invocations (continuation 1, 2, ...), then
    ``final`` (the summary of the finished batch). Every invocation's arguments are kept in ``executions``."""

    def __init__(self, *, pending_slices: int = 1, final: dict | None = None, control: dict | None = None):
        self.pending_slices = pending_slices
        self.final = final or {"summary": DONE}
        self.control = CONCLUDE if control is None else control
        self.executions: list[dict] = []
        self.decided: list[int] = []
        self.completed: list[dict] = []

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        return {"started": True, "turn_id": "t1", "max_model_calls": 2}

    @activity.defn(name="build_context")
    async def build_context(self, a: dict) -> dict:
        return {"context_artifact_id": "ctx"}

    @activity.defn(name="decide")
    async def decide(self, a: dict) -> dict:
        self.decided.append(int(a["slot"]))
        if a["slot"] == 0:
            return {"accepted": True, "decision_id": "t1-d0", "n_calls": 3, "control": self.control}
        return {"accepted": True, "decision_id": f"t1-d{a['slot']}", "n_calls": 0, "control": {}}

    @activity.defn(name="execute_decision")
    async def execute_decision(self, a: dict) -> dict:
        self.executions.append(a)
        if len(self.executions) <= self.pending_slices:
            return {"pending": True, "next_call": 2, "continuation": len(self.executions)}
        return self.final

    @activity.defn(name="complete_turn")
    async def complete_turn(self, a: dict) -> dict:
        self.completed.append(a)
        return {"status": "cancelled" if a["stop_reason"] == "cancelled" else "completed"}

    @activity.defn(name="record_turn_retry")
    async def record_turn_retry(self, a: dict) -> dict:
        return {}

    @activity.defn(name="check_news")
    async def check_news(self, a: dict) -> dict:
        return {"news": True}

    @activity.defn(name="mark_needs_input")
    async def mark_needs_input(self, a: dict) -> dict:
        return {"already": False}

    def acts(self):
        return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn,
                self.check_news, self.mark_needs_input, self.record_turn_retry]


async def run_turn(env, fake: FakeBatchLab, **options: Any) -> dict:
    """One TurnWorkflow, started directly the way the coordinator starts its child."""
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        return await env.client.execute_workflow(
            TurnWorkflow.run, {"investigation_id": "i", "project_id": "p", "index": 1},
            id=f"turn-batch-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE, **options)


async def test_a_pending_batch_is_executed_again_with_its_continuation_and_the_turn_finishes(env):
    fake = FakeBatchLab(pending_slices=1)
    r = await run_turn(env, fake)

    # the first execution has no continuation; the second carries the one the first returned, and is not final
    assert fake.executions == [BASE, {**BASE, "continuation": 1}]
    assert fake.decided == [0]  # the same decision both times, and no further model call
    # the turn finished normally: the decision's own control applies once its batch is done
    assert r == {"started": True, "turn_id": "t1", "stop_reason": "concluded", "control": CONCLUDE}
    assert len(fake.completed) == 1 and fake.completed[0]["stop_reason"] == "concluded"
    assert fake.completed[0]["control"] == CONCLUDE
    # the turn's record holds the finished batch's summary, not the pending slice
    assert fake.completed[0]["receipts"] == [{"decision_id": "t1-d0", "summary": DONE}]


async def test_a_batch_may_hand_the_wait_back_more_than_once(env):
    fake = FakeBatchLab(pending_slices=2)
    r = await run_turn(env, fake)
    assert fake.executions == [BASE, {**BASE, "continuation": 1}, {**BASE, "continuation": 2}]
    assert r["stop_reason"] == "concluded" and fake.completed[0]["receipts"] == [{"decision_id": "t1-d0", "summary": DONE}]


async def test_near_the_turns_timeout_the_continuation_is_told_it_is_final(env):
    """With less time left than the final margin, the workflow does not wait another slice: it runs the activity with
    ``final: True`` so that the calls that cannot start are recorded and the turn can still be completed."""
    fake = FakeBatchLab(pending_slices=1)
    r = await run_turn(env, fake, execution_timeout=timedelta(seconds=60))
    assert fake.executions == [BASE, {**BASE, "continuation": 1, "final": True}]
    assert r["stop_reason"] == "concluded" and fake.completed[0]["stop_reason"] == "concluded"


async def test_a_batch_stopped_after_waiting_cancels_the_turn_and_its_control_is_not_applied(env):
    fake = FakeBatchLab(pending_slices=1, final={"summary": STOPPED, "stopped": "investigation-stopped"})
    r = await run_turn(env, fake)
    assert len(fake.executions) == 2 and fake.executions[1]["continuation"] == 1
    assert r == {"started": True, "turn_id": "t1", "stop_reason": "cancelled", "control": {}}
    assert fake.completed[0]["stop_reason"] == "cancelled" and fake.completed[0]["control"] == {}
    assert fake.completed[0]["receipts"] == [{"decision_id": "t1-d0", "summary": STOPPED}]
    assert fake.decided == [0]  # no decision was requested after the stop


async def test_a_batch_that_is_never_pending_is_executed_once(env):
    fake = FakeBatchLab(pending_slices=0)
    r = await run_turn(env, fake)
    assert fake.executions == [BASE] and r["stop_reason"] == "concluded"
