"""Fresh-state repair (ADR 0023): a model reply that stays malformed after the one schema-guided re-ask is not answered
with an empty wait. The runtime asks again from a fresh state: the ORIGINAL request with an empty history, the malformed
replies not shown, plus a short correction note. Bounded by the config ``fresh_state_repairs`` (default 1), by
``max_requests_per_decision`` and by the decision's token envelope; every request is counted in usage, and the repair is
recorded in ``usage.structured_output.fresh_state_repairs`` / ``fresh_state``. Both output modes: the text modes
("repair", "json": ``_decide_text``) and the default tool mode (``_tool_fresh_state``).

Fixture models only: nothing here calls a provider or a network endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.domain.turns import validate_decision
from eidos.investigator import pydantic_ai_runtime as par
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionRequest, ModelCallFailed

pytestmark = pytest.mark.fixture_llm

GOOD = {"rationale": "baseline first", "purpose": "reproduction",
        "hypotheses": [{"statement": "h", "prediction": "p", "falsifier": "f"}],
        "calls": [{"tool": "run_component", "args": {"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}}}],
        "control": {"action": "wait_for_jobs", "summary": "wait"}}
# the same decision as a final_result tool call (tool mode encodes each call's args as a JSON string)
GOOD_TOOL = {"rationale": "baseline first", "purpose": "reproduction",
             "calls": [{"tool": "run_component",
                        "args_json": json.dumps({"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}})}],
             "control": {"action": "wait_for_jobs", "summary": "wait"}}
CONTEXT = "# Investigation\nQuestion: why?"
BOUNDED = "structured-output-invalid"
READ_NAMES = ["inspect_evidence", "search_components", "describe_component", "get_job"]


# -- helpers ---------------------------------------------------------------------------------------------------------
def request(tools: dict | None = None) -> DecisionRequest:
    return DecisionRequest(turn_id="t1", slot=0, context=CONTEXT, turn_so_far=[], limits={"max_calls": 4},
                           read_tools=tools if tools is not None else {})


def read_tools() -> dict:
    return {"inspect_evidence": lambda artifact_id, max_bytes=8192: {}, "search_components": lambda text="", capability=None: {},
            "describe_component": lambda component_ref: {}, "get_job": lambda job_id: {}}


def usage(inp: int = 10, out: int = 20) -> RequestUsage:
    return RequestUsage(input_tokens=inp, output_tokens=out)


def scripted(replies: list[Any], usages: list[RequestUsage] | None = None):
    """A FunctionModel replying in order (the last reply repeats); records every request's messages and info.
    A reply is a text, a dict (the final_result tool call with these arguments) or an exception to raise."""
    seen: list = []
    infos: list[AgentInfo] = []

    def fn(messages, info: AgentInfo):
        n = len(seen)
        seen.append(messages)
        infos.append(info)
        reply = replies[min(n, len(replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        u = usages[min(n, len(usages) - 1)] if usages else usage()
        part = ToolCallPart(info.output_tools[0].name, reply) if isinstance(reply, dict) else TextPart(reply)
        return ModelResponse(parts=[part], usage=u)
    return SimpleNamespace(model=FunctionModel(fn), seen=seen, infos=infos)


def runtime(script, mode: str = "repair", **cfg) -> PydanticAIRuntime:
    return PydanticAIRuntime({"_model_object": script.model, "model": "openai-compatible:fake", "output_mode": mode,
                              **cfg})


def prompt_of(messages) -> str:
    """The user prompt of the last request of one model call."""
    return str(messages[-1].parts[-1].content)


def is_fresh(messages) -> bool:
    """One message only: the request itself, no earlier request or reply."""
    return len(messages) == 1 and isinstance(messages[0], ModelRequest)


def replies_in(messages) -> list:
    return [m for m in messages if isinstance(m, ModelResponse)]


def so_of(res) -> dict:
    return res.usage["structured_output"]


def assert_bounded(res) -> None:
    """A bounded outcome has no proposal and reports every request."""
    assert res.proposal == {} and res.runtime_outcome == res.usage["runtime_outcome"] == BOUNDED
    assert res.usage["limit_reached"] and so_of(res)["valid"] is False


# -- 1. text mode: a fresh-state repair after the re-ask failed --------------------------------------------------------------
def test_text_mode_fresh_state_repair_recovers_after_the_reask_fails():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    res = runtime(script).decide(request())
    d = validate_decision(res.proposal, max_calls=4)
    assert d["calls"][0]["tool"] == "run_component" and d["rationale"] == "baseline first"
    assert len(script.seen) == 3 and res.usage["calls"] == 3
    rep = so_of(res)
    assert rep["fresh_state_repairs"] == 1 and rep["reasks"] == 1 and rep["valid"] is True
    assert rep["mode"] == "repair" and len(rep["errors"]) == 2
    assert res.model_settings["fresh_state_repairs"] == 1
    assert res.model_settings["structured_output"]["fresh_state_repairs"] == 1


def test_the_re_ask_keeps_the_history_but_the_fresh_request_does_not():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    runtime(script).decide(request())
    first, reask, fresh = script.seen
    assert is_fresh(first)
    assert len(first) == 1 and len(reask) == 3 and len(replies_in(reask)) == 1  # request, reply, re-ask
    assert "not json" in str(replies_in(reask)[0].parts[0].content)
    # the third request: the fresh request alone, with no earlier reply of the model in it
    assert is_fresh(fresh) and replies_in(fresh) == []
    text = json.dumps([str(p.content) for m in fresh for p in m.parts])
    assert "not json" not in text and "still not json" not in text
    assert "Your reply could not be used" not in prompt_of(fresh)  # nor the re-ask's own text


def test_the_fresh_request_is_the_original_prompt_plus_the_correction_note():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    res = runtime(script).decide(request())
    original, fresh = prompt_of(script.seen[0]), prompt_of(script.seen[2])
    assert CONTEXT in original and CONTEXT in fresh  # the original context
    assert fresh.startswith(original)  # byte for byte the original request ...
    note = fresh[len(original):]
    assert note.startswith("\n\n## Correction")  # ... and then only the note
    assert "## Correction" not in original and "## Correction" not in prompt_of(script.seen[1])
    error = so_of(res)["fresh_state"][0]["after_error"]
    assert note == "\n\n" + par.FRESH_STATE_NOTE.format(error=error)
    assert "fresh request" in note and "not shown" in note
    # the system instructions are those of the first request
    assert script.seen[2][0].instructions == script.seen[0][0].instructions


def test_the_fresh_state_repair_is_recorded_with_its_request_number_and_error():
    script = scripted(["not json", json.dumps({"purpose": "x", "calls": []}), json.dumps(GOOD)])
    res = runtime(script).decide(request())
    (rec,) = so_of(res)["fresh_state"]
    assert rec["request"] == 3 and "rationale" in rec["after_error"]  # the schema error that the re-ask failed with
    assert rec["after_error"] in prompt_of(script.seen[2])
    json.dumps(res.usage)  # the whole record travels in the ledger


def test_a_valid_reply_to_the_reask_needs_no_fresh_state_repair():
    script = scripted(["not json", json.dumps(GOOD)])
    res = runtime(script).decide(request())
    assert len(script.seen) == 2 and so_of(res)["fresh_state_repairs"] == 0 and so_of(res)["fresh_state"] == []
    assert so_of(res)["reasks"] == 1 and so_of(res)["valid"] is True


def test_json_mode_repairs_from_a_fresh_state_too():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    res = runtime(script, "json").decide(request())
    assert is_fresh(script.seen[2]) and so_of(res)["fresh_state_repairs"] == 1 and so_of(res)["mode"] == "json"
    # the constrained-decoding settings of the original request are kept on the fresh one
    assert script.infos[2].model_settings["extra_body"]["response_format"]["type"] == "json_schema"


# -- 2. text mode: bounded when every reply is malformed ---------------------------------------------------------------------
def test_four_malformed_replies_end_bounded_after_exactly_three_requests_with_the_default_config():
    script = scripted(["not json", "still not json", "nope", "never asked"])
    res = runtime(script).decide(request())
    assert len(script.seen) == 3  # 1 request + 1 re-ask + 1 fresh-state request; the fourth reply is never asked for
    assert par.DEFAULT_FRESH_STATE_REPAIRS == 1
    assert res.usage["calls"] == 3 and res.usage["input_tokens"] == 30 and res.usage["output_tokens"] == 60
    assert len(res.usage["per_request"]) == 3  # every request is counted, malformed or not
    rep = so_of(res)
    assert rep["reasks"] == 1 and rep["fresh_state_repairs"] == 1 and len(rep["errors"]) == 3 and not rep["valid"]
    assert_bounded(res)


def test_the_bounded_outcome_prices_every_request():
    script = scripted(["not json"], usages=[usage(100, 10), usage(200, 20), usage(300, 30)])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "openai-compatible:fake", "output_mode": "repair",
                             "price_per_mtok": {"input": 1.0, "output": 2.0}}).decide(request())
    assert res.usage["input_tokens"] == 600 and res.usage["output_tokens"] == 60
    assert res.usage["usd"] == pytest.approx((600 * 1.0 + 60 * 2.0) / 1e6)


# -- 3. the bound is the config ``fresh_state_repairs`` ----------------------------------------------------------------------
@pytest.mark.parametrize("n, requests", [(0, 2), (1, 3), (2, 4), (3, 5)])
def test_fresh_state_repairs_bounds_the_requests_when_every_reply_is_malformed(n, requests):
    script = scripted(["not json"])
    res = runtime(script, fresh_state_repairs=n).decide(request())
    assert len(script.seen) == requests == res.usage["calls"]  # n = 0 is the old behaviour: one re-ask, then bounded
    rep = so_of(res)
    assert rep["fresh_state_repairs"] == n and len(rep["fresh_state"]) == n and rep["reasks"] == 1
    assert [f["request"] for f in rep["fresh_state"]] == list(range(3, 3 + n))
    assert res.model_settings["fresh_state_repairs"] == n
    for i, messages in enumerate(script.seen):
        if i >= 2:  # a fresh-state request: no history, and the note appears once (notes never pile up)
            assert is_fresh(messages) and prompt_of(messages).count("## Correction") == 1
        else:
            assert "## Correction" not in prompt_of(messages)
    assert_bounded(res)


def test_without_fresh_state_repairs_the_old_behaviour_is_kept_exactly():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    res = runtime(script, fresh_state_repairs=0).decide(request())
    assert len(script.seen) == 2  # one re-ask and no more
    assert all("## Correction" not in prompt_of(m) for m in script.seen)
    assert so_of(res)["fresh_state_repairs"] == 0 and so_of(res)["fresh_state"] == []
    assert_bounded(res)


def test_two_fresh_state_repairs_can_still_recover_on_the_last_request():
    script = scripted(["not json", "still not json", "nope", json.dumps(GOOD)])
    res = runtime(script, fresh_state_repairs=2).decide(request())
    assert len(script.seen) == 4 and res.usage["calls"] == 4
    assert so_of(res)["fresh_state_repairs"] == 2 and so_of(res)["valid"] is True
    assert is_fresh(script.seen[2]) and is_fresh(script.seen[3])
    assert validate_decision(res.proposal, max_calls=4)["calls"][0]["tool"] == "run_component"
    assert [f["request"] for f in so_of(res)["fresh_state"]] == [3, 4]


def test_a_fresh_state_repair_is_made_only_after_a_malformed_reply():
    script = scripted([json.dumps(GOOD)])
    res = runtime(script, fresh_state_repairs=3).decide(request())
    assert len(script.seen) == 1 and so_of(res)["fresh_state_repairs"] == 0 and so_of(res)["reasks"] == 0


# -- 4. the repair respects the request budget and the token envelope --------------------------------------------------------------
@pytest.mark.parametrize("max_requests, config, requests, fresh", [
    (1, 1, 1, 0),  # no request left even for the re-ask
    (2, 1, 2, 0),  # request, re-ask: none left for a fresh-state request
    (3, 1, 3, 1),
    (3, 2, 3, 1),  # the config would allow two: the request budget allows one
    (4, 2, 4, 2),
    (8, 2, 4, 2),
])
def test_the_fresh_state_request_respects_max_requests_per_decision(max_requests, config, requests, fresh):
    script = scripted(["not json"])
    res = runtime(script, max_requests_per_decision=max_requests, fresh_state_repairs=config).decide(request())
    assert len(script.seen) == requests == res.usage["calls"]
    assert so_of(res)["fresh_state_repairs"] == fresh
    assert_bounded(res)


def test_with_a_budget_of_two_the_third_reply_is_never_asked_for():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    res = runtime(script, max_requests_per_decision=2).decide(request())
    assert len(script.seen) == 2 and res.proposal == {} and res.runtime_outcome == res.usage["runtime_outcome"] == BOUNDED
    assert so_of(res)["fresh_state"] == []


def test_with_a_budget_of_three_the_fresh_state_request_is_the_last_one_and_can_still_succeed():
    script = scripted(["not json", "still not json", json.dumps(GOOD)])
    res = runtime(script, max_requests_per_decision=3).decide(request())
    assert len(script.seen) == 3 and so_of(res)["valid"] is True and so_of(res)["fresh_state_repairs"] == 1
    assert "at most 3 model requests" in prompt_of(script.seen[2])  # the original budget text, unchanged


def envelope_for_refusing_the_fresh_request():
    """(limit, cap, probe) such that the first two requests are admitted and the fresh-state request is not: the limit is
    the worst case of the dearest of the first two requests, and the fresh one is dearer still (it is a whole request,
    not an increment on the reported usage)."""
    cap = 50
    probe = scripted(["not json"])
    rt = runtime(probe, max_output_tokens=cap)
    rt.decide(request())
    (a1, a2, a3) = rt.envelope.admitted[:3]
    limit = max(a1["spent_before"] + a1["input_estimate"], a2["spent_before"] + a2["input_estimate"]) + cap
    assert a3["spent_before"] + a3["input_estimate"] + cap > limit  # the premise of the tests below
    return limit, cap


def test_the_fresh_state_request_must_fit_the_token_envelope():
    limit, cap = envelope_for_refusing_the_fresh_request()
    script = scripted(["not json"])
    res = runtime(script, max_output_tokens=cap, max_tokens_per_decision=limit).decide(request())
    assert len(script.seen) == 2 and res.usage["calls"] == 2  # the fresh request was not started: nothing billed for it
    assert "token envelope" in res.usage["limit_reached"]
    assert res.proposal == {} and res.runtime_outcome == res.usage["runtime_outcome"] == BOUNDED


def test_a_fresh_state_repair_refused_by_the_token_envelope_is_not_counted():
    limit, cap = envelope_for_refusing_the_fresh_request()
    script = scripted(["not json"])
    res = runtime(script, max_output_tokens=cap, max_tokens_per_decision=limit).decide(request())
    assert len(script.seen) == 2
    assert so_of(res)["fresh_state_repairs"] == 0 and so_of(res)["fresh_state"] == []  # none was made


# -- 5. tool mode (the default output mode) ------------------------------------------------------------------------------------
def test_tool_mode_fresh_state_run_recovers_after_the_output_retries_fail():
    script = scripted(["garbage", "more garbage", GOOD_TOOL])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test"}).decide(request())
    assert res.model_settings["output_mode"] == "tool"
    d = validate_decision(res.proposal, max_calls=4)
    assert d["calls"][0]["tool"] == "run_component" and d["calls"][0]["args"]["inputs"] == {"train": "art_1"}
    rep = so_of(res)
    assert rep["fresh_state_repairs"] == 1 and rep["valid"] is True and rep["mode"] == "tool"
    assert [f["request"] for f in rep["fresh_state"]] == [3] and rep["errors"]
    assert res.usage["calls"] == len(script.seen) == 3
    assert res.model_settings["structured_output"]["fresh_state_repairs"] == 1
    # request 1, its output retry (history + retry prompt), then the fresh run's first request: a single message
    assert [len(m) for m in script.seen] == [1, 3, 1]
    fresh = script.seen[2]
    assert is_fresh(fresh) and replies_in(fresh) == []


def test_tool_mode_fresh_request_is_the_original_prompt_plus_the_correction_note():
    script = scripted(["garbage", "more garbage", GOOD_TOOL])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test"}).decide(request())
    original, fresh = prompt_of(script.seen[0]), prompt_of(script.seen[2])
    assert CONTEXT in original and fresh.startswith(original) and "## Correction" not in original
    error = so_of(res)["fresh_state"][0]["after_error"]
    assert fresh[len(original):] == "\n\n" + par.FRESH_STATE_NOTE.format(error=error)
    assert "garbage" not in fresh  # the malformed replies are not shown


def test_tool_mode_fresh_state_run_offers_no_read_tools():
    script = scripted(["garbage", "more garbage", GOOD_TOOL])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test"}).decide(request(read_tools()))
    assert [t.name for t in script.infos[0].function_tools] == READ_NAMES  # offered in the first run ...
    assert script.infos[2].function_tools == [] and script.infos[2].output_tools  # ... not in the fresh one
    assert so_of(res)["fresh_state_repairs"] == 1 and so_of(res)["valid"] is True


def test_tool_mode_fresh_state_run_gets_one_request_plus_the_output_retries():
    cfg = {"model": "function:test", "output_retries": 2}
    script = scripted(["garbage", "garbage", "garbage", GOOD_TOOL])
    res = PydanticAIRuntime({"_model_object": script.model, **cfg}).decide(request())
    assert [len(m) for m in script.seen] == [1, 3, 5, 1]  # three requests in the first run, then the fresh run
    assert res.usage["calls"] == 4 and so_of(res)["valid"] is True and so_of(res)["fresh_state_repairs"] == 1
    # the fresh run has its own output retries: its first reply may be garbage and its second one valid
    script = scripted(["garbage", "garbage", "garbage", "garbage", GOOD_TOOL])
    res = PydanticAIRuntime({"_model_object": script.model, **cfg}).decide(request())
    assert [len(m) for m in script.seen] == [1, 3, 5, 1, 3]
    assert res.usage["calls"] == 5 and so_of(res)["valid"] is True and so_of(res)["fresh_state_repairs"] == 1


def test_tool_mode_garbage_throughout_ends_bounded_without_an_exception():
    script = scripted(["garbage"])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test"}).decide(request())
    assert len(script.seen) == 4 == res.usage["calls"]  # the first run and one fresh run, each with one output retry
    assert [len(m) for m in script.seen] == [1, 3, 1, 3]
    rep = so_of(res)
    assert rep["fresh_state_repairs"] == 1 and len(rep["errors"]) == 2 and rep["mode"] == "tool"
    assert res.usage["input_tokens"] == 40 and res.usage["output_tokens"] == 80  # all four requests are billed
    assert "fresh-state repair" in res.usage["limit_reached"]
    assert_bounded(res)


def test_tool_mode_without_fresh_state_repairs_fails_after_the_output_retries():
    script = scripted(["garbage"])
    rt = PydanticAIRuntime({"_model_object": script.model, "model": "function:test", "fresh_state_repairs": 0})
    with pytest.raises(ModelCallFailed) as e:
        rt.decide(request())
    assert len(script.seen) == 2 and e.value.possible_usage["calls"] == 2  # the old behaviour: nothing is repaired


@pytest.mark.parametrize("max_requests, requests, fresh", [(2, 2, 0), (4, 4, 1), (5, 4, 1), (8, 4, 1)])
def test_tool_mode_fresh_state_run_respects_max_requests_per_decision(max_requests, requests, fresh):
    script = scripted(["garbage"])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test",
                             "max_requests_per_decision": max_requests}).decide(request())
    assert len(script.seen) == requests == res.usage["calls"]
    assert so_of(res)["fresh_state_repairs"] == fresh
    assert_bounded(res)


def test_tool_mode_with_one_request_left_for_the_fresh_run_ends_bounded_not_in_an_exception():
    script = scripted(["garbage"])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test",
                             "max_requests_per_decision": 3}).decide(request())
    assert len(script.seen) == 3 and res.usage["calls"] == 3
    assert_bounded(res)


def test_tool_mode_fresh_run_that_succeeds_with_its_only_request_is_valid():
    script = scripted(["garbage", "garbage", GOOD_TOOL])
    res = PydanticAIRuntime({"_model_object": script.model, "model": "function:test",
                             "max_requests_per_decision": 3}).decide(request())
    assert len(script.seen) == 3 and so_of(res)["valid"] is True and so_of(res)["fresh_state_repairs"] == 1


# -- provider failures during a fresh-state request are classified like any other (ADR 0023) ---------------------------------------
class Overloaded(Exception):
    status_code = 529
    response = SimpleNamespace(headers={"retry-after": "7"}, status_code=529)


@pytest.mark.parametrize("mode", ["repair", "tool"])
def test_a_provider_failure_in_the_fresh_state_request_is_classified_and_keeps_the_billed_usage(mode):
    script = scripted(["garbage", "more garbage", Overloaded("overloaded")])
    with pytest.raises(ModelCallFailed) as e:
        runtime(script, mode).decide(request())
    assert len(script.seen) == 3 and is_fresh(script.seen[2])
    assert e.value.retryable is True
    assert e.value.failure["kind"] == "transient" and e.value.failure["retry_after_seconds"] == 7.0
    assert e.value.possible_usage["calls"] == 2  # the two replies that were received; the failed request billed none
    assert e.value.possible_usage["structured_output"]["fresh_state_repairs"] == 1  # and the repair is on the record


@pytest.mark.parametrize("mode", ["repair", "tool"])
def test_a_quota_failure_in_the_fresh_state_request_is_not_retryable(mode):
    quota = ModelHTTPError(429, "minimax", {"base_resp": {"status_code": 2056, "status_msg": "usage limit exceeded"}})
    script = scripted(["garbage", "more garbage", quota])
    with pytest.raises(ModelCallFailed) as e:
        runtime(script, mode).decide(request())
    assert e.value.retryable is False and e.value.failure["kind"] == "quota-exhausted"
    assert e.value.possible_usage["failed_request"]["kind"] == "quota-exhausted"


# -- 6. bounded outcomes carry no model proposal --------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["repair", "json", "tool"])
def test_the_bounded_outcome_serializes_without_a_proposal(mode):
    script = scripted(["garbage"])
    res = runtime(script, mode, fresh_state_repairs=1).decide(request())
    assert res.proposal == {} and res.runtime_outcome == res.usage["runtime_outcome"] == BOUNDED
    assert json.loads(json.dumps(res.proposal)) == {}
    json.dumps(res.model_settings)
