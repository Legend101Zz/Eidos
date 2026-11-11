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
            "data_provenance": {"split": "held-out"}, "confirmation": {"eligible": True}, **over}


def make_charter(**over: Any) -> dict[str, Any]:
    return {"population": POPULATION, "success_rules": [{"id": "practical-accuracy-gain",
                                                         "decision_standard": {"kind": "paired-difference-ci",
                                                                               "confidence": 0.95}}], **over}


def derive(*, control: dict[str, Any] | None = None, claims: Any = "default", assessments: Any = None,
           plans: Any = None, charter: Any = "default") -> dict[str, Any]:
    assessments = assessments or []
    if plans is None:  # by default each assessment ran on a plan that was verified at freeze as able to confirm
        plans = list(({a["plan_id"]: make_plan(a["plan_id"], status="assessed") for a in assessments}).values())
    return A.derive_claim_state(control=control if control is not None else {},
                                claims=[make_claim()] if claims == "default" else claims,
                                assessments=assessments, plans=plans,
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


@pytest.mark.parametrize(("direction", "metric", "baseline", "candidate", "expected"), [
    ("higher_is_better", "val_loss", 0.50, 0.58, 0.08),
    (None, "score", 0.50, 0.58, 0.08),  # nothing says which way the metric points: the gain is candidate - baseline
    ("lower_is_better", "val_loss", 10.0, 8.0, 2.0),
    ("lower_is_better", "val_loss", 0.50, 0.58, -0.08),
    (None, "val_loss", 10.0, 8.0, 2.0),  # the conventional name says lower is better
    (None, "val_loss", 0.50, 0.58, -0.08),
])
def test_legacy_measurements_give_the_point_estimate_and_lower_is_better_flips_the_sign(direction, metric, baseline,
                                                                                         candidate, expected):
    measurement = {"metric": metric, "split": "validation", "denominator": "examples", "baseline": baseline,
                   "candidate": candidate, "uncertainty_method": "bootstrap",
                   **({"direction": direction} if direction else {})}
    control = {"scope": {"answer": {"status": "improvement_claimed", "measurements": [measurement]}}}
    out = derive(control=control, claims=[])
    estimate = out["point_estimate"]
    assert estimate["value"] == pytest.approx(expected)
    assert estimate["source"] == "investigator"
    assert (estimate["metric"], estimate["split"], estimate["denominator"]) == (metric, "validation", "examples")
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


# -- an abstention or a null in either field wins over the other --------------------------------------------------
def both_fields(requested: str | None, status: str | None, **block: Any) -> dict[str, Any]:
    """A conclusion that states what it wants in ``claim.disposition`` and in ``scope.answer.status`` (either may be
    absent)."""
    control: dict[str, Any] = {"action": "conclude"}
    if requested is not None or block:
        control["claim"] = {**({"disposition": requested} if requested is not None else {}), **block}
    if status is not None:
        control["scope"] = {"answer": {"status": status}}
    return control


@pytest.mark.parametrize(("requested", "status", "state"), [
    ("candidate", "abstained", "abstained"),
    ("confirmed", "abstained", "abstained"),
    ("candidate", "invalid", "abstained"),
    ("candidate", "no_improvement_found", "null-reported"),
    ("confirmed", "no_improvement_found", "null-reported"),
    ("abstain", "improvement_claimed", "abstained"),
    ("null", "improvement_claimed", "null-reported"),
    (None, "abstained", "abstained"),
    ("abstain", None, "abstained"),
])
@pytest.mark.parametrize("with_pass", [False, True], ids=["no-assessment", "assessor-pass"])
def test_an_abstention_or_a_null_in_either_field_wins_over_the_other_field(requested, status, state, with_pass):
    out = derive(control=both_fields(requested, status), assessments=[make_assessment()] if with_pass else [])
    assert out["state"] == state and out["confirmed"] is False
    assert out["requested_disposition"] == ("abstain" if state == "abstained" else "null")
    assert "downgraded_from" not in out


def test_a_claim_block_that_asks_for_confirmation_cannot_override_an_abstained_answer_status():
    control = both_fields("confirmed", "abstained", point_estimate={"value": 0.3})
    out = derive(control=control, assessments=[make_assessment()])
    assert out["state"] == "abstained" and out["confirmed"] is False and "downgraded_from" not in out


def test_candidate_and_improvement_claimed_in_both_fields_still_defer_to_the_evidence():
    out = derive(control=both_fields("candidate", "improvement_claimed"), assessments=[make_assessment()])
    assert out["state"] == "protocol-passed" and out["requested_disposition"] == "candidate"


# -- a pass on a plan that cannot confirm ----------------------------------------------------------------------------
def test_a_pass_on_a_plan_with_no_confirmation_block_is_a_submitted_candidate_naming_verified_held_out_data():
    plans = [make_plan(status="assessed")]
    del plans[0]["confirmation"]  # a plan frozen before the check existed
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()], plans=plans)
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False and out["downgraded_from"] == "confirmed"
    assert any("verified held-out" in o and "cannot confirm" in o for o in out["unfulfilled_obligations"])
    assert out["assessment_id"] == "asm_0001"


