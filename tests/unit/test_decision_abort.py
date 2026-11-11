"""Stop/deadline cancels in-flight investigator model requests (ADR 0017, I3): the ``DecisionAbort`` signal, the
runtime's abandonment of an in-flight request, the usage it reports for an aborted decision, and the decide activity's
deadline and watcher helpers. Fixture models only: nothing here calls a provider or a network endpoint."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.domain.turns import validate_decision
from eidos.investigator import activities
from eidos.investigator import pydantic_ai_runtime as par
from eidos.investigator.runtime import (
    ABORT_POLL_SECONDS,
    DecisionAbort,
    DecisionAborted,
    DecisionRequest,
    ModelCallFailed,
)

pytestmark = pytest.mark.fixture_llm

SLEEP = 30.0  # a request that would outlive the test 10x over: finishing early proves it was cancelled
WITHIN = 2.5  # generous bound on "decide returns promptly after the abort" (poll interval is 0.1 s)
MODES = ("json", "repair", "tool")
GOOD = {"rationale": "baseline first", "purpose": "reproduction",
        "hypotheses": [{"statement": "h", "prediction": "p", "falsifier": "f"}],
        "calls": [{"tool": "run_component", "args": {"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}}}],
        "control": {"action": "wait_for_jobs", "summary": "wait"}}
READ_NAMES = ["inspect_evidence", "search_components", "describe_component", "get_job"]


# -- helpers ---------------------------------------------------------------------------------------------------------
def usage(inp: int = 10, out: int = 20) -> RequestUsage:
    return RequestUsage(input_tokens=inp, output_tokens=out)


def response(inp: int = 10, out: int = 20) -> ModelResponse:
    return ModelResponse(parts=[TextPart("x")], usage=usage(inp, out), model_name="m")


def read_tools() -> dict:
    return {"inspect_evidence": lambda artifact_id, max_bytes=8192: {}, "search_components": lambda text="", c=None: {},
            "describe_component": lambda ref: {}, "get_job": lambda job_id: {"id": job_id, "status": "running"}}


def decision_reply(mode: str, info: AgentInfo, inp: int = 10, out: int = 20) -> ModelResponse:
    """A valid final decision in the shape ``mode`` expects (a tool call to the output tool, or JSON text)."""
    if mode == "tool":
        args = {**GOOD, "calls": [{"tool": c["tool"], "args_json": json.dumps(c["args"])} for c in GOOD["calls"]]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)], usage=usage(inp, out))
    return ModelResponse(parts=[TextPart(json.dumps(GOOD))], usage=usage(inp, out))


def read_reply(mode: str, inp: int = 111, out: int = 22) -> ModelResponse:
    """A read request for ``get_job`` (a native tool call in tool mode, the ``read`` JSON shape in the text modes)."""
    if mode == "tool":
        return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": "j1"})], usage=usage(inp, out))
    text = json.dumps({"rationale": "r", "read": [{"tool": "get_job", "args": {"job_id": "j1"}}]})
    return ModelResponse(parts=[TextPart(text)], usage=usage(inp, out))


class Provider:
    """An async ``FunctionModel`` function. The first ``len(fast)`` requests are answered at once by the entries of
    ``fast`` (a ``ModelResponse`` or a callable ``(info) -> ModelResponse``); every later one hangs on a 30 s sleep and
    records that it was cancelled, instead of finishing."""

    def __init__(self, fast=()):
        self.fast = list(fast)
        self.calls = 0
        self.cancelled = 0

    async def __call__(self, messages: list, info: AgentInfo) -> ModelResponse:
        n = self.calls
        self.calls += 1
        if n < len(self.fast):
            reply = self.fast[n]
            return reply(info) if callable(reply) else reply
        try:
            await asyncio.sleep(SLEEP)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("the slow request was never cancelled")


def runtime(fn, mode: str, **cfg) -> par.PydanticAIRuntime:
    return par.PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "openai-compatible:fake",
                                  "output_mode": mode, **cfg})


def request(abort: DecisionAbort | None, tools: dict | None = None) -> DecisionRequest:
    return DecisionRequest(turn_id="t1", slot=0, context="# Investigation\nQuestion: why?", turn_so_far=[],
                           limits={"max_calls": 4}, read_tools=tools or {}, abort=abort)


@contextmanager
def triggered_after(abort: DecisionAbort, seconds: float, reason: str):
    """Trigger ``abort`` from another thread (the decide activity's watcher) ``seconds`` from now."""
    timer = threading.Timer(seconds, abort.trigger, args=[reason])
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def timed(fn):
    t0 = time.monotonic()
    try:
        return fn(), time.monotonic() - t0
    except BaseException as exc:  # noqa: BLE001 - the caller inspects it
        return exc, time.monotonic() - t0


# -- 1. DecisionAbort ------------------------------------------------------------------------------------------------
def test_abort_first_reason_wins():
    abort = DecisionAbort()
    assert abort.reason is None and abort.poll() is None
    abort.trigger("investigation-stopped")
    abort.trigger("investigation-paused")
    abort.trigger("activity-cancelled")
    assert abort.reason == "investigation-stopped" and abort.poll() == "investigation-stopped"


def test_abort_without_a_deadline_never_expires():
    abort = DecisionAbort(clock=lambda: 1e12)
    assert abort.deadline is None and abort.poll() is None


def test_abort_poll_reports_the_deadline_only_once_it_passes():
    now = [100.0]
    abort = DecisionAbort(deadline=110.0, clock=lambda: now[0])
    assert abort.poll() is None
    now[0] = 109.999
    assert abort.poll() is None and abort.reason is None
    now[0] = 110.0  # the deadline itself counts as passed
    assert abort.poll() == "decision-deadline" and abort.reason == "decision-deadline"
    now[0] = 0.0  # once set, the reason stays
    assert abort.poll() == "decision-deadline"


def test_abort_deadline_does_not_override_an_earlier_reason_nor_is_overridden():
    now = [0.0]
    stopped = DecisionAbort(deadline=5.0, clock=lambda: now[0])
    stopped.trigger("investigation-stopped")
    now[0] = 6.0
    assert stopped.poll() == "investigation-stopped"  # a stop before the deadline passed is not relabelled

    expired = DecisionAbort(deadline=5.0, clock=lambda: now[0])
    assert expired.poll() == "decision-deadline"
    expired.trigger("investigation-stopped")  # and a stop after the deadline does not replace it
    assert expired.reason == "decision-deadline"


def test_abort_trigger_from_another_thread_is_seen_by_poll():
    abort = DecisionAbort()
    t = threading.Timer(0.05, abort.trigger, args=["investigation-paused"])
    t.start()
    end = time.monotonic() + WITHIN
    while abort.poll() is None and time.monotonic() < end:
        time.sleep(0.005)
    t.join()
    assert abort.poll() == "investigation-paused"


def test_abort_concurrent_triggers_keep_exactly_one_reason():
    abort = DecisionAbort()
    reasons = [f"reason-{i}" for i in range(16)]
    barrier = threading.Barrier(len(reasons))

    def go(reason: str) -> None:
        barrier.wait()
        abort.trigger(reason)

    threads = [threading.Thread(target=go, args=(r,)) for r in reasons]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert abort.reason in reasons
    winner = abort.reason
    abort.trigger("late")
    assert abort.reason == winner and abort.poll() == winner


def test_decision_aborted_is_a_non_retryable_model_call_failure():
    exc = DecisionAborted("investigation-stopped", in_flight=True, abandoned={"input_estimate": 5, "output_cap": 7},
                          possible_usage={"calls": 1})
    assert isinstance(exc, ModelCallFailed) and exc.retryable is False
    assert (exc.reason, exc.in_flight, exc.abandoned, exc.possible_usage) == (
        "investigation-stopped", True, {"input_estimate": 5, "output_cap": 7}, {"calls": 1})
    plain = DecisionAborted("investigation-paused")
    assert plain.in_flight is False and plain.abandoned is None and plain.possible_usage == {}


# -- 2. a slow provider ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
def test_deadline_abandons_the_in_flight_request_and_returns_a_no_effect_decision(mode):
    provider = Provider()
    rt = runtime(provider, mode, max_output_tokens=512)
    abort = DecisionAbort(deadline=time.monotonic() + 0.3)
    res, elapsed = timed(lambda: rt.decide(request(abort)))
    assert not isinstance(res, BaseException), res
    assert elapsed < WITHIN, f"decide took {elapsed:.2f}s: the in-flight request was not abandoned"
    assert provider.calls == 1 and provider.cancelled == 1  # the connection (task) was cancelled
    assert res.proposal["calls"] == []
    assert res.proposal["control"]["bounded_outcome"] == "decision-deadline"
    assert res.proposal["control"]["action"] == "wait_for_jobs"
    assert validate_decision(res.proposal, max_calls=4)["control"]["action"] == "wait_for_jobs"
    u = res.usage
    assert u["limit_reached"] and "deadline" in u["limit_reached"] and "abandoned" in u["limit_reached"]
    assert u["uncertain"] is True and u["calls"] == 0  # unknown bill: never recorded as zero
    assert u["aborted"]["reason"] == "decision-deadline" and u["aborted"]["in_flight"] is True
    abandoned = u["aborted"]["abandoned_request"]
    assert abandoned["input_estimate"] > 0 and abandoned["output_cap"] == 512 and abandoned["spent_before"] == 0
    assert res.raw_response == [] and res.prompt_revision == par.PROMPT_REVISION
    assert res.model_settings["runtime"] == "pydantic-ai"


@pytest.mark.parametrize("mode", MODES)
def test_stop_abandons_the_in_flight_request_and_raises(mode):
    provider = Provider()
    rt = runtime(provider, mode)
    abort = DecisionAbort()
    with triggered_after(abort, 0.2, "investigation-stopped"):
        res, elapsed = timed(lambda: rt.decide(request(abort)))
    assert isinstance(res, DecisionAborted), res
    assert elapsed < WITHIN, f"decide took {elapsed:.2f}s: the in-flight request was not abandoned"
    assert res.reason == "investigation-stopped" and res.in_flight is True and res.retryable is False
    assert res.abandoned is not None and res.abandoned["output_cap"] == par.output_cap({})
    pu = res.possible_usage
    assert pu["uncertain"] is True and pu["calls"] == 0 and pu["input_tokens"] == 0 and pu["output_tokens"] == 0
    assert pu["aborted"] == {"reason": "investigation-stopped", "in_flight": True, "abandoned_request": res.abandoned}
    assert provider.calls == 1 and provider.cancelled == 1


@pytest.mark.parametrize("mode", MODES)
def test_an_abort_set_before_decide_starts_no_request(mode):
    provider = Provider(fast=[lambda info: decision_reply(mode, info)])  # would answer at once if it were ever asked
    rt = runtime(provider, mode)
    abort = DecisionAbort()
    abort.trigger("investigation-stopped")
    with pytest.raises(DecisionAborted) as e:
        rt.decide(request(abort))
    assert provider.calls == 0
    assert e.value.reason == "investigation-stopped" and e.value.in_flight is False and e.value.abandoned is None
    pu = e.value.possible_usage
    assert pu.get("uncertain") is not True and pu["calls"] == 0
    assert pu["aborted"] == {"reason": "investigation-stopped", "in_flight": False}


@pytest.mark.parametrize("mode", MODES)
def test_an_expired_deadline_before_decide_returns_the_no_effect_decision_without_a_request(mode):
    provider = Provider(fast=[lambda info: decision_reply(mode, info)])
    rt = runtime(provider, mode)
    res = rt.decide(request(DecisionAbort(deadline=time.monotonic() - 1)))
    assert provider.calls == 0
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "decision-deadline"
    assert "no further request was started" in res.usage["limit_reached"]
    assert res.usage["aborted"] == {"reason": "decision-deadline", "in_flight": False}
    assert res.usage.get("uncertain") is not True and res.usage["calls"] == 0


# -- 3. responses received before the abort are counted --------------------------------------------------------------
@pytest.mark.parametrize("mode", ["json", "tool"])
def test_responses_received_before_the_stop_are_counted(mode):
    provider = Provider(fast=[read_reply(mode, inp=111, out=22)])  # answers the first request, hangs on the second
    rt = runtime(provider, mode)
    abort = DecisionAbort()
    with triggered_after(abort, 0.4, "investigation-stopped"):
        res, elapsed = timed(lambda: rt.decide(request(abort, read_tools())))
    assert isinstance(res, DecisionAborted), res
    assert elapsed < WITHIN
    assert provider.calls == 2 and provider.cancelled == 1
    pu = res.possible_usage
    assert pu["calls"] == 1 and pu["input_tokens"] == 111 and pu["output_tokens"] == 22
    assert pu["uncertain"] is True and res.in_flight is True
    assert [r["input_tokens"] for r in pu["per_request"]] == [111]
    # The abandoned request was admitted on top of what the first response already spent.
    assert res.abandoned["spent_before"] == 111 + 22 and res.abandoned["input_estimate"] > 0


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_deadline_after_a_response_keeps_its_usage_in_the_no_effect_decision(mode):
    provider = Provider(fast=[read_reply(mode, inp=111, out=22)])
    rt = runtime(provider, mode)
    abort = DecisionAbort(deadline=time.monotonic() + 0.4)
    res, elapsed = timed(lambda: rt.decide(request(abort, read_tools())))
    assert not isinstance(res, BaseException), res
    assert elapsed < WITHIN and provider.cancelled == 1
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "decision-deadline"
    assert res.usage["calls"] == 1 and res.usage["input_tokens"] == 111 and res.usage["output_tokens"] == 22
    assert res.usage["uncertain"] is True and res.usage["aborted"]["in_flight"] is True
    assert len(res.raw_response) >= 1  # the received response is kept in the record


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_abort_set_while_a_response_arrives_stops_before_the_next_request(mode):
    stopped = DecisionAbort()

    def first(info):
        stopped.trigger("investigation-paused")  # set while the first request is being answered
        return read_reply(mode, inp=40, out=9)

    provider = Provider(fast=[first, lambda info: decision_reply(mode, info)])
    rt = runtime(provider, mode)
    with pytest.raises(DecisionAborted) as e:
        rt.decide(request(stopped, read_tools()))
    assert provider.calls == 1  # the read request's follow-up was never sent
    assert e.value.reason == "investigation-paused" and e.value.in_flight is False
    pu = e.value.possible_usage
    assert pu["calls"] == 1 and pu["input_tokens"] == 40 and pu["output_tokens"] == 9
    assert pu.get("uncertain") is not True  # nothing was abandoned in flight: the received usage is exact


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_deadline_between_requests_is_a_no_effect_decision_with_exact_usage(mode):
    now = [0.0]
    abort = DecisionAbort(deadline=10.0, clock=lambda: now[0])

    def first(info):
        now[0] = 11.0  # the deadline passes while the first request is being answered
        return read_reply(mode, inp=40, out=9)

    provider = Provider(fast=[first, lambda info: decision_reply(mode, info)])
    res = runtime(provider, mode).decide(request(abort, read_tools()))
    assert provider.calls == 1
    assert res.proposal["control"]["bounded_outcome"] == "decision-deadline" and res.proposal["calls"] == []
    assert "no further request was started" in res.usage["limit_reached"]
    assert res.usage["calls"] == 1 and res.usage["input_tokens"] == 40 and res.usage["output_tokens"] == 9
    assert res.usage.get("uncertain") is not True and res.usage["aborted"]["in_flight"] is False


def test_tool_mode_final_request_without_tools_is_abandoned_too():
    """The request budget is spent on read rounds, so the runtime makes one last tool-free request; stopping during it
    still abandons it (``DecisionAborted`` passes through that fallback path) and counts the first response."""

    def fn_fast_then_slow(info):
        return read_reply("tool", inp=70, out=5)

    provider = Provider(fast=[fn_fast_then_slow])
    rt = runtime(provider, "tool", max_requests_per_decision=2)
    abort = DecisionAbort()
    with triggered_after(abort, 0.4, "investigation-stopped"):
        res, elapsed = timed(lambda: rt.decide(request(abort, read_tools())))
    assert isinstance(res, DecisionAborted), res
    assert elapsed < WITHIN and provider.calls == 2 and provider.cancelled == 1
    assert res.reason == "investigation-stopped" and res.in_flight is True
    assert res.possible_usage["calls"] == 1 and res.possible_usage["input_tokens"] == 70
    assert res.possible_usage["uncertain"] is True


# -- 4. no abort: the regression ------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
@pytest.mark.parametrize("kind", ["none", "never-triggered", "far-deadline"])
def test_without_an_abort_a_fast_model_decides_normally(mode, kind):
    abort = {"none": None, "never-triggered": DecisionAbort(),
             "far-deadline": DecisionAbort(deadline=time.monotonic() + 3600)}[kind]
    provider = Provider(fast=[lambda info: decision_reply(mode, info, inp=33, out=44)])
    res = runtime(provider, mode).decide(request(abort))
    assert provider.calls == 1 and provider.cancelled == 0
    assert res.proposal["calls"] == [{"tool": "run_component", "args": GOOD["calls"][0]["args"]}]
    assert res.proposal["rationale"] == GOOD["rationale"]
    assert "aborted" not in res.usage and "uncertain" not in res.usage and "limit_reached" not in res.usage
    assert res.usage["calls"] == 1 and res.usage["input_tokens"] == 33 and res.usage["output_tokens"] == 44
    assert abort is None or abort.reason is None


def test_without_an_abort_the_model_is_not_wrapped_in_the_abort_machinery():
    """The envelope still guards every request; only the abort polling is absent (``abort=None``)."""
    provider = Provider(fast=[lambda info: decision_reply("json", info)])
    rt = runtime(provider, "json")
    rt.decide(request(None))
    assert rt.abort is None and rt.envelope is not None and len(rt.envelope.responses) == 1


# -- 5. a response that completed is returned even though the abort was set meanwhile -------------------------------
@pytest.mark.parametrize("mode", ["json", "tool"])
def test_a_completed_response_survives_an_abort_set_as_it_arrives(mode):
    """The runtime never discards a response that completed: it is the decide activity that drops the decision of a
    stopped investigation (``abort.reason`` is checked after ``decide`` returns), after recording the usage."""
    abort = DecisionAbort()

    def answer(info):
        abort.trigger("investigation-stopped")
        return decision_reply(mode, info, inp=12, out=34)

    provider = Provider(fast=[answer])
    res = runtime(provider, mode).decide(request(abort))
    assert provider.calls == 1 and provider.cancelled == 0
    assert res.proposal["rationale"] == GOOD["rationale"] and res.proposal["calls"]
    assert "aborted" not in res.usage and "uncertain" not in res.usage
    assert res.usage["calls"] == 1 and res.usage["input_tokens"] == 12 and res.usage["output_tokens"] == 34
    assert abort.reason == "investigation-stopped"  # still set: the activity sees it and discards the decision


# -- _abortable ------------------------------------------------------------------------------------------------------
def test_abortable_returns_a_fast_request_without_polling_the_abort():
    class Spy(DecisionAbort):
        polls = 0

        def poll(self):
            Spy.polls += 1
            return super().poll()

    async def quick():
        return "answer"

    assert asyncio.run(par._abortable(quick(), Spy(), None)) == "answer"
    assert Spy.polls == 0


def test_abortable_cancels_the_request_and_reports_what_was_abandoned():
    seen = {"cancelled": False}
    abandoned = {"input_estimate": 123, "output_cap": 456, "spent_before": 0}

    async def slow():
        try:
            await asyncio.sleep(SLEEP)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def go():
        abort = DecisionAbort()
        asyncio.get_running_loop().call_later(0.15, abort.trigger, "investigation-stopped")
        t0 = time.monotonic()
        try:
            await par._abortable(slow(), abort, abandoned)
        except DecisionAborted as exc:
            return exc, time.monotonic() - t0
        raise AssertionError("not aborted")

    exc, elapsed = asyncio.run(go())
    assert seen["cancelled"] and elapsed < WITHIN
    assert exc.reason == "investigation-stopped" and exc.in_flight is True and exc.abandoned == abandoned


def test_abortable_is_aborted_even_if_the_transport_fails_on_cancellation():
    async def stubborn():
        try:
            await asyncio.sleep(SLEEP)
        except asyncio.CancelledError as exc:
            raise ConnectionError("connection reset while closing") from exc

    async def go():
        abort = DecisionAbort(deadline=time.monotonic() + 0.15)
        with pytest.raises(DecisionAborted) as e:
            await par._abortable(stubborn(), abort, None)
        return e.value

    exc = asyncio.run(go())
    assert exc.reason == "decision-deadline" and exc.in_flight is True and exc.abandoned is None


def test_abortable_propagates_an_outer_cancellation_to_the_request():
    seen = {"cancelled": False}

    async def slow():
        try:
            await asyncio.sleep(SLEEP)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def go():
        outer = asyncio.ensure_future(par._abortable(slow(), DecisionAbort(), None))
        await asyncio.sleep(ABORT_POLL_SECONDS * 2)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)  # let the inner task observe its cancellation

    asyncio.run(go())
    assert seen["cancelled"]


