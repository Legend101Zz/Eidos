"""F5d (review R9): a refused execution cancels the turn, and a conclusion the lab refused is a cancelled turn, on a
Temporal development server (dev server is for tests only). The lab side of the same finding (``complete_turn`` never
overwrites a stop or pause with ``concluded``) is tested against PostgreSQL in test_f5d_conclude_race.py.

``TurnWorkflow`` (patch ``refused-execution-cancels-v1``):

* ``execute_decision`` returns ``stopped`` (an effect was refused by admission: stop, pause or turn closure after the
  handoff) or ``skipped`` (nothing was handed off: stop, pause, closed turn, revoked decision, a run that lost the turn).
  The turn stops as ``cancelled`` at once and the decision's control (conclude, stop, wait, ...) is never acted on:
  ``complete_turn`` gets stop reason ``cancelled`` and no control, and no further decision is requested.
* If the turn stopped as ``concluded`` but the ``complete_turn`` activity reports ``{"status": "cancelled"}`` (the
  investigation was stopped or paused after the decision's effects ran; the lab refused the conclusion and closed the
  turn as cancelled), the workflow's own result is ``cancelled`` too. Otherwise the coordinator would take the result
  for a conclusion and finish.

Activities are in-test fakes with the same names as the real ones; no model is called. The fake lab behind
``complete_turn`` follows the real handler: it closes a turn once (a redelivered completion returns the first result,
and changes nothing), a ``cancelled`` stop reason closes the turn as cancelled, and a conclude/stop control on an
investigation that is not active is refused (the turn closes as cancelled). The tests assert on what the lab persisted
per turn and on the workflow's result, not on how often an activity ran: Temporal may deliver an activity again."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, InvestigationWorkflow, TurnWorkflow

temporal = pytest.mark.temporal
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")

CONCLUDE = {"action": "conclude", "summary": "done", "scope": {"m": 1}}
STOP = {"action": "stop", "summary": "enough"}
WAIT = {"action": "wait_for_jobs"}
REQUEST_INPUT = {"action": "request_input"}
OK = [{"tool": "file_claim", "ok": True, "replayed": False, "code": None}]  # the summary of an executed decision
REFUSED_EFFECT = [{"tool": "file_claim", "ok": True, "replayed": False, "code": None},
                  {"tool": "file_claim", "ok": False, "replayed": None, "code": "EFFECT_NOT_ADMITTED"}]


@pytest.fixture(scope="module")
async def env():
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    e = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield e
    await e.shutdown()


def decision(slot: int, *, control: dict | None, n_calls: int = 1) -> dict:
    return {"accepted": True, "decision_id": f"t1-d{slot}", "n_calls": n_calls, "control": control or {}}


class FakeLab:
    """Scripted investigator activities over a fake lab.

    ``decisions`` maps a slot to the decide result (an unscripted slot decides a decision with no calls, so a turn that
    goes on to it ends ``waiting``, which no test expects). ``executions`` maps a decision id to the execute_decision
    result (default: executed, all effects succeeded). ``investigation_status`` is the status the investigation has when
    ``complete_turn`` runs: anything but ``active`` makes the lab refuse a conclude/stop control, like the real handler.
    The fake is idempotent: a turn begins once per index, and its completion is persisted once, in ``closed`` (the first
    ``complete_turn`` per turn wins)."""

    def __init__(self, decisions: dict[int, dict], executions: dict[str, dict] | None = None,
                 investigation_status: str = "active"):
        self.decisions = dict(decisions)
        self.executions = dict(executions or {})
        self.investigation_status = investigation_status
        self.begun: dict[int, dict] = {}  # begin_turn results as persisted, per turn index
        self.decided: set[int] = set()  # the slots a decision was requested for
        self.executed: set[str] = set()  # the decisions execute_decision ran for
        self.completed: list[dict] = []  # every complete_turn invocation
        self.closed: dict[str, dict] = {}  # the turn completions as the lab persists them: the first one per turn

    def closing(self, turn_id: str = "t1") -> dict:
        return self.closed[turn_id]

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        if a["index"] not in self.begun:
            self.begun[a["index"]] = {"started": True, "turn_id": f"t{a['index']}", "max_model_calls": 2}
        return self.begun[a["index"]]

    @activity.defn(name="build_context")
    async def build_context(self, a: dict) -> dict:
        return {"context_artifact_id": "ctx"}

    @activity.defn(name="decide")
    async def decide(self, a: dict) -> dict:
        self.decided.add(int(a["slot"]))
        return self.decisions.get(int(a["slot"])) or decision(int(a["slot"]), control=None, n_calls=0)

    @activity.defn(name="execute_decision")
    async def execute_decision(self, a: dict) -> dict:
        self.executed.add(a["decision_id"])
        return self.executions.get(a["decision_id"]) or {"summary": OK}

    @activity.defn(name="complete_turn")
    async def complete_turn(self, a: dict) -> dict:
        self.completed.append(a)
        done = self.closed.get(a["turn_id"])
        if done is not None:  # already closed: the first completion stands and a redelivery gets its result
            return {"status": done["status"]}
        reason, refused = a["stop_reason"], None
        if (a.get("control") or {}).get("action") in ("conclude", "stop") and self.investigation_status != "active":
            refused = f"investigation-{self.investigation_status}"
            if reason == "concluded":
                reason = "cancelled"
        status = "cancelled" if reason == "cancelled" else ("failed" if reason == "error" else "completed")
        self.closed[a["turn_id"]] = {"stop_reason": reason, "status": status, "control": a.get("control"),
                                     "sent_stop_reason": a["stop_reason"], "receipts": a.get("receipts"),
                                     **({"control_refused": refused} if refused else {})}
        return {"status": status}

    @activity.defn(name="reconcile_finalization")
    async def reconcile_finalization(self, a: dict) -> dict:
        return {"finalization": False}

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
                self.reconcile_finalization, self.check_news, self.mark_needs_input, self.record_turn_retry]


async def run_turn(env, fake: FakeLab) -> dict:
    """One TurnWorkflow, started directly the way the coordinator starts its child."""
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        return await env.client.execute_workflow(
            TurnWorkflow.run, {"investigation_id": "i", "project_id": "p", "index": 1},
            id=f"turn-f5d-{uuid.uuid4().hex[:6]}", task_queue=WORKFLOW_QUEUE, execution_timeout=timedelta(seconds=90))


# =====================================================================================================================
# 1-2. execute_decision refused (skipped or stopped): the turn is cancelled and the decision's control is not acted on
# =====================================================================================================================
REFUSED_EXECUTIONS = [
    pytest.param({"summary": [], "skipped": "investigation-stopped"}, id="skipped-investigation-stopped"),
    pytest.param({"summary": [], "skipped": "investigation-paused"}, id="skipped-investigation-paused"),
    pytest.param({"summary": [], "skipped": "turn-cancelled"}, id="skipped-turn-closed"),
    pytest.param({"summary": [], "skipped": "decision-revoked"}, id="skipped-decision-revoked"),
    pytest.param({"summary": [], "skipped": "superseded-execution"}, id="skipped-run-lost-the-turn"),
    pytest.param({"summary": REFUSED_EFFECT, "stopped": "investigation-stopped"}, id="stopped-investigation-stopped"),
    pytest.param({"summary": REFUSED_EFFECT, "stopped": "investigation-paused"}, id="stopped-investigation-paused"),
]


def assert_cancelled_without_control(fake: FakeLab, result: dict, execution: dict, slots: set[int]) -> None:
    assert result == {"started": True, "turn_id": "t1", "stop_reason": "cancelled", "control": {}}
    done = fake.closing()
    assert done["sent_stop_reason"] == "cancelled" and done["stop_reason"] == "cancelled"
    assert done["status"] == "cancelled" and done["control"] == {}  # the decision's control never reached the lab
    assert "control_refused" not in done
    assert done["receipts"][-1]["summary"] == execution["summary"]  # what execution did before it was refused
    assert fake.decided == slots  # and no decision was requested after the refused one


@temporal
@pytest.mark.parametrize("execution", REFUSED_EXECUTIONS)
async def test_refused_execution_of_a_conclude_decision_cancels_the_turn(env, execution):
    fake = FakeLab({0: decision(0, control=CONCLUDE)}, executions={"t1-d0": execution})
    result = await run_turn(env, fake)
    assert_cancelled_without_control(fake, result, execution, {0})
    assert fake.executed == {"t1-d0"}


@temporal
@pytest.mark.parametrize("control", [STOP, None, WAIT, REQUEST_INPUT],
                         ids=["stop", "no-control", "wait-for-jobs", "request-input"])
async def test_refused_execution_cancels_the_turn_whatever_the_decisions_control(env, control):
    """Not only a conclusion: a decision with no control would have gone on to the next slot (and a wait or request for
    input would have parked the investigation) although the stop already refused its effects."""
    execution = {"summary": [], "skipped": "investigation-stopped"}
    fake = FakeLab({0: decision(0, control=control)}, executions={"t1-d0": execution})
    result = await run_turn(env, fake)
    assert_cancelled_without_control(fake, result, execution, {0})


@temporal
async def test_refused_execution_in_a_later_slot_cancels_the_turn_there(env):
    """The stop arrives between two decisions of one turn: the first executed normally, the second one is refused."""
    refused = {"summary": REFUSED_EFFECT, "stopped": "investigation-paused"}
    fake = FakeLab({0: decision(0, control=None), 1: decision(1, control=CONCLUDE)}, executions={"t1-d1": refused})
    result = await run_turn(env, fake)
    assert result == {"started": True, "turn_id": "t1", "stop_reason": "cancelled", "control": {}}
    done = fake.closing()
    assert done["sent_stop_reason"] == "cancelled" and done["status"] == "cancelled" and done["control"] == {}
    assert fake.decided == {0, 1} and fake.executed == {"t1-d0", "t1-d1"}
    assert [r["decision_id"] for r in done["receipts"]] == ["t1-d0", "t1-d1"]
    assert done["receipts"][0]["summary"] == OK and done["receipts"][1]["summary"] == REFUSED_EFFECT


# =====================================================================================================================
# 3. the lab refused the conclusion at completion: the workflow's result is cancelled, not concluded
# =====================================================================================================================
@temporal
@pytest.mark.parametrize("control", [CONCLUDE, STOP], ids=["conclude", "stop"])
@pytest.mark.parametrize("status", ["stopped", "paused"])
async def test_conclusion_refused_by_the_lab_after_execution_is_a_cancelled_turn(env, status, control):
    """The decision's effects ran; the operator stopped or paused before ``complete_turn``. The workflow sent
    ``concluded`` with the control, the lab closed the turn as cancelled, and the workflow reports ``cancelled``."""
    fake = FakeLab({0: decision(0, control=control)}, investigation_status=status)
    result = await run_turn(env, fake)
    assert fake.executed == {"t1-d0"}
    done = fake.closing()
    assert done["sent_stop_reason"] == "concluded" and done["control"] == control  # what the workflow sent
    assert done["stop_reason"] == "cancelled" and done["status"] == "cancelled"  # what the lab made of it
    assert done["control_refused"] == f"investigation-{status}"
    assert result["started"] is True and result["turn_id"] == "t1"
    assert result["stop_reason"] == "cancelled"  # not "concluded": the coordinator must not take it for a conclusion


@temporal
@pytest.mark.parametrize("status", ["stopped", "paused"])
async def test_conclusion_of_a_decision_without_calls_refused_by_the_lab_is_a_cancelled_turn(env, status):
    """No call: nothing to execute, so no execution could be refused; the lab's refusal at completion is all there is."""
    fake = FakeLab({0: decision(0, control=CONCLUDE, n_calls=0)}, investigation_status=status)
    result = await run_turn(env, fake)
    assert fake.executed == set() and fake.decided == {0}
    done = fake.closing()
    assert done["sent_stop_reason"] == "concluded" and done["control"] == CONCLUDE
    assert done["stop_reason"] == "cancelled" and done["control_refused"] == f"investigation-{status}"
    assert result["stop_reason"] == "cancelled"


