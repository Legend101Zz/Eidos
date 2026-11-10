"""Runaway reasoning (ADR 0019): a reasoning model that spends a request's whole output cap on reasoning and returns no
answer (``finish_reason="length"``, only reasoning or blank text; MiniMax-M3.1-Flash in pilot D12-r3: 16384 of 16384
output tokens) is re-asked ONCE within the decision, concisely and decision-only; a second runaway ends the decision in
the bounded no-effect outcome ``reasoning-runaway``. Every runaway request is counted and recorded.

The core tests serve RECORDED OpenAI-compatible ``chat.completion`` bodies (the shape the provider returned in D12-r3)
to a real ``OpenAIChatModel`` through ``httpx.MockTransport`` and assert on the request bodies the runtime sent. The
edge cases use ``FunctionModel``. Nothing here calls a provider or a network endpoint."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage

from eidos.domain.turns import validate_decision
from eidos.investigator.pydantic_ai_runtime import (
    REASONING_EFFORTS,
    RUNAWAY_NOTE,
    RUNAWAY_TOOL_NOTE,
    PydanticAIRuntime,
    is_runaway,
    runaway_record,
)
from eidos.investigator.runtime import (
    DecisionAbort,
    DecisionAborted,
    DecisionRequest,
    DecisionResult,
    ModelCallFailed,
)

pytestmark = pytest.mark.fixture_llm

CAP = 16384
MARK = "RUNAWAY-REASONING-0742cc"  # appears only in the reasoning of a runaway reply: never in what is sent back
GOOD = {"rationale": "baseline first", "purpose": "reproduction",
        "hypotheses": [{"statement": "h", "prediction": "p", "falsifier": "f"}],
        "calls": [{"tool": "run_component", "args": {"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}}}],
        "control": {"action": "wait_for_jobs", "summary": "wait"}}
PRICE = {"input": 1.0, "output": 2.0}


# -- recorded bodies -------------------------------------------------------------------------------------------------
REASONING = (MARK + ": Okay, let me think about what the investigator should do here. The question is why the "
             "baseline's validation accuracy is flat while the training loss falls. First I should check whether the "
             "split is leaking, then whether the labels in the validation split are shuffled, then whether the "
             "metric is computed on the right column. Let me reconsider the budget. Hmm, wait. Actually the "
             "evidence preview shows the predictions are constant, so maybe the head is dead. Let me go through "
             "each possibility again from the beginning, carefully, and enumerate the hypotheses one by one, and")

# The body the provider returned in pilot D12-r3 (MiniMax-M3.1-Flash, max_output_tokens 16384): the whole output cap
# spent on reasoning, empty content, finish_reason "length". The reasoning text is shortened here.
RECORDED_RUNAWAY: dict[str, Any] = {
    "id": "0742cc63ea2975169b93dbb76bc87d08", "object": "chat.completion", "created": 1778000000,
    "model": "MiniMax-M3.1-Flash",
    "choices": [{"index": 0, "finish_reason": "length",
                 "message": {"role": "assistant", "content": "", "reasoning_content": REASONING}}],
    "usage": {"prompt_tokens": 37196, "completion_tokens": 16384, "total_tokens": 53580,
              "completion_tokens_details": {"reasoning_tokens": 16384}}}

# How the same runaway can arrive from OpenAI-compatible endpoints (pydantic-ai raises for all but "whitespace")
RUNAWAY_VARIANTS = ["reasoning_content", "null-content", "think-tags", "no-reasoning"]


def runaway_body(kind: str = "reasoning_content", **usage: int) -> dict[str, Any]:
    body = copy.deepcopy(RECORDED_RUNAWAY)
    msg = body["choices"][0]["message"]
    if kind == "null-content":
        msg["content"] = None
    elif kind == "think-tags":  # reasoning inline as <think>...</think>, nothing after it
        del msg["reasoning_content"]
        msg["content"] = f"<think>{REASONING}</think>"
    elif kind == "no-reasoning":  # nothing at all: empty content, no reasoning field
        del msg["reasoning_content"]
    elif kind == "whitespace":  # blank but not empty text
        msg["content"] = "  \n"
    else:
        assert kind == "reasoning_content"
    body["usage"].update(usage)
    return body


def completion(message: dict[str, Any], finish: str = "stop", prompt: int = 37_300, out: int = 412,
               reasoning: int | None = 180, rid: str = "d0") -> dict[str, Any]:
    u: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out}
    if reasoning is not None:
        u["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"id": rid, "object": "chat.completion", "created": 1778000001, "model": "MiniMax-M3.1-Flash",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}], "usage": u}


def text_reply(text: str, **kw: Any) -> dict[str, Any]:
    return completion({"role": "assistant", "content": text}, **kw)


def read_request(*reads: tuple[str, dict], rationale: str = "check the evidence") -> str:
    return json.dumps({"rationale": rationale, "read": [{"tool": t, "args": a} for t, a in reads]})


def tool_call_reply(name: str, args: dict[str, Any] | str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("finish", "tool_calls")
    return completion({"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}]}, **kw)


DECISION = text_reply(json.dumps(GOOD), rid="d1")
READ = text_reply(read_request(("get_job", {"job_id": "job_0"})), prompt=36_000, out=90, reasoning=None, rid="r0")
TOOL_DECISION = tool_call_reply("final_result", {
    "rationale": "baseline first", "purpose": "reproduction",
    "calls": [{"tool": "run_component", "args_json": json.dumps(GOOD["calls"][0]["args"] | {"x": 1})}],
    "control": {"action": "wait_for_jobs", "summary": "wait"}}, rid="d2")
TOOL_READ = tool_call_reply("get_job", {"job_id": "job_0"}, prompt=36_000, out=90, reasoning=None, rid="r1")


class Wire:
    """Serves recorded bodies (in order; the last repeats; an int is an HTTP error status) to a real OpenAIChatModel
    over ``httpx.MockTransport`` and records the JSON body of every request."""

    def __init__(self, *replies: Any):
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.on_request: Any = None  # called with the 1-based request number once its body is recorded
        client = AsyncOpenAI(api_key="x", base_url="http://fake/v1", max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)))
        self.model = OpenAIChatModel("MiniMax-M3.1-Flash", provider=OpenAIProvider(openai_client=client))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.on_request:
            self.on_request(len(self.requests))
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "upstream failure", "type": "server_error"}})
        return httpx.Response(200, json=reply)


def text_of(content: Any) -> str:
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def last_user(body: dict[str, Any]) -> str:
    return text_of([m for m in body["messages"] if m["role"] == "user"][-1]["content"])


def roles(body: dict[str, Any]) -> list[str]:
    return [m["role"] for m in body["messages"]]


def schema_of(body: dict[str, Any]) -> dict[str, Any]:
    return body["response_format"]["json_schema"]["schema"]


def tool_names(body: dict[str, Any]) -> list[str]:
    return [t["function"]["name"] for t in body.get("tools", [])]


def dumped(body: dict[str, Any]) -> str:
    return json.dumps(body["messages"])


# -- shared helpers --------------------------------------------------------------------------------------------------
def request(tools: dict[str, Any] | None = None, **limits: Any) -> DecisionRequest:
    return DecisionRequest(turn_id="t1", slot=0, context="# Investigation\nQuestion: why?", turn_so_far=[],
                           limits={"max_calls": 4, **limits}, read_tools=tools or {})


def read_tools(log: list) -> dict[str, Any]:
    def get_job(job_id):
        log.append(job_id)
        return {"id": job_id, "status": "succeeded"}

    def inspect_evidence(artifact_id, max_bytes=8192):
        log.append(artifact_id)
        return {"preview": "ids,predictions\n0,3"}
    return {"get_job": get_job, "inspect_evidence": inspect_evidence}


def wire_runtime(wire: Wire, mode: str = "json", **cfg: Any) -> PydanticAIRuntime:
    return PydanticAIRuntime({"_model_object": wire.model, "model": "openai-compatible:MiniMax-M3.1-Flash",
                              "output_mode": mode, "max_output_tokens": CAP, **cfg})


def runaway_of(res: DecisionResult) -> list[tuple[int, bool]]:
    return [(r["request"], r["reask"]) for r in res.usage["structured_output"]["runaway"]]


def assert_bounded_runaway(res: DecisionResult) -> None:
    ctl = res.proposal["control"]
    assert ctl["bounded_outcome"] == "reasoning-runaway" and ctl["action"] == "wait_for_jobs"
    assert ctl["summary"] == "reasoning-runaway" and res.proposal["calls"] == []
    assert validate_decision(res.proposal, max_calls=4)["calls"] == []  # the lab accepts the no-effect decision
    assert res.usage["limit_reached"] and not res.usage["structured_output"].get("valid", False)


# FunctionModel scripts ---------------------------------------------------------------------------------------------
def runaway_response(out: int = CAP, inp: int = 3000) -> ModelResponse:
    return ModelResponse(parts=[ThinkingPart(REASONING)], model_name="m", finish_reason="length",
                         usage=RequestUsage(input_tokens=inp, output_tokens=out, details={"reasoning_tokens": out}))


TOOL = object()  # a scripted reply: the decision as the output tool call


def scripted(*replies: Any) -> SimpleNamespace:
    """A FunctionModel replying in order (the last repeats): a str is a text reply, ``TOOL`` the decision as an output
    tool call, a ModelResponse is used as is. Records every request's messages and AgentInfo."""
    seen: list[list] = []
    infos: list[AgentInfo] = []

    def fn(messages, info: AgentInfo):
        reply = replies[min(len(seen), len(replies) - 1)]
        seen.append(list(messages))
        infos.append(info)
        if isinstance(reply, ModelResponse):
            return reply
        usage = RequestUsage(input_tokens=10, output_tokens=20)
        if reply is TOOL:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"rationale": "r", "calls": []})],
                                 usage=usage)
        return ModelResponse(parts=[TextPart(reply)], usage=usage)
    return SimpleNamespace(model=FunctionModel(fn), seen=seen, infos=infos)


