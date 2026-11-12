"""ADR 0042 arithmetic and real FunctionModel requests under an accelerated monotonic clock."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.investigator import pydantic_ai_runtime as runtime_module
from eidos.investigator.conclude_split import ConcludeSplit
from eidos.investigator.pydantic_ai_runtime import FINALIZATION_FALLBACK_NOTE, PydanticAIRuntime
from eidos.investigator.runtime import DecisionAbort, DecisionAborted, DecisionRequest, ModelCallFailed

pytestmark = pytest.mark.fixture_llm
CONCLUSION = {"rationale": "Visible evidence only.", "calls": [],
              "control": {"action": "conclude", "summary": "Synthetic result, unverified."}}


@pytest.mark.parametrize("attempt,armed", [(59, False), (60, True), (61, True)])
def test_minimum_final_attempt(attempt, armed):
    split = ConcludeSplit(200)
    timeout, actual = split.final_attempt(attempt + 60 + 2, 300)
    assert actual is armed
    assert timeout == (attempt if armed else attempt + 60)


@pytest.mark.parametrize("elapsed,allowed", [(23.999, True), (24, False), (24.001, False)])
def test_read_slot_is_half_open(elapsed, allowed):
    assert ConcludeSplit(200).reads_allowed(elapsed) is allowed


@pytest.mark.parametrize("window,reads,fallback", [(0, 10, 45), (30, 10, 45), (100, 12, 45),
                                                 (162, 19.44, 48.6), (253, 30, 75.9), (1000, 30, 120)])
def test_clamps_and_d02_windows(window, reads, fallback):
    split = ConcludeSplit(window)
    assert split.read_slot_s == pytest.approx(reads)
    assert split.fallback_reserve_s == pytest.approx(fallback)
    timeout, armed = split.final_attempt(window - 10.7, 300)
    assert armed is (window in (162, 253, 1000))
    if armed:
        assert timeout == pytest.approx(min(300, window - 10.7 - fallback - 2))


class FastClock:
    """Real hanging coroutines are cancelled by wait_for after milliseconds, then virtual time advances."""

    def __init__(self, monkeypatch):
        self.now = 0.0
        real_wait_for = asyncio.wait_for

        async def accelerated(awaitable, timeout):
            if timeout is None or timeout < 1:
                return await real_wait_for(awaitable, timeout)
            task = asyncio.ensure_future(awaitable)
            try:
                return await real_wait_for(task, 0.01)
            except TimeoutError:
                if task.cancelled():
                    self.now += timeout
                raise

        monkeypatch.setattr(runtime_module, "ABORT_POLL_SECONDS", 0.001)
        monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(asyncio, "wait_for", accelerated)

    def __call__(self):
        return self.now


def exercise(monkeypatch, mode, actions, *, window=253, tools=False, config=None):
    clock = FastClock(monkeypatch)
    abort = DecisionAbort(window, clock=clock)
    seen, reads = [], []

    async def provider(messages, info):
        seen.append((deepcopy(messages), deepcopy(info)))
        action = actions[len(seen) - 1]
        if action in ("hang", "stop", "pause", "cancel"):
            if action != "hang":
                abort.trigger(action)
            await asyncio.Future()
        if action == "early-timeout":
            raise TimeoutError("provider timeout before the admitted timeout")
        if action == "runaway":
            return ModelResponse(parts=[TextPart("")], finish_reason="length",
                                 usage=RequestUsage(input_tokens=100, output_tokens=16_384))
        if isinstance(action, tuple) and action[0] == "read":
            clock.now += action[1]
            value = {"rationale": "Read visible receipts.", "read": [
                {"tool": "get_job", "args": {"job_id": str(i)}} for i in range(4)]}
            parts = [ToolCallPart("get_job", {"job_id": str(i)}) for i in range(4)] if mode == "tool" else [
                TextPart(json.dumps(value))]
        else:
            value = CONCLUSION if action == "valid" else {"rationale": "malformed", "control": {"action": "wait_for_jobs"}}
            parts = [ToolCallPart(info.output_tools[0].name, value)] if mode == "tool" else [
                TextPart(json.dumps(value))]
        return ModelResponse(parts=parts, usage=RequestUsage(input_tokens=100, output_tokens=20))

    runtime = PydanticAIRuntime({"_model_object": FunctionModel(provider), "model": "openai-compatible:fixture",
        "output_mode": mode, "request_timeout": 300, "max_output_tokens": 16_384,
        "max_tokens_per_decision": 262_144, **(config or {})})
    req = DecisionRequest("t", 0, "Visible evidence.", [], {}, abort=abort, conclude_only=True,
                          read_tools={"get_job": lambda jid: reads.append(jid) or {"id": jid}} if tools else {})
    return runtime, req, seen, reads, clock


@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
def test_hanging_final_attempt_falls_back_with_uncertain_usage(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang", "valid"],
                                   config={"runaway_reasoning_effort": "low", "price_per_mtok": {"input": 1, "output": 2}})
    out = rt.decide(req)
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "conclude"
    report = out.usage["structured_output"]
    assert report["time_split"] == {"window_s": 253, "read_slot_s": 30, "fallback_reserve_s": pytest.approx(75.9),
                                    "final_timeout_s": pytest.approx(175.1), "armed": True}
    assert report["final_attempt_timed_out"] and report["fallback_used"] and report["fallback_outcome"] == "valid"
    assert len(seen) == len(rt.envelope.admitted) == 2 and out.usage["calls"] == 1
    assert out.usage["uncertain"] is True and out.usage["input_tokens"] == 100
    estimate = out.usage["in_flight_estimate"]
    assert estimate["input_tokens"] == rt.envelope.admitted[0]["input_estimate"] > 0
    assert estimate["output_tokens"] == 16_384 and estimate["usd"] > 0
    assert rt.envelope.admitted[1]["spent_before"] == estimate["input_tokens"] + 16_384
    assert FINALIZATION_FALLBACK_NOTE in str(seen[1][0]) and FINALIZATION_FALLBACK_NOTE in str(out.raw_response)
    for index, (_, info) in enumerate(seen):
        timeout = info.model_settings["timeout"]
        assert info.model_settings["max_tokens"] == 16_384 and not info.function_tools
        assert f"usable request budget: {timeout:.3f}" in info.instructions, (index, info.instructions)
    assert seen[1][1].model_settings["extra_body"]["reasoning_effort"] == "low"
    if mode == "json":
        schema = seen[1][1].model_settings["extra_body"]["response_format"]["json_schema"]["schema"]
        assert "read" not in schema["properties"]
    assert out.model_settings["structured_output"] == report


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_both_requests_hang_without_fabricating_a_proposal(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang", "hang"])
    out = rt.decide(req)
    assert out.runtime_outcome == "deadline-exhausted" and not out.proposal and len(seen) == 2
    assert out.usage["structured_output"]["fallback_outcome"] == "deadline-exhausted"
    assert out.usage["in_flight_estimate"]["output_tokens"] == 2 * 16_384
    assert out.usage["in_flight_estimate"]["input_tokens"] == sum(a["input_estimate"] for a in rt.envelope.admitted)


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_first_attempt_returns_without_fallback(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["valid"])
    out = rt.decide(req)
    report = out.usage["structured_output"]
    assert report["time_split"]["armed"] is True and report["fallback_used"] is False
    assert not report["final_attempt_timed_out"] and len(seen) == out.usage["calls"] == 1
    assert not out.usage.get("uncertain")


@pytest.mark.parametrize("mode", ["json", "tool"])
@pytest.mark.parametrize("reason", ["stop", "pause", "cancel"])
def test_abort_during_first_attempt_never_falls_back(monkeypatch, mode, reason):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, [reason])
    with pytest.raises(DecisionAborted) as caught:
        rt.decide(req)
    assert caught.value.reason == reason and len(seen) == 1
    assert caught.value.possible_usage["structured_output"]["fallback_used"] is False
    assert caught.value.possible_usage["in_flight_estimate"]["output_tokens"] == 16_384


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_small_window_uses_one_clipped_attempt(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang"], window=100)
    out = rt.decide(req)
    assert out.runtime_outcome == "deadline-exhausted" and len(seen) == 1
    assert seen[0][1].model_settings["timeout"] == 98
    assert out.usage["structured_output"]["time_split"]["armed"] is False


@pytest.mark.parametrize("mode", ["json", "tool"])
@pytest.mark.parametrize("actions", [["runaway", "valid"], ["runaway", "hang"], ["hang", "runaway"],
                                      [("read", 5), ("read", 5.7), "runaway", "valid"]])
def test_timeout_and_runaway_share_one_extra_terminal_request(monkeypatch, mode, actions):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, actions, tools=isinstance(actions[0], tuple))
    out = rt.decide(req)
    assert len(seen) == len(actions)
    report = out.usage["structured_output"]
    assert int(report.get("runaway_reasks", 0)) + int(report["fallback_used"]) == 1
    if actions[-1] == "valid":
        assert out.proposal["control"]["action"] == "conclude"
    else:
        assert out.runtime_outcome in ("deadline-exhausted", "reasoning-runaway") and not out.proposal


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_expired_read_slot_forfeits_second_round(monkeypatch, mode):
    rt, req, seen, reads, _ = exercise(monkeypatch, mode, [("read", 30), "valid"], tools=True)
    out = rt.decide(req)
    assert len(seen) == 2 and len(reads) == 4 and not seen[1][1].function_tools
    assert out.proposal["control"]["action"] == "conclude"
    assert out.usage["structured_output"]["read_rounds_forfeited"] is True
    assert seen[1][1].model_settings["timeout"] == pytest.approx(145.1)


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_fallback_malformed_reply_uses_only_existing_correction(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang", "malformed", "valid"])
    out = rt.decide(req)
    assert len(seen) == 3 and out.proposal["control"]["action"] == "conclude"
    assert out.usage["structured_output"]["fallback_outcome"] == "valid"


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_early_provider_timeout_does_not_trigger_fallback(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["early-timeout"])
    with pytest.raises(ModelCallFailed) as caught:
        rt.decide(req)
    assert not isinstance(caught.value, DecisionAborted) and len(seen) == 1
    assert not caught.value.possible_usage["structured_output"]["fallback_used"]


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_fallback_must_fit_original_token_envelope(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang"], config={"max_tokens_per_decision": 30_000})
    out = rt.decide(req)
    assert not out.proposal and out.runtime_outcome == "request-budget-exhausted" and len(seen) == 1
    assert out.usage["uncertain"] and out.usage["in_flight_estimate"]["output_tokens"] == 16_384
    assert not out.usage["structured_output"]["fallback_used"]


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_fallback_uses_all_remaining_time_beyond_configured_request_timeout(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang", "valid"],
                                   config={"request_timeout": 120})
    out = rt.decide(req)
    assert out.proposal["control"]["action"] == "conclude"
    assert seen[0][1].model_settings["timeout"] == 120
    assert seen[1][1].model_settings["timeout"] == 131
    assert "usable request budget: 131.000" in seen[1][1].instructions
    assert "reasoning_effort" not in seen[1][1].model_settings.get("extra_body", {})


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_request_budget_counts_the_abandoned_attempt(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang", "malformed"],
                                   config={"max_requests_per_decision": 2})
    out = rt.decide(req)
    assert not out.proposal and len(seen) == len(rt.envelope.admitted) == 2
    assert out.runtime_outcome in ("structured-output-invalid", "request-budget-exhausted")
    assert out.usage["structured_output"]["fallback_used"]


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_provider_error_in_fallback_preserves_its_outcome_and_abandoned_usage(monkeypatch, mode):
    rt, req, seen, _, _ = exercise(monkeypatch, mode, ["hang", "early-timeout"])
    with pytest.raises(ModelCallFailed) as caught:
        rt.decide(req)
    usage = caught.value.possible_usage
    assert usage["structured_output"]["fallback_outcome"] == "provider-failed" and len(seen) == 2
    assert usage["in_flight_estimate"]["output_tokens"] == 16_384