@temporal
async def test_the_cancelled_override_applies_only_to_a_conclusion(env):
    """A turn that ends waiting is not rewritten, whatever the lab says about the investigation."""
    fake = FakeLab({0: decision(0, control=WAIT)}, investigation_status="paused")
    result = await run_turn(env, fake)
    assert result["stop_reason"] == "waiting"
    done = fake.closing()
    assert done["sent_stop_reason"] == "waiting" and done["stop_reason"] == "waiting" and done["status"] == "completed"


# =====================================================================================================================
# 4. control cases: nothing refused, the turn ends as the decision asked
# =====================================================================================================================
@temporal
@pytest.mark.parametrize("control, n_calls, expected", [
    pytest.param(CONCLUDE, 1, "concluded", id="conclude-with-calls"),
    pytest.param(STOP, 1, "concluded", id="stop-with-calls"),
    pytest.param(CONCLUDE, 0, "concluded", id="conclude-without-calls"),
    pytest.param(STOP, 0, "concluded", id="stop-without-calls"),
    pytest.param(WAIT, 1, "waiting", id="wait-for-jobs"),
    pytest.param(REQUEST_INPUT, 1, "input-requested", id="request-input"),
])
async def test_an_executed_decision_on_an_active_investigation_ends_the_turn_as_it_asked(
        env, control, n_calls, expected):
    fake = FakeLab({0: decision(0, control=control, n_calls=n_calls)})
    result = await run_turn(env, fake)
    assert result == {"started": True, "turn_id": "t1", "stop_reason": expected, "control": control}
    assert fake.executed == ({"t1-d0"} if n_calls else set())
    done = fake.closing()
    assert done["sent_stop_reason"] == expected and done["stop_reason"] == expected and done["control"] == control
    assert done["status"] == "completed" and "control_refused" not in done
    assert fake.decided == {0}


