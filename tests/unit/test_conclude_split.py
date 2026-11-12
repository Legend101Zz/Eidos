"""ADR 0042 arithmetic and real FunctionModel requests under an accelerated monotonic clock."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

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


def exercise(clock, mode, actions, *, window=253, tools=False, config=None):
    abort = DecisionAbort(window, clock=clock)
    seen, reads = [], []

    async def provider(messages, info):
        seen.append((deepcopy(messages), deepcopy(info)))
        action = actions[len(seen) - 1]
        if action in ("hang", "stop", "pause", "cancel"):
            if action != "hang":
                abort.trigger(action)
            if action == "hang":
                await clock.hang()
            await asyncio.Future()
        if action == "early-timeout":
            raise TimeoutError("provider timeout before the admitted timeout")
        if action == "runaway":
            return ModelResponse(parts=[TextPart("")], finish_reason="length",
                                 usage=RequestUsage(input_tokens=100, output_tokens=16_384))
        if isinstance(action, tuple) and action[0] == "read":
            clock.advance(action[1])
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
def test_hanging_final_attempt_falls_back_with_uncertain_usage(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang", "valid"],
                                   config={"runaway_reasoning_effort": "low", "price_per_mtok": {"input": 1, "output": 2}})
    out = rt.decide(req)
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "conclude"
    report = out.usage["structured_output"]
    assert report["time_split"] == {"window_s": 253, "read_slot_s": 30, "fallback_reserve_s": pytest.approx(75.9),
                                    "final_timeout_s": pytest.approx(175.1), "armed": True,
                                    "requests": [{"request": 1, "timeout_s": pytest.approx(175.1), "armed": True},
                                                 {"request": 2, "timeout_s": pytest.approx(75.9), "armed": False}]}
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
def test_both_requests_hang_without_fabricating_a_proposal(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang", "hang"])
    out = rt.decide(req)
    assert out.runtime_outcome == "deadline-exhausted" and not out.proposal and len(seen) == 2
    assert out.usage["structured_output"]["fallback_outcome"] == "deadline-exhausted"
    assert out.usage["in_flight_estimate"]["output_tokens"] == 2 * 16_384
    assert out.usage["in_flight_estimate"]["input_tokens"] == sum(a["input_estimate"] for a in rt.envelope.admitted)


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_first_attempt_returns_without_fallback(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["valid"])
    out = rt.decide(req)
    report = out.usage["structured_output"]
    assert report["time_split"]["armed"] is True and report["fallback_used"] is False
    assert not report["final_attempt_timed_out"] and len(seen) == out.usage["calls"] == 1
    assert not out.usage.get("uncertain")


@pytest.mark.parametrize("mode", ["json", "tool"])
@pytest.mark.parametrize("reason", ["stop", "pause", "cancel"])
def test_abort_during_first_attempt_never_falls_back(conclude_clock, mode, reason):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, [reason])
    with pytest.raises(DecisionAborted) as caught:
        rt.decide(req)
    assert caught.value.reason == reason and len(seen) == 1
    assert caught.value.possible_usage["structured_output"]["fallback_used"] is False
    assert caught.value.possible_usage["in_flight_estimate"]["output_tokens"] == 16_384


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_small_window_uses_one_clipped_attempt(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang"], window=100)
    out = rt.decide(req)
    assert out.runtime_outcome == "deadline-exhausted" and len(seen) == 1
    assert seen[0][1].model_settings["timeout"] == 98
    assert out.usage["structured_output"]["time_split"]["armed"] is False


@pytest.mark.parametrize("mode", ["json", "tool"])
@pytest.mark.parametrize("actions", [["runaway", "valid"], ["runaway", "hang"], ["hang", "runaway"],
                                      [("read", 5), ("read", 5.7), "runaway", "valid"]])
def test_timeout_and_runaway_share_one_extra_terminal_request(conclude_clock, mode, actions):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, actions, tools=isinstance(actions[0], tuple))
    out = rt.decide(req)
    assert len(seen) == len(actions)
    report = out.usage["structured_output"]
    assert int(report.get("runaway_reasks", 0)) + int(report["fallback_used"]) == 1
    if actions[-1] == "valid":
        assert out.proposal["control"]["action"] == "conclude"
    else:
        assert out.runtime_outcome in ("deadline-exhausted", "reasoning-runaway") and not out.proposal


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_expired_read_slot_forfeits_second_round(conclude_clock, mode):
    rt, req, seen, reads, _ = exercise(conclude_clock, mode, [("read", 30), "valid"], tools=True)
    out = rt.decide(req)
    assert len(seen) == 2 and len(reads) == 4 and not seen[1][1].function_tools
    assert out.proposal["control"]["action"] == "conclude"
    assert out.usage["structured_output"]["read_rounds_forfeited"] is True
    assert seen[1][1].model_settings["timeout"] == pytest.approx(145.1)
    assert "1 finalization read round(s) left" not in str(seen[1][0])


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_fallback_malformed_reply_uses_only_existing_correction(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang", "malformed", "valid"])
    out = rt.decide(req)
    assert len(seen) == 3 and out.proposal["control"]["action"] == "conclude"
    assert out.usage["structured_output"]["fallback_outcome"] == "valid"


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_early_provider_timeout_does_not_trigger_fallback(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["early-timeout"])
    with pytest.raises(ModelCallFailed) as caught:
        rt.decide(req)
    assert not isinstance(caught.value, DecisionAborted) and len(seen) == 1
    assert not caught.value.possible_usage["structured_output"]["fallback_used"]


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_fallback_must_fit_original_token_envelope(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang"], config={"max_tokens_per_decision": 30_000})
    out = rt.decide(req)
    assert not out.proposal and out.runtime_outcome == "request-budget-exhausted" and len(seen) == 1
    assert out.usage["uncertain"] and out.usage["in_flight_estimate"]["output_tokens"] == 16_384
    report = out.usage["structured_output"]
    assert not report["fallback_used"] and report["final_attempt_timed_out"]
    assert report["fallback_outcome"] == "refused"


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_fallback_uses_all_remaining_time_beyond_configured_request_timeout(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang", "valid"],
                                   config={"request_timeout": 120})
    out = rt.decide(req)
    assert out.proposal["control"]["action"] == "conclude"
    assert seen[0][1].model_settings["timeout"] == 120
    assert seen[1][1].model_settings["timeout"] == 131
    assert "usable request budget: 131.000" in seen[1][1].instructions
    assert "reasoning_effort" not in seen[1][1].model_settings.get("extra_body", {})


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_request_budget_counts_the_abandoned_attempt(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang", "malformed"],
                                   config={"max_requests_per_decision": 2})
    out = rt.decide(req)
    assert not out.proposal and len(seen) == len(rt.envelope.admitted) == 2
    assert out.runtime_outcome in ("structured-output-invalid", "request-budget-exhausted")
    assert out.usage["structured_output"]["fallback_used"]


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_provider_error_in_fallback_preserves_its_outcome_and_abandoned_usage(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang", "early-timeout"])
    with pytest.raises(ModelCallFailed) as caught:
        rt.decide(req)
    usage = caught.value.possible_usage
    assert usage["structured_output"]["fallback_outcome"] == "provider-failed" and len(seen) == 2
    assert usage["in_flight_estimate"]["output_tokens"] == 16_384


@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
@pytest.mark.parametrize("actions", [["hang", "valid"], [("read", 5), "hang", "valid"]])
def test_slow_read_capable_conclusion_gets_decision_only_fallback(conclude_clock, mode, actions):
    rt, req, seen, reads, clock = exercise(conclude_clock, mode, actions, tools=True)
    out = rt.decide(req)
    report = out.usage["structured_output"]
    assert out.proposal["control"]["action"] == "conclude" and report["fallback_outcome"] == "valid"
    assert len(seen) == len(actions) and len(reads) == 4 * (len(actions) - 2)
    requests = report["time_split"]["requests"]
    assert all(entry["armed"] for entry in requests[:-1]) and requests[-1]["armed"] is False
    assert report["fallback_used"] and report["final_attempt_timed_out"] and clock.cancelled == 1
    assert len(rt.envelope.abandoned) == 1 and out.usage["in_flight_estimate"]["output_tokens"] == 16_384
    for index, (_, info) in enumerate(seen):
        assert f"usable request budget: {requests[index]['timeout_s']:.3f}" in info.instructions
        assert info.model_settings["max_tokens"] == 16_384
        if mode == "tool":
            assert bool(info.function_tools) is (index < len(seen) - 1)
        elif mode == "json":
            schema = info.model_settings["extra_body"]["response_format"]["json_schema"]["schema"]
            assert ("read" in schema["properties"]) is (index < len(seen) - 1)
    assert FINALIZATION_FALLBACK_NOTE in str(seen[-1][0])


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_envelope_refusal_does_not_report_an_armed_request(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, [], config={"max_tokens_per_decision": 12_000})
    out = rt.decide(req)
    split = out.usage["structured_output"]["time_split"]
    assert not seen and not rt.envelope.admitted
    assert split["armed"] is False and split["final_timeout_s"] is None and split["requests"] == []


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_request_budget_refuses_fallback_explicitly(conclude_clock, mode):
    rt, req, seen, _, _ = exercise(conclude_clock, mode, ["hang"], config={"max_requests_per_decision": 1})
    out = rt.decide(req)
    report = out.usage["structured_output"]
    assert len(seen) == 1 and out.runtime_outcome == "request-budget-exhausted"
    assert report["final_attempt_timed_out"] and not report["fallback_used"]
    assert report["fallback_outcome"] == "refused"


@pytest.mark.parametrize("mode", ["json", "tool"])
def test_stop_at_split_timeout_never_starts_fallback(conclude_clock, mode):
    rt, req, seen, _, clock = exercise(conclude_clock, mode, ["hang"], tools=True)
    clock.on_advance = lambda seconds: req.abort.trigger("stop")
    with pytest.raises(DecisionAborted) as caught:
        rt.decide(req)
    assert caught.value.reason == "stop" and len(seen) == 1 and clock.cancelled == 1
    assert caught.value.possible_usage["in_flight_estimate"]["output_tokens"] == 16_384
    assert not caught.value.possible_usage["structured_output"]["fallback_used"]


@pytest.mark.parametrize("report", [{"errors": ["\\\"" * 20_000] * 4}, {"reads": ["λ" * 20_000]},
                                  {"time_split": {"armed": True}, "errors": []}])
def test_ledger_structured_output_copy_has_a_hard_size_cap(report):
    from eidos.application.handlers.investigations import (
        STRUCTURED_OUTPUT_DETAIL_BYTES,
        _structured_output_detail,
    )

    detail = _structured_output_detail(report)
    assert len(json.dumps(detail).encode()) <= STRUCTURED_OUTPUT_DETAIL_BYTES
    if len(json.dumps(report).encode()) <= STRUCTURED_OUTPUT_DETAIL_BYTES:
        assert detail == report
    else:
        assert detail["truncated"] is True
