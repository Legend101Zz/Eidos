"""``assess_candidate`` and the claim state of a conclusion (ADR 0021), end to end against PostgreSQL.

Reuses the ``Flow`` of test_classification_accuracy_e2e.py: the real command service, the real ``AttemptRunner``
(development profile ``dev:same-user``), the real built-in report/template/assessor components and the real assessor
activity. No model is called. A training job of the project's own component is run per recipe; ``assess_candidate`` then
turns two jobs, attested labels and a charter rule into a frozen, launched plan, or into a recorded refusal.

* An operator (no investigation) gets a launched plan whose assessment decides the claim state.
* Inside an investigation the candidate must be named by a ``candidate_lock`` note and the conclusion's claim state is
  derived from recorded assessments, never from the disposition the model asked for.
* Every validation failure is a committed refusal (kind ``assessment_request``) with a reason code, and no plan.
* ``draft_evidence_plan`` accepts a draft over selection data, but ``freeze_evidence_plan`` refuses it.
"""

from __future__ import annotations

import copy
import json

import pytest
from test_classification_accuracy_e2e import (
    ASSESSOR_REF,
    BASELINE_RECIPE,
    CANDIDATE_RECIPE,
    CHARTER,
    ISOLATION,
    N_HELDOUT,
    RULE_ID,
    SEED,
    TEMPLATE_REF,
    Flow,
    _run_plan_jobs,
)
from test_f5_turn_authorization import RUN_A
from test_f5c_ticket_and_handoff import _accept, _begin_turn
from test_f5d_conclude_race import _complete

from eidos.domain.errors import Invalid

pytestmark = pytest.mark.pg

HELD_OUT = {"split": "held-out"}
CLAIM = {"statement": "the candidate recipe raises held-out accuracy", "scope": {"task": "toy-3class"}}
LIMITS = {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100}