@temporal
async def test_a_decision_without_a_control_still_goes_on_to_the_next_slot(env):
    """The refusal check does not fire for an execution that neither stopped nor was skipped: the turn goes on."""
    fake = FakeLab({0: decision(0, control=None), 1: decision(1, control=CONCLUDE)})
    result = await run_turn(env, fake)
    assert result["stop_reason"] == "concluded"
    assert fake.decided == {0, 1} and fake.executed == {"t1-d0", "t1-d1"}
    assert fake.closing()["stop_reason"] == "concluded"


# =====================================================================================================================
# the coordinator sees the same
# =====================================================================================================================
class Coordinated(FakeLab):
    """The fake lab as the coordinator sees it. Turn 1 decides a conclusion whose execution is refused by the stop (or
    pause); every later ``begin_turn`` is answered as the real handler does for an investigation that is not active
    (``started`` false, reason ``investigation is <status>``). ``check_news`` reports nothing, so a coordinator that
    waits stays waiting."""

    def __init__(self, status: str):
        super().__init__({0: decision(0, control=CONCLUDE)},
                         executions={"t1-d0": {"summary": [], "skipped": f"investigation-{status}"}},
                         investigation_status=status)
        self.status = status
        self.begin_attempts: list[int] = []
        self.news_checks = 0

    @activity.defn(name="begin_turn")
    async def begin_turn(self, a: dict) -> dict:
        if a["index"] not in self.begin_attempts:
            self.begin_attempts.append(a["index"])
        if a["index"] == 1:
            return await FakeLab.begin_turn(self, a)
        return {"started": False, "reason": f"investigation is {self.status}"}

    @activity.defn(name="check_news")
    async def check_news(self, a: dict) -> dict:
        self.news_checks += 1
        return {"news": False}


