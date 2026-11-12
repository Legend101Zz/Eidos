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
SCOPE_SCHEMA = {"type": "object", "properties": {"result": {"type": "object", "required": ["next_test"]}}}
MALFORMED_SCOPE = {"result": {"next_tset": "fixture"}}
FIXED_SCOPE = {"result": {"next_test": "fixture"}}


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


def run_text(mode, replies, results, *, conclude_only=True, context="fixture", tokens=262_144, scope_schema=None):
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
        conclusion_scope_schema=scope_schema,
        read_tools={name: lambda *args: None for name in ("get_job", "inspect_evidence", "describe_component")})
    with patch("eidos.investigator.pydantic_ai_runtime._read", side_effect=read):
        out = rt.decide(req)
    return out, seen, schemas, executed, rt.envelope.admitted


@pytest.mark.parametrize("mode", ["json", "repair"])
@pytest.mark.parametrize("terminal_read", [False, True])
def test_d02_two_source_rounds_then_decision_only(mode, terminal_read):
    context, recorded, results = reply_and_results()
    terminal = [synthetic(READ), synthetic(READ)] if terminal_read else [synthetic(CONCLUSION)]
    out, seen, schemas, executed, admitted = run_text(mode, [*recorded, *terminal], results, context=context)
    assert len(seen) == 2 + len(terminal) and out.usage["calls"] == len(seen)
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
    assert [message["kind"] for message in out.raw_response] == ["request", "response"] * len(seen)
    assert out.usage["structured_output"]["reasks"] == int(terminal_read)


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


@pytest.mark.parametrize("repair", [False, True])
def test_native_tool_finalization_rejects_oversized_batch_and_disables_reads_for_terminal_request(repair):
    calls, reads = [], []

    def model(messages, info):
        calls.append((messages, [tool.name for tool in info.function_tools]))
        if len(calls) <= 2:
            count = 5 if len(calls) == 1 else 4
            return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": str(i)}) for i in range(count)])
        reply = {**CONCLUSION, "control": {"action": "wait_for_jobs"}} if repair and len(calls) == 3 else CONCLUSION
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, reply)])

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": "tool"})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          read_tools={"get_job": lambda jid: reads.append(jid) or {"result": jid}}, conclude_only=True)
    out = rt.decide(req)
    assert len(calls) == 3 + int(repair)
    assert all(not tools for _, tools in calls[2:])
    assert sorted(reads) == ["0", "1", "2", "3"]
    assert "read batch refused" in str(calls[2][0])
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"



def test_terminal_text_repair_can_conclude_without_any_additional_read():
    results = [[{"tool": "get_job", "args": {"job_id": "fixture"}, "result": {"visible": True}}]] * 2
    out, seen, schemas, executed, _ = run_text("json", [synthetic(READ)] * 3 + [synthetic(CONCLUSION)], results)
    assert len(seen) == 4 and len(executed) == 2
    assert all("read" not in schema["json_schema"]["schema"]["properties"] for schema in schemas[2:])
    assert out.runtime_outcome is None and out.usage["structured_output"]["reasks"] == 1



def test_terminal_text_repair_not_sent_if_original_budget_cannot_admit_it():
    results = [[{"tool": "get_job", "args": {"job_id": "fixture"}, "result": {"visible": "x" * 5_000}}]] * 2
    replies = [synthetic(READ, input_tokens=19_000)] * 2 + [synthetic(READ, input_tokens=42_000)]
    out, seen, _, executed, _ = run_text("json", replies, results, tokens=100_000, context="x" * 18_000)
    assert len(seen) == 3 and len(executed) == 2
    assert out.runtime_outcome == "structured-output-invalid" and not out.proposal
    assert out.usage["structured_output"]["reasks"] == 0
    assert "decision token envelope" in out.usage["limit_reached"]


def scoped_reply(scope, action="conclude", *, calls=None):
    return {**CONCLUSION, "calls": calls or [], "control": {**CONCLUSION["control"], "action": action, "scope": scope}}


def test_scope_feedback_preserves_initial_reply_and_two_read_rounds():
    replies = [synthetic(READ), synthetic(READ), synthetic(scoped_reply(MALFORMED_SCOPE))]
    results = [[{"tool": "get_job", "args": {"job_id": "fixture"}, "result": {"visible": True}}]] * 2
    fixed, error = scoped_reply(FIXED_SCOPE), "control.scope.result.next_test: required"
    out, seen, _, reads, _ = run_text("json", [*replies, synthetic(fixed)], results, scope_schema=SCOPE_SCHEMA)
    assert out.runtime_outcome is None and out.proposal["control"]["scope"] == fixed["control"]["scope"]
    assert len(seen) == out.usage["calls"] == 4 and out.usage["structured_output"]["reasks"] == 1
    assert reads == [entry for batch in results for entry in batch]
    assert error in out.usage["structured_output"]["errors"][0] and error in str(seen[-1])
    assert [message["kind"] for message in out.raw_response] == ["request", "response"] * 4
    for expected, retained in zip(replies, out.raw_response[1:6:2], strict=True):
        actual = ModelMessagesTypeAdapter.validate_python([retained])[0]
        assert actual.parts == expected.parts and actual.usage == expected.usage


