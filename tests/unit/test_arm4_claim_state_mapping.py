"""Study arm 4: the mapping from a conclusion's evidence-derived claim state (ADR 0021) onto the eidos-answer/0.2
status fields (``study.eidos_arms.arm4.map_claim_state``).

Pure function tests: no lab, no model, no network. The model's own status survives only when the conclusion has no
claim state; otherwise the recorded evidence decides what the answer claims, and the mapping says so in its deviations.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from study.eidos_arms.arm4 import CONFIRMATION_PENDING, DEFAULT_SCOPE, map_claim_state
except ImportError as exc:  # pragma: no cover - the study tree is part of the repository
    pytest.skip(f"study.eidos_arms.arm4 cannot be imported in this environment: {exc}", allow_module_level=True)

CANDIDATE = {"code_path": "candidate/source/project.py"}
MEASUREMENTS = [{"metric": "accuracy", "baseline": 0.50, "candidate": 0.58, "split": "validation"}]
MODEL_SCOPE = "Packaged task; one training seed"


def state(name: str, value=0.08, **over):
    claim_state = {"schema": "conclusion-claim/1", "state": name, "confirmed": name == "protocol-passed",
                   "point_estimate": None if value is None else {"value": value, "source": "assessor"},
                   "assessment_id": "asm_0001", "success_rule_ref": "practical-accuracy-gain@1"}
    claim_state.update(over)
    return claim_state


def metadata(**over):
    return {"status": "improvement_claimed", "claim_type": "recipe_improvement", "scope": MODEL_SCOPE, **over}


def test_the_confirmation_marker_text():
    assert CONFIRMATION_PENDING == "pending independent confirmation"


# -- candidate-submitted -------------------------------------------------------------------------------------------
def test_a_submitted_candidate_with_a_positive_estimate_and_a_measurement_claims_pending_confirmation():
    status, claim_type, scope, deviations = map_claim_state(state("candidate-submitted"), metadata(), CANDIDATE,
                                                            MEASUREMENTS)
    assert (status, claim_type) == ("improvement_claimed", "recipe_improvement")
    assert scope == f"{MODEL_SCOPE}; pending independent confirmation"
    assert scope.endswith("pending independent confirmation")
    assert len(deviations) == 1


@pytest.mark.parametrize("value", [0.0, -0.01, -3, None, True, "0.2", float("nan")])
def test_a_submitted_candidate_without_a_positive_numeric_estimate_is_inconclusive(value):
    status, claim_type, scope, deviations = map_claim_state(state("candidate-submitted", value=value), metadata(),
                                                            CANDIDATE, MEASUREMENTS)
    assert (status, claim_type, scope) == ("inconclusive", "no_positive_claim", MODEL_SCOPE)
    assert any("without a positive point estimate" in d for d in deviations)


@pytest.mark.parametrize("estimate", [None, {}, {"source": "investigator"}, {"value": None}])
def test_a_missing_or_unreadable_point_estimate_is_inconclusive(estimate):
    status, claim_type, _, _ = map_claim_state(state("candidate-submitted", point_estimate=estimate), metadata(),
                                               CANDIDATE, MEASUREMENTS)
    assert (status, claim_type) == ("inconclusive", "no_positive_claim")


@pytest.mark.parametrize("measurements", [[], None])
def test_a_positive_submitted_candidate_without_recorded_measurements_is_inconclusive(measurements):
    status, claim_type, scope, deviations = map_claim_state(state("candidate-submitted", value=0.2), metadata(),
                                                            CANDIDATE, measurements)
    assert (status, claim_type, scope) == ("inconclusive", "no_positive_claim", MODEL_SCOPE)
    assert any("no recorded measurement" in d and "candidate-submitted" in d for d in deviations)


# -- protocol-passed -----------------------------------------------------------------------------------------------
def test_a_protocol_pass_with_a_measurement_is_an_improvement_and_names_the_assessment():
    status, claim_type, scope, deviations = map_claim_state(state("protocol-passed"), metadata(), CANDIDATE,
                                                            MEASUREMENTS)
    assert (status, claim_type) == ("improvement_claimed", "recipe_improvement")
    assert "asm_0001" in scope and "practical-accuracy-gain@1" in scope and scope.endswith(MODEL_SCOPE)
    assert CONFIRMATION_PENDING not in scope
    assert len(deviations) == 1


def test_a_protocol_pass_does_not_depend_on_the_sign_of_the_recorded_estimate():
    status, _, scope, _ = map_claim_state(state("protocol-passed", value=None), metadata(), CANDIDATE, MEASUREMENTS)
    assert status == "improvement_claimed" and "asm_0001" in scope


def test_a_protocol_pass_with_no_recorded_measurement_is_still_inconclusive_in_the_answer():
    status, claim_type, scope, deviations = map_claim_state(state("protocol-passed"), metadata(), CANDIDATE, [])
    assert (status, claim_type, scope) == ("inconclusive", "no_positive_claim", MODEL_SCOPE)
    assert any("no recorded measurement" in d for d in deviations)


# -- not supported, null, abstained --------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["not-supported", "null-reported"])
@pytest.mark.parametrize("measurements", [MEASUREMENTS, []])
def test_a_failure_or_a_reported_null_is_no_improvement_found(name, measurements):
    status, claim_type, scope, deviations = map_claim_state(state(name), metadata(), CANDIDATE, measurements)
    assert (status, claim_type, scope) == ("no_improvement_found", "no_positive_claim", MODEL_SCOPE)
    assert len(deviations) == 1


def test_an_abstention_is_abstained_whatever_the_model_said():
    status, claim_type, scope, deviations = map_claim_state(state("abstained"), metadata(), CANDIDATE, MEASUREMENTS)
    assert (status, claim_type, scope) == ("abstained", "no_positive_claim", MODEL_SCOPE)
    assert len(deviations) == 1


@pytest.mark.parametrize("name", ["not-supported", "null-reported", "abstained"])
def test_a_model_claiming_an_improvement_cannot_override_these_states(name):
    status, _, _, _ = map_claim_state(state(name, value=0.5), metadata(status="improvement_claimed"), CANDIDATE,
                                      MEASUREMENTS)
    assert status != "improvement_claimed"


# -- no claim state ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("claim_state", [None, {}, {"state": None}, {"state": "no-claim"}, "no-claim", []])
def test_without_a_claim_state_or_with_no_claim_the_models_fields_are_kept(claim_state):
    meta = metadata(status="no_improvement_found", claim_type="no_positive_claim", scope="my own scope")
    assert map_claim_state(claim_state, meta, CANDIDATE, MEASUREMENTS) == (
        "no_improvement_found", "no_positive_claim", "my own scope", [])


def test_the_models_fields_default_when_the_metadata_is_empty():
    assert map_claim_state(None, {}, CANDIDATE, []) == ("inconclusive", "no_positive_claim", DEFAULT_SCOPE, [])
    assert map_claim_state(None, {}, None, []) == ("abstained", "no_positive_claim", DEFAULT_SCOPE, [])
    assert map_claim_state(state("no-claim"), {}, None, []) == ("abstained", "no_positive_claim", DEFAULT_SCOPE, [])


# -- deviations ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["protocol-passed", "candidate-submitted", "not-supported", "abstained",
                                  "null-reported"])
def test_a_mapped_state_adds_a_deviation_naming_the_state_and_the_models_own_status(name):
    *_, deviations = map_claim_state(state(name), metadata(status="inconclusive"), CANDIDATE, MEASUREMENTS)
    assert deviations
    first = deviations[0]
    assert repr(name) in first and "ADR 0021" in first and "'inconclusive'" in first
    assert "independent evaluator still decides" in first


def test_an_unknown_state_keeps_the_models_fields_and_says_so():
    status, claim_type, scope, deviations = map_claim_state(state("proven"), metadata(), CANDIDATE, MEASUREMENTS)
    assert (status, claim_type, scope) == ("improvement_claimed", "recipe_improvement", MODEL_SCOPE)
    assert any("Unknown claim state 'proven'" in d for d in deviations)


def test_the_inputs_are_not_modified():
    claim_state, meta, measurements = state("candidate-submitted"), metadata(), [dict(m) for m in MEASUREMENTS]
    before = (repr(claim_state), repr(meta), repr(measurements))
    map_claim_state(claim_state, meta, CANDIDATE, measurements)
    assert (repr(claim_state), repr(meta), repr(measurements)) == before
