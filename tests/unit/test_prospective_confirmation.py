"""Prospective confirmation (ADR 0021): what a plan is verified to be able to confirm at freeze.

``can_confirm``, ``plan_steps``, ``literal_inputs`` and ``confirmation_status`` decide from the ledger's rows, not from
what a draft says. A plan pinning results that already exist is refused; a plan that declares held-out data the charter
does not attest is refused; anything else not verified held-out can be frozen and run but stays exploratory.

Author tests of pure rules: no database, no models, no network.
"""

from __future__ import annotations

import pytest

from eidos.domain import assessment as A
from eidos.domain import evidence as ev
from eidos.domain.errors import Invalid

LABELS_ART, LABELS_DIGEST = "art_labels", "sha256:labels-attested"
CHARTER_ID = "charter:accuracy-study"
CHARTER = {"held_out": {"labels": [LABELS_DIGEST], "splits": ["final"]}}
LABELS_ROW = {"schema": "class-labels/1", "digest": LABELS_DIGEST}


def run_job(arm: str, seed: int, *, labels: str | None = LABELS_ART, split: str | None = None, **inputs):
    config = {"role": arm, "cluster": str(seed)}
    if split:
        config["evaluation"] = {"split": split}
    wired = {"baseline": f"baseline.s{seed}.train.predictions", "candidate": f"candidate.s{seed}.train.predictions",
             **({"labels": labels} if labels else {}), **inputs}
    return [{"id": f"{arm}.s{seed}.train", "component": "eidos.pytorch.train", "inputs": {}, "config": {"seed": seed}},
            {"id": f"{arm}.s{seed}.report", "component": A.REPORT_REF, "inputs": wired, "config": config}]


def make_plan(*, provenance="default", labels=LABELS_ART, split=None, seeds=(1, 2), **extra_inputs):
    arms = [{"id": role, "role": role,
             "runs": [{"seed": s, "cluster": str(s),
                       "jobs": run_job(role, s, labels=labels, split=split, **extra_inputs)} for s in seeds]}
            for role in A.ROLES]
    return {"arms": arms, "charter_ref": {"id": CHARTER_ID, "revision": 1},
            "data_provenance": {"split": "held-out"} if provenance == "default" else provenance}


def status(plan, *, charter=CHARTER, artifacts=None, results=frozenset()):
    return ev.confirmation_status(plan, charter=charter, artifacts={LABELS_ART: LABELS_ROW, **(artifacts or {})},
                                  result_digests=set(results))


# -- can_confirm ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("plan", [None, {}, {"arms": []}, {"confirmation": None}, {"confirmation": {}},
                                  {"confirmation": {"eligible": False}}, {"confirmation": {"eligible": "yes"}},
                                  {"confirmation": {"eligible": 1}}])
def test_a_plan_with_no_confirmation_verdict_or_a_false_one_cannot_confirm(plan):
    assert A.can_confirm(plan) is False


def test_a_plan_body_without_a_confirmation_key_is_a_historical_plan_that_cannot_confirm():
    historical = {"success_rule_id": "r", "data_provenance": {"split": "held-out"}, "arms": []}
    assert "confirmation" not in historical and A.can_confirm(historical) is False


def test_only_an_explicitly_eligible_plan_can_confirm():
    assert A.can_confirm({"confirmation": {"eligible": True}}) is True


# -- plan_steps and literal_inputs ---------------------------------------------------------------------------------
def test_plan_steps_reads_both_arm_shapes():
    plan = {"arms": [{"id": "a", "run": {"component_ref": "c", "inputs": {"x": "art_1"}}},
                     {"id": "b", "runs": [{"seed": 1, "jobs": [{"id": "b.s1.train"}, {"id": "b.s1.report"}]}]}]}
    assert [(s["arm"], s["id"]) for s in ev.plan_steps(plan)] == [("a", "run"), ("b", "b.s1.train"), ("b", "b.s1.report")]
    assert ev.plan_steps(plan)[0]["spec"] == plan["arms"][0]["run"]
    assert ev.plan_steps({}) == [] and ev.plan_steps({"arms": [{"id": "z"}]}) == []


