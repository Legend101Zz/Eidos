"""F2b: the decide activity watches for a Temporal cancellation and for a stopped or paused investigation from the
provider request until the decision is accepted, not only while the provider request runs (ADR 0017).

A cancellation that arrives AFTER the last provider response (while usage is recorded or the response uploaded) must
still keep the decision from being proposed: ``accept_decision`` is never sent and the activity raises the
non-retryable ``DECISION_ABORTED``. The deadline alone does not stop a decision that has its response, but it must not
hide a later cancellation (the abort keeps only the first reason). A stop or pause the watcher saw after the response
returns ``investigation-stopped``/``-paused`` without proposing. ``execute_decision`` never executes a decision whose
turn was closed (the cancelled turn is the durable cancellation).

The lab clients are fakes that record every command in order and let a test inject a side effect on a given command;
the runtime is a stub. Nothing here calls a provider or a network endpoint."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from eidos.investigator import activities
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.runtime import DecisionAborted, DecisionResult
from eidos_wire import command_key

pytestmark = pytest.mark.fixture_llm

P, INV, TURN, SLOT = "proj_1", "inv_1", "turn_1", 0
PROPOSAL = {"rationale": "baseline", "calls": [], "control": {"action": "conclude", "summary": "done"}}
USAGE = {"provider": "fixture", "model": "fixture:x", "input_tokens": 12, "output_tokens": 7, "usd": 0.0, "calls": 1}
ACCEPTED = {"accepted": True, "decision_id": "dec_1", "calls": [{"tool": "run_component"}],
            "control": {"action": "wait_for_jobs"}, "reason": None}
ARGS = {"project_id": P, "investigation_id": INV, "turn_id": TURN, "slot": SLOT}
POLL = 0.02  # abort_poll_seconds for the watcher in these tests


# -- fakes -----------------------------------------------------------------------------------------------------------
class World:
    """The shared state of one decide call: the ordered log of every effectful call (commands and uploads, on either
    client), the cancellation flag, the investigation status the lab reports, and one-shot side effects by call name."""

    def __init__(self):
        self.log: list[str] = []
        self.payloads: dict[str, list[dict]] = {}
        self.keys: dict[str, list[str]] = {}
        self.queries: list[str] = []
        self.cancelled = False
        self.inv_status: str | None = "active"
        self.hooks: dict[str, Callable[[], None]] = {}
        self.accept_result: dict = dict(ACCEPTED)
        self.turn: dict = make_turn()
        self.effect_result: dict = {"job_id": "job_1"}
        self.begin_result: dict = {"epoch": 1}  # F2c: begin_decision's answer (epoch, or a decision / revocation)

    def count(self, name: str) -> int:
        return self.log.count(name)

    def record(self, name: str, payload: dict | None, key: str | None) -> None:
        self.log.append(name)
        self.payloads.setdefault(name, []).append(payload or {})
        self.keys.setdefault(name, []).append(key or "")
        hook = self.hooks.get(name)
        if hook:
            hook()


class FakeWorkflowClient:
    """The ``workflow`` principal's client: turn bookkeeping commands and turn / investigation queries."""

    def __init__(self, w: World):
        self.w = w

    def command(self, name: str, payload: dict | None = None, *, project: str | None = None, key: str | None = None,
                **_: Any) -> dict:
        self.w.record(name, payload, key)
        if name == "begin_decision":
            return {"result": self.w.begin_result}
        if name == "reserve_model_budget":
            return {"result": {"reserved": True, "reservation_ref": "res_1"}}
        if name == "accept_decision":
            return {"result": self.w.accept_result}
        if name == "hand_off_decision":  # F5 (C1): this run owns the turn
            return {"result": {"decision_id": (payload or {}).get("decision_id"), "handed_off": True}}
        return {"result": {}}

    def query(self, name: str, params: dict | None = None, *, project: str | None = None) -> Any:
        self.w.queries.append(name)
        if name == "get_turn":
            return self.w.turn
        if name == "get_record" and (params or {}).get("kind") == "investigation":
            return {"status": self.w.inv_status}
        if name == "list_jobs":
            return []
        raise AssertionError(f"unexpected workflow query {name!r}")


