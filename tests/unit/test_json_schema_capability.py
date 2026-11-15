"""Offline schema capability regressions; raw MiniMax SDK bodies frozen from aeffe60."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage

from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionRequest

pytestmark = pytest.mark.fixture_llm

GOLDENS = json.loads((Path(__file__).parents[1] / "fixtures/providers/minimax-schema-aeffe60.json").read_text())
VARIANTS = [
    ("normal", (), False, False),
    ("reads", ("get_job",), False, False),
    ("conclude_only", (), True, False),
    ("conclude_only_reads", ("get_job",), True, False),
    ("runaway", (), False, True),
    ("runaway_conclude_only", (), True, True),
]
ROOT_COMBINATORS = {"oneOf", "anyOf", "allOf", "enum", "const", "not"}
CONCLUSION = {"rationale": "Evidence is limited.", "calls": [],
              "control": {"action": "stop", "summary": "No verified result."}}
READ = [{"tool": "get_job", "args": {"job_id": "fixture"}}]
EFFECT = [{"tool": "record_note", "args": {"text": "must not execute"}}]


def settings_for(rt, reads, conclude_only, runaway):
    settings = {"max_tokens": 16384, "extra_body": rt._json_mode_settings(reads, conclude_only=conclude_only)}
    if runaway:
        # An active read schema must be replaced on the runaway path.
        settings["extra_body"] = rt._json_mode_settings(("get_job",), conclude_only=conclude_only)
        settings = rt._runaway_settings(settings, "json", conclude_only=conclude_only)
    return settings


async def request_bytes(settings):
    sent = []

    def respond(request):
        sent.append(request.content)
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 0, "model": "MiniMax-M3.1-Flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "fixture"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        AsyncOpenAI(api_key="fixture", base_url="http://fixture.invalid/v1",
                    max_retries=0, http_client=http) as client,
    ):
        model = OpenAIChatModel("MiniMax-M3.1-Flash", provider=OpenAIProvider(openai_client=client))
        await Agent(model, output_type=str).run("schema golden", model_settings=settings)
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize("name,reads,conclude_only,runaway", VARIANTS)
@pytest.mark.parametrize("explicit_default", [False, True])
async def test_default_minimax_request_bytes_match_aeffe60(name, reads, conclude_only, runaway, explicit_default):
    # The fixture was captured before edits by compiling the builder methods and schema
    # constant from git show aeffe60, then sending them through this same SDK/MockTransport.
    config = {"model": "openai-compatible:MiniMax-M3.1-Flash"}
    if explicit_default:
        config["json_schema_top_level_combinators"] = True
    rt = PydanticAIRuntime(config)
    assert await request_bytes(settings_for(rt, reads, conclude_only, runaway)) == GOLDENS[name].encode()


@pytest.mark.parametrize("name,reads,conclude_only,runaway", VARIANTS)
@pytest.mark.parametrize("provider", ["openai", "openai-compatible"])
async def test_opt_out_schema_has_object_root_and_only_property_constraints(name, reads, conclude_only, runaway, provider):
    rt = PydanticAIRuntime({"model": provider + ":fixture", "json_schema_top_level_combinators": False})
    body = json.loads(await request_bytes(settings_for(rt, reads, conclude_only, runaway)))
    schema = body["response_format"]["json_schema"]["schema"]
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object" and not ROOT_COMBINATORS.intersection(schema)
    if conclude_only:
        assert schema["properties"]["calls"]["maxItems"] == 0
        assert schema["properties"]["control"]["properties"]["action"]["enum"] == ["conclude", "stop"]
        if reads and not runaway:
            assert schema["required"] == ["rationale"]
            assert Draft202012Validator(schema).is_valid({"rationale": "inspect", "read": READ})
        else:
            assert schema["required"] == ["rationale", "control"]
            assert schema["properties"]["read"] == {"type": "array", "maxItems": 0}
            assert not Draft202012Validator(schema).is_valid({**CONCLUSION, "read": READ})
    assert Draft202012Validator(schema).is_valid(CONCLUSION)


def decide(replies, *, capability, with_reads):
    seen, executed = [], []

    def model(messages, info):
        seen.append((messages, info.model_settings))
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        return reply if isinstance(reply, ModelResponse) else ModelResponse(parts=[TextPart(json.dumps(reply))])

    rt = PydanticAIRuntime({
        "model": "openai:fixture", "_model_object": FunctionModel(model), "output_mode": "json",
        "json_schema_top_level_combinators": capability, "runaway_reasoning_effort": "low",
    })
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude from visible evidence.", turn_so_far=[],
                          limits={"max_calls": 0}, conclude_only=True,
                          read_tools={"get_job": lambda job: executed.append(job) or {"visible": True}} if with_reads else {})
    return rt.decide(req), seen, executed


# Each of these is excluded by the original conclude-only schema and must never
# become a proposal or execute a read, even if an endpoint ignores constrained decoding.
INVALID = [
    (True, {"rationale": "missing both read and control"}),
    (True, {"rationale": "effect only", "calls": EFFECT}),
    (True, {"rationale": "mixed control", "read": READ, "control": CONCLUSION["control"]}),
    (True, {"rationale": "mixed calls", "read": READ, "calls": EFFECT}),
    (True, {"rationale": "mixed all", "read": READ, "calls": EFFECT, "control": CONCLUSION["control"]}),
    (True, {**CONCLUSION, "control": {}}),
    (True, {**CONCLUSION, "control": {"action": "wait_for_jobs"}}),
    (True, {**CONCLUSION, "control": {"action": "request_input"}}),
    (True, {**CONCLUSION, "calls": EFFECT}),
    (False, {"rationale": "missing control", "calls": []}),
    (False, {"rationale": "terminal read", "read": READ}),
    (False, {**CONCLUSION, "read": READ}),
    (False, {**CONCLUSION, "calls": EFFECT}),
    (False, {**CONCLUSION, "control": {"action": "wait_for_jobs"}}),
]


@pytest.mark.parametrize("with_reads,invalid", INVALID)
@pytest.mark.parametrize("capability", [True, False])
@pytest.mark.parametrize("corrected", [True, False])
def test_excluded_decisions_keep_existing_bounded_validation(with_reads, invalid, capability, corrected):
    golden = "conclude_only_reads" if with_reads else "conclude_only"
    old_schema = json.loads(GOLDENS[golden])["response_format"]["json_schema"]["schema"]
    assert not Draft202012Validator(old_schema).is_valid(invalid)
    out, seen, executed = decide([invalid, CONCLUSION] if corrected else [invalid],
                                 capability=capability, with_reads=with_reads)
    assert not executed and len(seen) == out.usage["calls"] == 2
    report = out.usage["structured_output"]
    assert report["reasks"] == 1 and report["errors"] and not report["fresh_state_repairs"]
    if corrected:
        assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"
    else:
        assert out.runtime_outcome == "structured-output-invalid" and not out.proposal
    second_schema = seen[1][1]["extra_body"]["response_format"]["json_schema"]["schema"]
    if not capability:
        assert not ROOT_COMBINATORS.intersection(second_schema)


@pytest.mark.parametrize("with_reads", [False, True])
@pytest.mark.parametrize("capability", [True, False])
@pytest.mark.parametrize("empty", [None, []])
def test_existing_empty_read_repair_is_preserved(with_reads, capability, empty):
    # The parser already repairs empty/null read fields before classifying a decision,
    # even though the old top-level combinators excluded their presence.
    out, seen, executed = decide([{**CONCLUSION, "read": empty}], capability=capability, with_reads=with_reads)
    assert not executed and len(seen) == 1 and out.runtime_outcome is None
    assert out.proposal["control"]["action"] == "stop"
    assert out.usage["structured_output"]["repairs"] == ["empty-read"]


@pytest.mark.parametrize("capability", [True, False])
@pytest.mark.parametrize("empty_fields", [{}, {"calls": []}, {"control": {}}, {"calls": [], "control": None}])
def test_read_only_replies_keep_existing_empty_field_tolerance(capability, empty_fields):
    # Presence-only oneOf was stricter than the parser for inert empty fields.
    # Preserve the evidence-only behavior; no mixed decision is returned as a proposal.
    out, seen, executed = decide([{"rationale": "inspect", "read": READ, **empty_fields}, CONCLUSION],
                                 capability=capability, with_reads=True)
    assert executed == ["fixture"] and len(seen) == 2
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"
    assert out.usage["structured_output"]["reasks"] == 0
    assert out.usage["structured_output"]["read_requests"] == 1


@pytest.mark.parametrize("with_reads", [False, True])
@pytest.mark.parametrize("capability", [True, False])
def test_runaway_reask_uses_terminal_capability_and_still_refuses_reads(with_reads, capability):
    runaway = ModelResponse(parts=[TextPart("")], finish_reason="length",
                            usage=RequestUsage(input_tokens=100, output_tokens=16384))
    out, seen, executed = decide([runaway, {**CONCLUSION, "read": READ}, CONCLUSION],
                                 capability=capability, with_reads=with_reads)
    assert not executed and len(seen) == out.usage["calls"] == 3
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"
    report = out.usage["structured_output"]
    assert report["runaway_reasks"] == report["reasks"] == 1
    body = seen[1][1]["extra_body"]
    assert body["reasoning_effort"] == "low"
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["type"] == "object"
    if not capability:
        assert not ROOT_COMBINATORS.intersection(schema)
        assert schema["properties"]["read"]["maxItems"] == 0