def fn_runtime(script: SimpleNamespace, mode: str = "repair", model: str = "openai-compatible:fake",
               **cfg: Any) -> PydanticAIRuntime:
    return PydanticAIRuntime({"_model_object": script.model, "model": model, "output_mode": mode,
                              "max_output_tokens": CAP, **cfg})


def prompt_of(messages: list) -> str:
    return str(messages[-1].parts[-1].content)


def every_text(messages: list) -> str:
    return ModelMessagesTypeAdapter.dump_json(messages).decode()


# -- is_runaway / runaway_record -------------------------------------------------------------------------------------
def _response(*parts: Any, finish: str | None = "length") -> ModelResponse:
    return ModelResponse(parts=list(parts), usage=RequestUsage(input_tokens=10, output_tokens=20), model_name="m",
                         finish_reason=finish)  # type: ignore[arg-type]


CALL = ToolCallPart("get_job", {"job_id": "j"})


@pytest.mark.parametrize("parts, finish, expected", [
    ([ThinkingPart("hmm")], "length", True),
    ([ThinkingPart("a"), ThinkingPart("b")], "length", True),
    ([TextPart("")], "length", True),
    ([TextPart("  \n\t")], "length", True),
    ([ThinkingPart("hmm"), TextPart("")], "length", True),
    ([ThinkingPart("hmm"), TextPart("   ")], "length", True),
    ([], "length", True),
    ([TextPart('{"rationale": "cut off mid')], "length", False),
    ([ThinkingPart("hmm"), TextPart("a partial answer")], "length", False),
    ([ThinkingPart("hmm"), TextPart(""), TextPart("x")], "length", False),
    ([CALL], "length", False),
    ([ThinkingPart("hmm"), CALL], "length", False),
    ([ThinkingPart("hmm")], "stop", False),
    ([ThinkingPart("hmm")], None, False),
    ([ThinkingPart("hmm")], "content_filter", False),
    ([ThinkingPart("hmm")], "tool_call", False),
    ([TextPart("")], "stop", False),
    ([], "stop", False),
    ([], None, False),
], ids=lambda v: v if isinstance(v, str) else None)
def test_is_runaway_truth_table(parts, finish, expected):
    assert is_runaway(_response(*parts, finish=finish)) is expected


