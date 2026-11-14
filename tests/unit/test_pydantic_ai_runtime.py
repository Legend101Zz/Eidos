"""Pydantic AI adapter qualification against *fixture* models (FunctionModel/TestModel).
These are fixture-only: real-provider qualification is a separate, controller-run step."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.exceptions import ModelHTTPError
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
    assert res.usage["calls"] == 2 and res.prompt_revision == "investigator-prompt/5"
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


def test_repeated_invalid_output_ends_bounded_after_one_fresh_state_repair():
    # ADR 0023: invalid through the output retries -> one fresh-state run -> still invalid -> a bounded no-effect outcome
    seen = []

    def fn(messages, info: AgentInfo):
        seen.append(messages)
        return ModelResponse(parts=[TextPart("I refuse to use the schema")])

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "output_retries": 1})
    res = rt.decide(req())
    assert res.proposal == {} and res.runtime_outcome == res.usage["runtime_outcome"] == "structured-output-invalid"
    assert res.usage["structured_output"]["fresh_state_repairs"] == 1 and res.usage["calls"] == len(seen) == 4
    assert len(seen[2]) == 1  # the fresh-state run starts from the original prompt alone, not the malformed history


def test_repeated_invalid_output_fails_without_unbounded_retries_when_fresh_state_repair_is_off():
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("I refuse to use the schema")])

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "output_retries": 1,
                            "fresh_state_repairs": 0})
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
    assert res.proposal == {} and res.usage["calls"] == 3 and res.usage["limit_reached"]  # exact cap (Mac 13)
    assert res.runtime_outcome == res.usage["runtime_outcome"] == "request-budget-exhausted"


def test_provider_quota_during_reserved_tool_free_request_keeps_provider_failure():
    def fn(messages, info: AgentInfo):
        if info.function_tools:
            return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": "x"})])
        raise ModelHTTPError(429, "minimax", {"base_resp": {"status_code": 2056, "status_msg": "usage limit exceeded"}})

    tools = {"get_job": lambda job: {"status": "running"}}
    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test",
                            "max_requests_per_decision": 3})
    with pytest.raises(ModelCallFailed) as failure:
        rt.decide(req(tools))
    assert failure.value.failure["kind"] == "quota-exhausted"
    assert failure.value.possible_usage["calls"] == 2
    assert failure.value.possible_usage["failed_request"]["status_code"] == 429


def test_authored_tool_mode_wait_is_still_a_proposal():
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "rationale": "A job is pending.",
            "calls": [],
            "control": {"action": "wait_for_jobs", "summary": "Wait for the running job."},
        })])

    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test"})
    decision = rt.decide(req())
    assert decision.runtime_outcome is None and "runtime_outcome" not in decision.usage
    assert validate_decision(decision.proposal, max_calls=4)["control"]["action"] == "wait_for_jobs"


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


@pytest.mark.parametrize("mode", ["tool", "repair", "json"])
def test_anthropic_refusal_is_bounded_without_reask(monkeypatch, mode):
    from pathlib import Path

    import httpx2 as httpx
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/providers/anthropic-refusal.json').read_text())
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=fixture)

    client = AsyncAnthropic(api_key='fixture-key', max_retries=0, base_url='http://127.0.0.1:9',
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    model = AnthropicModel('claude-haiku-5-5', provider=AnthropicProvider(anthropic_client=client))
    runtime = PydanticAIRuntime({'model': 'anthropic:claude-haiku-5-5', '_model_object': model,
                                'output_mode': mode, 'price_per_mtok': {'input': .1, 'output': .5}})
    result = runtime.decide(req())
    assert len(calls) == 1
    assert result.proposal == {} and result.runtime_outcome == 'provider-refused'
    from eidos.domain.turns import LOCAL_DECISION_BOUNDS

    assert result.runtime_outcome not in LOCAL_DECISION_BOUNDS  # no slot/turn retries of refused output
    assert result.usage['provider_refusal'] is True
    body = json.loads(calls[0].content)
    assert not {'thinking', 'temperature', 'top_p', 'top_k'} & body.keys()
    assert result.usage['input_tokens'] == 100 and result.usage['output_tokens'] == 12
    assert result.usage['usd'] == pytest.approx(.000016)
    assert result.raw_response[-1]['provider_details']['finish_reason'] == 'refusal'


def test_anthropic_base_url_and_opt_in_auto_choice(monkeypatch):
    import asyncio

    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.tools import ToolDefinition

    from eidos.investigator.pydantic_ai_runtime import build_model

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-key')
    model = build_model({'model': 'anthropic:claude-3-5-haiku-latest', 'base_url': 'http://127.0.0.1:9',
                         'tool_choice': 'auto'})
    try:
        assert str(model.client.base_url) == 'http://127.0.0.1:9'
        assert model.client.max_retries == 0
        params = ModelRequestParameters(output_tools=[ToolDefinition(name='decision')], allow_text_output=False)
        _, choice = model._prepare_tools_and_tool_choice({}, params)
        assert choice['type'] == 'auto'
        default = build_model({'model': 'anthropic:claude-3-5-haiku-latest'})
        try:
            _, choice = default._prepare_tools_and_tool_choice({}, params)
            assert choice['type'] == 'any'
        finally:
            asyncio.run(default.client.close())
    finally:
        asyncio.run(model.client.close())


def test_anthropic_auto_setting_changes_outgoing_tool_choice(monkeypatch):
    import asyncio

    import httpx2 as httpx
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.tools import ToolDefinition

    from eidos.investigator.pydantic_ai_runtime import build_model

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-key')
    choices = []
    async def call(setting):
        cfg = {'model': 'anthropic:claude-3-5-haiku-latest'}
        if setting:
            cfg['tool_choice'] = setting
        model = build_model(cfg)
        def respond(request):
            choices.append(json.loads(request.content)['tool_choice'])
            return httpx.Response(200, json={
                'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                'model': 'claude-3-5-haiku-latest', 'content': [{'type': 'text', 'text': 'fixture'}],
                'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}})
        await model.client._client.aclose()
        model.client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await model.request([ModelRequest(parts=[UserPromptPart('hi')])], {'max_tokens': 16},
                ModelRequestParameters(output_mode='tool', output_tools=[ToolDefinition(name='decision')], allow_text_output=False))
        finally:
            await model.client.close()
    asyncio.run(call(None))
    asyncio.run(call('auto'))
    assert choices == [{'type': 'any'}, {'type': 'auto'}]


def test_anthropic_refusal_after_read_preserves_all_usage():
    from pydantic_ai.usage import RequestUsage

    calls = []

    def respond(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart('search_components', {'text': 'fixture'})],
                                 model_name='claude-haiku-5-5', usage=RequestUsage(input_tokens=10, output_tokens=5))
        return ModelResponse(parts=[TextPart('Refused')], provider_details={'finish_reason': 'refusal'},
                             model_name='claude-haiku-5-5', usage=RequestUsage(input_tokens=20, output_tokens=3))

    runtime = PydanticAIRuntime({'model': 'anthropic:claude-haiku-5-5', '_model_object': FunctionModel(respond)})
    result = runtime.decide(req({'search_components': lambda text='', capability=None: {'components': []}}))
    assert result.runtime_outcome == 'provider-refused' and result.proposal == {}
    assert len(calls) == 2 and result.usage['calls'] == 2
    assert result.usage['input_tokens'] == 30 and result.usage['output_tokens'] == 8
    assert len(result.raw_response) == 2
