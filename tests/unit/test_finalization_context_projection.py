"""Bounded model-facing recovery context and receipt-equivalent D04 replay."""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.domain.turns import MODEL_CONTEXT_CHAR_LIMIT
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.context import POLICY_ID, POLICY_V1, _pack_v2
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionRequest

HEADER = "## Bounded autonomous finalization\n"
FINALIZATION_PROJECTION = "finalization-context/1"
# D04-r1 settled development transcript c46eedc8..., finalization usage seq2257.
# The provider text was unavailable. These three billing pairs are recorded,
# while the corresponding model texts and read results below are synthetic.
RECORDED_BILLING = [(43607, 826, 681), (52135, 6762, 5003), (58933, 2282, 1065)]


def view_fixture():
    jobs = [f"job_{index:016x}" for index in range(39)]
    artifacts = [f"art_{index:016x}" for index in range(2_000)]
    finalization = {
        "mode": "autonomous", "turn_id": "turn_fixture_20", "trigger": "runtime-bounded",
        "runtime_outcome": "deadline-exhausted",
        "incumbent": {"job_id": jobs[-1], "version": 3, "reproduced_by": jobs[-2],
                      "reproduction": {"criterion": "recorded rule", "reproduced_by": jobs[-2]}},
        "locked_candidate": {"id": jobs[-1], "status": "succeeded", "component": "proj.train@1.0.0",
                             "outputs": [{"artifact_id": artifacts[-1]}]},
        "selection_rule": {"metric": "RMSE", "direction": "decrease", "scorer": "proj.score@1.0.0",
                           "split": "visible-validation", "data": {"config": {"split": "validation"}}},
        "evidence_allowlist": {"jobs": jobs, "artifacts": artifacts, "components": ["proj.train@1.0.0"]},
        "metrics": [{"job_id": job, "config": {"private_marker": "x" * 1_500},
                     "receipt": {"duplicated_receipt": "r" * 900}, "outputs": []} for job in jobs],
    }
    return {
        "investigation": {"id": "inv_fixture", "status": "active", "body": {"question": "Inspect recorded work."}},
        "horizon": 2214, "finalization": finalization, "budgets": {"model_tokens": {"used": 100}},
        "completion": {"finalization": True, "remaining_seconds": 90, "conclude_now": True},
    }


def pack_view(view, catalog=(), policy=POLICY_ID, blocked=None):
    turn = {"spec": {"context_policy": policy, "finalization": view["finalization"]}}
    return InvestigatorActivities._pack_model_context(turn, view, list(catalog), blocked or set())


def test_projection_reserves_total_cap_and_keeps_exact_authority_and_locked_ids():
    view = view_fixture()
    original = deepcopy(view)
    for policy in (POLICY_ID, POLICY_V1):
        packed = pack_view(view, policy=policy)
        assert packed["chars"] == len(packed["text"]) <= MODEL_CONTEXT_CHAR_LIMIT
        projected = json.loads(packed["text"].split(HEADER, 1)[1])
        final = original["finalization"]
        assert projected["schema"] == FINALIZATION_PROJECTION
        assert projected["selection_rule"] == final["selection_rule"]
        assert projected["incumbent"]["job_id"] == final["incumbent"]["job_id"]
        assert projected["reproduction"]["reproduced_by"] == final["incumbent"]["reproduced_by"]
        assert projected["locked_candidate"]["id"] == final["locked_candidate"]["id"]
        assert projected["addressable"]["jobs"][:2] == [final["incumbent"]["job_id"], final["incumbent"]["reproduced_by"]]
        assert projected["addressable"]["artifacts"][0] == final["locked_candidate"]["outputs"][0]["artifact_id"]
        assert projected["omitted"]["artifacts"] > 0
        assert "metrics" in projected["omitted"]["fields"]
        assert "locked_candidate.outputs" in projected["omitted"]["fields"]
        assert set(projected["addressable"]["artifacts"]) <= set(final["evidence_allowlist"]["artifacts"])
        assert "private_marker" not in packed["text"] and "duplicated_receipt" not in packed["text"]
        assert packed["truncations"]["finalization_projection"]["omitted"] == projected["omitted"]
    assert view == original