@pytest.mark.parametrize("value", [None, "length", {"finish_reason": "length"}, ModelRequest(parts=[])])
def test_is_runaway_is_false_for_anything_that_is_not_a_model_response(value):
    assert is_runaway(value) is False


def test_runaway_record_names_the_request_its_tokens_and_its_id():
    r = ModelResponse(parts=[ThinkingPart("x")], model_name="m", finish_reason="length", provider_response_id="rid-1",
                      usage=RequestUsage(input_tokens=100, output_tokens=CAP, details={"reasoning_tokens": CAP}))
    assert runaway_record(r, 2, True) == {"request": 2, "reask": True, "finish_reason": "length",
                                          "output_tokens": CAP, "reasoning_tokens": CAP, "charged_output": CAP,
                                          "request_id": "rid-1"}
    # a provider that bills reasoning separately: the reasoning is charged on top of the reported output tokens
    separate = runaway_record(r, 1, False, "separate")
    assert separate["output_tokens"] == CAP and separate["charged_output"] == 2 * CAP
    no_details = ModelResponse(parts=[], model_name="m", finish_reason="length",
                               usage=RequestUsage(input_tokens=1, output_tokens=7))
    rec = runaway_record(no_details, 1, False)
    assert rec["reasoning_tokens"] is None and rec["charged_output"] == 7 and rec["request_id"] is None


# -- recorded runaway, JSON mode -------------------------------------------------------------------------------------
def test_recorded_runaway_in_json_mode_is_reasked_once_and_the_decision_accepted():
    log: list = []
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, runaway_reasoning_effort="low", price_per_mtok=PRICE).decide(request(read_tools(log)))
    first, reask = wire.requests
    assert len(wire.requests) == 2 and log == []
    assert res.proposal["calls"][0]["tool"] == "run_component"
    assert "bounded_outcome" not in (res.proposal.get("control") or {})
    # what was billed: both requests, including the runaway's whole output cap
    u = res.usage
    assert u["calls"] == 2 and u["input_tokens"] == 37_196 + 37_300 and u["output_tokens"] == CAP + 412
    assert [r["finish_reason"] for r in u["per_request"]] == ["length", "stop"]
    assert [r.get("reasoning_tokens") for r in u["per_request"]] == [CAP, 180]
    assert u["per_request"][0]["output_tokens"] == CAP and u["reasoning_tokens"] == CAP + 180
    assert u["reasoning_accounting"] == "included-in-output"
    assert u["request_ids"] == ["0742cc63ea2975169b93dbb76bc87d08", "d1"]
    assert u["served_models"] == ["MiniMax-M3.1-Flash"]
    assert u["usd"] == pytest.approx(((37_196 + 37_300) * 1.0 + (CAP + 412) * 2.0) / 1e6)
    # recorded in the decision's usage and settings
    rep = u["structured_output"]
    assert rep["runaway"] == [{"request": 1, "reask": False, "finish_reason": "length", "output_tokens": CAP,
                               "reasoning_tokens": CAP, "charged_output": CAP,
                               "request_id": "0742cc63ea2975169b93dbb76bc87d08"}]
    assert rep["runaway_reasks"] == 1 and rep["reasks"] == 0 and rep["valid"] and rep["errors"] == []
    assert res.model_settings["structured_output"]["runaway_reasks"] == 1
    assert res.model_settings["runaway_reasoning_effort"] == "low"
    # the re-ask: same output cap and system prompt, same last message plus the note, decision-only response schema
    assert first["max_completion_tokens"] == reask["max_completion_tokens"] == CAP
    assert roles(first) == roles(reask) == ["system", "user"] and first["messages"][0] == reask["messages"][0]
    assert last_user(reask) == last_user(first) + "\n\n" + RUNAWAY_NOTE.format(cap=CAP)
    assert "whole output limit (16384 tokens)" in last_user(reask) and "Do not request further reads" in last_user(reask)
    assert "read" in schema_of(first)["properties"] and "read" not in schema_of(reask)["properties"]
    assert schema_of(reask)["properties"].keys() == schema_of(first)["properties"].keys() - {"read"}
    assert schema_of(reask)["required"] == ["rationale"]
    assert reask["response_format"]["json_schema"]["name"] == "eidos_decision"
    # the reduced reasoning request is sent with the re-ask only
    assert "reasoning_effort" not in first and reask["reasoning_effort"] == "low"
    # the runaway reply is not sent back and is not in the recorded history; its tokens are only in the usage
    assert MARK not in dumped(reask) and MARK not in json.dumps(res.raw_response)


@pytest.mark.parametrize("kind", RUNAWAY_VARIANTS)
def test_every_recorded_runaway_shape_is_reasked_and_counted(kind):
    wire = Wire(runaway_body(kind), DECISION)
    res = wire_runtime(wire).decide(request())
    assert len(wire.requests) == 2 and res.proposal["calls"][0]["tool"] == "run_component"
    assert last_user(wire.requests[1]).endswith(RUNAWAY_NOTE.format(cap=CAP))
    assert MARK not in dumped(wire.requests[1]) and len(wire.requests[1]["messages"]) == 2
    rep = res.usage["structured_output"]
    assert rep["runaway_reasks"] == 1 and runaway_of(res) == [(1, False)]
    assert rep["runaway"][0]["output_tokens"] == CAP and rep["runaway"][0]["finish_reason"] == "length"
    assert res.usage["calls"] == 2 and res.usage["output_tokens"] == CAP + 412
    assert "reasoning_effort" not in wire.requests[1]  # opt-in: not configured, not sent


def test_a_whitespace_only_reply_at_the_cap_is_a_runaway_too():
    # pydantic-ai raises "token limit exceeded before any response" only for an EMPTY text part (or none); a reply of
    # whitespace at the cap reaches the runtime as text. It carries no answer either: the runtime treats it as the
    # same runaway (one concise re-ask), not as a malformed reply.
    wire = Wire(runaway_body("whitespace"), DECISION)
    res = wire_runtime(wire, max_tokens_per_decision=250_000).decide(request())
    rep = res.usage["structured_output"]
    assert runaway_of(res) == [(1, False)] and rep["runaway_reasks"] == 1 and rep["reasks"] == 0 and rep["valid"]
    assert rep["errors"] == [] and res.usage["calls"] == 2
    assert "used its whole output limit" in last_user(wire.requests[1])