def test_abortable_returns_a_request_that_finishes_just_as_the_abort_is_set():
    abort = DecisionAbort()

    async def answer():
        abort.trigger("investigation-stopped")
        return "completed"

    assert asyncio.run(par._abortable(answer(), abort, None)) == "completed"


# -- 6. decision_deadline --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, None, 0.0, -5, -0.1])
def test_decision_deadline_is_none_without_a_turn_deadline(value):
    assert activities.decision_deadline(1000.0, value) is None


@pytest.mark.parametrize("seconds,margin", [(600, 15), (10, 1), (100, 10), (150, 15), (1000, 15), (1, 0.1), (60, 6)])
def test_decision_deadline_leaves_ten_percent_up_to_the_margin(seconds, margin):
    assert activities.DEADLINE_MARGIN_SECONDS == 15.0
    assert activities.decision_deadline(1000.0, seconds) == pytest.approx(1000.0 + seconds - margin)


def test_decision_deadline_examples():
    started = time.monotonic()
    assert activities.decision_deadline(started, 600) == pytest.approx(started + 585)
    assert activities.decision_deadline(started, 10) == pytest.approx(started + 9)
    assert activities.decision_deadline(started, 600) < started + 600  # always strictly before the activity's timeout


# -- 7. _watching ----------------------------------------------------------------------------------------------------
def wait_for(predicate, timeout: float = WITHIN) -> bool:
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        time.sleep(0.005)
    return bool(predicate())