def test_projection_excludes_blocked_ids_and_records_oversized_rule_omission():
    view = view_fixture()
    final = view["finalization"]
    blocked = final["evidence_allowlist"]["artifacts"][-1]
    final["selection_rule"]["data"]["config"]["oversized"] = "x" * 12_000
    packed = pack_view(view, blocked={blocked})
    assert blocked not in packed["text"]
    projection = json.loads(packed["text"].split(HEADER, 1)[1])
    assert projection["selection_rule"]["metric"] == "RMSE"
    assert len(projection["selection_rule"]["sha256"]) == 64
    assert "selection_rule" in projection["omitted"]


def receipt_equivalent_decision(context):
    replies = [
        {"rationale": "Check visible evidence", "read": [{"tool": "get_job", "args": {"job_id": "job_fixture"}}]},
        {"control": {"action": "stop", "summary": "SYNTHETIC_MALFORMED_FIRST"}},
        {"control": {"action": "stop", "summary": "SYNTHETIC_MALFORMED_SECOND"}},
        {"rationale": "Synthetic follow-up to witness admission only.", "calls": [],
         "control": {"action": "stop", "summary": "No scientific verification is asserted."}},
    ]
    requests = []

    def model(messages, info):
        index = len(requests)
        requests.append(messages)
        input_tokens, output_tokens, reasoning_tokens = [*RECORDED_BILLING, (20_000, 200, 0)][index]
        return ModelResponse(parts=[TextPart(json.dumps(replies[index]))], finish_reason="stop",
                             usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                                                details={"reasoning_tokens": reasoning_tokens}))

    runtime = PydanticAIRuntime({
        "_model_object": FunctionModel(model), "model": "openai-compatible:fixture", "output_mode": "json",
        "max_tokens_per_decision": 262_144, "max_output_tokens": 16_384, "max_requests_per_decision": 8,
    })
    result = runtime.decide(DecisionRequest(
        turn_id="D04-receipt-equivalent", slot=0, context=context, turn_so_far=[],
        limits={"max_calls": 0, "deadline_seconds": 90},
        read_tools={tool: (lambda *args: {"visible_fixture": True})
                    for tool in ("get_job", "inspect_evidence", "describe_component")},
    ))
    return result, requests, runtime.envelope.admitted


def test_receipt_equivalent_repair_fits_without_replaying_archived_responses_to_model():
    view = view_fixture()
    catalog = []
    if path := os.environ.get("EIDOS_D04_SOURCE_VIEW"):
        raw = Path(path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == "408e6c9283b2a189771b66142dc5efc931af999cf41781f3ab87470170ecfa04"
        view = json.loads(raw)
        catalog = view.pop("catalog")
    if path := os.environ.get("EIDOS_D04_PACKED_CONTEXT"):
        raw = Path(path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == "2d6fbbaf783893e9f42b42624a4f3c71d09f1d2e0d829e68d8123902e56b4167"
        original = raw.decode()
    else:
        # The previous path appended the full finalization object after ordinary packing.
        original = _pack_v2(view, catalog, char_limit=MODEL_CONTEXT_CHAR_LIMIT, v3=True)["text"]
        original += "\n\n" + HEADER + json.dumps(view["finalization"], sort_keys=True)
    bounded, old_requests, _ = receipt_equivalent_decision(original)
    assert bounded.runtime_outcome == "structured-output-invalid" and len(old_requests) == 3
    assert "97599 of 262144 left" in bounded.usage["limit_reached"]
    assert [message["kind"] for message in bounded.raw_response] == ["request", "response"] * 3
    assert all(message.get("timestamp") for message in bounded.raw_response)
    packed = pack_view(view, catalog)
    corrected, new_requests, admitted = receipt_equivalent_decision(packed["text"])
    assert corrected.runtime_outcome is None and len(new_requests) == 4
    assert corrected.usage["structured_output"]["fresh_state_repairs"] == 1
    assert admitted[-1]["spent_before"] == 164_545
    assert admitted[-1]["input_estimate"] + admitted[-1]["output_cap"] <= 97_599
    assert "SYNTHETIC_MALFORMED_FIRST" not in repr(new_requests[-1])
    assert "SYNTHETIC_MALFORMED_SECOND" not in repr(new_requests[-1])
    assert [message["kind"] for message in corrected.raw_response] == ["request", "response"] * 4
    archived = [m for m in corrected.raw_response if m["kind"] == "response"]
    assert len(archived) == 4
    assert "SYNTHETIC_MALFORMED_FIRST" in str(archived[1])
    assert "SYNTHETIC_MALFORMED_SECOND" in str(archived[2])