@pytest.mark.parametrize("action", ["conclude", "stop"])
def test_conclusion_scope_is_optional_and_normal_turn_keeps_durable_refusal(action):
    malformed = synthetic(scoped_reply(MALFORMED_SCOPE, action))
    for options in ({}, {"conclude_only": False, "scope_schema": SCOPE_SCHEMA}):
        out, seen, _, _, _ = run_text("repair", [malformed], [], **options)
        assert len(seen) == 1 and out.proposal["control"]["scope"] == MALFORMED_SCOPE
    omitted = synthetic({**CONCLUSION, "control": {"action": action, "summary": "No scope supplied."}})
    out, seen, _, _, _ = run_text("json", [omitted], [], scope_schema=SCOPE_SCHEMA)
    assert len(seen) == 1 and out.proposal["control"]["action"] == action
    assert "scope" not in out.proposal["control"]


def test_scope_repair_has_no_effects_and_no_extra_budget():
    malformed = synthetic(scoped_reply(MALFORMED_SCOPE), input_tokens=45_000)
    out, seen, _, reads, _ = run_text("json", [malformed], [], tokens=65_000, context="x" * 12_000,
                                      scope_schema=SCOPE_SCHEMA)
    assert len(seen) == 1 and not reads and not out.proposal
    assert out.runtime_outcome == "structured-output-invalid"
    assert "decision token envelope" in out.usage["limit_reached"]
    effect = {"tool": "record_note", "args": {"kind": "hypothesis", "text": "must not execute"}}
    out, seen, _, reads, _ = run_text("json", [malformed, synthetic(scoped_reply(FIXED_SCOPE, calls=[effect]))],
                                      [], scope_schema=SCOPE_SCHEMA)
    assert len(seen) == 2 and not reads and not out.proposal
    assert out.runtime_outcome == "structured-output-invalid"


@pytest.mark.parametrize("initial_read_tools", [False, True])
def test_native_conclude_only_uses_existing_retry_for_scope_error(initial_read_tools):
    seen = []

    def model(messages, info):
        seen.append(messages)
        assert bool(info.function_tools) == (initial_read_tools and len(seen) == 1)
        scope = MALFORMED_SCOPE if len(seen) == 1 else FIXED_SCOPE
        output = {**CONCLUSION, "control": {**CONCLUSION["control"], "scope_json": json.dumps(scope)}}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": "tool"})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          conclude_only=True, conclusion_scope_schema=SCOPE_SCHEMA,
                          read_tools={"get_job": lambda _: pytest.fail("unexpected read")} if initial_read_tools else {})
    out = rt.decide(req)
    assert out.runtime_outcome is None and out.proposal["control"]["scope"] == FIXED_SCOPE
    assert len(seen) == out.usage["calls"] == 2
    assert "control.scope.result.next_test: required" in str(seen[-1])
    assert "next_tset" in str(out.raw_response)


@pytest.mark.parametrize("mode", ["repair", "json", "tool"])
@pytest.mark.parametrize("bad", [None, [], 0, "", "{}", {"bad": True}])
def test_supplied_scope_is_validated_as_authored_in_every_mode(mode, bad):
    schema = {"type": "object", "required": ["next_test"]}
    seen = []
    bad_decision = {**CONCLUSION, "control": {**CONCLUSION["control"], "scope": bad}}
    fixed_decision = {**CONCLUSION, "control": {**CONCLUSION["control"], "scope": {"next_test": "visible"}}}

    def model(messages, info):
        seen.append((messages, info.function_tools))
        value = bad_decision if len(seen) == 1 else fixed_decision
        if mode != "tool":
            return synthetic(value)
        control = dict(value["control"])
        control["scope_json"] = json.dumps(control.pop("scope"))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**value, "control": control})])

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": mode})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          read_tools={"get_job": lambda _: pytest.fail("correction executed a read")},
                          conclude_only=True, conclusion_scope_schema=schema)
    out = rt.decide(req)
    assert out.proposal["control"]["scope"] == {"next_test": "visible"} and out.runtime_outcome is None
    assert len(seen) == out.usage["calls"] == 2
    assert "conclusion scope does not match" in str(seen[1][0])
    assert not seen[1][1]