class FakeInvestigatorClient:
    """The ``investigator`` principal's client: artifact download / upload and effect commands."""

    def __init__(self, w: World):
        self.w = w

    def download(self, project: str, artifact_id: str, dest: Any) -> None:
        text = {"art_ctx": "# Investigation\nQuestion: why?", "art_src": json.dumps({"view": "v"})}[artifact_id]
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)

    def upload(self, project: str, path: Any = None, *, data: bytes | None = None, key: str | None = None,
               **kw: Any) -> dict:
        self.w.record("upload", {"data": data, **kw}, key)
        return {"result": {"artifact_id": "art_resp"}, "replayed": False}

    def command(self, name: str, payload: dict | None = None, *, project: str | None = None, key: str | None = None,
                **_: Any) -> dict:
        self.w.record(name, payload, key)
        return {"result": self.w.effect_result, "replayed": False}

    def query(self, name: str, params: dict | None = None, *, project: str | None = None) -> Any:
        raise AssertionError(f"decide/execute_decision must not use the investigator query {name!r} here")


class StubRuntime:
    """Stands in for ``build_runtime(cfg)``: ``decide(req)`` keeps the request (and so the shared ``DecisionAbort``),
    optionally runs ``during`` (the provider call in progress) and returns a result or raises ``raises``."""

    def __init__(self):
        self.req = None
        self.during: Callable[[Any], None] | None = None
        self.raises: Exception | None = None

    def decide(self, req):
        self.req = req
        if self.during:
            self.during(req)
        if self.raises:
            raise self.raises
        return DecisionResult(PROPOSAL, dict(USAGE), {"raw": "response"}, {"runtime": "fixture", "temperature": 0},
                              "rev-1")


def make_turn(*, deadline: float = 600, status: str = "open", decisions: list | None = None,
              settings: dict | None = None) -> dict:
    return {"id": TURN, "status": status, "decisions": decisions or [],
            "context": {"context_artifact_id": "art_ctx", "source_manifest_artifact_id": "art_src"},
            "spec": {"runtime": {"runtime": "fixture", "model": None, "settings": {"abort_poll_seconds": POLL,
                                                                                   **(settings or {})}},
                     "limits": {"deadline_seconds": deadline, "max_model_calls": 4, "max_calls": 4}}}


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    monkeypatch.setattr(activities.activity, "info", lambda: SimpleNamespace(attempt=1, workflow_run_id="run_1"))
    monkeypatch.setattr(activities.activity, "is_cancelled", lambda: w.cancelled)
    return w


@pytest.fixture
def rt(monkeypatch) -> StubRuntime:
    stub = StubRuntime()
    monkeypatch.setattr(activities, "build_runtime", lambda cfg: stub)
    return stub


@pytest.fixture
def acts(world) -> InvestigatorActivities:
    return InvestigatorActivities(FakeInvestigatorClient(world), FakeWorkflowClient(world))


def cancel_on(w: World, name: str) -> None:
    """Inject the side effect: Temporal cancels the activity while ``name`` is being sent."""
    w.hooks[name] = lambda: setattr(w, "cancelled", True)