def test_watching_precondition_activity_context_is_absent_in_unit_tests():
    from temporalio import activity
    with pytest.raises(Exception):  # noqa: B017, PT011 - the watcher must tolerate whatever this raises
        activity.is_cancelled()


def test_watching_triggers_with_the_reason_the_check_returns():
    abort = DecisionAbort()
    calls = []

    def check():
        calls.append(1)
        return "investigation-stopped" if len(calls) >= 3 else None

    with activities._watching(abort, check, every=0.02):
        assert wait_for(lambda: abort.reason is not None), "the watcher never triggered the abort"
    assert abort.reason == "investigation-stopped" and len(calls) == 3  # polled until the first reason, then done


def test_watching_stops_after_the_first_reason():
    abort = DecisionAbort()
    calls = []

    def check():
        calls.append(1)
        return "investigation-paused"

    with activities._watching(abort, check, every=0.02):
        assert wait_for(lambda: abort.reason is not None)
        time.sleep(0.15)
    assert abort.reason == "investigation-paused" and len(calls) == 1


def test_watching_skips_a_check_that_raises_and_keeps_polling():
    abort = DecisionAbort()
    calls = []

    def check():
        calls.append(1)
        if len(calls) < 4:
            raise ConnectionError("the lab is briefly unreachable")
        return "investigation-stopped"

    with activities._watching(abort, check, every=0.02):
        assert wait_for(lambda: abort.reason is not None), "a failing check ended the watcher"
    assert len(calls) == 4 and abort.reason == "investigation-stopped"