def test_a_pass_on_a_plan_missing_from_the_ledger_view_cannot_confirm():
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()], plans=[])
    assert out["state"] == "candidate-submitted"


def test_an_exploratory_plan_does_not_hide_an_intact_failure_on_it():
    plans = [make_plan(status="assessed", confirmation={"eligible": False})]
    out = derive(assessments=[make_assessment(outcome="protocol-failed")], plans=plans)
    assert out["state"] == "not-supported"


# -- the direction of a reported metric ------------------------------------------------------------------------------
@pytest.mark.parametrize(("metric", "explicit", "charter", "expected"), [
    ("rmse", None, None, -1), ("val_loss", None, None, -1), ("MAE", None, None, -1), ("test_error", None, None, -1),
    ("accuracy", None, None, 1), ("macro-f1", None, None, 1), ("auc", None, None, 1),
    ("score", None, None, None), ("zorp", None, {}, None), (None, None, None, None),
    ("score", "lower_is_better", None, -1), ("score", "decrease", None, -1), ("score", "minimize", None, -1),
    ("score", "higher_is_better", None, 1), ("score", "increase", None, 1),
    ("rmse", "higher_is_better", None, 1), ("accuracy", "lower_is_better", None, -1),  # what was stated wins
    ("score", None, {"metrics": {"score": {"direction": "decrease"}}}, -1),
    ("score", None, {"metrics": {"score": {"direction": "increase"}}}, 1),
    ("accuracy", None, {"metrics": {"accuracy": {"direction": "decrease"}}}, -1),  # the charter outranks the name
    ("score", "higher_is_better", {"metrics": {"score": {"direction": "decrease"}}}, 1),  # stated outranks the charter
    ("custom/1:score", None, {"metrics": {"s": {"identity": "custom/1:score", "direction": "decrease"}}}, -1),
    ("score", None, {"metrics": {"score": {"direction": "sideways"}}}, None),
    ("score", None, {"metrics": None}, None),
])
def test_metric_direction_is_the_statement_then_the_charter_then_the_conventional_name(metric, explicit, charter,
                                                                                       expected):
    assert A.metric_direction(metric, explicit, charter) == expected


# -- reported estimates are signed improvements ------------------------------------------------------------------------
def reported(point: dict[str, Any], *, charter: Any = "default", state: str = "candidate") -> dict[str, Any]:
    return derive(control=disposition(state, point_estimate=point), charter=charter)["point_estimate"]


def test_an_rmse_that_falls_is_a_positive_improvement():
    estimate = reported({"metric": "rmse", "baseline": 2.0, "candidate": 1.5})
    assert estimate["value"] == pytest.approx(0.5) and estimate["source"] == "investigator"
    assert (estimate["baseline"], estimate["candidate"], estimate["metric"]) == (2.0, 1.5, "rmse")
    assert "direction_unknown" not in estimate


def test_an_rmse_that_rises_is_a_negative_improvement():
    assert reported({"metric": "rmse", "baseline": 1.5, "candidate": 2.0})["value"] == pytest.approx(-0.5)


@pytest.mark.parametrize(("baseline", "candidate", "expected"), [(2.0, 1.5, 0.5), (1.5, 2.0, -0.5)])
def test_a_lower_is_better_direction_from_the_charter_normalizes_a_neutral_metric(baseline, candidate, expected):
    charter = make_charter(metrics={"score": {"direction": "decrease"}, "rmse": {"direction": "decrease"}})
    estimate = reported({"metric": "score", "baseline": baseline, "candidate": candidate}, charter=charter)
    assert estimate["value"] == pytest.approx(expected)
    by_identity = make_charter(metrics={"m": {"identity": "custom/1:score", "direction": "decrease"}})
    assert reported({"metric": "custom/1:score", "baseline": baseline, "candidate": candidate},
                    charter=by_identity)["value"] == pytest.approx(expected)