# -- helpers --------------------------------------------------------------------------------------------------------
class Scene:
    """One Flow project with a charter that attests the held-out labels, and shorthands for the lab."""

    def __init__(self, lab, tmp_path):
        self.lab = lab
        self.flow = Flow(lab, tmp_path)
        self.pid = self.flow.pid
        self.labels = self.flow.artifact(self.flow.labels(), "class-labels/1")
        self.digest = self.q("get_artifact", {"artifact_id": self.labels})["digest"]
        self.charter_id = self.commit_charter("att")

    def cmd(self, name: str, payload: dict) -> dict:
        return self.lab.cmd(self.lab.admin, name, payload, project=self.pid)["result"]

    def q(self, name: str, params: dict | None = None) -> dict:
        return self.lab.q(self.lab.admin, name, params or {}, project=self.pid)

    def commit_charter(self, suffix: str, *, identity: str | None = None, labels: list[str] | None = None) -> str:
        """A copy of the flow's charter under another study id, with the held-out labels attested (by digest)."""
        charter = copy.deepcopy(CHARTER)
        charter["study_id"] = f"cls-acc-{suffix}"
        charter["held_out"] = {"labels": [self.digest] if labels is None else labels}
        if identity:
            charter["metrics"]["accuracy"]["identity"] = identity
        self.cmd("commit_study_charter", {"charter": charter})
        return f"charter:{charter['study_id']}"

    def job(self, recipe: dict) -> str:
        """Run the project's training job for a recipe (``Flow.train``); returns the job id."""
        before = {j["id"] for j in self.q("list_jobs")}
        self.flow.train(recipe)
        (new,) = {j["id"] for j in self.q("list_jobs")} - before
        return new

    def request(self, base_job: str, cand_job: str, /, **extra) -> dict:
        """A valid paired request over seed 11 and the attested labels; ``extra`` overrides any field."""
        return {"success_rule_id": RULE_ID, "charter_id": self.charter_id, "baseline": {SEED: base_job},
                "candidate": {SEED: cand_job}, "labels": self.labels, "data_provenance": HELD_OUT,
                "required_isolation": ISOLATION, "environment": "lab", "claim": CLAIM, **extra}

    def assess_candidate(self, payload: dict) -> dict:
        return self.cmd("assess_candidate", payload)

    def run_and_assess(self, receipt: dict) -> dict:
        """Run the launched plan's jobs and the assessor; returns the committed assessment."""
        for done in _run_plan_jobs(self.flow, receipt["jobs"]).values():
            assert done["status"] == "succeeded", done.get("terminal")
        assert self.flow.plan_status(receipt["plan_id"]) == "awaiting-assessment"
        return self.flow.assess(receipt["plan_id"])

    # investigations
    def investigation(self, charter_id: str | None = None) -> str:
        return self.cmd("start_investigation", {
            "question": "does the candidate recipe beat the baseline?", "charter_id": charter_id or self.charter_id,
            "environment": "lab", "limits": LIMITS,
            "investigator": {"runtime": "fixture", "settings": {"max_tokens_per_decision": 120_000}}})["investigation_id"]

    def lock(self, inv: str, job_id: str) -> None:
        self.cmd("record_note", {"investigation_id": inv, "kind": "candidate_lock", "text": json.dumps({"job_id": job_id})})

    def conclude(self, inv: str, claim: dict | None) -> dict:
        """Close a turn of the investigation with control ``conclude``; returns the conclusion's claim state."""
        control = {"action": "conclude", "summary": "the investigation is done", **({"claim": claim} if claim else {})}
        tid = _begin_turn(self.lab, self.pid, inv, RUN_A)["turn_id"]
        _accept(self.lab, self.pid, tid, {"rationale": "conclude", "calls": [], "control": control})
        assert _complete(self.lab, self.pid, tid, "concluded", control)["result"]["status"] == "completed"
        body = self.q("get_investigation", {"investigation_id": inv})
        assert body["status"] == "concluded"
        return body["body"]["conclusion"]["claim_state"]

    # checks
    def record(self, kind: str, rid: str) -> dict:
        return self.q("get_record", {"kind": kind, "id": rid})

    def plans(self) -> list[dict]:
        return self.q("list_records", {"kind": "evidence_plan"})

    def claims(self) -> list[str]:
        return sorted(c["id"] for c in self.q("list_records", {"kind": "claim"}))

    def assert_refused(self, receipt: dict, *codes: str, claims: list[str], exploratory: bool = False) -> None:
        """A refusal: the reasons are returned and recorded; no plan and no claim came of it."""
        assert receipt["status"] == "refused", receipt
        got = {r["code"] for r in receipt["reasons"]}
        assert set(codes) <= got, receipt["reasons"]
        assert receipt["exploratory"] is exploratory
        assert self.plans() == [] and self.claims() == claims  # nothing was created
        rec = self.record("assessment_request", receipt["request_id"])
        assert rec["status"] == "refused", rec
        assert {r["code"] for r in rec["body"]["reasons"]} == got and rec["body"]["status"] == "refused"
        assert "plan_id" not in rec["body"]


@pytest.fixture()
def scene(lab, tmp_path):
    return Scene(lab, tmp_path)


