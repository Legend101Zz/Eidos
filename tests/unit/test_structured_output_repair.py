"""Bounded structured-output repair (ADR 0014): strict extraction, one schema-guided re-ask, JSON mode, and
repairs that are counted in usage and settings. Fixture models only; recorded local-model replies live in
tests/fixtures/structured-output/ (from the 1.1.0 G4 re-measurement)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from eidos.domain.turns import validate_decision
from eidos.investigator import structured_output as so
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionRequest

pytestmark = pytest.mark.fixture_llm
GOOD = {"rationale": "baseline first", "purpose": "reproduction",
        "hypotheses": [{"statement": "h", "prediction": "p", "falsifier": "f"}],
        "calls": [{"tool": "run_component", "args": {"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}}}],
        "control": {"action": "wait_for_jobs", "summary": "wait"}}
RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "structured-output"


def req():
    return DecisionRequest(turn_id="t1", slot=0, context="# Investigation\nQuestion: why?", turn_so_far=[],
                           limits={"max_calls": 4}, read_tools={})


@pytest.mark.parametrize("text, repairs", [
    (json.dumps(GOOD), []),
    ("```json\n" + json.dumps(GOOD, indent=2) + "\n```", ["code-fence"]),
    ("Here is my decision:\n" + json.dumps(GOOD) + "\nLet me know if you need more.", ["leading-prose", "trailing-prose"]),
    (json.dumps(GOOD)[:-1] + ",}", ["trailing-comma"]),
    (json.dumps(GOOD).replace('"', "'"), ["single-quotes-or-python-literals"]),
    (json.dumps(GOOD).replace("true", "True"), []),
])
def test_extract_json_handles_common_malformations_and_names_each_repair(text, repairs):
    obj, applied = so.extract_json(text)
    assert obj["rationale"] == "baseline first" and applied == repairs


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", "{\"rationale\": \"unterminated"])
def test_extract_json_refuses_what_it_cannot_recover(text):
    with pytest.raises(so.StructuredOutputError):
        so.extract_json(text)


def test_normalize_maps_lenient_shapes_onto_the_decision_model():
    obj, repairs = so.normalize({**GOOD, "control": {"action": "conclude", "scope": {"model": "m"}}})
    assert json.loads(obj["calls"][0]["args_json"])["component_ref"] == "a@1.0.0"
    assert obj["control"]["scope_json"] == '{"model": "m"}' and repairs == ["args-object", "scope-object"]


def _model(replies):
    seen = []

    def fn(messages, info: AgentInfo):
        seen.append(messages)
        return ModelResponse(parts=[TextPart(replies[min(len(seen) - 1, len(replies) - 1)])])
    return FunctionModel(fn), seen


def test_repair_mode_extracts_a_fenced_reply_without_a_reask_and_reports_the_repair():
    m, seen = _model(["Sure!\n```json\n" + json.dumps(GOOD) + "\n```"])
    res = PydanticAIRuntime({"_model_object": m, "model": "function:test", "output_mode": "repair"}).decide(req())
    d = validate_decision(res.proposal, max_calls=4)
    assert d["calls"][0]["args"]["inputs"] == {"train": "art_1"} and len(seen) == 1
    so_rep = res.usage["structured_output"]
    assert so_rep == {**so_rep, "mode": "repair", "valid": True, "reasks": 0}
    assert set(so_rep["repairs"]) == {"code-fence", "leading-prose", "args-object"}
    assert res.model_settings["output_mode"] == "repair" and res.model_settings["structured_output"]["repairs"]


def test_one_schema_guided_reask_includes_the_validation_error_and_is_counted():
    m, seen = _model([json.dumps({"purpose": "open-search", "calls": []}), json.dumps(GOOD)])
    res = PydanticAIRuntime({"_model_object": m, "model": "function:test", "output_mode": "repair",
                             "max_requests_per_decision": 3}).decide(req())
    assert res.proposal["rationale"] == "baseline first" and len(seen) == 2
    reask = seen[1][-1]
    assert isinstance(reask, ModelRequest) and "rationale" in str(reask.parts[-1].content)  # names the missing field
    assert res.usage["calls"] == 2 and res.usage["structured_output"]["reasks"] == 1
    assert res.usage["structured_output"]["errors"] and res.usage["structured_output"]["valid"]


def test_a_second_malformed_reply_ends_in_a_bounded_no_effect_decision():
    m, seen = _model(["not json", "still not json"])
    res = PydanticAIRuntime({"_model_object": m, "model": "function:test", "output_mode": "repair",
                             "max_requests_per_decision": 3}).decide(req())
    assert len(seen) == 2  # never more than one re-ask
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
    assert res.usage["calls"] == 2 and res.usage["limit_reached"] and not res.usage["structured_output"]["valid"]
    validate_decision(res.proposal, max_calls=4)


def test_no_reask_when_the_decision_budget_is_one_request():
    m, seen = _model(["not json", json.dumps(GOOD)])
    res = PydanticAIRuntime({"_model_object": m, "model": "function:test", "output_mode": "repair",
                             "max_requests_per_decision": 1}).decide(req())
    assert len(seen) == 1 and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"


def test_json_mode_sends_response_format_to_openai_compatible_endpoints_only():
    rt = PydanticAIRuntime({"model": "openai-compatible:qwen2.5:3b-instruct", "output_mode": "json"})
    rf = rt._json_mode_settings()["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"]["required"] == ["rationale"]
    assert PydanticAIRuntime({"model": "anthropic:claude-sonnet-5-5", "output_mode": "json"})._json_mode_settings() is None
    m, _ = _model([json.dumps(GOOD)])
    res = PydanticAIRuntime({"_model_object": m, "model": "openai-compatible:x", "output_mode": "json"}).decide(req())
    assert res.usage["structured_output"]["json_mode"] is True


def test_unknown_output_mode_is_refused():
    from eidos.investigator.runtime import ModelCallFailed
    with pytest.raises(ModelCallFailed):
        PydanticAIRuntime({"model": "function:test", "output_mode": "yolo"}).decide(req())


def _recorded():
    return sorted(RECORDED.glob("*.json")) if RECORDED.exists() else []


@pytest.mark.parametrize("path", _recorded(), ids=lambda p: p.stem)
def test_recorded_local_model_replies(path):
    """Replies recorded from qwen2.5 3B/7B through Ollama: each is either recovered by extraction (with the
    recorded repairs) or refused, exactly as recorded; nothing passes silently."""
    rec = json.loads(path.read_text())
    if rec["expect"] == "recovered":
        obj, repairs = so.extract_json(rec["reply"])
        obj, shape = so.normalize(obj)
        from eidos.investigator.pydantic_ai_runtime import Decision
        Decision.model_validate(obj)
        assert sorted(set(repairs + shape)) == sorted(rec["repairs"])
    else:
        with pytest.raises((so.StructuredOutputError, ValueError)):
            obj, _ = so.extract_json(rec["reply"])
            obj, _ = so.normalize(obj)
            from eidos.investigator.pydantic_ai_runtime import Decision
            Decision.model_validate(obj)


@pytest.mark.parametrize("text", ["{1, 2}", "{[1]: 2}", "{b'a': 1}", "{'a': -1j}"])
def test_python_literals_that_are_not_json_are_refused_cleanly(text):
    with pytest.raises(so.StructuredOutputError):
        so.extract_json(text)


def test_no_reask_when_the_decision_token_budget_is_spent():
    m, seen = _model(["not json " * 50, json.dumps(GOOD)])
    res = PydanticAIRuntime({"_model_object": m, "model": "function:test", "output_mode": "repair",
                             "max_requests_per_decision": 3, "max_tokens_per_decision": 10}).decide(req())
    assert len(seen) == 1 and res.usage["structured_output"]["reasks"] == 0  # never beyond the token reservation
    assert res.proposal["control"]["bounded_outcome"] in ("structured-output-invalid", "request-budget-exhausted")