def test_literal_inputs_are_artifact_ids_and_not_references_to_the_plans_own_steps():
    literals = ev.literal_inputs(make_plan(seeds=(1,)))
    assert {(i["arm"], i["step"], i["port"], i["artifact_id"]) for i in literals} == {
        ("baseline", "baseline.s1.report", "labels", LABELS_ART), ("candidate", "candidate.s1.report", "labels", LABELS_ART)}


def test_literal_inputs_skip_values_that_are_not_text():
    plan = {"arms": [{"id": "a", "run": {"inputs": {"data": "art_1", "n": 3, "none": None}}}]}
    assert ev.literal_inputs(plan) == [{"arm": "a", "step": "run", "port": "data", "artifact_id": "art_1"}]


# -- confirmation_status: verified held-out data (F1) --------------------------------------------------------------
@pytest.mark.parametrize("declared", [None, {}, {"split": "selection"}, {"split": "validation"}, "held-out"])
def test_a_plan_with_no_held_out_provenance_is_exploratory_with_no_verified_held_out_reason(declared):
    result = status(make_plan(provenance=declared))
    assert result["eligible"] is False and result["verified"] == {} and result["data_keys"] == []
    assert any("no verified held-out" in reason for reason in result["reasons"])


def test_a_plan_with_no_data_provenance_at_all_is_exploratory():
    plan = make_plan()
    del plan["data_provenance"]
    result = status(plan)
    assert result["eligible"] is False and any("no verified held-out" in r for r in result["reasons"])


def test_a_plan_declaring_held_out_labels_the_charter_does_not_attest_is_refused():
    other = {"schema": "class-labels/1", "digest": "sha256:not-attested"}
    with pytest.raises(Invalid) as exc:
        status(make_plan(labels="art_other"), artifacts={"art_other": other})
    assert exc.value.details["reason_code"] == "held-out-unattested"
    assert exc.value.details["arms"] == ["baseline", "candidate"]
    assert exc.value.details["attested"] == {"labels": [LABELS_DIGEST], "splits": ["final"]}


def test_a_plan_declaring_held_out_with_an_unattested_evaluation_split_is_refused():
    with pytest.raises(Invalid) as exc:
        status(make_plan(labels=None, split="validation"))
    assert exc.value.details["reason_code"] == "held-out-unattested"


@pytest.mark.parametrize("charter", [{}, {"held_out": None}, {"held_out": {}}, {"held_out": {"labels": []}}])
def test_a_charter_attesting_nothing_refuses_a_plan_declaring_held_out(charter):
    with pytest.raises(Invalid) as exc:
        status(make_plan(), charter=charter)
    assert exc.value.details["reason_code"] == "held-out-unattested"


def test_an_arm_with_no_labels_and_no_evaluation_split_is_refused_even_when_the_declaration_is_held_out():
    with pytest.raises(Invalid) as exc:
        status(make_plan(labels=None, split=None))
    assert exc.value.details["reason_code"] == "held-out-unattested"
    assert exc.value.details["arms"] == ["baseline", "candidate"]


def test_only_the_arm_whose_labels_are_not_attested_is_named():
    plan = make_plan()
    for step in ev.plan_steps(plan):
        if step["arm"] == "candidate":
            step["spec"]["inputs"]["labels"] = "art_other"
    with pytest.raises(Invalid) as exc:
        status(plan, artifacts={"art_other": {"schema": "class-labels/1", "digest": "sha256:not-attested"}})
    assert exc.value.details["arms"] == ["candidate"]


def test_a_plan_whose_labels_are_attested_is_eligible_and_names_the_labels_it_uses():
    result = status(make_plan())
    assert result["eligible"] is True and result["reasons"] == []
    assert result["data_keys"] == [f"labels:{LABELS_DIGEST}"]
    assert result["verified"] == {"split": "held-out", "attested_by": "charter", "labels_digests": [LABELS_DIGEST],
                                  "evaluation_splits": []}


def test_a_plan_whose_evaluation_split_is_attested_is_eligible_and_names_the_split():
    result = status(make_plan(labels=None, split="final"))
    assert result["eligible"] is True
    assert result["data_keys"] == [f"split:{CHARTER_ID}:final"]
    assert result["verified"]["evaluation_splits"] == ["final"]