def test_a_whitespace_only_reply_twice_is_the_bounded_runaway_outcome():
    wire = Wire(runaway_body("whitespace"), runaway_body("whitespace"))
    res = wire_runtime(wire, max_tokens_per_decision=250_000).decide(request())
    assert res.proposal["control"]["bounded_outcome"] == "reasoning-runaway" and res.proposal["calls"] == []
    assert runaway_of(res) == [(1, False), (2, True)] and res.usage["calls"] == 2


@pytest.mark.parametrize("kind", ["truncated", "truncated-after-reasoning"])
def test_a_truncated_answer_is_not_a_runaway_it_is_a_malformed_reply(kind):
    msg = {"role": "assistant", "content": '{"rationale": "the baseline is fine", "calls": [{"tool": "run_comp'}
    if kind == "truncated-after-reasoning":
        msg["reasoning_content"] = REASONING
    wire = Wire(completion(msg, finish="length", prompt=3000, out=CAP, reasoning=None), DECISION)
    res = wire_runtime(wire, runaway_reasoning_effort="low").decide(request())
    rep = res.usage["structured_output"]
    assert rep["runaway"] == [] and rep["runaway_reasks"] == 0 and rep["reasks"] == 1 and rep["valid"]
    assert len(wire.requests) == 2 and "Your reply could not be used" in last_user(wire.requests[1])
    assert RUNAWAY_NOTE.split(".")[0] not in last_user(wire.requests[1])
    assert "reasoning_effort" not in wire.requests[1]
    assert res.usage["per_request"][0]["finish_reason"] == "length"


def test_a_truncated_answer_twice_is_the_structured_output_outcome_not_the_runaway_one():
    truncated = completion({"role": "assistant", "content": '{"rationale": "cut'}, finish="length", prompt=3000,
                           out=CAP, reasoning=None)
    res = wire_runtime(Wire(truncated)).decide(request())
    assert res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
    assert res.usage["structured_output"]["runaway"] == [] and res.usage["calls"] == 2


# -- runaway twice, in the last request, after reads ------------------------------------------------------------------
def test_runaway_twice_ends_in_the_bounded_reasoning_runaway_outcome_with_both_requests_counted():
    wire = Wire(RECORDED_RUNAWAY)
    res = wire_runtime(wire, runaway_reasoning_effort="low").decide(request(read_tools([])))
    assert len(wire.requests) == 2  # never a third request
    assert_bounded_runaway(res)
    u = res.usage
    assert u["calls"] == 2 and u["output_tokens"] == 2 * CAP and u["input_tokens"] == 2 * 37_196
    assert u["reasoning_tokens"] == 2 * CAP and [r["finish_reason"] for r in u["per_request"]] == ["length"] * 2
    assert "twice" in u["limit_reached"] and str(CAP) in u["limit_reached"]
    assert runaway_of(res) == [(1, False), (2, True)] and u["structured_output"]["runaway_reasks"] == 1
    assert res.model_settings["structured_output"]["runaway_reasks"] == 1
    assert MARK not in json.dumps(res.raw_response)


def test_runaway_in_the_only_allowed_request_is_bounded_without_a_reask():
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, max_requests_per_decision=1).decide(request())
    assert len(wire.requests) == 1 and res.usage["calls"] == 1 and res.usage["output_tokens"] == CAP
    assert_bounded_runaway(res)
    assert "last of 1 requests" in res.usage["limit_reached"]
    assert runaway_of(res) == [(1, False)] and res.usage["structured_output"]["runaway_reasks"] == 0


def test_runaway_in_the_last_allowed_request_after_a_read_is_bounded_without_a_reask():
    log: list = []
    wire = Wire(READ, RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, max_requests_per_decision=2, max_tokens_per_decision=250_000).decide(
        request(read_tools(log)))
    assert len(wire.requests) == 2 and log == ["job_0"]  # the read was executed, then the runaway ended the decision
    assert_bounded_runaway(res)
    assert res.usage["calls"] == 2 and res.usage["output_tokens"] == 90 + CAP
    assert "last of 2 requests" in res.usage["limit_reached"]
    assert runaway_of(res) == [(2, False)] and res.usage["structured_output"]["runaway_reasks"] == 0
    assert res.usage["structured_output"]["read_requests"] == 1


def test_runaway_after_a_read_round_is_reasked_with_the_read_results_history_and_without_the_runaway():
    log: list = []
    wire = Wire(READ, RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, max_tokens_per_decision=250_000).decide(request(read_tools(log)))
    r1, r2, r3 = wire.requests
    assert len(wire.requests) == 3 and log == ["job_0"]
    assert res.proposal["calls"][0]["tool"] == "run_component"
    # request 2 sent the read results; the re-ask repeats it (same history, same message) plus the note
    assert roles(r2) == roles(r3) == ["system", "user", "assistant", "user"]
    assert last_user(r2).startswith("## Read results\n") and "7 model requests left" in last_user(r2)
    assert last_user(r3) == last_user(r2) + "\n\n" + RUNAWAY_NOTE.format(cap=CAP)
    assert r3["messages"][:-1] == r2["messages"][:-1]
    assert "read" in schema_of(r1)["properties"] and "read" in schema_of(r2)["properties"]
    assert "read" not in schema_of(r3)["properties"]
    assert MARK not in dumped(r3) and MARK not in json.dumps(res.raw_response)
    u = res.usage
    assert u["calls"] == 3 and u["output_tokens"] == 90 + CAP + 412 and u["input_tokens"] == 36_000 + 37_196 + 37_300
    rep = u["structured_output"]
    assert runaway_of(res) == [(2, False)] and rep["runaway_reasks"] == 1 and rep["read_requests"] == 1
    assert rep["reasks"] == 0 and rep["valid"] and [r["executed"] for r in rep["reads"]] == [True]


def test_runaway_reask_after_a_read_round_is_admitted_when_its_true_worst_case_fits_the_envelope():
    wire = Wire(READ, RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, max_tokens_per_decision=150_000).decide(request(read_tools([])))
    assert len(wire.requests) == 3 and res.proposal["calls"][0]["tool"] == "run_component"


