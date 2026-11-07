"""Workflow behaviour on a Temporal development server (dev server is for tests only; the shipped
runtime is the regular server). Activities are replaced by in-test fakes with the same names."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from eidos.orchestration.names import ASSESSOR_QUEUE, INVESTIGATOR_QUEUE, RUNNER_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, InvestigationWorkflow, JobWorkflow, TurnWorkflow

pytestmark = pytest.mark.temporal
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")
HISTORIES = Path(__file__).parent.parent / "fixtures" / "histories"


@pytest.fixture(scope="module")
async def env():
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    e = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield e
    await e.shutdown()


class FakeInvestigator:
    """Scripted activities: each turn makes one decision with one call, then 'waits' every 3rd turn."""

    def __init__(self, stop_after: int = 1000, fail_payload: bool = False):
        self.turns = 0
        self.executed: list[str] = []
        self.completed: list[dict] = []
        self.stop_after = stop_after
        self.fail_payload = fail_payload

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        if self.turns >= self.stop_after:
            return {"started": False, "reason": "max-turns-reached"}
        self.turns += 1
        return {"started": True, "turn_id": f"t{a['index']}", "max_model_calls": 2}

    @activity.defn(name="build_context")
    async def build_context(self, a: dict) -> dict:
        return {"context_artifact_id": "ctx"}

    @activity.defn(name="decide")
    async def decide(self, a: dict) -> dict:
        if self.fail_payload:
            raise ApplicationError("too big", type="PAYLOAD_TOO_LARGE", non_retryable=True)
        return {"accepted": True, "decision_id": f"{a['turn_id']}-d{a['slot']}", "n_calls": 1,
                "control": {"action": "wait_for_jobs"} if a["slot"] == 1 else {}}

    @activity.defn(name="execute_decision")
    async def execute_decision(self, a: dict) -> dict:
        self.executed.append(a["decision_id"])
        return {"summary": [{"tool": "record_note", "ok": True}]}

    @activity.defn(name="complete_turn")
    async def complete_turn(self, a: dict) -> dict:
        self.completed.append(a)
        return {"status": "completed"}

    def acts(self):
        return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn]


async def test_coordinator_rolls_over_and_carries_counters(env):
    fake = FakeInvestigator(stop_after=7)
    wid = f"inv-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        h = await env.client.start_workflow(
            InvestigationWorkflow.run, {"investigation_id": wid, "project_id": "p", "turns_per_run": 3,
                                        "idle_poll_seconds": 1, "history_limit": 10_000},
            id=wid, task_queue=WORKFLOW_QUEUE)
        # Every turn waits for jobs; wake it repeatedly so it keeps going.
        import asyncio
        for _ in range(40):
            await asyncio.sleep(0.3)
            try:
                await h.signal("wake", 1)
            except Exception:
                break
        result = await h.result()
    assert result["status"] == "finished" and result["reason"] == "max-turns-reached"
    assert result["turn_counter"] == 8  # 7 turns + the refused 8th, numbered across rollovers
    assert fake.turns == 7 and all(c["stop_reason"] == "waiting" for c in fake.completed)
    # Continue-As-New happened (3 turns per run): the first run's history ended in continue-as-new.
    first = env.client.get_workflow_handle(wid, run_id=h.first_execution_run_id)
    hist = await first.fetch_history()
    from temporalio.api.enums.v1 import EventType
    assert hist.events[-1].event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
    # Record once; later code versions must keep replaying this frozen history (upgrade guard).
    HISTORIES.mkdir(parents=True, exist_ok=True)
    frozen = HISTORIES / "investigation_v1_rollover.json"
    if not frozen.exists():
        frozen.write_text(hist.to_json())


async def test_turn_with_non_retryable_payload_error_closes_turn(env):
    fake = FakeInvestigator(fail_payload=True)
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        r = await env.client.execute_workflow(TurnWorkflow.run, {"investigation_id": "i", "project_id": "p", "index": 1},
                                              id=f"turn-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE)
    assert r["stop_reason"] == "error"
    assert fake.completed and fake.completed[0]["stop_reason"] == "error"  # turn record always closed


async def test_job_workflow_cancel_signal_and_retry_exhaustion(env):
    calls = {"n": 0, "failed": 0}

    @activity.defn(name="run_attempt")
    async def run_attempt(a: dict) -> dict:
        calls["n"] += 1
        if a.get("mode") == "flaky":
            raise ApplicationError("transient", type="RetryableAttempt")
        import asyncio
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.2)

    @activity.defn(name="fail_job")
    async def fail_job(a: dict) -> dict:
        calls["failed"] += 1
        return {"job_status": "failed"}

    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=RUNNER_QUEUE, activities=[run_attempt, fail_job]):
        flaky = await env.client.execute_workflow(JobWorkflow.run, {"job_id": "j1", "project_id": "p", "queue": RUNNER_QUEUE,
                                                                     "mode": "flaky", "max_attempts": 3},
                                                  id=f"job-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE)
        assert flaky["job_status"] == "failed-after-retries" and calls["n"] == 3 and calls["failed"] == 1
        h = await env.client.start_workflow(JobWorkflow.run, {"job_id": "j2", "project_id": "p", "queue": RUNNER_QUEUE,
                                                              "heartbeat_timeout": 5},
                                            id=f"job-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE)
        import asyncio
        await asyncio.sleep(1.0)
        await h.signal("cancel", "cancel")
        res = await h.result()
        assert res["job_status"] == "failed-after-retries"  # cancelled activity surfaces as failure; fail_job reconciles


async def test_recorded_histories_replay_on_current_code():
    """Upgrade guard: every recorded history must replay deterministically on today's workflows."""
    files = sorted(HISTORIES.glob("*.json"))
    if not files:
        pytest.skip("no recorded histories yet")
    from temporalio.client import WorkflowHistory
    replayer = Replayer(workflows=ALL_WORKFLOWS)
    for f in files:
        await replayer.replay_workflow(WorkflowHistory.from_json(f.stem, json.loads(f.read_text())))
    _ = (ASSESSOR_QUEUE, WorkflowFailureError)
