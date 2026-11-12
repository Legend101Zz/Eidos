"""Conclude-only provider read cap, with an optional private exact D02 replay."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionRequest

CONCLUSION = {"rationale": "Synthetic scoped conclusion for request admission only.", "calls": [],
              "control": {"action": "stop", "summary": "No scientific verification is asserted."}}
READ = {"rationale": "Inspect the visible results.", "read": [{"tool": "get_job", "args": {"job_id": "fixture"}}]}


def synthetic(value, *, input_tokens=15_000, output_tokens=300):
    return ModelResponse(parts=[TextPart(json.dumps(value))], finish_reason="stop",
                         usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens))


def reply_and_results():
    path = os.environ.get("EIDOS_D02_FINALIZATION_TRANSCRIPT")
    if not path:
        results = [{"tool": "get_job", "args": {"job_id": "fixture"}, "result": {"visible": "fixture"}}]
        return "Bounded finalization fixture.", [synthetic(READ), synthetic(READ)], [results, results]
    raw = Path(path).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "454577b01ea85e0a7d4896616cc7afe61dcd9133cbb39e600cf6df10f2b8dbf0"
    line = next(line for line in raw.splitlines(keepends=True)
                if (row := json.loads(line))["type"] == "runtime_response"
                and row["data"]["artifact_id"] == "art_2163b372aca04148")
    assert hashlib.sha256(line).hexdigest() == "72b566be7c5632c22238f127e2b9847bab5a8317ea8b5d5276c4a30ea0e5ebd5"
    messages = json.loads(line)["data"]["response"]
    replies = [ModelMessagesTypeAdapter.validate_python([messages[index]])[0] for index in (1, 3)]

    def user_prompt(index):
        return next(part["content"] for part in messages[index]["parts"] if part["part_kind"] == "user-prompt")

    results = [json.JSONDecoder().raw_decode(user_prompt(index).split("\n", 1)[1])[0] for index in (2, 4)]
    return user_prompt(0).split("\n\n## This turn so far", 1)[0], replies, results


def run_text(mode, replies, results, *, conclude_only=True, context="fixture", tokens=262_144):
    seen, executed, schemas = [], [], []
    expected = iter(entry for batch in results for entry in batch)

    def model(messages, info):
        index = len(seen)
        seen.append(messages)
        schemas.append((info.model_settings or {}).get("extra_body", {}).get("response_format", {}))
        return replies[index]

    def read(_tools, name, *args):
        entry = next(expected)
        assert name == entry["tool"] and args[:len(entry["args"])] == tuple(entry["args"].values())
        executed.append(entry)
        return entry["result"]

    rt = PydanticAIRuntime({"model": "openai-compatible:fixture", "_model_object": FunctionModel(model),
        "output_mode": mode, "max_tokens_per_decision": tokens, "max_output_tokens": 16_384})
    req = DecisionRequest(turn_id="t", slot=0, context=context, turn_so_far=[],
        limits={"max_calls": 0, "deadline_seconds": 90}, conclude_only=conclude_only,
        read_tools={name: lambda *args: None for name in ("get_job", "inspect_evidence", "describe_component")})
    with patch("eidos.investigator.pydantic_ai_runtime._read", side_effect=read):
        out = rt.decide(req)
    return out, seen, schemas, executed, rt.envelope.admitted


@pytest.mark.parametrize("mode", ["json", "repair"])
@pytest.mark.parametrize("terminal_read", [False, True])
def test_d02_two_source_rounds_then_decision_only(mode, terminal_read):
    context, recorded, results = reply_and_results()
    terminal = synthetic(READ if terminal_read else CONCLUSION)
    out, seen, schemas, executed, admitted = run_text(mode, [*recorded, terminal], results, context=context)
    assert len(seen) == 3 and out.usage["calls"] == 3
    assert admitted[-1]["spent_before"] + admitted[-1]["input_estimate"] + admitted[-1]["output_cap"] <= 262_144
    assert out.usage["structured_output"]["read_requests"] == 2
    assert executed == [entry for batch in results for entry in batch]
    assert all(json.dumps(batch) in str(seen[-1]) or
               any(part.content.startswith("## Read results\n" + json.dumps(batch))
                   for message in seen[-1] for part in message.parts
                   if getattr(part, "part_kind", None) == "user-prompt")
               for batch in results)
    assert "No further reads are available" in str(seen[-1])
    if mode == "json":
        assert "read" not in schemas[-1]["json_schema"]["schema"]["properties"]
    if terminal_read:
        assert out.runtime_outcome == "structured-output-invalid" and not out.proposal
    else:
        assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"
    assert [message["kind"] for message in out.raw_response] == ["request", "response"] * 3


def test_decision_only_request_not_sent_when_original_envelope_cannot_admit_it():
    results = [[{"tool": "get_job", "args": {"job_id": "fixture"}, "result": {"visible": "x" * 5_000}}]] * 2
    replies = [synthetic(READ, input_tokens=19_000)] * 2
    out, seen, _, executed, _ = run_text("json", replies, results, tokens=72_000, context="x" * 18_000)
    assert len(seen) == len(executed) == 2
    assert out.runtime_outcome == "request-budget-exhausted" and not out.proposal


def test_exploration_can_use_more_than_two_read_rounds():
    results = [[{"tool": "get_job", "args": {"job_id": "fixture"}, "result": {"visible": True}}]] * 3
    out, seen, _, executed, _ = run_text("repair", [synthetic(READ)] * 3 + [synthetic(CONCLUSION)],
                                    results, conclude_only=False)
    assert len(seen) == 4 and len(executed) == 3
    assert out.runtime_outcome is None


def test_native_tool_finalization_rejects_oversized_batch_and_disables_reads_for_terminal_request():
    calls, reads = [], []

    def model(messages, info):
        calls.append((messages, [tool.name for tool in info.function_tools]))
        if len(calls) <= 2:
            count = 5 if len(calls) == 1 else 4
            return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": str(i)}) for i in range(count)])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, CONCLUSION)])

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": "tool"})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          read_tools={"get_job": lambda jid: reads.append(jid) or {"result": jid}}, conclude_only=True)
    out = rt.decide(req)
    assert len(calls) == 3
    assert calls[2][1] == []
    assert sorted(reads) == ["0", "1", "2", "3"]
    assert "read batch refused" in str(calls[2][0])
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"