def test_a_read_request_as_the_reply_to_a_runaway_reask_is_refused_not_executed_and_reasked_once():
    log: list = []
    wire = Wire(RECORDED_RUNAWAY, READ, DECISION)
    res = wire_runtime(wire, max_tokens_per_decision=250_000).decide(request(read_tools(log)))
    assert log == [] and len(wire.requests) == 3  # runaway, refused read request, schema re-ask
    rep = res.usage["structured_output"]
    assert rep["read_requests"] == 0 and rep["reads"] == [] and rep["runaway_reasks"] == 1 and rep["reasks"] == 1
    assert len(rep["errors"]) == 1 and "no read tools are available" in rep["errors"][0] and rep["valid"]
    assert "Your reply could not be used" in last_user(wire.requests[2])
    assert res.proposal["calls"][0]["tool"] == "run_component" and res.usage["calls"] == 3


def test_after_a_refused_read_request_the_schema_reask_is_a_normal_request_bounded_by_the_request_budget():
    # The decision-only scope belongs to the runaway re-ask alone: the schema re-ask that follows a refused read request
    # is an ordinary request (read property in the schema), so a model that keeps asking to read reads from then on,
    # within max_requests_per_decision.
    log: list = []
    seen: list[int] = []
    wire = Wire(RECORDED_RUNAWAY, READ)
    wire.on_request = lambda n: seen.append(len(log))
    res = wire_runtime(wire, max_requests_per_decision=4, max_tokens_per_decision=400_000).decide(
        request(read_tools(log)))
    assert len(wire.requests) == 4 and seen == [0, 0, 0, 1]  # request 2's read request was not executed
    assert "read" not in schema_of(wire.requests[1])["properties"] and "read" in schema_of(wire.requests[2])["properties"]
    assert res.proposal["control"]["bounded_outcome"] == "request-budget-exhausted" and res.usage["calls"] == 4
    assert log == ["job_0"] and runaway_of(res) == [(1, False)]


def test_runaway_reask_refused_by_the_token_envelope_is_the_bounded_runaway_outcome():
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, max_tokens_per_decision=60_000).decide(request())
    assert len(wire.requests) == 1  # 53580 charged + the re-ask's worst case (input estimate + 16384) > 60000
    assert_bounded_runaway(res)
    assert "token envelope" in res.usage["limit_reached"]
    assert res.usage["calls"] == 1 and res.usage["output_tokens"] == CAP
    assert runaway_of(res) == [(1, False)] and res.usage["structured_output"]["runaway_reasks"] == 0


def test_runaway_counts_against_the_request_budget_the_decision_may_still_use_the_last_request():
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, max_requests_per_decision=2).decide(request())
    assert len(wire.requests) == 2 and res.usage["calls"] == 2 and res.usage["structured_output"]["valid"]


def test_a_decision_deadline_after_a_runaway_keeps_the_runaway_billed_and_recorded():
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    # one more request (timeout 120s) could outlive a 100s deadline: no re-ask, but the runaway is not lost
    res = wire_runtime(wire, request_timeout=120).decide(request(deadline_seconds=100))
    assert len(wire.requests) == 1
    assert res.proposal["control"]["bounded_outcome"] == "decision-deadline" and res.proposal["calls"] == []
    assert res.usage["calls"] == 1 and res.usage["output_tokens"] == CAP
    assert runaway_of(res) == [(1, False)] and res.usage["structured_output"]["runaway_reasks"] == 0


# -- repair mode -----------------------------------------------------------------------------------------------------
def test_runaway_in_repair_mode_is_reasked_once_with_the_note_and_the_effort():
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    res = wire_runtime(wire, "repair", runaway_reasoning_effort="medium").decide(request(read_tools([])))
    first, reask = wire.requests
    assert len(wire.requests) == 2 and res.usage["structured_output"]["mode"] == "repair"
    assert "response_format" not in first and "response_format" not in reask  # repair mode has no constrained decoding
    assert "reasoning_effort" not in first and reask["reasoning_effort"] == "medium"
    assert last_user(reask) == last_user(first) + "\n\n" + RUNAWAY_NOTE.format(cap=CAP)
    assert MARK not in dumped(reask) and runaway_of(res) == [(1, False)]
    assert res.usage["structured_output"]["runaway_reasks"] == 1 and res.usage["calls"] == 2
    assert res.usage["structured_output"]["json_mode"] is False


def test_runaway_in_repair_mode_without_the_effort_option_sends_no_extra_field():
    wire = Wire(RECORDED_RUNAWAY, DECISION)
    wire_runtime(wire, "repair").decide(request())
    assert all(k not in wire.requests[1] for k in ("reasoning_effort", "response_format"))


def test_runaway_twice_in_repair_mode_is_bounded():
    wire = Wire(RECORDED_RUNAWAY)
    res = wire_runtime(wire, "repair").decide(request())
    assert len(wire.requests) == 2 and res.usage["calls"] == 2 and runaway_of(res) == [(1, False), (2, True)]
    assert_bounded_runaway(res)


# -- tool mode -------------------------------------------------------------------------------------------------------
def test_runaway_in_tool_mode_gets_one_final_request_without_read_tools_and_the_runaway_note():
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION)
    res = wire_runtime(wire, "tool", runaway_reasoning_effort="high").decide(request(read_tools([])))
    first, final = wire.requests
    assert len(wire.requests) == 2
    assert tool_names(first) == ["inspect_evidence", "search_components", "describe_component", "get_job",
                                 "final_result"]
    assert tool_names(final) == ["final_result"]  # no read tools in the final request
    assert last_user(final).endswith(RUNAWAY_TOOL_NOTE.format(cap=CAP)) and "No further reads" in last_user(final)
    assert "reasoning_effort" not in first and final["reasoning_effort"] == "high"
    assert final["max_completion_tokens"] == CAP and MARK not in dumped(final)
    assert res.proposal["calls"][0]["tool"] == "run_component" and res.proposal["calls"][0]["args"]["x"] == 1
    u = res.usage
    assert u["calls"] == 2 and u["output_tokens"] == CAP + 412 and u["input_tokens"] == 37_196 + 37_300
    assert [r["finish_reason"] for r in u["per_request"]] == ["length", "tool_call"]
    rep = u["structured_output"]
    assert rep["mode"] == "tool" and rep["runaway_reasks"] == 1 and runaway_of(res) == [(1, False)]
    assert rep["runaway"][0]["output_tokens"] == CAP and rep["runaway"][0]["reasoning_tokens"] == CAP
    assert res.model_settings["runaway_reasoning_effort"] == "high"
    assert res.model_settings["structured_output"]["runaway_reasks"] == 1
    assert MARK not in json.dumps(res.raw_response)