def test_watching_does_not_trigger_while_the_check_finds_nothing():
    abort = DecisionAbort()
    calls = []
    with activities._watching(abort, lambda: calls.append(1), every=0.02):  # returns None
        assert wait_for(lambda: len(calls) >= 3)
    assert abort.reason is None


def test_watching_stops_polling_after_the_body_exits():
    abort = DecisionAbort()
    calls = []
    with activities._watching(abort, lambda: calls.append(1), every=0.02):
        assert wait_for(lambda: len(calls) >= 2)
    time.sleep(0.1)  # a poll that was already running may still finish
    settled = len(calls)
    time.sleep(0.3)
    assert len(calls) == settled and abort.reason is None


def test_watching_stops_polling_when_the_body_raises():
    abort = DecisionAbort()
    calls = []
    with pytest.raises(RuntimeError, match="boom"), activities._watching(abort, lambda: calls.append(1), every=0.02):
        assert wait_for(lambda: len(calls) >= 1)
        raise RuntimeError("boom")
    time.sleep(0.1)
    settled = len(calls)
    time.sleep(0.2)
    assert len(calls) == settled


def test_watching_triggers_when_temporal_cancels_the_activity(monkeypatch):
    monkeypatch.setattr(activities.activity, "is_cancelled", lambda: True)
    checked = []
    abort = DecisionAbort()
    with activities._watching(abort, lambda: checked.append(1), every=0.02):
        assert wait_for(lambda: abort.reason is not None)
    assert abort.reason == "activity-cancelled"
    assert checked == []  # the cancellation is enough; the lab is not asked