def wait_for(predicate: Callable[[], Any], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(0.005)
    return bool(predicate())


def assert_aborted_by_cancellation(acts: InvestigatorActivities) -> ApplicationError:
    with pytest.raises(ApplicationError) as err:
        acts.decide(dict(ARGS))
    assert err.value.type == "DECISION_ABORTED"
    assert err.value.non_retryable is True
    return err.value


# -- 5. control: nothing wrong -> accepted exactly once ----------------------------------------------------------------
def test_control_no_cancellation_sends_accept_once_and_returns_its_result(world, rt, acts):
    out = acts.decide(dict(ARGS))
    assert world.log == ["begin_decision", "reserve_model_budget", "record_model_usage", "upload", "accept_decision"]
    assert world.count("accept_decision") == 1
    assert out == {"accepted": True, "decision_id": "dec_1", "n_calls": 1, "control": {"action": "wait_for_jobs"},
                   "reason": None}
    accept = world.payloads["accept_decision"][0]
    assert accept["proposal"] == PROPOSAL and accept["usage"] == USAGE and accept["usage_recorded"] is True
    assert accept["response_artifact_id"] == "art_resp" and accept["attempt"] == 1
    assert world.keys["accept_decision"] == [f"accept:{TURN}:{SLOT}:1:1"]
    assert rt.req is not None and rt.req.abort.reason is None


def test_control_the_lab_s_refusal_of_the_accept_is_returned_as_is(world, rt, acts):
    world.accept_result = {"accepted": False, "decision_id": "dec_9", "calls": [], "control": None,
                           "reason": "recovered"}
    out = acts.decide(dict(ARGS))
    assert world.count("accept_decision") == 1
    assert out["accepted"] is False and out["reason"] == "recovered" and out["n_calls"] == 0


def test_the_watcher_covers_the_whole_decision_not_only_the_provider_call(world, rt, acts):
    """The fix itself: the watcher thread is still alive after ``runtime.decide`` returns (it polls the investigation
    status while usage is recorded and the response uploaded) and stops once the activity is done."""
    polled_after_response: list[int] = []

    def slow_upload() -> None:
        before = world.queries.count("get_record")
        assert wait_for(lambda: world.queries.count("get_record") >= before + 2), "the watcher stopped polling"
        polled_after_response.append(world.queries.count("get_record") - before)

    world.hooks["upload"] = slow_upload
    acts.decide(dict(ARGS))
    assert polled_after_response and polled_after_response[0] >= 2
    time.sleep(POLL * 4)  # a poll already running may still finish
    settled = world.queries.count("get_record")
    time.sleep(POLL * 8)
    assert world.queries.count("get_record") == settled  # and it stops with the activity


# -- 1. cancellation flips while the usage is recorded ----------------------------------------------------------------
def test_cancellation_during_usage_recording_aborts_before_the_upload_and_accept(world, rt, acts):
    cancel_on(world, "record_model_usage")
    err = assert_aborted_by_cancellation(acts)
    assert "activity-cancelled" in str(err)
    assert world.count("accept_decision") == 0
    assert world.count("record_model_usage") == 1  # the billed response is still charged, once
    recorded = dict(world.payloads["record_model_usage"][0]["usage"])
    assert recorded.pop("attempt") == 1 and recorded.pop("elapsed_seconds") >= 0  # ADR 0023: per attempt, timed
    assert recorded == USAGE
    assert world.keys["record_model_usage"] == [f"usage:{TURN}:{SLOT}:1:1"]
    assert world.log == ["begin_decision", "reserve_model_budget", "record_model_usage"]  # the first check stops before the upload


# -- 2. cancellation flips during the response upload ------------------------------------------------------------------
def test_cancellation_during_the_response_upload_aborts_before_accept(world, rt, acts):
    cancel_on(world, "upload")
    assert_aborted_by_cancellation(acts)
    assert world.count("accept_decision") == 0
    assert world.count("record_model_usage") == 1 and world.count("upload") == 1
    assert world.log == ["begin_decision", "reserve_model_budget", "record_model_usage", "upload"]


def test_cancellation_that_arrives_before_the_first_response_check_is_not_lost_either(world, rt, acts):
    """Cancelled while the provider call was in progress but the stub returned a result anyway."""
    rt.during = lambda req: setattr(world, "cancelled", True)
    assert_aborted_by_cancellation(acts)
    assert world.count("accept_decision") == 0 and world.count("record_model_usage") == 1


def test_cancellation_during_the_provider_call_records_usage_and_raises(world, rt, acts):
    rt.raises = DecisionAborted("activity-cancelled", in_flight=True, possible_usage={"calls": 1})
    assert_aborted_by_cancellation(acts)
    assert world.log == ["begin_decision", "reserve_model_budget", "record_model_usage"]
    assert world.payloads["record_model_usage"][0]["usage"] == {"calls": 1, "uncertain": True}
    assert world.keys["record_model_usage"] == [f"usage-abort:{TURN}:{SLOT}:1:1"]


# -- 3. the deadline fired first, then the cancellation ---------------------------------------------------------------
def deadline_fires_first(req) -> None:
    """The runtime notices the (tiny) turn deadline while the request is in flight, as ``runtime.decide`` does."""
    assert wait_for(lambda: req.abort.poll() == "decision-deadline", 2.0)


@pytest.mark.parametrize("flip_on", ["record_model_usage", "upload"])
def test_cancellation_after_the_deadline_already_fired_is_still_honoured(world, rt, acts, flip_on):
    world.turn = make_turn(deadline=0.2)  # decision deadline = start + 0.2 - min(15, 0.02) = start + 0.18 s
    rt.during = deadline_fires_first
    cancel_on(world, flip_on)
    assert_aborted_by_cancellation(acts)
    assert rt.req.abort.reason == "decision-deadline"  # the abort kept the deadline: it cannot report the cancellation
    assert world.count("accept_decision") == 0
    assert world.count("record_model_usage") == 1


def test_cancellation_the_watcher_saw_after_the_deadline_is_still_honoured(world, rt, acts):
    """The deadline fires, then Temporal cancels while the provider call is still running: the watcher's trigger loses
    (first reason wins), yet the response that then arrives is not proposed."""
    world.turn = make_turn(deadline=0.2)

    def during(req) -> None:
        deadline_fires_first(req)
        world.cancelled = True
        time.sleep(POLL * 5)  # the watcher polls ``is_cancelled`` and triggers a reason that does not stick

    rt.during = during
    assert_aborted_by_cancellation(acts)
    assert rt.req.abort.reason == "decision-deadline"
    assert world.count("accept_decision") == 0 and world.count("record_model_usage") == 1


def test_the_deadline_alone_does_not_stop_a_decision_that_has_its_response(world, rt, acts):
    world.turn = make_turn(deadline=0.2)
    rt.during = deadline_fires_first
    out = acts.decide(dict(ARGS))
    assert rt.req.abort.reason == "decision-deadline"
    assert world.count("accept_decision") == 1  # the lab checks the attempt's deadline itself
    assert out["accepted"] is True and out["decision_id"] == "dec_1"


# -- 4. the investigation stops after the response, during a slow upload ----------------------------------------------
@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_stop_or_pause_seen_by_the_watcher_after_the_response_is_not_proposed(world, rt, acts, status):
    def slow_upload() -> None:
        world.inv_status = status  # the investigation changes only now, after runtime.decide returned
        assert wait_for(lambda: rt.req.abort.reason is not None), "the watcher never saw the new status"

    world.hooks["upload"] = slow_upload
    out = acts.decide(dict(ARGS))
    assert out == {"accepted": False, "reason": f"investigation-{status}", "n_calls": 0}
    assert world.count("accept_decision") == 0
    assert world.count("record_model_usage") == 1 and world.count("upload") == 1
    assert world.log == ["begin_decision", "reserve_model_budget", "record_model_usage", "upload"]


def test_stop_during_a_slow_upload_is_seen_even_when_the_poll_interval_is_short(world, rt, acts):
    """The status flips only once the response has been uploaded for longer than ``abort_poll_seconds``."""
    def slow_upload() -> None:
        time.sleep(POLL * 3)  # the watcher polls and finds the investigation active several times
        world.inv_status = "stopped"
        time.sleep(POLL * 10)  # longer than the poll interval: the watcher must notice before the upload returns

    world.hooks["upload"] = slow_upload
    out = acts.decide(dict(ARGS))
    assert out["accepted"] is False and out["reason"] == "investigation-stopped"
    assert world.count("accept_decision") == 0


def test_a_cancellation_beats_a_stop_for_the_error_raised(world, rt, acts):
    world.inv_status = "stopped"

    def slow_upload() -> None:
        world.cancelled = True
        assert wait_for(lambda: rt.req.abort.reason is not None)

    world.hooks["upload"] = slow_upload
    assert_aborted_by_cancellation(acts)  # DECISION_ABORTED (not a quiet investigation-stopped result)
    assert world.count("accept_decision") == 0


# -- 6. execute_decision on a closed turn ----------------------------------------------------------------------------
def decision(calls: list[dict], control: dict | None = None) -> dict:
    return {"id": "dec_1", "slot": SLOT, "accepted": True, "body": {"calls": calls, "control": control}}


def call(index: int, tool: str, args: dict) -> dict:
    return {"index": index, "tool": tool, "args": args, "command_key": command_key(P, TURN, "dec_1", index)}


EXEC_ARGS = {"project_id": P, "investigation_id": INV, "turn_id": TURN, "decision_id": "dec_1"}


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_execute_decision_skips_every_call_when_the_turn_is_not_open(world, acts, status):
    world.turn = make_turn(status=status, decisions=[decision([
        call(0, "run_component", {"component_ref": "a@1.0.0", "inputs": {}}),
        call(1, "author_source", {"files": {"train.py": "print(1)"}}),
    ])])
    out = acts.execute_decision(dict(EXEC_ARGS))
    assert out == {"summary": [], "skipped": f"turn-{status}"}
    assert world.log == []  # no effect command, no upload, not even the receipts record


def test_execute_decision_executes_the_calls_when_the_turn_is_open(world, acts):
    args = {"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}}
    world.turn = make_turn(status="open", decisions=[decision([call(0, "run_component", args)])])
    out = acts.execute_decision(dict(EXEC_ARGS))
    assert "skipped" not in out
    assert out["summary"] == [{"tool": "run_component", "ok": True, "replayed": False, "code": None}]
    assert world.log == ["hand_off_decision", "run_component", "record_decision_receipts"]
    assert world.payloads["run_component"][0] == {**args, "investigation_id": INV}
    assert world.keys["run_component"] == [command_key(P, TURN, "dec_1", 0)]
    receipts = world.payloads["record_decision_receipts"][0]
    assert receipts["decision_id"] == "dec_1" and receipts["receipts"][0]["result"] == {"job_id": "job_1"}