def test_runaway_in_tool_mode_without_the_effort_option_sends_no_reasoning_effort():
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION)
    res = wire_runtime(wire, "tool").decide(request())
    assert "reasoning_effort" not in wire.requests[1] and "runaway_reasoning_effort" not in res.model_settings


def test_runaway_in_tool_mode_after_a_read_round_keeps_the_tool_results_and_drops_the_runaway():
    log: list = []
    wire = Wire(TOOL_READ, RECORDED_RUNAWAY, TOOL_DECISION)
    res = wire_runtime(wire, "tool", max_tokens_per_decision=250_000).decide(request(read_tools(log)))
    r1, r2, final = wire.requests
    assert len(wire.requests) == 3 and log == ["job_0"]
    assert roles(r2) == ["system", "user", "assistant", "tool"]
    assert roles(final) == ["system", "user", "assistant", "tool", "user"]
    assert final["messages"][:4] == r2["messages"] and "succeeded" in final["messages"][3]["content"]
    assert last_user(final).endswith(RUNAWAY_TOOL_NOTE.format(cap=CAP)) and tool_names(final) == ["final_result"]
    assert MARK not in dumped(final)
    assert res.proposal["calls"][0]["tool"] == "run_component" and res.usage["calls"] == 3
    assert runaway_of(res) == [(2, False)] and res.usage["output_tokens"] == 90 + CAP + 412


def test_runaway_twice_in_tool_mode_is_bounded_and_both_requests_are_counted():
    wire = Wire(RECORDED_RUNAWAY)
    res = wire_runtime(wire, "tool").decide(request(read_tools([])))
    assert len(wire.requests) == 2
    assert_bounded_runaway(res)
    u = res.usage
    assert u["calls"] == 2 and u["output_tokens"] == 2 * CAP and u["input_tokens"] == 2 * 37_196
    assert "twice" in u["limit_reached"] and runaway_of(res) == [(1, False), (2, True)]
    assert u["structured_output"]["runaway_reasks"] == 1 and res.model_settings["structured_output"]["mode"] == "tool"
    assert MARK not in json.dumps(res.raw_response)


def test_runaway_in_the_only_allowed_tool_mode_request_is_bounded_without_a_final_request():
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION)
    res = wire_runtime(wire, "tool", max_requests_per_decision=1).decide(request(read_tools([])))
    assert len(wire.requests) == 1 and res.usage["calls"] == 1
    assert_bounded_runaway(res)
    assert "last of 1 requests" in res.usage["limit_reached"] and res.usage["structured_output"]["runaway_reasks"] == 0


def test_runaway_final_request_refused_by_the_token_envelope_is_the_bounded_runaway_outcome():
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION)
    res = wire_runtime(wire, "tool", max_tokens_per_decision=60_000).decide(request())
    assert len(wire.requests) == 1
    assert_bounded_runaway(res)
    assert "token envelope" in res.usage["limit_reached"]
    assert res.usage["calls"] == 1 and runaway_of(res) == [(1, False)]
    assert res.usage["structured_output"]["runaway_reasks"] == 0


@pytest.mark.parametrize("kind", RUNAWAY_VARIANTS)
def test_every_recorded_runaway_shape_is_recovered_in_tool_mode(kind):
    wire = Wire(runaway_body(kind), TOOL_DECISION)
    res = wire_runtime(wire, "tool").decide(request())
    assert len(wire.requests) == 2 and runaway_of(res) == [(1, False)]
    assert res.proposal["calls"][0]["tool"] == "run_component"


def test_a_tool_call_cut_off_by_the_output_cap_is_not_a_runaway_it_fails_as_before():
    cut = tool_call_reply("final_result", '{"rationale": "baseline", "calls": [{"tool": "run_comp', finish="length",
                          prompt=3000, out=CAP, reasoning=None)
    wire = Wire(cut)
    with pytest.raises(ModelCallFailed) as exc:
        wire_runtime(wire, "tool").decide(request())
    assert len(wire.requests) == 2 and exc.value.possible_usage["calls"] == 2  # pydantic-ai's one output retry
    assert "runaway" not in exc.value.possible_usage.get("structured_output", {})


# -- effort option ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("model, effort, expected", [
    ("openai:gpt-x", "low", "low"), ("openai-compatible:fake", "high", "high"),
    ("openai-compatible:fake", "minimal", "minimal"), ("openai-compatible:fake", "medium", "medium"),
    ("anthropic:claude-sonnet-5-5", "low", None), ("anthropic:claude-sonnet-5-5", "high", None),
    ("openai-compatible:fake", None, None), ("openai-compatible:fake", "", None),
])
def test_runaway_effort_is_sent_only_when_configured_and_only_to_openai_providers(model, effort, expected):
    cfg: dict[str, Any] = {"model": model, "output_mode": "json"}
    if effort is not None:
        cfg["runaway_reasoning_effort"] = effort
    assert PydanticAIRuntime(cfg)._runaway_effort() == expected


def test_runaway_effort_not_configured_is_none_for_every_provider():
    for model in ("openai:x", "openai-compatible:x", "anthropic:x", "test"):
        assert PydanticAIRuntime({"model": model})._runaway_effort() is None


@pytest.mark.parametrize("effort", ["extreme", "LOW", "none", "xhigh", 3])
def test_an_invalid_runaway_effort_is_refused_non_retryably(effort):
    with pytest.raises(ModelCallFailed, match="runaway_reasoning_effort must be one of") as exc:
        PydanticAIRuntime({"model": "openai-compatible:x", "runaway_reasoning_effort": effort})._runaway_effort()
    assert exc.value.retryable is False and all(e in str(exc.value) for e in REASONING_EFFORTS)


