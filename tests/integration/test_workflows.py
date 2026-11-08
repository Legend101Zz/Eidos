"""Workflow behaviour on a Temporal development server (dev server is for tests only; the shipped
runtime is the regular server). Activities are replaced by in-test fakes with the same names."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from temporalio import activity, workflow
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

    @activity.defn(name="check_news")
    async def check_news(self, a: dict) -> dict:
        return {"news": True}

    @activity.defn(name="mark_needs_input")
    async def mark_needs_input(self, a: dict) -> dict:
        self.marked = getattr(self, "marked", 0) + 1
        return {"already": False}

    def acts(self):
        return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn,
                self.check_news, self.mark_needs_input]


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


async def test_stress_many_turns_bounded_histories_and_oversized_results(env):
    """16 turns (EIDOS_STRESS_TURNS) with frequent rollover; every run's history stays below its limit; an activity
    result far above the workflow envelope is reduced by `bounded()` instead of failing tasks."""
    from eidos.investigator.activities import bounded

    class Big(FakeInvestigator):
        @activity.defn(name="decide")
        async def decide(self, a: dict) -> dict:
            huge = {"accepted": True, "decision_id": f"{a['turn_id']}-d{a['slot']}", "n_calls": 1,
                    "control": {"action": "wait_for_jobs"}, "reason": "x" * 3_000_000}
            return bounded(huge)

        def acts(self):
            return [self.begin_turn, self.build_context, self.decide, self.execute_decision, self.complete_turn,
                    self.check_news]

    fake = Big(stop_after=int(os.environ.get("EIDOS_STRESS_TURNS", "16")))
    wid = f"inv-stress-{uuid.uuid4().hex[:6]}"
    import asyncio
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        h = await env.client.start_workflow(
            InvestigationWorkflow.run, {"investigation_id": wid, "project_id": "p", "turns_per_run": 4,
                                        "idle_poll_seconds": 1, "history_limit": 400}, id=wid, task_queue=WORKFLOW_QUEUE)
        for _ in range(200):
            await asyncio.sleep(0.15)
            try:
                await h.signal("wake", 1)
            except Exception:
                break
        result = await h.result()
    assert result["status"] == "finished" and fake.turns == int(os.environ.get("EIDOS_STRESS_TURNS", "16"))
    # Walk the run chain and check each history stayed bounded.
    lengths, run_id = [], h.first_execution_run_id
    from temporalio.api.enums.v1 import EventType
    while run_id:
        hist = await env.client.get_workflow_handle(wid, run_id=run_id).fetch_history()
        lengths.append(len(hist.events))
        last = hist.events[-1]
        run_id = (last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                  if last.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW else None)
    # Each run may exceed the soft limit only by what one in-flight turn adds (signals during a child).
    assert len(lengths) >= fake.turns // 4 and max(lengths) < 400 + 120, lengths



@workflow.defn(name="TurnWorkflow", sandboxed=False)
class _TurnBeforeInvalidFeedback:
    """TurnWorkflow as it was before the invalid-proposal feedback change (review finding e/6)."""

    @workflow.run
    async def run(self, a):
        from datetime import timedelta

        from eidos.orchestration.workflows import MODEL, SHORT
        b = await workflow.execute_activity("begin_turn", a, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        base = {"turn_id": b["turn_id"], "project_id": a["project_id"], "investigation_id": a["investigation_id"]}
        stop = "calls-exhausted"
        try:
            await workflow.execute_activity("build_context", base, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
            for slot in range(int(b.get("max_model_calls", 4))):
                d = await workflow.execute_activity("decide", {**base, "slot": slot}, task_queue=INVESTIGATOR_QUEUE,
                                                    start_to_close_timeout=timedelta(seconds=60), retry_policy=MODEL)
                if not d.get("accepted"):
                    stop = "stale-revision" if d.get("reason") == "stale-revision" else "error"
                    break
        finally:
            await workflow.execute_activity("complete_turn", {**base, "stop_reason": stop}, task_queue=INVESTIGATOR_QUEUE,
                                            start_to_close_timeout=timedelta(seconds=60), retry_policy=SHORT)
        return {"started": True, "stop_reason": stop}


async def test_inflight_turn_with_invalid_proposal_replays_after_upgrade(env):
    """A history recorded by the pre-change TurnWorkflow (invalid proposal ends the turn) must replay on
    the current code (patched guard). The history is frozen as a fixture for future upgrades."""
    @activity.defn(name="begin_turn")
    async def begin_turn(a):
        return {"started": True, "turn_id": "t1", "max_model_calls": 3}

    @activity.defn(name="build_context")
    async def build_context(a):
        return {}

    @activity.defn(name="decide")
    async def decide(a):
        return {"accepted": False, "reason": "invalid", "decision_id": "d"}

    @activity.defn(name="complete_turn")
    async def complete_turn(a):
        return {"status": "completed"}

    frozen = HISTORIES / "turn_v1_invalid_proposal.json"
    if not frozen.exists():
        async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=[_TurnBeforeInvalidFeedback]), \
                Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=[begin_turn, build_context, decide, complete_turn]):
            h = await env.client.start_workflow("TurnWorkflow", {"investigation_id": "i", "project_id": "p", "index": 1},
                                                id=f"t-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE)
            assert (await h.result())["stop_reason"] == "error"
            frozen.write_text((await h.fetch_history()).to_json())
    from temporalio.client import WorkflowHistory
    await Replayer(workflows=ALL_WORKFLOWS).replay_workflow(
        WorkflowHistory.from_json("turn-invalid", json.loads(frozen.read_text())))
    # New behaviour: consecutive invalid proposals end the turn with a waiting stop reason.
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=[begin_turn, build_context, decide, complete_turn]):
        r = await env.client.execute_workflow(TurnWorkflow.run, {"investigation_id": "i", "project_id": "p", "index": 2},
                                              id=f"t-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE)
    assert r["stop_reason"] == "invalid-proposals"


async def test_idle_polls_without_news_spend_no_model_turns(env):
    import asyncio
    fake = FakeInvestigator(stop_after=3)
    checks = {"n": 0}

    @activity.defn(name="check_news")
    async def check_news(a: dict) -> dict:
        checks["n"] += 1
        return {"news": False}

    wid = f"inv-idle-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=[*fake.acts()[:-2], check_news, fake.mark_needs_input]):
        h = await env.client.start_workflow(InvestigationWorkflow.run, {"investigation_id": wid, "project_id": "p",
                                            "idle_poll_seconds": 1}, id=wid, task_queue=WORKFLOW_QUEUE)
        await asyncio.sleep(5)
        assert fake.turns == 1 and checks["n"] >= 2  # quiet polls checked the ledger, no new turn
        await h.signal("wake", 1)
        await asyncio.sleep(2)
        assert fake.turns == 2  # a wake starts a turn
        await h.terminate()


async def test_refused_proposal_turns_retry_boundedly_then_surface_for_input(env):
    """A turn ending on the invalid-proposal limit gets bounded immediate retry turns; after that the
    investigation waits for operator news only (job wakes and quiet polls do not start turns)."""
    import asyncio
    fake = FakeInvestigator()
    seen = {"operator_only": 0, "operator": False}

    @activity.defn(name="decide")
    async def decide(a: dict) -> dict:
        return {"accepted": False, "reason": "invalid", "decision_id": f"{a['turn_id']}-d{a['slot']}"}

    @activity.defn(name="check_news")
    async def check_news(a: dict) -> dict:
        if a.get("operator_only"):
            seen["operator_only"] += 1
            return {"news": seen["operator"]}
        return {"news": True}

    acts = [fake.begin_turn, fake.build_context, decide, fake.execute_decision, fake.complete_turn, check_news,
            fake.mark_needs_input]
    wid = f"inv-invalid-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=acts):
        h = await env.client.start_workflow(InvestigationWorkflow.run, {"investigation_id": wid, "project_id": "p",
                                            "idle_poll_seconds": 1}, id=wid, task_queue=WORKFLOW_QUEUE)
        await asyncio.sleep(6)
        assert fake.turns == 3, fake.turns  # the turn plus two bounded retries
        assert all(c["stop_reason"] == "invalid-proposals" for c in fake.completed)
        state = await h.query("state")
        assert "needs operator input" in state["status"], state
        assert fake.marked == 1  # surfaced durably (ledger label + event) exactly once
        await h.signal("wake", 1)  # a job/evidence wake does not restart the loop
        await asyncio.sleep(3)
        assert fake.turns == 3 and seen["operator_only"] >= 2
        seen["operator"] = True  # e.g. the operator steered
        await asyncio.sleep(3)
        assert fake.turns >= 4
        await h.terminate()


async def test_needs_input_survives_continue_as_new(env):
    """Review 3 finding 2: quiet polls during a needs-input wait grow history; the rollover must keep waiting
    for operator news instead of starting fresh turns."""
    import asyncio
    fake = FakeInvestigator()
    seen = {"operator": False}

    @activity.defn(name="decide")
    async def decide(a: dict) -> dict:
        return {"accepted": False, "reason": "invalid", "decision_id": f"{a['turn_id']}-d{a['slot']}"}

    @activity.defn(name="check_news")
    async def check_news(a: dict) -> dict:
        return {"news": seen["operator"]} if a.get("operator_only") else {"news": True}

    acts = [fake.begin_turn, fake.build_context, decide, fake.execute_decision, fake.complete_turn, check_news,
            fake.mark_needs_input]
    wid = f"inv-rollover-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=acts):
        h = await env.client.start_workflow(InvestigationWorkflow.run, {"investigation_id": wid, "project_id": "p",
                                            "idle_poll_seconds": 1, "history_limit": 80}, id=wid, task_queue=WORKFLOW_QUEUE)
        await asyncio.sleep(25)  # several rollovers' worth of quiet polls
        assert fake.turns == 3, fake.turns
        seen["operator"] = True
        await asyncio.sleep(4)
        assert fake.turns >= 4
        await h.terminate()


async def test_wait_after_failed_calls_is_not_a_park(env):
    """Mac request 8: accepted wait decisions whose calls left nothing outstanding end the turn like refused
    proposals (bounded retries, then needs operator input) instead of waiting for news that never comes."""
    import asyncio
    fake = FakeInvestigator()

    @activity.defn(name="decide")
    async def decide(a: dict) -> dict:
        return {"accepted": True, "decision_id": f"{a['turn_id']}-d{a['slot']}", "n_calls": 1,
                "control": {"action": "wait_for_jobs"}}

    @activity.defn(name="execute_decision")
    async def execute_decision(a: dict) -> dict:
        return {"summary": [{"tool": "launch_trial", "ok": False}], "nothing_outstanding": True,
                "failed_calls": [{"tool": "launch_trial", "code": "NOT_FOUND"}]}

    @activity.defn(name="check_news")
    async def check_news(a: dict) -> dict:
        return {"news": False}

    acts = [fake.begin_turn, fake.build_context, decide, execute_decision, fake.complete_turn, check_news,
            fake.mark_needs_input]
    wid = f"inv-failwait-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=acts):
        h = await env.client.start_workflow(InvestigationWorkflow.run, {"investigation_id": wid, "project_id": "p",
                                            "idle_poll_seconds": 1}, id=wid, task_queue=WORKFLOW_QUEUE)
        await asyncio.sleep(6)
        assert fake.turns == 3 and all(c["stop_reason"] == "invalid-proposals" for c in fake.completed), fake.completed
        assert "needs operator input" in (await h.query("state"))["status"]
        await h.terminate()