def test_a_plan_with_attested_labels_and_an_attested_split_names_both():
    result = status(make_plan(split="final"))
    assert result["eligible"] is True
    assert result["data_keys"] == [f"labels:{LABELS_DIGEST}", f"split:{CHARTER_ID}:final"]


# -- confirmation_status: a pinned checkpoint is not a fresh run (F1) ----------------------------------------------
CKPT = {"schema": "eidos-checkpoint/1", "digest": "sha256:ckpt", "producer_job": "job_trained"}


def test_a_checkpoint_an_earlier_job_trained_pinned_on_the_checkpoint_port_makes_the_plan_exploratory():
    result = status(make_plan(checkpoint="art_ckpt"), artifacts={"art_ckpt": CKPT})
    assert result["eligible"] is False
    assert any("'checkpoint'" in r and "already trained" in r for r in result["reasons"])
    assert result["data_keys"] == [f"labels:{LABELS_DIGEST}"]  # the data are still verified; the runs are not fresh


def test_a_port_named_parent_checkpoint_does_not_make_the_plan_exploratory():
    result = status(make_plan(parent_checkpoint="art_ckpt"), artifacts={"art_ckpt": CKPT})
    assert result["eligible"] is True and result["reasons"] == []


def test_a_checkpoint_schema_on_any_other_port_makes_the_plan_exploratory():
    result = status(make_plan(weights="art_ckpt"), artifacts={"art_ckpt": {**CKPT, "schema": "model-file/1"}})
    assert result["eligible"] is False and any("'weights'" in r for r in result["reasons"])


def test_a_pinned_checkpoint_that_no_job_produced_does_not_make_the_plan_exploratory():
    uploaded = {"schema": "eidos-checkpoint/1", "digest": "sha256:ckpt"}
    result = status(make_plan(checkpoint="art_ckpt"), artifacts={"art_ckpt": uploaded})
    assert result["eligible"] is True


def test_a_checkpoint_produced_by_a_step_of_the_plan_is_not_a_pinned_input():
    plan = make_plan(seeds=(1,), checkpoint="baseline.s1.train.checkpoint")
    assert status(plan)["eligible"] is True


# -- confirmation_status: results that already exist are refused (F2) ----------------------------------------------
def test_a_plan_pinning_class_predictions_is_refused_as_observed_evidence():
    with pytest.raises(Invalid) as exc:
        status(make_plan(baseline="art_pred"), artifacts={"art_pred": {"schema": "class-predictions/1",
                                                                         "digest": "sha256:p"}})
    assert exc.value.details["reason_code"] == "observed-evidence"
    assert {i["artifact_id"] for i in exc.value.details["inputs"]} == {"art_pred"}
    assert "already exist" in exc.value.message


@pytest.mark.parametrize("schema", ["prediction-array/1", "classification-pair-rows/1", "labeled-binary-pairs/1"])
def test_every_result_schema_is_refused_as_observed_evidence(schema):
    with pytest.raises(Invalid) as exc:
        status(make_plan(candidate="art_res"), artifacts={"art_res": {"schema": schema, "digest": "sha256:r"}})
    assert exc.value.details["reason_code"] == "observed-evidence"


def test_a_digest_that_is_a_known_result_is_refused_even_under_another_schema():
    retyped = {"schema": "dataset/1", "digest": "sha256:seen-result"}
    with pytest.raises(Invalid) as exc:
        status(make_plan(baseline="art_alias"), artifacts={"art_alias": retyped}, results={"sha256:seen-result"})
    assert exc.value.details["reason_code"] == "observed-evidence"
    assert {i["port"] for i in exc.value.details["inputs"]} == {"baseline"}


def test_observed_evidence_is_refused_before_provenance_is_considered():
    plan = make_plan(provenance=None, baseline="art_pred")
    with pytest.raises(Invalid) as exc:
        status(plan, artifacts={"art_pred": {"schema": "class-predictions/1", "digest": "sha256:p"}})
    assert exc.value.details["reason_code"] == "observed-evidence"


def test_a_digest_that_is_not_a_known_result_is_not_refused():
    other = {"schema": "dataset/1", "digest": "sha256:inputs"}
    result = status(make_plan(extra="art_inputs"), artifacts={"art_inputs": other}, results={"sha256:elsewhere"})
    assert result["eligible"] is True