@pytest.mark.parametrize("mode", ["json", "repair"])
def test_an_invalid_effort_fails_a_text_mode_decision_before_any_request(mode):
    wire = Wire(DECISION)
    with pytest.raises(ModelCallFailed, match="runaway_reasoning_effort") as exc:
        wire_runtime(wire, mode, runaway_reasoning_effort="extreme").decide(request())
    assert exc.value.retryable is False and wire.requests == []


def test_an_invalid_effort_is_refused_before_any_request_in_tool_mode():
    # Validated up front (as in the text modes): a runaway request is never billed and then lost from the usage.
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION)
    with pytest.raises(ModelCallFailed, match="runaway_reasoning_effort") as exc:
        wire_runtime(wire, "tool", runaway_reasoning_effort="extreme").decide(request())
    assert exc.value.retryable is False and wire.requests == []


@pytest.mark.parametrize("mode, model, expected_extra", [
    ("repair", "openai-compatible:fake", {"reasoning_effort": "low"}),
    ("repair", "openai:fake", {"reasoning_effort": "low"}),
    ("repair", "anthropic:fake", None),
    ("json", "openai-compatible:fake", {"reasoning_effort": "low", "response_format": "decision-only"}),
    ("json", "anthropic:fake", None),
])
def test_settings_of_the_reask_carry_the_effort_only_for_openai_providers(mode, model, expected_extra):
    script = scripted(runaway_response(), json.dumps(GOOD))
    res = fn_runtime(script, mode, model, runaway_reasoning_effort="low").decide(request(read_tools([])))
    assert len(script.seen) == 2 and res.usage["structured_output"]["runaway_reasks"] == 1
    first, reask = script.infos[0].model_settings, script.infos[1].model_settings
    assert reask["max_tokens"] == first["max_tokens"] == CAP
    assert "reasoning_effort" not in (first.get("extra_body") or {})
    extra = reask.get("extra_body")
    if expected_extra is None:
        assert extra is None
        return
    assert extra["reasoning_effort"] == "low"
    if mode == "json":
        assert extra["response_format"]["json_schema"]["name"] == "eidos_decision"
        assert "read" not in extra["response_format"]["json_schema"]["schema"]["properties"]
        assert "read" in first["extra_body"]["response_format"]["json_schema"]["schema"]["properties"]
    else:
        assert set(extra) == {"reasoning_effort"} and "extra_body" not in first


def test_settings_of_the_json_mode_reask_without_the_effort_are_the_decision_only_schema():
    script = scripted(runaway_response(), json.dumps(GOOD))
    fn_runtime(script, "json").decide(request(read_tools([])))
    extra = script.infos[1].model_settings["extra_body"]
    assert set(extra) == {"response_format"} and "read" not in extra["response_format"]["json_schema"]["schema"][
        "properties"]


def test_the_effort_is_recorded_in_the_settings_when_configured():
    rt = PydanticAIRuntime({"model": "openai-compatible:fake", "runaway_reasoning_effort": "low"})
    assert rt._settings({}, 8)["runaway_reasoning_effort"] == "low"
    assert "runaway_reasoning_effort" not in PydanticAIRuntime({"model": "openai-compatible:fake"})._settings({}, 8)
    anthropic = PydanticAIRuntime({"model": "anthropic:x", "runaway_reasoning_effort": "low"})
    assert anthropic._settings({}, 8)["runaway_reasoning_effort"] == "low"  # recorded as configured, even if unused
    script = scripted(json.dumps(GOOD))
    res = fn_runtime(script, "repair", runaway_reasoning_effort="high").decide(request())
    assert res.model_settings["runaway_reasoning_effort"] == "high"


# -- runaway interplay with the schema re-ask and the request budget (FunctionModel) ----------------------------------
def test_a_runaway_then_a_malformed_reply_then_a_decision_uses_both_reasks():
    script = scripted(runaway_response(), "not json at all", json.dumps(GOOD))
    res = fn_runtime(script).decide(request())
    assert len(script.seen) == 3 and res.usage["calls"] == 3
    rep = res.usage["structured_output"]
    assert rep["runaway_reasks"] == 1 and rep["reasks"] == 1 and rep["valid"] and len(rep["errors"]) == 1
    assert "Your reply could not be used" in prompt_of(script.seen[2])
    assert res.proposal["calls"][0]["tool"] == "run_component"


def test_a_runaway_then_two_malformed_replies_is_bounded_invalid_not_runaway():
    script = scripted(runaway_response(), "not json at all")
    res = fn_runtime(script).decide(request())
    assert len(script.seen) == 3 and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
    assert res.usage["calls"] == 3 and res.usage["output_tokens"] == CAP + 40


def test_a_malformed_reply_after_a_runaway_with_no_request_left_is_bounded_invalid():
    script = scripted(runaway_response(), "not json at all", json.dumps(GOOD))
    res = fn_runtime(script, max_requests_per_decision=2).decide(request())
    assert len(script.seen) == 2 and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
    assert res.usage["calls"] == 2


def test_a_runaway_during_the_schema_reask_is_reasked_with_the_correction_and_the_note():
    script = scripted("not json at all", runaway_response(), json.dumps(GOOD))
    res = fn_runtime(script).decide(request())
    assert len(script.seen) == 3 and res.usage["calls"] == 3
    correction = prompt_of(script.seen[1])
    assert correction.startswith("Your reply could not be used")
    assert prompt_of(script.seen[2]) == correction + "\n\n" + RUNAWAY_NOTE.format(cap=CAP)
    assert MARK not in every_text(script.seen[2])  # the bad reply stays in the history, the runaway does not
    assert "not json at all" in every_text(script.seen[2])
    rep = res.usage["structured_output"]
    assert rep["reasks"] == 1 and rep["runaway_reasks"] == 1 and runaway_of(res) == [(2, False)] and rep["valid"]


def test_runaway_during_the_schema_reask_twice_is_bounded_runaway():
    script = scripted("not json at all", runaway_response())
    res = fn_runtime(script).decide(request())
    assert len(script.seen) == 3 and res.usage["calls"] == 3
    assert_bounded_runaway(res)
    assert runaway_of(res) == [(2, False), (3, True)]


