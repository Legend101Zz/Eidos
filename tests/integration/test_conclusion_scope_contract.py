"""Prospective conclude-scope contract exercised through the real command ledger."""
from __future__ import annotations

from copy import deepcopy

import pytest
from study.eidos_arms.arm4 import CONCLUSION_SCOPE_SCHEMA
from study.eidos_arms.common import classify
from test_incumbent_challengers import Scene

from eidos.domain.errors import Invalid

pytestmark = pytest.mark.pg

# Minimized projection of candidate 454a9a6 Tier 1 N01-r1 arm4, turn 13. The model's
# response is art_dda37d364ff148b1 (transcript line SHA-256 7c675bf1ef2436ab360524efa9fd33b0f5d11f82be6cb9ab36857be4d5a7f911).
# The accepted conclude is sequence 1004. Prose values are synthetic; the action,
# answer status, claim type, abstained flag and misspelled key are source-backed.
RECORDED_SHAPE = {
    "action": "conclude", "summary": "Candidate submitted for independent evaluation.",
    "scope": {"answer": {
        "status": "improvement_claimed", "claim_type": "recipe_improvement",
        "abstention": {"abstained": False, "reason": None, "unresolved": ["Independent confirmation"],
                       "next_discraminating_test": "Evaluate the fixed candidate on evaluator-owned inputs."},
    }},
}
RESPONSE_ARTIFACT = "art_dda37d364ff148b1"
CORRECT_KEY = "next_discriminating_test"


@pytest.fixture
def scene(lab):
    return Scene(lab)


def accept(scene, turn, control, *, slot=0, calls=None):
    return scene.cmd("accept_decision", {
        "turn_id": turn, "slot": slot, "usage_recorded": True, "response_artifact_id": RESPONSE_ARTIFACT,
        "proposal": {"control": control, "calls": calls or []},
    })


def complete(scene, turn, control, *, reason="concluded"):
    return scene.cmd("complete_turn", {"turn_id": turn, "stop_reason": reason, "control": control})


def show(scene, inv):
    return scene.q("get_investigation", {"investigation_id": inv})


@pytest.mark.parametrize("action", ["conclude", "stop"])
def test_recorded_malformed_scope_is_refused_and_can_only_be_replaced_by_author(scene, action):
    inv = scene.investigation(conclusion_scope_schema=CONCLUSION_SCOPE_SCHEMA)
    turn = scene.begin(inv)["turn_id"]
    proposal = deepcopy(RECORDED_SHAPE)
    proposal["action"] = action
    # An unrelated model-supplied field cannot relax the operator configuration.
    proposal["conclusion_scope_schema"] = {}
    calls = [{"tool": "record_note", "args": {"kind": "note", "text": "must not execute"}}]
    refused = accept(scene, turn, proposal, calls=calls)
    assert refused["accepted"] is False and refused["reason"] == "invalid"
    assert refused["error"]["details"]["problems"] == [f"control.scope.answer.abstention.{CORRECT_KEY}: required"]
    saved, = scene.q("get_turn", {"turn_id": turn})["decisions"]
    assert saved["body"]["response_artifact_id"] == RESPONSE_ARTIFACT
    assert saved["body"]["proposal"] == {"control": proposal, "calls": calls}
    assert not scene.events(inv, "decision.accepted")
    assert "conclusion" not in show(scene, inv)["body"]

    # Synthetic follow-up: the investigator, rather than the adapter, supplies a new proposal.
    corrected = deepcopy(RECORDED_SHAPE)
    abstention = corrected["scope"]["answer"]["abstention"]
    abstention[CORRECT_KEY] = abstention.pop("next_discraminating_test")
    assert accept(scene, turn, corrected, slot=1)["accepted"]
    complete(scene, turn, corrected)
    assert show(scene, inv)["body"]["conclusion"]["scope"] == corrected["scope"]


def test_unrepaired_refusal_has_no_investigator_conclusion(scene):
    inv = scene.investigation(conclusion_scope_schema=CONCLUSION_SCOPE_SCHEMA)
    turn = scene.begin(inv)["turn_id"]
    assert not accept(scene, turn, RECORDED_SHAPE)["accepted"]
    with pytest.raises(Invalid, match="conclusion scope"):
        complete(scene, turn, RECORDED_SHAPE)
    complete(scene, turn, {}, reason="invalid-proposals")
    investigation = show(scene, inv)
    assert "conclusion" not in investigation["body"]
    assert classify(investigation, [], [])[0] == "crash"


@pytest.mark.parametrize("scope", [
    None,
    {"population": "visible development inputs"},
    {"answer": {"status": "inconclusive"}},
    {"answer": {"status": "abstained", "claim_type": "no_positive_claim",
                "abstention": {"abstained": True, "reason": "Evidence is incomplete.",
                               "unresolved": ["Independent confirmation"], CORRECT_KEY: "Run the independent test."}}},
])
def test_absent_metadata_and_genuine_abstention_remain_accepted(scene, scope):
    inv = scene.investigation(conclusion_scope_schema=CONCLUSION_SCOPE_SCHEMA)
    assert show(scene, inv)["body"]["investigator"]["conclusion_scope_schema"] == CONCLUSION_SCOPE_SCHEMA
    turn = scene.begin(inv)["turn_id"]
    control = {"action": "conclude", "summary": "The investigator's own conclusion."}
    if scope is not None:
        control["scope"] = scope
    assert accept(scene, turn, control)["accepted"]
    complete(scene, turn, control)
    assert show(scene, inv)["body"]["conclusion"]["scope"] == scope


def test_legacy_start_and_free_form_conclusion_remain_accepted(scene):
    inv = scene.investigation()
    turn = scene.begin(inv)["turn_id"]
    assert accept(scene, turn, RECORDED_SHAPE)["accepted"]


def test_malformed_operator_schema_is_refused_at_start(scene):
    with pytest.raises(Invalid, match="invalid conclusion scope schema"):
        scene.investigation(conclusion_scope_schema={"type": "object", "properties": {"x": {"pattern": "["}}})