# =====================================================================================================================
# 1. an operator's request launches a plan; the assessor's pass is the claim's state
# =====================================================================================================================
def test_operator_request_launches_a_plan_and_a_pass_confirms_the_claim(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    receipt = scene.assess_candidate(scene.request(baseline, candidate))
    assert receipt["status"] == "launched", receipt
    assert receipt["path"] == "classification" and receipt["template"] == TEMPLATE_REF == \
        "eidos.templates.improves-under-budget@1.2.0"
    assert receipt["assessor"] == ASSESSOR_REF == "eidos.assessors.paired-ci@1.2.0"
    assert {arm: len(ids) for arm, ids in receipt["jobs"].items()} == {"baseline": 1, "candidate": 1}
    expected = {"split": "held-out", "attested_by": "charter", "labels_digest": scene.digest}
    assert receipt["data_provenance"] == expected

    plan = scene.record("evidence_plan", receipt["plan_id"])
    assert plan["status"] == "executing"
    assert plan["body"]["data_provenance"] == expected  # recorded on the plan itself
    assert plan["body"]["template"]["ref"] == TEMPLATE_REF and plan["body"]["assessor"]["ref"] == ASSESSOR_REF
    assert plan["body"]["claim_ref"]["id"] == receipt["claim_id"]
    assert plan["body"]["charter_ref"]["id"] == scene.charter_id

    request = scene.record("assessment_request", receipt["request_id"])
    assert request["status"] == "launched"
    assert request["body"]["plan_id"] == receipt["plan_id"] and request["body"]["investigation_id"] is None
    assert request["body"]["data_provenance"] == expected and request["body"]["path"] == "classification"

    assessment = scene.run_and_assess(receipt)
    assert assessment["outcome"] == "protocol-passed" and assessment["conformance"] == "conformant", assessment
    assert assessment["evaluation"]["units_by_role"] == {"baseline": N_HELDOUT, "candidate": N_HELDOUT}
    claim = scene.q("get_claim", {"claim_id": receipt["claim_id"]})
    assert claim["claim_state"]["state"] == "protocol-passed" and claim["claim_state"]["confirmed"] is True
    assert [a["outcome"] for a in claim["assessments"]] == ["protocol-passed"]


# =====================================================================================================================
# 2. a conclusion after a pass, inside an investigation
# =====================================================================================================================
def test_conclusion_after_a_pass_is_protocol_passed(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    scene.lock(inv, candidate)
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    assert receipt["status"] == "launched", receipt
    assert scene.record("evidence_plan", receipt["plan_id"])["body"]["investigation_id"] == inv
    assert scene.record("assessment_request", receipt["request_id"])["body"]["investigation_id"] == inv
    assessment = scene.run_and_assess(receipt)
    assert assessment["outcome"] == "protocol-passed", assessment

    state = scene.conclude(inv, {"disposition": "confirmed"})
    assert state["state"] == "protocol-passed" and state["confirmed"] is True, state
    assert state["requested_disposition"] == "confirmed" and "downgraded_from" not in state
    assert state["claim_id"] == receipt["claim_id"] and state["plan_id"] == receipt["plan_id"]
    assert state["assessment_id"] == assessment["assessment_id"]
    assert state["point_estimate"]["source"] == "assessor" and state["point_estimate"]["metric"] == "accuracy"
    assert state["point_estimate"]["value"] == pytest.approx(assessment["evaluation"]["effect"]["estimate"])
    assert state["point_estimate"]["value"] > 0.1


# =====================================================================================================================
# 3. a conclusion with no assessment cannot confirm
# =====================================================================================================================
def test_confirmation_requested_without_an_assessment_is_downgraded_to_a_submitted_candidate(scene):
    inv = scene.investigation()
    claim_id = scene.cmd("file_claim", {"investigation_id": inv, "kind": "improves", **CLAIM})["claim_id"]
    estimate = {"metric": "accuracy", "baseline": 0.8, "candidate": 0.9}

    state = scene.conclude(inv, {"disposition": "confirmed", "point_estimate": estimate})
    assert state["state"] == "candidate-submitted" and state["confirmed"] is False, state
    assert state["requested_disposition"] == "confirmed" and state["downgraded_from"] == "confirmed"
    assert state["claim_id"] == claim_id and state["assessment_id"] is None
    assert state["point_estimate"]["source"] == "investigator"
    assert state["point_estimate"]["value"] == pytest.approx(0.1)
    assert state["unfulfilled_obligations"], state
    assert any("held-out" in o for o in state["unfulfilled_obligations"])


# =====================================================================================================================
# 4. a conclusion after a failed assessment
# =====================================================================================================================
def test_conclusion_after_a_failed_assessment_is_not_supported(scene):
    baseline, candidate = scene.job(CANDIDATE_RECIPE), scene.job(CANDIDATE_RECIPE)  # equal recipes: zero gain
    inv = scene.investigation()
    scene.lock(inv, candidate)
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    assert receipt["status"] == "launched", receipt
    assessment = scene.run_and_assess(receipt)
    assert assessment["outcome"] == "protocol-failed" and assessment["conformance"] == "conformant", assessment

    state = scene.conclude(inv, {"disposition": "confirmed"})
    assert state["state"] == "not-supported" and state["confirmed"] is False, state
    assert state["downgraded_from"] == "confirmed"
    assert state["assessment_id"] == assessment["assessment_id"] and state["claim_id"] == receipt["claim_id"]
    claim = scene.q("get_claim", {"claim_id": receipt["claim_id"]})
    assert claim["claim_state"]["state"] == "not-supported" and claim["claim_state"]["confirmed"] is False


# =====================================================================================================================
# 5. validation failures are recorded refusals
# =====================================================================================================================
def _no_provenance(s, b, c):
    return s.request(b, c, data_provenance=None), ("provenance-required",), False


def _selection(s, b, c):
    return s.request(b, c, data_provenance={"split": "selection"}), ("selection-data",), True


def _unattested(s, b, c):
    other = s.flow.artifact(s.flow.labels()[:-1], "class-labels/1")  # other bytes: a digest the charter does not name
    return s.request(b, c, labels=other), ("held-out-unattested",), False


def _unpaired(s, b, c):
    return s.request(b, c, candidate={SEED: c, "12": c}), ("seed-pairing",), False


def _wrong_seed(s, b, c):
    return s.request(b, c, baseline={"12": b}, candidate={"12": c}), ("seed-mismatch",), False


def _unknown_job(s, b, c):
    return s.request(b, c, baseline={SEED: "job_does_not_exist"}), ("job-invalid",), False


def _wrong_slot(s, b, c):
    return s.request(b, c, predictions_slot="not-published"), ("output-schema",), False


def _no_assessment_path(s, b, c):
    charter_id = s.commit_charter("other-identity", identity="classification-report/1:macro-f1")
    return s.request(b, c, charter_id=charter_id), ("no-assessment-path",), False


def _template_not_approved(s, b, c):
    s.cmd("withdraw_component", {"component_ref": TEMPLATE_REF, "defect": "test: the template is no longer approved"})
    return s.request(b, c), ("protocol-not-approved",), False


@pytest.mark.parametrize("make", [
    pytest.param(_no_provenance, id="provenance-required"), pytest.param(_selection, id="selection-data"),
    pytest.param(_unattested, id="held-out-unattested"), pytest.param(_unpaired, id="seed-pairing"),
    pytest.param(_wrong_seed, id="seed-mismatch"), pytest.param(_unknown_job, id="job-invalid"),
    pytest.param(_wrong_slot, id="output-schema"), pytest.param(_no_assessment_path, id="no-assessment-path"),
    pytest.param(_template_not_approved, id="protocol-not-approved")])
def test_an_invalid_request_is_a_recorded_refusal_and_creates_no_plan(scene, make):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    claims = scene.claims()  # the flow's own claim
    payload, codes, exploratory = make(scene, baseline, candidate)
    scene.assert_refused(scene.assess_candidate(payload), *codes, claims=claims, exploratory=exploratory)


def test_a_wrong_charter_id_inside_an_investigation_is_refused(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    scene.lock(inv, candidate)
    claims = scene.claims()
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv,
                                                   charter_id=f"charter:{CHARTER['study_id']}"))
    scene.assert_refused(receipt, "charter-mismatch", claims=claims)
    (reason,) = [r for r in receipt["reasons"] if r["code"] == "charter-mismatch"]
    assert reason["details"]["charter_id"] == scene.charter_id  # names the investigation's pinned charter


def test_an_investigation_with_no_candidate_lock_is_refused(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    claims = scene.claims()
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    scene.assert_refused(receipt, "candidate-not-locked", claims=claims)
    assert "candidate_lock" in next(r["message"] for r in receipt["reasons"] if r["code"] == "candidate-not-locked")
    assert scene.record("assessment_request", receipt["request_id"])["body"]["investigation_id"] == inv


# =====================================================================================================================
# 6. a candidate job of another recipe than the locked one
# =====================================================================================================================
def test_a_candidate_job_that_is_not_the_locked_recipe_is_refused(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    scene.lock(inv, baseline)  # the investigation locked the baseline's recipe, not the candidate's
    claims = scene.claims()
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    scene.assert_refused(receipt, "candidate-not-locked", claims=claims)
    (reason,) = [r for r in receipt["reasons"] if r["code"] == "candidate-not-locked"]
    assert candidate in reason["message"] and baseline in reason["message"]  # the job and the locked job are named

    scene.lock(inv, candidate)  # the latest lock note wins: the same request is now accepted
    again = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    assert again["status"] == "launched", again


# =====================================================================================================================
# 7. a draft over selection data can be drafted but not frozen
# =====================================================================================================================
def test_a_plan_draft_over_selection_data_is_drafted_but_never_frozen(scene):
    flow = scene.flow
    placeholder = flow.artifact([{"id": 1, "prediction": 0}], "class-predictions/1")  # planning does not read the rows
    draft = flow.draft(placeholder, placeholder, scene.labels)["draft"]
    draft["data_provenance"] = {"split": "selection"}

    plan = scene.cmd("draft_evidence_plan", {"claim_id": flow.claim_id, "charter_id": f"charter:{CHARTER['study_id']}",
                                             "template_ref": TEMPLATE_REF, "draft": draft})
    assert plan["obligations"] == []
    drafted = scene.record("evidence_plan", plan["plan_id"])
    assert drafted["status"] == "draft" and drafted["body"]["data_provenance"] == {"split": "selection"}

    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": plan["plan_id"]})
    assert "selection" in err.value.message and err.value.details["data_provenance"] == {"split": "selection"}
    assert scene.record("evidence_plan", plan["plan_id"])["status"] == "draft"  # still a draft: nothing was frozen


# =====================================================================================================================
# 8. eidos.project.command publishes prediction-array/1 and task-array/1: typed references to the same bytes
def test_prediction_array_outputs_reach_a_verdict_through_typed_references(scene):
    from test_classification_accuracy_e2e import TRAIN_CODE, TRAIN_MANIFEST
    from test_command_path import _publish

    flow = scene.flow
    manifest = {**TRAIN_MANIFEST, "id": "proj.array-centroid", "output": {"predictions": "prediction-array/1"}}
    code = TRAIN_CODE.replace('schema="class-predictions/1"', 'schema="prediction-array/1"')
    ref = _publish(scene.lab, scene.pid, manifest, {"m.py": code})["component_ref"]

    def train(recipe: dict) -> str:
        job_id = scene.cmd("run_component", {"component_ref": ref, "environment": "lab", "isolation_profile": "dev:same-user",
                                             "config": {**recipe, "data_seed": 7, "n_heldout": N_HELDOUT}})["job_id"]
        assert flow.run(job_id)["status"] == "succeeded"
        return job_id

    labels = flow.artifact(flow.labels(), "task-array/1")
    assert scene.q("get_artifact", {"artifact_id": labels})["digest"] == scene.digest  # the attested bytes
    baseline, candidate = train(BASELINE_RECIPE), train(CANDIDATE_RECIPE)
    receipt = scene.assess_candidate(scene.request(baseline, candidate, labels=labels))
    assert receipt["status"] == "launched", receipt

    arm = next(a for a in scene.record("evidence_plan", receipt["plan_id"])["body"]["arms"] if a["id"] == "candidate")
    inputs = arm["runs"][0]["jobs"][0]["inputs"]
    typed = scene.q("get_artifact", {"artifact_id": inputs["labels"]})
    assert typed["schema"] == "class-labels/1" and typed["digest"] == scene.digest and inputs["labels"] != labels
    assert scene.q("get_artifact", {"artifact_id": inputs["candidate"]})["schema"] == "class-predictions/1"
    assert scene.run_and_assess(receipt)["outcome"] == "protocol-passed"


# =====================================================================================================================
# 9. the train/evaluate path (paired-report metrics)
EVALUATION = {"split": "heldout", "data_seed": 180, "pairs": 100}
TRAIN_EVALUATE = {"seeds": [1, 2, 3], "baseline": {"train": {"epochs": 1}}, "candidate": {"train": {"epochs": 2}},
                  "evaluation": EVALUATION}


def _paired_charter(s, splits: list[str]) -> str:
    charter = copy.deepcopy(CHARTER)
    charter["study_id"] = f"paired-{len(splits)}"
    charter["metrics"]["accuracy"]["identity"] = "paired-report/1:both-correct-rate"
    charter["held_out"] = {"splits": splits}
    s.cmd("commit_study_charter", {"charter": charter})
    return f"charter:{charter['study_id']}"


def _install_pytorch_components(s) -> None:
    from test_classification_accuracy_e2e import _builtin
    from test_command_path import _publish

    for name in ("eidos.pytorch.train", "eidos.pytorch.evaluate"):
        manifest, files = _builtin(name)
        _publish(s.lab, s.pid, manifest, files)


def _paired_request(s, charter_id: str, spec) -> dict:
    return {"success_rule_id": RULE_ID, "charter_id": charter_id, "claim": CLAIM, "data_provenance": HELD_OUT,
            "required_isolation": ISOLATION, **({"train_evaluate": spec} if spec is not None else {})}


@pytest.mark.parametrize("spec, code, splits", [
    pytest.param(None, "request-invalid", ["heldout"], id="no-spec"),
    pytest.param({**TRAIN_EVALUATE, "parent": {"checkpoint_ref": "art_x"}}, "not-fresh-initialization", ["heldout"], id="parent"),
    pytest.param({**TRAIN_EVALUATE, "seeds": []}, "seed-pairing", ["heldout"], id="no-seeds"),
    pytest.param({**TRAIN_EVALUATE, "seeds": [1, 1]}, "seed-pairing", ["heldout"], id="repeated-seed"),
    pytest.param({**TRAIN_EVALUATE, "evaluation": {"data_seed": 1}}, "output-schema", ["heldout"], id="no-split"),
    pytest.param(TRAIN_EVALUATE, "held-out-unattested", ["other"], id="split-unattested"),
])
def test_the_train_evaluate_path_refuses_an_invalid_request(scene, spec, code, splits):
    _install_pytorch_components(scene)
    claims = scene.claims()
    receipt = scene.assess_candidate(_paired_request(scene, _paired_charter(scene, splits), spec))
    scene.assert_refused(receipt, code, claims=claims)


def test_the_train_evaluate_path_refuses_when_its_components_are_not_installed(scene):
    claims = scene.claims()
    receipt = scene.assess_candidate(_paired_request(scene, _paired_charter(scene, ["heldout"]), TRAIN_EVALUATE))
    scene.assert_refused(receipt, "output-schema", claims=claims)


def test_a_step_refused_at_launch_rolls_the_whole_pipeline_back(scene):
    """A valid train/evaluate request reaches launch; the project has no model adapter, so admission of the first
    step is refused. Nothing is left behind: no plan, no new claim, and the reason is recorded."""
    _install_pytorch_components(scene)
    claims = scene.claims()
    receipt = scene.assess_candidate(_paired_request(scene, _paired_charter(scene, ["heldout"]), TRAIN_EVALUATE))
    scene.assert_refused(receipt, "step-blocked", claims=claims)
    assert "blocked" in receipt["reasons"][0]["details"]
