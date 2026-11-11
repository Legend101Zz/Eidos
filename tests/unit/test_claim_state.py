"""Evidence-derived claim state (ADR 0021): ``derive_claim_state``, ``lifecycle_state``, ``validate_claim_block``.

Author tests of pure rules: no database, no models, no network. The state of a conclusion comes from recorded
assessments; a model may abstain or report a null but cannot confirm.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from eidos.domain import assessment as A
from eidos.domain.errors import Invalid

CLAIM_ID = "clm_0001"
POPULATION = "held-out examples of toy-3class"


# -- builders ------------------------------------------------------------------------------------------------------
def make_claim(revision: int = 1, claim_id: str = CLAIM_ID, **over: Any) -> dict[str, Any]:
    return {"id": claim_id, "revision": revision, "statement": "the candidate recipe raises held-out accuracy",
            "scope": {"task": "toy-3class"}, **over}


def make_effect(**over: Any) -> dict[str, Any]:
    return {"estimate": 0.042, "ci_low": 0.011, "ci_high": 0.073, "method": "cluster-robust normal", **over}


def make_assessment(*, id: str = "asm_0001", outcome: str = "protocol-passed", conformance: str = "conformant",
                    health: str = "intact", claim_id: str = CLAIM_ID, claim_revision: int = 1, plan_id: str = "plan_0001",
                    effect: Any = "default", **over: Any) -> dict[str, Any]:
    evaluation = {"metric": "classification-report/1:accuracy", "direction": "increase", "interval": "paired-ci",
                  "effect": make_effect() if effect == "default" else effect}
    return {"id": id, "claim_id": claim_id, "claim_revision": claim_revision, "plan_id": plan_id, "outcome": outcome,
            "conformance": conformance, "health": health, "evaluation": evaluation,
            "success_rule_ref": "practical-accuracy-gain@1", "scope": {"task": "toy-3class", "split": "held-out"},
            "remaining_alternatives": ["gain specific to the evaluation population"], "confidence": 0.95, **over}


def make_plan(plan_id: str = "plan_0001", status: str = "frozen", **over: Any) -> dict[str, Any]:
    return {"plan_id": plan_id, "status": status, "obligations": [], "claim_id": CLAIM_ID,
            "data_provenance": {"split": "held-out"}, **over}


def make_charter(**over: Any) -> dict[str, Any]:
    return {"population": POPULATION, "success_rules": [{"id": "practical-accuracy-gain",
                                                         "decision_standard": {"kind": "paired-difference-ci",
                                                                               "confidence": 0.95}}], **over}


def derive(*, control: dict[str, Any] | None = None, claims: Any = "default", assessments: Any = None,
           plans: Any = None, charter: Any = "default") -> dict[str, Any]:
    return A.derive_claim_state(control=control if control is not None else {},
                                claims=[make_claim()] if claims == "default" else claims,
                                assessments=assessments or [], plans=plans or [],
                                charter=make_charter() if charter == "default" else charter)


def disposition(value: str, **block: Any) -> dict[str, Any]:
    return {"action": "conclude", "claim": {"disposition": value, **block}}


# -- (a) a conformant, intact pass ---------------------------------------------------------------------------------
def test_conformant_intact_pass_on_the_current_revision_is_protocol_passed():
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()], plans=[make_plan(status="assessed")])
    assert out["schema"] == "conclusion-claim/1"
    assert out["state"] == A.PROTOCOL_PASSED == "protocol-passed"
    assert out["confirmed"] is True
    assert out["label"] == A.LABELS["protocol-passed"]
    assert out["requested_disposition"] == "confirmed"
    assert "downgraded_from" not in out and "downgrade_reason" not in out
    assert (out["claim_id"], out["claim_revision"]) == (CLAIM_ID, 1)
    assert out["assessment_id"] == "asm_0001" and out["plan_id"] == "plan_0001"
    assert out["success_rule_ref"] == "practical-accuracy-gain@1"
    assert out["unfulfilled_obligations"] == []
    assert out["remaining_alternatives"] == ["gain specific to the evaluation population"]


def test_protocol_passed_estimate_and_interval_come_from_the_assessment_effect():
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()])
    assert out["point_estimate"] == {"metric": "classification-report/1:accuracy", "value": 0.042,
                                     "direction": "increase", "source": "assessor"}
    assert out["uncertainty"] == {"kind": "confidence-interval", "low": 0.011, "high": 0.073, "confidence": 0.95,
                                  "method": "cluster-robust normal", "source": "assessor"}


def test_protocol_passed_ignores_what_the_investigator_reported():
    block = {"point_estimate": {"value": 0.9}, "uncertainty": {"low": 0.8, "high": 1.0}}
    out = derive(control=disposition("confirmed", **block), assessments=[make_assessment()])
    assert out["point_estimate"]["value"] == 0.042 and out["point_estimate"]["source"] == "assessor"
    assert out["uncertainty"]["source"] == "assessor" and out["uncertainty"]["low"] == 0.011


def test_population_is_the_charters_then_the_claim_scope_then_the_reported_one():
    passed = [make_assessment(scope={"population": "assessment scope population"})]
    block = {"population": "reported population"}
    with_charter = derive(control=disposition("confirmed", **block), assessments=passed)
    assert with_charter["population"] == POPULATION
    no_charter_population = derive(control=disposition("confirmed", **block), assessments=passed,
                                   charter=make_charter(population=None))
    assert no_charter_population["population"] == "assessment scope population"
    no_scope_population = derive(control=disposition("candidate", **block), assessments=[],
                                 claims=[make_claim()], charter=None)
    assert no_scope_population["population"] == "reported population"


def test_interval_confidence_and_method_fall_back_gracefully():
    effect = {"estimate": 0.5, "ci_low": 0.1, "ci_high": 0.9}  # no method on the effect: the evaluation's interval
    out = derive(control=disposition("confirmed"), assessments=[make_assessment(effect=effect)], charter=None)
    assert out["uncertainty"]["method"] == "paired-ci" and out["uncertainty"]["confidence"] is None


def test_assessment_without_a_numeric_effect_gives_no_estimate():
    for effect in (None, {}, {"estimate": "high"}, {"estimate": math.nan}, {"estimate": True}):
        out = derive(control=disposition("confirmed"), assessments=[make_assessment(effect=effect)])
        assert out["state"] == "protocol-passed"
        assert out["point_estimate"] is None and out["uncertainty"] is None


# -- (b) no assessment ---------------------------------------------------------------------------------------------
def test_filed_claim_without_an_assessment_is_a_submitted_candidate_with_the_reported_estimate():
    point = {"metric": "accuracy", "baseline": 0.50, "candidate": 0.58}
    out = derive(control={"action": "conclude", "claim": {"point_estimate": point}})
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["claim_id"] == CLAIM_ID and out["assessment_id"] is None and out["plan_id"] is None
    estimate = out["point_estimate"]
    assert estimate["source"] == "investigator"
    assert estimate["value"] == pytest.approx(0.08)
    assert estimate["baseline"] == 0.50 and estimate["candidate"] == 0.58 and estimate["metric"] == "accuracy"
    obligations = out["unfulfilled_obligations"]
    assert obligations and any("independent confirmation" in o for o in obligations)
    assert any("no conformant, healthy assessor pass" in o for o in obligations)
    assert "remaining_alternatives" not in out


def test_reported_value_and_gain_are_used_before_baseline_and_candidate():
    value = derive(control={"claim": {"point_estimate": {"value": 0.2, "gain": 0.9, "baseline": 0, "candidate": 1}}})
    gain = derive(control={"claim": {"point_estimate": {"gain": 0.3, "baseline": 0, "candidate": 1}}})
    assert value["point_estimate"]["value"] == 0.2
    assert gain["point_estimate"]["value"] == 0.3 and gain["point_estimate"]["source"] == "investigator"


def test_reported_uncertainty_is_passed_through_marked_as_reported():
    block = {"point_estimate": {"value": 0.1}, "uncertainty": {"low": 0.0, "high": 0.2}}
    out = derive(control={"claim": block})
    assert out["uncertainty"] == {"low": 0.0, "high": 0.2}


def test_a_pending_plan_is_named_among_the_unfulfilled_obligations():
    plans = [make_plan("plan_a", "frozen"), make_plan("plan_b", "assessed"), make_plan("plan_c", "executing", claim_id=None),
             make_plan("plan_d", "frozen", claim_id="clm_other")]
    out = derive(control=disposition("candidate"), plans=plans)
    text = "\n".join(out["unfulfilled_obligations"])
    assert "plan_a is frozen" in text and "plan_c is executing" in text
    assert "plan_b" not in text and "plan_d" not in text


def test_unresolved_plan_obligations_and_the_investigators_obligations_are_listed_once():
    plans = [make_plan("plan_a", "draft", obligations=["assessor"])]
    block = {"obligations": ["held-out evaluation", "held-out evaluation"], "evidence_refs": ["art_1", 7]}
    out = derive(control=disposition("candidate", **block), plans=plans)
    obligations = out["unfulfilled_obligations"]
    assert any("plan_a has unresolved obligations" in o and "assessor" in o for o in obligations)
    assert obligations.count("held-out evaluation") == 1
    assert obligations[-1] == "held-out evaluation"  # the model's own come after the derived ones
    assert out["evidence_refs"] == ["art_1", "7"]


# -- (c) an intact failure -----------------------------------------------------------------------------------------
def test_intact_protocol_failure_is_not_supported():
    failed = make_assessment(outcome="protocol-failed", effect=make_effect(estimate=-0.01, ci_low=-0.03, ci_high=0.01))
    out = derive(assessments=[failed])
    assert out["state"] == "not-supported" and out["confirmed"] is False
    assert out["assessment_id"] == "asm_0001"
    assert out["point_estimate"]["value"] == -0.01 and out["point_estimate"]["source"] == "assessor"
    assert "downgraded_from" not in out  # nothing was requested, so nothing was downgraded
    assert "remaining_alternatives" not in out


# -- (d) a requested confirmation is downgraded --------------------------------------------------------------------
def test_requested_confirmation_without_a_pass_is_a_submitted_candidate_and_recorded_as_downgraded():
    out = derive(control=disposition("confirmed"))
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["requested_disposition"] == "confirmed" and out["downgraded_from"] == "confirmed"
    assert "cannot claim confirmation" in out["downgrade_reason"]


def test_requested_confirmation_after_a_failure_is_not_supported_and_downgraded():
    out = derive(control=disposition("confirmed"), assessments=[make_assessment(outcome="protocol-failed")])
    assert out["state"] == "not-supported" and out["confirmed"] is False
    assert out["downgraded_from"] == "confirmed"


def test_a_requested_confirmation_with_no_claim_filed_is_still_a_downgraded_candidate():
    out = derive(control=disposition("confirmed"), claims=[])
    assert out["state"] == "candidate-submitted" and out["downgraded_from"] == "confirmed"
    assert out["claim_id"] is None


def test_other_dispositions_are_not_recorded_as_downgraded():
    for value in ("candidate", "null", "abstain"):
        assert "downgraded_from" not in derive(control=disposition(value))


# -- (e) abstain and null ------------------------------------------------------------------------------------------
def test_abstain_wins_even_over_a_recorded_pass():
    out = derive(control=disposition("abstain"), assessments=[make_assessment()])
    assert out["state"] == "abstained" and out["confirmed"] is False
    assert out["requested_disposition"] == "abstain"
    assert "downgraded_from" not in out


def test_abstain_wins_even_over_an_intact_failure():
    out = derive(control=disposition("abstain"), assessments=[make_assessment(outcome="protocol-failed")])
    assert out["state"] == "abstained"


def test_null_is_reported_null_without_an_intact_failure():
    out = derive(control=disposition("null"))
    assert out["state"] == "null-reported" and out["confirmed"] is False
    with_pass = derive(control=disposition("null"), assessments=[make_assessment()])
    assert with_pass["state"] == "null-reported" and with_pass["confirmed"] is False


def test_null_after_an_intact_failure_is_not_supported():
    out = derive(control=disposition("null"), assessments=[make_assessment(outcome="protocol-failed")])
    assert out["state"] == "not-supported"


def test_a_reported_estimate_accompanies_a_null_or_an_abstention():
    block = {"point_estimate": {"value": 0.0}}
    assert derive(control=disposition("null", **block))["point_estimate"] == {"value": 0.0, "source": "investigator"}
    assert derive(control=disposition("abstain", **block))["point_estimate"]["source"] == "investigator"


# -- (f) inconclusive ----------------------------------------------------------------------------------------------
def test_inconclusive_assessment_leaves_the_candidate_submitted_and_says_why():
    inconclusive = make_assessment(outcome="inconclusive", conformance="conformant",
                                   effect=make_effect(estimate=0.02, ci_low=-0.01, ci_high=0.05))
    inconclusive["evaluation"]["reason"] = "interval straddles the minimum effect"
    out = derive(control=disposition("confirmed"), assessments=[inconclusive])
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["downgraded_from"] == "confirmed"
    text = "\n".join(out["unfulfilled_obligations"])
    assert "inconclusive" in text and "asm_0001" in text and "interval straddles the minimum effect" in text
    assert any("independent confirmation" in o for o in out["unfulfilled_obligations"])
    assert out["point_estimate"]["source"] == "assessor" and out["point_estimate"]["value"] == 0.02


def test_inconclusive_without_a_reason_still_names_the_assessment():
    out = derive(assessments=[make_assessment(outcome="inconclusive", conformance="conformant")])
    assert any("inconclusive" in o and "(conformant)" in o for o in out["unfulfilled_obligations"])


# -- (g) health and conformance ------------------------------------------------------------------------------------
@pytest.mark.parametrize("health", ["affected", "withdrawn"])
def test_a_pass_with_affected_or_withdrawn_health_never_confirms(health):
    out = derive(control=disposition("confirmed"), assessments=[make_assessment(health=health)])
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["downgraded_from"] == "confirmed"
    assert any(f"health is {health}" in o for o in out["unfulfilled_obligations"])


def test_a_pass_with_a_deviated_conformance_never_confirms():
    out = derive(control=disposition("confirmed"), assessments=[make_assessment(conformance="deviated")])
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert any("did not pass conformantly" in o and "deviated" in o for o in out["unfulfilled_obligations"])


def test_an_affected_failure_does_not_count_as_a_failure():
    out = derive(assessments=[make_assessment(outcome="protocol-failed", health="affected")])
    assert out["state"] == "candidate-submitted"


def test_a_withdrawn_failure_does_not_veto_a_later_intact_pass():
    first = make_assessment(id="asm_0001", outcome="protocol-failed", health="withdrawn")
    second = make_assessment(id="asm_0002")
    out = derive(assessments=[first, second])
    assert out["state"] == "protocol-passed" and out["assessment_id"] == "asm_0002"


# -- (h) several assessments, revisions ----------------------------------------------------------------------------
def test_a_pass_and_an_intact_failure_on_the_same_revision_is_not_supported():
    passed = make_assessment(id="asm_0001")
    failed = make_assessment(id="asm_0002", outcome="protocol-failed", plan_id="plan_0002")
    for ordered in ([passed, failed], [failed, passed]):
        out = derive(control=disposition("confirmed"), assessments=ordered)
        assert out["state"] == "not-supported" and out["confirmed"] is False
        assert out["assessment_id"] == "asm_0002" and out["downgraded_from"] == "confirmed"


def test_the_latest_assessment_of_the_winning_kind_is_the_one_recorded():
    first = make_assessment(id="asm_0001", plan_id="plan_0001", effect=make_effect(estimate=0.03))
    second = make_assessment(id="asm_0002", plan_id="plan_0002", effect=make_effect(estimate=0.05))
    out = derive(assessments=[first, second])
    assert out["state"] == "protocol-passed" and out["assessment_id"] == "asm_0002" and out["plan_id"] == "plan_0002"
    assert out["point_estimate"]["value"] == 0.05


def test_assessments_of_an_older_claim_revision_are_ignored_for_the_state():
    old = make_assessment(claim_revision=1)
    out = derive(control=disposition("confirmed"), claims=[make_claim(revision=2)], assessments=[old])
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["claim_revision"] == 2 and out["assessment_id"] is None
    assert out["downgraded_from"] == "confirmed"
    assert [a["id"] for a in out["assessments_considered"]] == ["asm_0001"]  # still listed, just not decisive


def test_an_old_revision_failure_does_not_veto_a_pass_on_the_current_revision():
    old_failure = make_assessment(id="asm_0001", claim_revision=1, outcome="protocol-failed")
    current_pass = make_assessment(id="asm_0002", claim_revision=2)
    out = derive(claims=[make_claim(revision=2)], assessments=[old_failure, current_pass])
    assert out["state"] == "protocol-passed" and out["assessment_id"] == "asm_0002"


def test_assessments_considered_lists_each_assessment_with_its_verdict_fields():
    out = derive(assessments=[make_assessment(), make_assessment(id="asm_0002", outcome="inconclusive")])
    assert out["assessments_considered"][0] == {"id": "asm_0001", "plan_id": "plan_0001", "claim_id": CLAIM_ID,
                                                "outcome": "protocol-passed", "conformance": "conformant",
                                                "health": "intact"}
    assert [a["id"] for a in out["assessments_considered"]] == ["asm_0001", "asm_0002"]


def test_without_a_designated_claim_the_most_recently_assessed_current_claim_is_the_target():
    other = make_claim(claim_id="clm_0002")
    third = make_claim(claim_id="clm_0003")
    passed = make_assessment(claim_id="clm_0002")
    out = derive(claims=[make_claim(), other, third], assessments=[passed])
    assert out["claim_id"] == "clm_0002" and out["state"] == "protocol-passed"
    no_assessment = derive(claims=[make_claim(), other, third])
    assert no_assessment["claim_id"] == "clm_0003"  # else the most recently filed


def test_a_designated_claim_selects_its_own_assessments():
    other = make_claim(claim_id="clm_0002")
    passed_other = make_assessment(claim_id="clm_0002")
    out = derive(control=disposition("candidate", claim_id=CLAIM_ID), claims=[make_claim(), other],
                 assessments=[passed_other])
    assert out["claim_id"] == CLAIM_ID and out["state"] == "candidate-submitted" and out["assessment_id"] is None


# -- (i) no claim, and the legacy answer ---------------------------------------------------------------------------
def test_no_claim_and_no_disposition_is_no_claim():
    out = derive(claims=[])
    assert out["state"] == "no-claim" and out["confirmed"] is False
    assert out["label"] == A.LABELS["no-claim"]
    assert out["requested_disposition"] is None
    assert out["claim_id"] is None and out["claim_revision"] is None and out["statement"] is None
    assert out["unfulfilled_obligations"] == [] and out["point_estimate"] is None


@pytest.mark.parametrize(("status", "requested", "state"), [
    ("abstained", "abstain", "abstained"),
    ("invalid", "abstain", "abstained"),
    ("no_improvement_found", "null", "null-reported"),
    ("inconclusive", "candidate", "candidate-submitted"),
    ("improvement_claimed", "candidate", "candidate-submitted"),
])
def test_legacy_answer_status_is_read_when_no_disposition_is_given(status, requested, state):
    out = derive(control={"action": "conclude", "scope": {"answer": {"status": status}}}, claims=[])
    assert out["requested_disposition"] == requested
    assert out["state"] == state and out["confirmed"] is False


def test_legacy_status_does_not_confirm_and_an_unknown_status_is_ignored():
    out = derive(control={"scope": {"answer": {"status": "confirmed"}}}, claims=[])
    assert out["state"] == "no-claim" and out["requested_disposition"] is None
    claimed = derive(control={"scope": {"answer": {"status": "improvement_claimed"}}}, assessments=[make_assessment()])
    assert claimed["state"] == "protocol-passed"  # the evidence decides, the model's status only requested a candidate
    assert claimed["requested_disposition"] == "candidate"


def test_an_explicit_disposition_wins_over_the_legacy_status():
    control = {"claim": {"disposition": "abstain"}, "scope": {"answer": {"status": "improvement_claimed"}}}
    out = derive(control=control, claims=[])
    assert out["requested_disposition"] == "abstain" and out["state"] == "abstained"


def test_no_positive_claim_scope_is_a_null():
    out = derive(control={"scope": {"claim": "no_positive_claim"}}, claims=[])
    assert out["requested_disposition"] == "null" and out["state"] == "null-reported"


@pytest.mark.parametrize(("direction", "baseline", "candidate", "expected"), [
    ("higher_is_better", 0.50, 0.58, 0.08),
    (None, 0.50, 0.58, 0.08),
    ("lower_is_better", 10.0, 8.0, 2.0),
    ("lower_is_better", 0.50, 0.58, -0.08),
])
def test_legacy_measurements_give_the_point_estimate_and_lower_is_better_flips_the_sign(direction, baseline, candidate,
                                                                                         expected):
    measurement = {"metric": "val_loss", "split": "validation", "denominator": "examples", "baseline": baseline,
                   "candidate": candidate, "uncertainty_method": "bootstrap",
                   **({"direction": direction} if direction else {})}
    control = {"scope": {"answer": {"status": "improvement_claimed", "measurements": [measurement]}}}
    out = derive(control=control, claims=[])
    estimate = out["point_estimate"]
    assert estimate["value"] == pytest.approx(expected)
    assert estimate["source"] == "investigator"
    assert (estimate["metric"], estimate["split"], estimate["denominator"]) == ("val_loss", "validation", "examples")
    assert (estimate["baseline"], estimate["candidate"]) == (baseline, candidate)
    assert out["uncertainty"] == "bootstrap"


def test_legacy_measurements_skip_entries_that_cannot_be_read_and_use_the_first_complete_one():
    measurements = ["not an object", {"metric": "a", "baseline": 1.0}, {"metric": "b", "baseline": 0.2, "candidate": 0.5},
                    {"metric": "c", "baseline": 0.0, "candidate": 9.0}]
    control = {"scope": {"answer": {"status": "inconclusive", "measurements": measurements}}}
    out = derive(control=control, claims=[])
    assert out["point_estimate"]["metric"] == "b" and out["point_estimate"]["value"] == pytest.approx(0.3)


def test_a_claim_block_point_estimate_takes_precedence_over_legacy_measurements():
    control = {"claim": {"point_estimate": {"value": 0.7}},
               "scope": {"answer": {"status": "improvement_claimed",
                                    "measurements": [{"metric": "m", "baseline": 0.0, "candidate": 0.1}]}}}
    out = derive(control=control, claims=[])
    assert out["point_estimate"]["value"] == 0.7


def test_malformed_scope_and_answer_do_not_break_the_derivation():
    for control in ({"scope": "text"}, {"scope": {"answer": "text"}}, {"scope": None}, {"claim": "text"}):
        out = derive(control=control, claims=[])
        assert out["state"] == "no-claim"


# -- (j) unbounded intervals ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(("low", "high", "want_low", "want_high"), [
    (float("-inf"), 0.07, None, 0.07),
    (0.01, float("inf"), 0.01, None),
    (float("-inf"), float("inf"), None, None),
    (math.nan, 0.07, None, 0.07),
    (None, None, None, None),
])
def test_an_unbounded_interval_side_is_recorded_as_null_and_the_state_stays_json_safe(low, high, want_low, want_high):
    effect = make_effect(ci_low=low, ci_high=high)
    out = derive(control=disposition("confirmed"), assessments=[make_assessment(effect=effect)])
    assert out["uncertainty"]["low"] == want_low and out["uncertainty"]["high"] == want_high
    assert out["point_estimate"]["value"] == 0.042
    json.dumps(out, allow_nan=False)  # raises on any NaN or infinity


# -- (k) designated claim, selection data --------------------------------------------------------------------------
def test_an_unknown_designated_claim_is_named_in_the_obligations_and_never_confirms():
    out = derive(control=disposition("confirmed", claim_id="clm_missing"), assessments=[make_assessment()])
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["claim_id"] is None and out["assessment_id"] is None
    assert out["unfulfilled_obligations"][0] == "designated claim clm_missing is not a claim of this investigation"
    assert out["downgraded_from"] == "confirmed"


def test_an_unknown_designated_claim_without_a_disposition_is_named_even_for_no_claim():
    out = derive(control={"claim": {"claim_id": "clm_missing"}})
    assert out["state"] == "no-claim"
    assert out["unfulfilled_obligations"] == ["designated claim clm_missing is not a claim of this investigation"]


def test_a_pass_over_selection_data_never_gives_protocol_passed():
    plans = [make_plan(status="assessed", data_provenance={"split": "selection"})]
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()], plans=plans)
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["downgraded_from"] == "confirmed"
    assert "remaining_alternatives" not in out


def test_a_pass_over_held_out_data_on_a_plan_still_confirms():
    plans = [make_plan(status="assessed", data_provenance={"split": "held-out"})]
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()], plans=plans)
    assert out["state"] == "protocol-passed"


# -- lifecycle_state -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("outcome", "conformance", "health", "expected"), [
    ("protocol-passed", "conformant", "intact", "protocol-passed"),
    ("protocol-passed", "conformant", None, "protocol-passed"),
    ("protocol-passed", "deviated", "intact", "candidate-submitted"),
    ("protocol-passed", None, "intact", "candidate-submitted"),
    ("protocol-passed", "conformant", "affected", "candidate-submitted"),
    ("protocol-passed", "conformant", "withdrawn", "candidate-submitted"),
    ("protocol-failed", "conformant", "intact", "not-supported"),
    ("protocol-failed", "deviated", "intact", "not-supported"),
    ("protocol-failed", "conformant", None, "not-supported"),
    ("protocol-failed", "conformant", "affected", "candidate-submitted"),
    ("protocol-failed", "conformant", "withdrawn", "candidate-submitted"),
    ("inconclusive", "conformant", "intact", "candidate-submitted"),
    ("inconclusive", "deviated", "intact", "candidate-submitted"),
    (None, None, None, "candidate-submitted"),
    ("something-new", "conformant", "intact", "candidate-submitted"),
])
def test_lifecycle_state_truth_table(outcome, conformance, health, expected):
    assert A.lifecycle_state(outcome, conformance, health) == expected


def test_only_a_conformant_healthy_pass_confirms():
    confirming = [(o, c, h) for o in ("protocol-passed", "protocol-failed", "inconclusive", None)
                  for c in ("conformant", "deviated", None) for h in ("intact", "affected", "withdrawn", None)
                  if A.lifecycle_state(o, c, h) == A.PROTOCOL_PASSED]
    assert confirming == [("protocol-passed", "conformant", "intact"), ("protocol-passed", "conformant", None)]


def test_claim_state_vocabulary_is_complete():
    assert set(A.CLAIM_STATES) == set(A.LABELS) == {"protocol-passed", "candidate-submitted", "not-supported", "abstained",
                                                    "null-reported", "no-claim"}
    assert A.DISPOSITIONS == ("confirmed", "candidate", "null", "abstain")


# -- validate_claim_block ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("block", [
    {}, {"disposition": None}, *({"disposition": d} for d in A.DISPOSITIONS),
    {"disposition": "candidate", "claim_id": "clm_1", "point_estimate": {"value": 0.1}, "evidence_refs": ["a"],
     "obligations": ["b"], "uncertainty": "anything"},
    {"point_estimate": None, "evidence_refs": None, "obligations": None},
])
def test_valid_claim_blocks_are_accepted(block):
    assert A.validate_claim_block(block) is None


@pytest.mark.parametrize("block", [None, "candidate", ["candidate"], 3, True])
def test_a_claim_block_must_be_an_object(block):
    with pytest.raises(Invalid, match="must be an object") as exc:
        A.validate_claim_block(block)
    assert exc.value.code == "INVALID" and exc.value.details["got"] == type(block).__name__


@pytest.mark.parametrize("value", ["certain", "Confirmed", "", 3, ["confirmed"]])
def test_an_unknown_disposition_is_refused(value):
    with pytest.raises(Invalid, match="disposition") as exc:
        A.validate_claim_block({"disposition": value})
    assert exc.value.details["got"] == value


@pytest.mark.parametrize("value", [0.1, "0.1", [0.1], True])
def test_a_point_estimate_must_be_an_object(value):
    with pytest.raises(Invalid, match="point_estimate must be an object"):
        A.validate_claim_block({"point_estimate": value})


@pytest.mark.parametrize("key", ["evidence_refs", "obligations"])
@pytest.mark.parametrize("value", ["art_1", {"a": 1}, 3, True])
def test_evidence_refs_and_obligations_must_be_arrays(key, value):
    with pytest.raises(Invalid, match=f"{key} must be an array"):
        A.validate_claim_block({key: value})
