"""Pydantic AI adapter qualification against *fixture* models (FunctionModel/TestModel).
These are fixture-only: real-provider qualification is a separate, controller-run step."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from eidos.domain.turns import validate_decision
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionRequest, ModelCallFailed

pytestmark = pytest.mark.fixture_llm


def req(tools=None):
    return DecisionRequest(turn_id="t1", slot=0, context="# Investigation\nQuestion: why?", turn_so_far=[],
                           limits={"max_calls": 4}, read_tools=tools or {})


def test_reads_tool_then_returns_valid_decision():
    seen = []
    tools = {"search_components": lambda text="", capability=None: seen.append(text) or {"components": [{"ref": "a@1.0.0"}]},
             "inspect_evidence": lambda a, m=8192: {}, "describe_component": lambda r: {}, "get_job": lambda j: {}}

    def fn(messages, info: AgentInfo):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search_components", {"text": "pair"})])
        out = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(out, {"rationale": "score pairs", "purpose": "measurement-development",
                                                       "hypotheses": [{"statement": "h", "prediction": "p", "falsifier": "f"}],
                                                       "calls": [{"tool": "run_component", "args_json": json.dumps({"component_ref": "a@1.0.0"})}]})])

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test"})
    res = rt.decide(req(tools))
    assert seen == ["pair"]
    assert res.usage["calls"] == 2 and res.prompt_revision == "investigator-prompt/1"
    assert res.model_settings["transport_retries"] == 0
    d = validate_decision(res.proposal, max_calls=4)
    assert d["calls"][0]["tool"] == "run_component" and d["calls"][0]["args"]["component_ref"] == "a@1.0.0"
    assert json.dumps(res.raw_response)  # full message history is retained as an artifact


def test_invalid_output_is_repaired_within_bound_and_counted():
    attempts = {"n": 0}

    def fn(messages, info: AgentInfo):
        attempts["n"] += 1
        out = info.output_tools[0].name
        if attempts["n"] == 1:  # invalid: args_json is not JSON -> one bounded repair round
            return ModelResponse(parts=[ToolCallPart(out, {"rationale": "x", "calls": [{"tool": "record_note", "args_json": "{oops"}]})])
        return ModelResponse(parts=[ToolCallPart(out, {"rationale": "fixed", "calls": []})])

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "output_retries": 1})
    res = rt.decide(req())
    assert res.proposal["rationale"] == "fixed" and res.usage["calls"] == 2


def test_repeated_invalid_output_fails_without_unbounded_retries():
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("I refuse to use the schema")])

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "output_retries": 1})
    with pytest.raises(ModelCallFailed):
        rt.decide(req())


def test_provider_error_maps_to_retryable_failure_with_possible_spend():
    def fn(messages, info: AgentInfo):
        raise TimeoutError("provider timed out after accepting request")

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "anthropic:claude-sonnet-5-5"})
    with pytest.raises(ModelCallFailed) as e:
        rt.decide(req())
    assert e.value.retryable and e.value.possible_usage["provider"] == "anthropic"


def test_tool_loop_gets_a_tool_free_final_request_and_decides():
    def fn(messages, info: AgentInfo):
        if info.function_tools:  # keeps reading while tools are offered (the receipt-#2 behaviour)
            return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": "x"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "rationale": "decide from gathered evidence",
            "calls": [{"tool": "record_note", "args_json": json.dumps({"text": "job x still running"})}]})])

    tools = {"get_job": lambda j: {"available": False}, "inspect_evidence": lambda a, m=8192: {},
             "search_components": lambda t="", c=None: {}, "describe_component": lambda r: {}}
    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "max_requests_per_decision": 4})
    res = rt.decide(req(tools))
    assert res.usage["tool_budget_exhausted"] and res.usage["calls"] == 4  # 3 tool rounds + 1 forced decision
    assert validate_decision(res.proposal, max_calls=4)["calls"][0]["tool"] == "record_note"


def test_request_budget_is_a_bounded_outcome_with_usage_counted():
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": "x"})])

    tools = {"get_job": lambda j: {"status": "running"}, "inspect_evidence": lambda a, m=8192: {},
             "search_components": lambda t="", c=None: {}, "describe_component": lambda r: {}}
    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "max_requests_per_decision": 3})
    res = rt.decide(req(tools))
    assert res.proposal["calls"] == [] and res.usage["calls"] >= 3 and res.usage["limit_reached"]
    assert validate_decision(res.proposal, max_calls=4)["control"]["action"] == "wait_for_jobs"


def test_output_schema_has_no_open_objects_for_strict_providers():
    from pydantic_ai import Agent

    from eidos.investigator.pydantic_ai_runtime import Decision
    schema = Decision.model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" not in node and "$ref" not in node:
                raise AssertionError(f"open object in output schema: {node}")
            if node.get("additionalProperties") not in (None, False):
                raise AssertionError(f"free-form additionalProperties: {node}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    assert Agent  # schema is what the provider receives as the output tool


def test_failure_classification():
    from pydantic_ai.exceptions import ModelHTTPError

    from eidos.investigator.pydantic_ai_runtime import classify_failure
    assert classify_failure(ModelHTTPError(404, "m", {"type": "not_found_error"})) is False
    assert classify_failure(ModelHTTPError(400, "m", {})) is False
    assert classify_failure(ModelHTTPError(429, "m", {})) is True
    assert classify_failure(ModelHTTPError(529, "m", {})) is True
    assert classify_failure(TimeoutError()) is True


def test_real_provider_clients_disable_transport_retries():
    pytest.importorskip("anthropic")
    from eidos.investigator.pydantic_ai_runtime import build_model
    m = build_model({"model": "anthropic:claude-sonnet-5-5", "api_key_env": "NO_SUCH_KEY_ENV"})
    assert m.client.max_retries == 0
    o = build_model({"model": "openai-compatible:local-model", "base_url": "http://127.0.0.1:9/v1"})
    assert o.client.max_retries == 0 and str(o.client.base_url).startswith("http://127.0.0.1:9/v1")