async def until(predicate, timeout: float = 30.0, what: str = "condition", step: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@asynccontextmanager
async def coordinated(env, fake: FakeLab, **args):
    """The workflow and investigator workers plus a started coordinator; yields (handle, workflow id)."""
    wid = f"inv-f5d-{uuid.uuid4().hex[:6]}"
    full: dict[str, Any] = {"investigation_id": wid, "project_id": "p", "idle_poll_seconds": 1, **args}
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
            Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        h = await env.client.start_workflow(InvestigationWorkflow.run, full, id=wid, task_queue=WORKFLOW_QUEUE,
                                            execution_timeout=timedelta(seconds=120))
        try:
            yield h, wid
        finally:
            try:
                await h.terminate()
            except Exception:  # already finished
                pass


@temporal
async def test_coordinator_does_not_finish_as_concluded_when_the_stop_refused_the_conclusion(env):
    """A stopped investigation: turn 1's conclusion is refused at execution, so the turn is cancelled (not concluded)
    and the coordinator asks for the next turn, is told the investigation is stopped, and finishes for that reason."""
    fake = Coordinated("stopped")
    async with coordinated(env, fake) as (h, _):
        result = await asyncio.wait_for(h.result(), 60)
    assert result == {"status": "finished", "reason": "investigation is stopped", "turn_counter": 2}
    assert fake.begin_attempts == [1, 2]
    done = fake.closing()
    assert done["sent_stop_reason"] == "cancelled" and done["control"] == {}


@temporal
async def test_coordinator_keeps_waiting_when_the_pause_refused_the_conclusion(env):
    """A paused investigation: the cancelled turn is followed by a begin_turn that is refused as paused, and the
    coordinator parks waiting for news. It does not finish: the pause stands, and a resume continues the investigation."""
    fake = Coordinated("paused")
    async with coordinated(env, fake) as (h, _):
        await until(lambda: fake.news_checks >= 2, 30, "the coordinator's quiet polls while the investigation is paused")
        assert fake.begin_attempts == [1, 2]
        assert (await h.query("state"))["status"] == "waiting (investigation is paused)"
        assert (await h.describe()).status.name == "RUNNING"
    done = fake.closing()
    assert done["sent_stop_reason"] == "cancelled" and done["control"] == {}