def test_a_decision_that_is_a_read_request_after_a_runaway_is_refused_even_with_requests_left():
    log: list = []
    script = scripted(runaway_response(),
                      json.dumps({"rationale": "r", "read": [{"tool": "get_job", "args": {"job_id": "j"}}]}),
                      json.dumps(GOOD))
    res = fn_runtime(script, "json").decide(request(read_tools(log)))
    assert log == [] and res.usage["structured_output"]["read_requests"] == 0
    assert res.usage["structured_output"]["reasks"] == 1 and res.usage["structured_output"]["valid"]


def test_functionmodel_runaway_usage_carries_reported_reasoning_tokens():
    script = scripted(runaway_response(out=CAP, inp=3000), json.dumps(GOOD))
    res = fn_runtime(script, price_per_mtok=PRICE).decide(request())
    per = res.usage["per_request"]
    assert per[0]["finish_reason"] == "length" and per[0]["reasoning_tokens"] == CAP and per[0]["output_tokens"] == CAP
    assert res.usage["input_tokens"] == 3010 and res.usage["output_tokens"] == CAP + 20
    assert res.usage["usd"] == pytest.approx((3010 * 1.0 + (CAP + 20) * 2.0) / 1e6)


def test_reasoning_billed_separately_is_charged_for_the_runaway_and_recorded():
    # a provider that reports reasoning on top of the output (reasoning_tokens_billing "separate"): the runaway request
    # is charged output + reasoning, in the usage and in its record
    script = scripted(runaway_response(out=2000), json.dumps(GOOD))
    res = fn_runtime(script, reasoning_tokens_billing="separate", max_tokens_per_decision=200_000).decide(request())
    rec = res.usage["structured_output"]["runaway"][0]
    assert rec["output_tokens"] == 2000 and rec["reasoning_tokens"] == 2000 and rec["charged_output"] == 4000
    assert res.usage["output_tokens"] == 4000 + 20 and res.usage["reasoning_accounting"] == "added-to-output"


def test_runaway_tool_mode_functionmodel_twice_and_effort():
    script = scripted(runaway_response(), TOOL)
    res = fn_runtime(script, "tool", runaway_reasoning_effort="low").decide(request(read_tools([])))
    assert len(script.seen) == 2 and res.usage["calls"] == 2
    assert [i.function_tools for i in script.infos][1] == [] and script.infos[0].function_tools
    assert "extra_body" not in script.infos[0].model_settings
    assert script.infos[1].model_settings["extra_body"] == {"reasoning_effort": "low"}
    assert prompt_of(script.seen[1]) == RUNAWAY_TOOL_NOTE.format(cap=CAP)
    assert MARK not in every_text(script.seen[1])
    anthropic = scripted(runaway_response(), TOOL)
    fn_runtime(anthropic, "tool", "anthropic:fake", runaway_reasoning_effort="low").decide(request())
    assert "extra_body" not in anthropic.infos[1].model_settings


# -- other failures stay failures ------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
def test_a_server_error_is_still_a_retryable_failure_with_its_usage(mode):
    wire = Wire(500)
    with pytest.raises(ModelCallFailed) as exc:
        wire_runtime(wire, mode).decide(request())
    assert exc.value.retryable is True and len(wire.requests) == 1
    assert exc.value.possible_usage["calls"] == 0
    assert exc.value.possible_usage["failed_request"]["status_code"] == 500


@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
def test_a_client_error_is_still_a_non_retryable_failure(mode):
    with pytest.raises(ModelCallFailed) as exc:
        wire_runtime(Wire(400), mode).decide(request())
    assert exc.value.retryable is False and exc.value.possible_usage["failed_request"]["status_code"] == 400


@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
def test_a_server_error_on_the_runaway_reask_is_retryable_and_reports_the_runaway_request(mode):
    wire = Wire(RECORDED_RUNAWAY, 500)
    with pytest.raises(ModelCallFailed) as exc:
        wire_runtime(wire, mode).decide(request())
    usage = exc.value.possible_usage
    assert exc.value.retryable is True and len(wire.requests) == 2
    assert usage["calls"] == 1 and usage["output_tokens"] == CAP and usage["failed_request"]["status_code"] == 500
    assert [r["request"] for r in usage["structured_output"]["runaway"]] == [1]


@pytest.mark.parametrize("mode", ["json", "repair"])
def test_a_server_error_on_the_reask_after_a_read_round_reports_every_billed_request(mode):
    wire = Wire(READ, RECORDED_RUNAWAY, 500)
    with pytest.raises(ModelCallFailed) as exc:
        wire_runtime(wire, mode, max_tokens_per_decision=250_000).decide(request(read_tools([])))
    usage = exc.value.possible_usage
    assert usage["calls"] == 2 and usage["output_tokens"] == 90 + CAP and exc.value.retryable is True


# -- stop and deadline after a runaway -------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
def test_a_stop_after_a_runaway_prevents_the_reask_and_reports_the_runaway_usage(mode):
    abort = DecisionAbort()
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION if mode == "tool" else DECISION)
    wire.on_request = lambda n: abort.trigger("stop") if n == 1 else None
    req = request(read_tools([]))
    req.abort = abort
    with pytest.raises(DecisionAborted) as exc:
        wire_runtime(wire, mode).decide(req)
    assert len(wire.requests) == 1 and exc.value.reason == "stop" and exc.value.in_flight is False
    assert exc.value.possible_usage["calls"] == 1 and exc.value.possible_usage["output_tokens"] == CAP
    assert exc.value.possible_usage["aborted"]["reason"] == "stop"


@pytest.mark.parametrize("mode", ["json", "repair", "tool"])
def test_a_decision_deadline_after_a_runaway_is_the_deadline_outcome_with_the_runaway_billed(mode):
    abort = DecisionAbort()
    wire = Wire(RECORDED_RUNAWAY, TOOL_DECISION if mode == "tool" else DECISION)
    wire.on_request = lambda n: abort.trigger("decision-deadline") if n == 1 else None
    req = request(read_tools([]))
    req.abort = abort
    res = wire_runtime(wire, mode).decide(req)
    assert len(wire.requests) == 1 and res.proposal["control"]["bounded_outcome"] == "decision-deadline"
    assert res.proposal["calls"] == [] and res.usage["calls"] == 1 and res.usage["output_tokens"] == CAP