def test_watching_lets_the_abort_deadline_apply_independently_of_the_watcher():
    abort = DecisionAbort(deadline=time.monotonic() + 0.05)
    with activities._watching(abort, lambda: None, every=0.02):
        assert wait_for(lambda: abort.poll() is not None)
    assert abort.reason == "decision-deadline"


# -- 8. usage settlement ---------------------------------------------------------------------------------------------
def test_token_envelope_record_keeps_every_response_in_order():
    env = par.TokenEnvelope(limit=100_000, cap=1000)
    assert env.responses == [] and env.spent == 0
    r1, r2, r3 = response(10, 20), response(30, 5), response(0, 7)
    env.record(r1)
    assert env.responses == [r1] and env.spent == 30 and env.last == (10, 20)
    env.record(r2)
    assert env.responses == [r1, r2] and env.spent == 65 and env.last == (30, 5)
    env.record(r3)  # no reported input: still kept (it may have billed output), but there is no input to extend from
    assert env.responses == [r1, r2, r3] and env.spent == 72 and env.last is None


def test_token_envelope_responses_feed_the_usage_of_a_stopped_decision():
    env = par.TokenEnvelope(limit=100_000, cap=1000)
    env.record(response(10, 20))
    env.record(response(30, 5))
    u = par.usage_from_messages(env.responses, "openai-compatible", "fake")
    assert u["calls"] == 2 and u["input_tokens"] == 40 and u["output_tokens"] == 25