@pytest.mark.parametrize(("baseline", "candidate", "expected"), [(2.0, 1.5, 0.5), (1.5, 2.0, -0.5)])
def test_an_explicit_lower_is_better_direction_normalizes_a_neutral_metric(baseline, candidate, expected):
    point = {"metric": "score", "baseline": baseline, "candidate": candidate, "direction": "lower_is_better"}
    estimate = reported(point, charter=None)
    assert estimate["value"] == pytest.approx(expected) and estimate["direction"] == "lower_is_better"


def test_an_explicit_direction_wins_over_the_charter_and_the_name():
    charter = make_charter(metrics={"rmse": {"direction": "decrease"}})
    point = {"metric": "rmse", "baseline": 2.0, "candidate": 1.5, "direction": "higher_is_better"}
    assert reported(point, charter=charter)["value"] == pytest.approx(-0.5)


def test_a_higher_is_better_accuracy_is_unchanged():
    estimate = reported({"metric": "accuracy", "baseline": 0.50, "candidate": 0.58}, charter=None)
    assert estimate["value"] == pytest.approx(0.08) and "direction_unknown" not in estimate
    assert reported({"metric": "accuracy", "baseline": 0.58, "candidate": 0.50})["value"] == pytest.approx(-0.08)


def test_a_known_direction_wins_over_a_value_the_investigator_gave():
    estimate = reported({"metric": "rmse", "value": -0.5, "baseline": 2.0, "candidate": 1.5})
    assert estimate["value"] == pytest.approx(0.5)


def test_an_unrecognized_metric_with_baseline_and_candidate_and_no_direction_gives_no_value():
    estimate = reported({"metric": "zorp", "baseline": 1.0, "candidate": 2.0}, charter=None)
    assert estimate["value"] is None and estimate["direction_unknown"] is True
    assert (estimate["baseline"], estimate["candidate"], estimate["source"]) == (1.0, 2.0, "investigator")


def test_an_unknown_direction_keeps_the_value_or_gain_the_investigator_gave():
    given = reported({"metric": "zorp", "baseline": 1.0, "candidate": 2.0, "value": 0.7})
    assert given["value"] == 0.7 and "direction_unknown" not in given
    gain = reported({"metric": "zorp", "baseline": 1.0, "candidate": 2.0, "gain": 0.4})
    assert gain["value"] == 0.4 and "direction_unknown" not in gain


def test_a_value_alone_is_passed_through_as_the_improvement_whatever_the_metric():
    assert reported({"metric": "rmse", "value": 0.25})["value"] == 0.25
    assert reported({"value": 0.25})["value"] == 0.25


def test_the_unknown_direction_marker_reaches_the_conclusion_json_safely():
    out = derive(control=disposition("candidate", point_estimate={"metric": "zorp", "baseline": 1.0, "candidate": 2.0}))
    json.dumps(out)
    assert out["point_estimate"]["value"] is None and out["point_estimate"]["direction_unknown"] is True


@pytest.mark.parametrize("state", ["candidate", "null", "abstain"])
def test_the_reported_estimate_is_normalized_in_every_state_that_carries_it(state):
    estimate = reported({"metric": "rmse", "baseline": 2.0, "candidate": 1.5}, state=state)
    assert estimate["value"] == pytest.approx(0.5)


def test_the_legacy_measurements_path_uses_the_charter_when_the_measurement_states_no_direction():
    measurement = {"metric": "score", "baseline": 2.0, "candidate": 1.5}
    control = {"scope": {"answer": {"status": "improvement_claimed", "measurements": [measurement]}}}
    charter = make_charter(metrics={"score": {"direction": "decrease"}})
    assert derive(control=control, claims=[], charter=charter)["point_estimate"]["value"] == pytest.approx(0.5)
    assert derive(control=control, claims=[])["point_estimate"]["value"] == pytest.approx(-0.5)  # nothing says: default
    stated = {**measurement, "direction": "lower_is_better"}
    control = {"scope": {"answer": {"status": "improvement_claimed", "measurements": [stated]}}}
    assert derive(control=control, claims=[])["point_estimate"]["value"] == pytest.approx(0.5)


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
    plans = [make_plan(status="assessed", data_provenance={"split": "selection"}, confirmation={"eligible": False})]
    out = derive(control=disposition("confirmed"), assessments=[make_assessment()], plans=plans)
    assert out["state"] == "candidate-submitted" and out["confirmed"] is False
    assert out["downgraded_from"] == "confirmed"
    assert "remaining_alternatives" not in out
    assert any("selection data" in o and "cannot confirm" in o for o in out["unfulfilled_obligations"])


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