@pytest.mark.parametrize("mode", ["repair", "json", "tool"])
@pytest.mark.parametrize("bad_control", [{"action": "wait_for_jobs"},
    {"action": "conclude", "claim_json": '{"disposition":"candidate"}'}])
def test_early_finalization_rejects_control_and_unlocked_positive_claim(mode, bad_control):
    seen = []

    def model(messages, info):
        seen.append((messages, info.function_tools))
        value = {**CONCLUSION, "control": bad_control} if len(seen) == 1 else CONCLUSION
        if mode == "tool":
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, value)])
        return synthetic(value)

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": mode})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          read_tools={"get_job": lambda _: pytest.fail("correction executed a read")},
                          conclude_only=True, finalization_has_eligible_lock=False)
    out = rt.decide(req)
    assert out.proposal["control"]["action"] == "stop" and out.usage["calls"] == 2
    assert not seen[1][1]
    error = "No eligible locked candidate" if "claim_json" in bad_control else "No further reads are available"
    assert error in str(seen[1][0])


def test_native_finalization_does_not_send_scope_repair_that_cannot_fit():
    seen = []

    def model(messages, info):
        seen.append(messages)
        control = {**CONCLUSION["control"], "scope_json": "{}"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**CONCLUSION, "control": control})],
                             usage=RequestUsage(input_tokens=61_000, output_tokens=100))

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": "tool",
                            "max_tokens_per_decision": 70_000, "max_output_tokens": 4_096})
    req = DecisionRequest(turn_id="t", slot=0, context="fixture" * 1_000, turn_so_far=[], limits={"max_calls": 0},
                          conclude_only=True, conclusion_scope_schema={"type": "object", "required": ["next_test"]})
    out = rt.decide(req)
    assert len(seen) == out.usage["calls"] == 1
    assert out.runtime_outcome == "structured-output-invalid" and out.proposal == {}
    assert "decision token envelope" in out.usage["limit_reached"]
    assert out.usage["structured_output"]["last_error_code"] == "conclusion-scope-contract"


@pytest.mark.parametrize("offer_initial_read_tools", [True, False])
def test_native_mixed_read_and_conclusion_executes_neither_and_gets_decision_only_feedback(offer_initial_read_tools):
    seen = []

    def model(messages, info):
        seen.append((messages, info.function_tools))
        parts = [ToolCallPart(info.output_tools[0].name, CONCLUSION)]
        if len(seen) == 1:
            parts.append(ToolCallPart("get_job", {"job_id": "fixture"}))
        return ModelResponse(parts=parts)

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": "tool"})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          conclude_only=True,
                          read_tools={"get_job": lambda _: pytest.fail("mixed read must never execute")}
                          if offer_initial_read_tools else {})
    out = rt.decide(req)
    assert out.runtime_outcome is None and out.proposal["control"]["action"] == "stop"
    assert len(seen) == out.usage["calls"] == 2
    assert not seen[1][1] and "Submit one conclusion without read calls" in str(seen[1][0])


@pytest.mark.parametrize("mode", ["repair", "json", "tool"])
def test_prior_shape_correction_does_not_grant_another_scope_repair(mode):
    seen = []

    def model(messages, info):
        seen.append(messages)
        if mode != "tool":
            if len(seen) == 1:
                return ModelResponse(parts=[TextPart("synthetic malformed reply")])
            return synthetic({**CONCLUSION, "control": {**CONCLUSION["control"], "scope": MALFORMED_SCOPE}})
        args = {"calls": []} if len(seen) == 1 else {
            **CONCLUSION, "control": {**CONCLUSION["control"], "scope_json": json.dumps(MALFORMED_SCOPE)},
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    rt = PydanticAIRuntime({"model": "function:fixture", "_model_object": FunctionModel(model), "output_mode": mode})
    req = DecisionRequest(turn_id="t", slot=0, context="Conclude only.", turn_so_far=[], limits={"max_calls": 0},
                          conclude_only=True, conclusion_scope_schema=SCOPE_SCHEMA,
                          read_tools={"get_job": lambda _: pytest.fail("repair executed a read")})
    out = rt.decide(req)
    assert len(seen) == out.usage["calls"] == 2
    assert out.runtime_outcome == "structured-output-invalid" and out.proposal == {}
    report = out.usage["structured_output"]
    assert report["last_error_code"] == "conclusion-scope-contract"
    assert len(report["errors"]) == 2
