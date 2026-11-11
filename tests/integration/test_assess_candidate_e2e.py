"""``assess_candidate`` and the claim state of a conclusion (ADR 0021), end to end against PostgreSQL.

Reuses the ``Flow`` of test_classification_accuracy_e2e.py: the real command service, the real ``AttemptRunner``
(development profile ``dev:same-user``), the real built-in report/template/assessor components and the real assessor
activity. No model is called. A training job of the project's own component is run per recipe; ``assess_candidate`` then
turns two jobs, attested labels and a charter rule into a frozen, launched plan, or into a recorded refusal.

* An operator (no investigation) gets a launched plan whose assessment decides the claim state. Recorded jobs only
  name recipes: the plan freezes the recipes, fresh seeds, the attested labels and the rule, and its arms are trained
  and reported after the freeze (confirmation is prospective).
* Inside an investigation the candidate must be named by a ``candidate_lock`` note and the conclusion's claim state is
  derived from recorded assessments, never from the disposition the model asked for.
* Every validation failure is a committed refusal (kind ``assessment_request``) with a reason code, and no plan.
* ``draft_evidence_plan`` accepts a draft over selection data, but ``freeze_evidence_plan`` refuses it; it also refuses
  plans around results that already exist, plans declaring held-out data the charter does not attest, and a second
  confirmation on held-out data a plan already used. A plan with no held-out provenance freezes but stays exploratory.
"""

from __future__ import annotations

import copy
import json
import uuid

import pytest
from test_classification_accuracy_e2e import (
    ASSESSOR_REF,
    BASELINE_RECIPE,
    CANDIDATE_RECIPE,
    CHARTER,
    FRESH_SEED,
    ISOLATION,
    N_HELDOUT,
    REPORT,
    RULE_ID,
    SEED,
    TEMPLATE_REF,
    Flow,
    _run_plan,
)
from test_f5_turn_authorization import RUN_A
from test_f5c_ticket_and_handoff import _accept, _begin_turn
from test_f5d_conclude_race import _complete

from eidos.domain.assessment import REPORT_REF
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
        """A valid request: the two recorded jobs name the recipes, the plan trains them anew under one fresh seed on
        the attested labels; ``extra`` overrides any field."""
        return {"success_rule_id": RULE_ID, "charter_id": self.charter_id, "baseline": {"job_id": base_job},
                "candidate": {"job_id": cand_job}, "seeds": [int(FRESH_SEED)], "labels": self.labels,
                "data_provenance": HELD_OUT, "required_isolation": ISOLATION, "environment": "lab", "claim": CLAIM, **extra}

    def assess_candidate(self, payload: dict) -> dict:
        return self.cmd("assess_candidate", payload)

    def run_and_assess(self, receipt: dict) -> dict:
        """Drain the launched plan's step DAG (each arm trains, then reports) and run the assessor; returns the
        committed assessment."""
        return self.run_plan_and_assess(receipt["plan_id"])

    def run_plan_and_assess(self, plan_id: str) -> dict:
        finished = _run_plan(self.flow, plan_id)
        assert len(finished) == 4, finished.keys()  # two arms x (training step, report step) for one seed
        for done in finished.values():
            assert done["status"] == "succeeded", done.get("terminal")
        assert self.flow.plan_status(plan_id) == "awaiting-assessment"
        return self.flow.assess(plan_id)

    # manual plans: a draft built by hand, through the same commands an investigator or operator uses
    def fresh_draft(self, base_job: str, cand_job: str, *, labels: str | None = None, seeds: list[str] | None = None) -> dict:
        return self.flow.fresh_draft(base_job, cand_job, labels or self.labels, seeds=seeds)

    def draft_plan(self, draft: dict, *, claim_id: str | None = None, investigation_id: str | None = None) -> str:
        plan = self.cmd("draft_evidence_plan", {
            "claim_id": claim_id or self.flow.claim_id, "charter_id": self.charter_id, "template_ref": TEMPLATE_REF,
            "draft": draft, **({"investigation_id": investigation_id} if investigation_id else {})})
        assert plan["obligations"] == []
        return plan["plan_id"]

    def launch_plan(self, draft: dict, **kw) -> str:
        """Draft, freeze and launch a hand-built draft; returns the plan id."""
        plan_id = self.draft_plan(draft, **kw)
        self.cmd("freeze_evidence_plan", {"plan_id": plan_id})
        self.cmd("launch_evidence_plan", {"plan_id": plan_id})
        return plan_id

    # investigations
    def investigation(self, charter_id: str | None = None) -> str:
        return self.cmd("start_investigation", {
            "question": "does the candidate recipe beat the baseline?", "charter_id": charter_id or self.charter_id,
            "environment": "lab", "limits": LIMITS,
            "investigator": {"runtime": "fixture", "settings": {"max_tokens_per_decision": 120_000}}})["investigation_id"]

    def lock(self, inv: str, job_id: str, *, text: str | None = None, refs: list | None = None) -> None:
        """A ``candidate_lock`` note as the ledger keeps it. These tests are about which lock assess_candidate reads;
        the selection checks a lock passes on record_note (a rule recorded first, re-locks only on better recorded
        evidence: ADR 0023, R14) are covered by test_incumbent_challengers, so the note is written directly."""
        body = {"investigation_id": inv, "kind": "candidate_lock", "text": text if text is not None
                else json.dumps({"job_id": job_id}), **({"evidence_refs": refs} if refs is not None else {})}
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            tx.put_record("note", f"note_{uuid.uuid4().hex[:12]}", self.pid, body, created_by=self.lab.admin.id,
                          status="open", parent_id=inv)

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
    # The launch admits one fresh training step (one job at a time, ADR 0022 addendum R11); the rest follow as jobs end.
    assert {arm: len(ids) for arm, ids in receipt["jobs"].items()} == {"baseline": 1}
    assert not {j for ids in receipt["jobs"].values() for j in ids} & {baseline, candidate}  # recipes, not evidence
    assert receipt["pending_steps"] == 3
    expected = {"split": "held-out", "attested_by": "charter", "labels_digest": scene.digest}
    assert receipt["data_provenance"] == expected

    plan = scene.record("evidence_plan", receipt["plan_id"])
    assert plan["status"] == "executing"
    assert plan["body"]["data_provenance"] == expected  # recorded on the plan itself
    confirmation = plan["body"]["confirmation"]  # verified at freeze, from the ledger
    assert confirmation["eligible"] is True and confirmation["reasons"] == []
    assert confirmation["data_keys"] == [f"labels:{scene.digest}"]
    assert confirmation["verified"]["labels_digests"] == [scene.digest]
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
    # The jobs the launch admitted were admitted under the frozen plan, after the recipe jobs they name were recorded.
    for job_id in (j for ids in receipt["jobs"].values() for j in ids):
        job = scene.q("get_job", {"job_id": job_id})
        assert job["lock"]["plan_digest"] == receipt["plan_digest"] and job["purpose"] == "confirmation"
        assert job["lock"]["step"].endswith(".train") and job["created_at"] > scene.q("get_job", {"job_id": candidate})["created_at"]


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
    return s.request(b, c, data_provenance=None), ("provenance-required",), True


def _selection(s, b, c):
    return s.request(b, c, data_provenance={"split": "selection"}), ("selection-data",), True


def _unattested(s, b, c):
    other = s.flow.artifact(s.flow.labels()[:-1], "class-labels/1")  # other bytes: a digest the charter does not name
    return s.request(b, c, labels=other), ("held-out-unattested",), True


def _no_seeds(s, b, c):
    return s.request(b, c, seeds=[]), ("seed-pairing",), False


def _repeated_seed(s, b, c):
    return s.request(b, c, seeds=[int(FRESH_SEED), FRESH_SEED]), ("seed-pairing",), False


def _seed_already_run(s, b, c):  # the recipe jobs ran seed 11: re-running it reproduces results that are already visible
    return s.request(b, c, seeds=[int(SEED)]), ("seed-already-observed",), False


def _unknown_job(s, b, c):
    return s.request(b, c, baseline={"job_id": "job_does_not_exist"}), ("job-invalid",), False


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
    pytest.param(_unattested, id="held-out-unattested"), pytest.param(_no_seeds, id="seed-pairing"),
    pytest.param(_repeated_seed, id="seed-pairing-repeated"), pytest.param(_seed_already_run, id="seed-already-observed"),
    pytest.param(_unknown_job, id="job-invalid"), pytest.param(_wrong_slot, id="output-schema"),
    pytest.param(_no_assessment_path, id="no-assessment-path"),
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

    scene.lock(inv, candidate)  # the latest lock note wins: the same request is now accepted (the refusal ran nothing)
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

    # The labels are registered once under the schema the report declares; the arms' predictions are not named yet:
    # the report step reads the fresh training step's output, converted when the step is admitted.
    arm = next(a for a in scene.record("evidence_plan", receipt["plan_id"])["body"]["arms"] if a["id"] == "candidate")
    train_step, report = arm["runs"][0]["jobs"]
    assert train_step["id"] == f"candidate.s{FRESH_SEED}.train" and report["id"] == f"candidate.s{FRESH_SEED}.report"
    typed = scene.q("get_artifact", {"artifact_id": report["inputs"]["labels"]})
    assert typed["schema"] == "class-labels/1" and typed["digest"] == scene.digest and report["inputs"]["labels"] != labels
    assert report["inputs"]["candidate"] == f"candidate.s{FRESH_SEED}.train.predictions"
    assert report["input_schemas"] == {"baseline": "class-predictions/1", "candidate": "class-predictions/1"}

    assert scene.run_and_assess(receipt)["outcome"] == "protocol-passed"
    done = scene.flow.step_job(receipt["plan_id"], f"candidate/candidate.s{FRESH_SEED}.report")
    train_job = scene.flow.step_job(receipt["plan_id"], f"candidate/candidate.s{FRESH_SEED}.train")
    published = next(o for o in train_job["outputs"] if o["slot"] == "predictions")
    assert published["schema"] == "prediction-array/1"  # what the project's command published
    bound = done["lock"]["inputs"]["candidate"]
    assert bound["schema"] == "class-predictions/1" and bound["digest"] == published["digest"]  # same bytes, typed
    assert bound["artifact_id"] != published["artifact_id"]


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


def _install_pytorch_components(s) -> str:
    """Publish the built-in train and evaluate components; returns the release ref the train step is pinned to."""
    from test_classification_accuracy_e2e import _builtin
    from test_command_path import _publish

    refs = {}
    for name in ("eidos.pytorch.train", "eidos.pytorch.evaluate"):
        manifest, files = _builtin(name)
        refs[name] = _publish(s.lab, s.pid, manifest, files)["component_ref"]
    return refs["eidos.pytorch.train"]


def _paired_request(s, charter_id: str, spec) -> dict:
    return {"success_rule_id": RULE_ID, "charter_id": charter_id, "claim": CLAIM, "data_provenance": HELD_OUT,
            "required_isolation": ISOLATION, **({"train_evaluate": spec} if spec is not None else {})}


@pytest.mark.parametrize("spec, code, splits, exploratory", [
    pytest.param(None, "request-invalid", ["heldout"], False, id="no-spec"),
    pytest.param({**TRAIN_EVALUATE, "parent": {"checkpoint_ref": "art_x"}}, "not-fresh-initialization", ["heldout"], False,
                 id="parent"),
    pytest.param({**TRAIN_EVALUATE, "seeds": []}, "seed-pairing", ["heldout"], False, id="no-seeds"),
    pytest.param({**TRAIN_EVALUATE, "seeds": [1, 1]}, "seed-pairing", ["heldout"], False, id="repeated-seed"),
    # no split: the evaluation names no data, so nothing attests it either (the refusal is also exploratory)
    pytest.param({**TRAIN_EVALUATE, "evaluation": {"data_seed": 1}}, "output-schema", ["heldout"], True, id="no-split"),
    pytest.param(TRAIN_EVALUATE, "held-out-unattested", ["other"], True, id="split-unattested"),
])
def test_the_train_evaluate_path_refuses_an_invalid_request(scene, spec, code, splits, exploratory):
    _install_pytorch_components(scene)
    claims = scene.claims()
    receipt = scene.assess_candidate(_paired_request(scene, _paired_charter(scene, splits), spec))
    scene.assert_refused(receipt, code, claims=claims, exploratory=exploratory)


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


# =====================================================================================================================
# 10. confirmation is prospective (ADR 0021): what the freeze verifies and refuses
# =====================================================================================================================
def _file_claim(scene: Scene, inv: str) -> tuple[str, int]:
    claim_id = scene.cmd("file_claim", {"investigation_id": inv, "kind": "improves", **CLAIM})["claim_id"]
    return claim_id, scene.record("claim", claim_id)["revision"]


def test_a_plan_with_no_held_out_provenance_freezes_as_exploratory_and_never_confirms(scene):
    """F1a: the plan declares nothing about its data. It freezes and runs, but its assessment is inconclusive whatever
    the numbers say, and the claim stays a submitted candidate."""
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    draft = scene.fresh_draft(baseline, candidate)
    del draft["data_provenance"]
    plan_id = scene.launch_plan(draft)
    confirmation = scene.record("evidence_plan", plan_id)["body"]["confirmation"]
    assert confirmation["eligible"] is False and confirmation["verified"] == {} and confirmation["data_keys"] == []
    assert any("held-out" in reason for reason in confirmation["reasons"]), confirmation

    assessment = scene.run_plan_and_assess(plan_id)
    assert assessment["evaluation"]["outcome"] == "protocol-passed"  # the evaluation itself passes ...
    assert assessment["evaluation"]["effect"]["ci_low"] >= 0.02 and assessment["conformance"] == "conformant"
    assert assessment["outcome"] == "inconclusive", assessment  # ... and the verdict does not count
    assert any("exploratory" in f["finding"] for f in assessment["findings"]), assessment["findings"]
    claim = scene.q("get_claim", {"claim_id": scene.flow.claim_id})
    assert [a["outcome"] for a in claim["assessments"]] == ["inconclusive"]
    assert claim["claim_state"]["state"] == "candidate-submitted" and claim["claim_state"]["confirmed"] is False


def test_a_conclusion_after_an_exploratory_plan_is_a_submitted_candidate(scene):
    """F1a through an investigation: the model asks for confirmation, the plan was never verified, so it cannot."""
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    claim_id, _ = _file_claim(scene, inv)
    draft = scene.fresh_draft(baseline, candidate)
    del draft["data_provenance"]
    plan_id = scene.launch_plan(draft, claim_id=claim_id, investigation_id=inv)
    assert scene.record("evidence_plan", plan_id)["body"]["confirmation"]["eligible"] is False
    assessment = scene.run_plan_and_assess(plan_id)
    assert assessment["outcome"] == "inconclusive" and assessment["evaluation"]["outcome"] == "protocol-passed"

    assert scene.q("get_claim", {"claim_id": claim_id})["claim_state"]["state"] == "candidate-submitted"
    state = scene.conclude(inv, {"disposition": "confirmed"})
    assert state["state"] == "candidate-submitted" and state["confirmed"] is False, state
    assert state["downgraded_from"] == "confirmed" and state["claim_id"] == claim_id
    assert state["assessment_id"] == assessment["assessment_id"] and state["unfulfilled_obligations"]


def test_a_plan_declaring_held_out_data_the_charter_does_not_attest_is_refused_at_freeze(scene):
    """F1b: a declaration is not an attestation, on any arm. Drafting records the plan; freezing refuses it."""
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    other = scene.flow.artifact(scene.flow.labels()[:-1], "class-labels/1")  # bytes the charter does not name

    plan_id = scene.draft_plan(scene.fresh_draft(baseline, candidate, labels=other))
    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": plan_id})
    assert err.value.details["reason_code"] == "held-out-unattested"
    assert err.value.details["arms"] == ["baseline", "candidate"]
    assert err.value.details["attested"]["labels"] == [scene.digest]
    assert scene.record("evidence_plan", plan_id)["status"] == "draft"

    draft = scene.fresh_draft(baseline, candidate)  # attested for the baseline arm, not for the candidate arm
    draft["arms"][1]["runs"][0]["jobs"][1]["inputs"]["labels"] = other
    mixed = scene.draft_plan(draft)
    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": mixed})
    assert err.value.details["reason_code"] == "held-out-unattested" and err.value.details["arms"] == ["candidate"]
    assert not [p for p in scene.plans() if p["status"] != "draft"]


def test_a_plan_frozen_before_confirmation_was_verified_never_confirms(scene):
    """F1c: records written before the freeze carried a verdict (no ``confirmation`` block) still read back, but a
    pass on such a plan is a submitted candidate, in the claim and in the conclusion."""
    inv = scene.investigation()
    old_claim, old_rev = _file_claim(scene, inv)
    new_claim, new_rev = _file_claim(scene, inv)
    charter_ref = {"id": scene.charter_id, "revision": 1}

    def plan(claim_id: str, rev: int, **extra) -> dict:
        return {"claim_ref": {"id": claim_id, "revision": rev}, "claim_kind": "improves", "charter_ref": charter_ref,
                "investigation_id": inv, "obligations": [], "arms": [], "frozen_by": "t", "launch": {"jobs": {}, "steps": {}, "blocked": {}},
                "data_provenance": {"split": "held-out", "attested_by": "charter", "labels_digest": scene.digest}, **extra}

    def assessment(claim_id: str, rev: int, plan_id: str) -> dict:
        effect = {"estimate": 0.2, "ci_low": 0.15, "ci_high": 0.25}
        return {"outcome": "protocol-passed", "conformance": "conformant", "plan_id": plan_id, "findings": [],
                "claim_ref": {"id": claim_id, "revision": rev}, "scope": CLAIM["scope"], "remaining_alternatives": [],
                "success_rule_ref": f"{scene.charter_id}@1#{RULE_ID}", "health": "intact",
                "evaluation": {"outcome": "protocol-passed", "metric": "accuracy", "direction": "increase",
                               "min_effect": 0.02, "effect": effect}}

    verified = {"eligible": True, "reasons": [], "verified": {"split": "held-out"}, "data_keys": [f"labels:{scene.digest}"]}
    with scene.lab.ledger.transaction() as tx:
        tx.project_id = scene.pid
        for claim_id, rev, plan_id, extra in ((old_claim, old_rev, "plan_old", {}),
                                              (new_claim, new_rev, "plan_new", {"confirmation": verified})):
            tx.put_record("evidence_plan", plan_id, scene.pid, plan(claim_id, rev, **extra), created_by="t",
                          status="assessed", parent_id=claim_id)
            tx.put_record("assessment", f"assess_{plan_id}", scene.pid, assessment(claim_id, rev, plan_id), created_by="t",
                          status="protocol-passed", parent_id=claim_id, labels={"health": "intact"})

    assert "confirmation" not in scene.record("evidence_plan", "plan_old")["body"]  # the old records read back
    assert scene.record("assessment", "assess_plan_old")["body"]["outcome"] == "protocol-passed"
    old = scene.q("get_claim", {"claim_id": old_claim})
    assert [(a["id"], a["outcome"]) for a in old["assessments"]] == [("assess_plan_old", "protocol-passed")]
    assert old["claim_state"]["state"] == "candidate-submitted" and old["claim_state"]["confirmed"] is False
    new = scene.q("get_claim", {"claim_id": new_claim})  # the same assessment on a verified plan does confirm
    assert new["claim_state"]["state"] == "protocol-passed" and new["claim_state"]["confirmed"] is True

    state = scene.conclude(inv, {"disposition": "confirmed", "claim_id": old_claim})
    assert state["state"] == "candidate-submitted" and state["confirmed"] is False, state
    assert state["downgraded_from"] == "confirmed" and state["assessment_id"] == "assess_plan_old"
    assert state["plan_id"] == "plan_old" and state["point_estimate"]["value"] == pytest.approx(0.2)
    assert any("exploratory" in o for o in state["unfulfilled_obligations"]), state["unfulfilled_obligations"]


def test_the_old_request_shape_over_observed_jobs_is_refused(scene):
    """F2a: `{seed: job}` maps name results that already exist; they are never admissible as confirmation."""
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    claims, jobs = scene.claims(), len(scene.q("list_jobs"))
    receipt = scene.assess_candidate(scene.request(baseline, candidate, baseline={SEED: baseline}, candidate={SEED: candidate}))
    scene.assert_refused(receipt, "observed-evidence", claims=claims)
    assert [r["code"] for r in receipt["reasons"]] == ["observed-evidence", "observed-evidence"]  # one per arm
    assert len(scene.q("list_jobs")) == jobs  # and nothing was run


def _pinned_draft(predictions: str, labels: str) -> dict:
    """A hand-written draft, not the fresh expansion: each arm is one report step over a pinned predictions artifact."""
    arm = {role: {"id": role, "role": role, "run": {"component": f"{REPORT}@1.0.0", "config": {"role": role, "cluster": SEED},
                                                    "inputs": {"baseline": predictions, "candidate": predictions,
                                                               "labels": labels}}}
           for role in ("baseline", "candidate")}
    return {"success_rule_id": RULE_ID, "assessor": {"ref": ASSESSOR_REF}, "arms": [arm["baseline"], arm["candidate"]],
            "analysis_unit": "example", "cluster_by": None, "seed_policy": {"kind": "pinned", "seeds": [int(SEED)]},
            "required_isolation": ISOLATION, "data_provenance": HELD_OUT}


@pytest.mark.parametrize("pin", ["schema", "digest"])
def test_a_plan_pinning_published_predictions_is_refused_at_freeze(scene, pin):
    """F2b: a plan that names results that exist cannot be frozen, however they are named: by the result schema, or by
    the same bytes registered under another schema."""
    baseline = scene.job(BASELINE_RECIPE)
    published = scene.flow.output(scene.flow.job(baseline), "predictions")[0]
    if pin == "digest":
        art = scene.q("get_artifact", {"artifact_id": published})
        published = scene.cmd("register_artifact", {"digest": art["digest"], "size": art["size"],
                                                    "schema": "scratch-dump/1", "media_type": "application/json"})["artifact_id"]
        assert scene.q("get_artifact", {"artifact_id": published})["schema"] == "scratch-dump/1"

    plan_id = scene.draft_plan(_pinned_draft(published, scene.labels))  # drafting only records it
    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": plan_id})
    assert err.value.details["reason_code"] == "observed-evidence"
    pinned = {(i["arm"], i["port"], i["artifact_id"]) for i in err.value.details["inputs"]}
    assert pinned == {(arm, port, published) for arm in ("baseline", "candidate") for port in ("baseline", "candidate")}
    assert scene.record("evidence_plan", plan_id)["status"] == "draft"


def test_held_out_data_a_confirmation_already_used_is_refused_for_a_second_plan(scene):
    """F2c: the first plan on attested held-out data confirms; any later plan on the same data (other seeds, another
    claim) is refused, because results seen there could have chosen it. Jobs of the first plan are fresh runs."""
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    first = scene.assess_candidate(scene.request(baseline, candidate))
    assert first["status"] == "launched", first
    jobs = [j for ids in first["jobs"].values() for j in ids]
    assert not set(jobs) & {baseline, candidate}  # the plan runs the recipes anew; it does not reuse the recorded jobs
    for job_id in jobs:
        job = scene.q("get_job", {"job_id": job_id})
        assert job["lock"]["plan_digest"] == first["plan_digest"] and job["lock"]["step"].endswith(".train")
        assert job["created_at"] > scene.q("get_job", {"job_id": candidate})["created_at"]  # admitted after the recipes

    claims = scene.claims()
    other_claim = {"statement": "the candidate also helps on a second claim", "scope": {"task": "toy-3class-v2"}}
    second = scene.assess_candidate(scene.request(baseline, candidate, seeds=[22], claim=other_claim))
    assert second["status"] == "refused" and second["exploratory"] is False, second
    (reason,) = [r for r in second["reasons"] if r["code"] == "held-out-reused"]
    assert reason["details"]["plan_id"] == first["plan_id"] and reason["details"]["data_keys"] == [f"labels:{scene.digest}"]
    assert [p["id"] for p in scene.plans()] == [first["plan_id"]] and scene.claims() == claims  # nothing was created
    rec = scene.record("assessment_request", second["request_id"])
    assert rec["status"] == "refused" and "plan_id" not in rec["body"]

    plan_id = scene.draft_plan(scene.fresh_draft(baseline, candidate, seeds=["23"]))  # the same holds for a manual plan
    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": plan_id})
    assert err.value.details["reason_code"] == "held-out-reused" and err.value.details["plan_id"] == first["plan_id"]
    assert scene.record("evidence_plan", plan_id)["status"] == "draft"


# =====================================================================================================================
# 11. the train/evaluate candidate must be the locked recipe
# =====================================================================================================================
def _recorded_train_job(scene: Scene, inv: str, train_ref: str, config: dict) -> str:
    """A recorded training job of the pinned release, written straight to the ledger (the project has no adapter to
    run eidos.pytorch.train): its recipe is what a ``candidate_lock`` note can name."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    with scene.lab.ledger.transaction() as tx:
        tx.project_id = scene.pid
        tx.insert_job({"id": job_id, "project_id": scene.pid, "investigation_id": inv, "kind": "run", "trial_id": None,
                       "purpose": "exploration", "status": "succeeded", "created_by": "t", "lock_digest": f"d-{job_id}",
                       "lock": {"component": {"ref": train_ref}, "inputs": {}, "config": config}})
    return job_id


def test_the_train_evaluate_candidate_must_be_the_locked_recipe(scene):
    train_ref = _install_pytorch_components(scene)
    charter_id = _paired_charter(scene, ["heldout"])
    inv = scene.investigation(charter_id)
    request = {**_paired_request(scene, charter_id, TRAIN_EVALUATE), "investigation_id": inv}  # candidate: epochs 2
    claims = scene.claims()

    receipt = scene.assess_candidate(request)  # no candidate_lock note
    scene.assert_refused(receipt, "candidate-not-locked", claims=claims)
    assert "candidate_lock" in next(r["message"] for r in receipt["reasons"] if r["code"] == "candidate-not-locked")

    other = _recorded_train_job(scene, inv, train_ref, {"epochs": 2, "lr": 0.5, "seed": 99})  # another recipe
    scene.lock(inv, other)
    receipt = scene.assess_candidate(request)
    scene.assert_refused(receipt, "candidate-not-locked", claims=claims)
    (reason,) = [r for r in receipt["reasons"] if r["code"] == "candidate-not-locked"]
    assert other in reason["message"] and reason["details"]["submitted"]["component"] == train_ref
    assert reason["details"]["submitted"]["config"] == {"epochs": 2}
    assert reason["details"]["recorded"]["config"] == {"epochs": 2, "lr": 0.5}  # seed aside, the lr differs

    matching = _recorded_train_job(scene, inv, train_ref, {"epochs": 2, "seed": 99})  # the submitted recipe, seed aside
    scene.lock(inv, matching)
    receipt = scene.assess_candidate(request)
    codes = {r["code"] for r in receipt["reasons"]}
    assert "candidate-not-locked" not in codes  # the lock no longer refuses it ...
    scene.assert_refused(receipt, "step-blocked", claims=claims)  # ... it fails later: this project has no model adapter


def test_a_train_evaluate_candidate_with_post_steps_is_not_the_locked_recipe(scene):
    train_ref = _install_pytorch_components(scene)
    charter_id = _paired_charter(scene, ["heldout"])
    inv = scene.investigation(charter_id)
    scene.lock(inv, _recorded_train_job(scene, inv, train_ref, {"epochs": 2, "seed": 99}))  # the train recipe matches
    post = {"component": "eidos.pytorch.evaluate", "config": {}}
    spec = {**TRAIN_EVALUATE, "candidate": {**TRAIN_EVALUATE["candidate"], "post": [post]}}
    claims = scene.claims()

    receipt = scene.assess_candidate({**_paired_request(scene, charter_id, spec), "investigation_id": inv})
    scene.assert_refused(receipt, "candidate-not-locked", claims=claims)
    (reason,) = [r for r in receipt["reasons"] if r["code"] == "candidate-not-locked"]
    assert "post steps" in reason["message"]


# =====================================================================================================================
# 12. a refusal over exploratory data is not the end: the candidate is still submitted at the conclusion
# =====================================================================================================================
@pytest.mark.parametrize("make", [pytest.param(_no_provenance, id="provenance-required"),
                                  pytest.param(_selection, id="selection-data"),
                                  pytest.param(_unattested, id="held-out-unattested")])
def test_an_exploratory_refusal_can_still_be_concluded_as_a_submitted_candidate(scene, make):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    scene.lock(inv, candidate)
    claim_id, _ = _file_claim(scene, inv)
    payload, (code,), _ = make(scene, baseline, candidate)

    receipt = scene.assess_candidate({**payload, "investigation_id": inv})
    assert receipt["status"] == "refused" and receipt["exploratory"] is True, receipt
    (reason,) = [r for r in receipt["reasons"] if r["code"] == code]
    assert "candidate submitted for confirmation" in reason["message"]  # the refusal says what the investigator can do
    rec = scene.record("assessment_request", receipt["request_id"])
    assert rec["status"] == "refused" and rec["body"]["exploratory"] is True and rec["body"]["investigation_id"] == inv
    assert scene.plans() == []

    estimate = {"metric": "accuracy", "baseline": 0.70, "candidate": 0.85, "split": "validation"}
    state = scene.conclude(inv, {"disposition": "candidate", "point_estimate": estimate})
    assert state["state"] == "candidate-submitted" and state["confirmed"] is False, state
    assert state["claim_id"] == claim_id and state["assessment_id"] is None and "downgraded_from" not in state
    assert state["point_estimate"]["source"] == "investigator" and state["point_estimate"]["value"] == pytest.approx(0.15)
    assert state["unfulfilled_obligations"]
    assert any("held-out" in o for o in state["unfulfilled_obligations"])


# =====================================================================================================================
# 15. R13: the freeze boundary covers manual plans too, and the pinned state and lock formats match it
# =====================================================================================================================
def _read_the_labels(scene: Scene, baseline: str, candidate: str, **extra) -> str:
    """An exploratory report over the attested labels: anything it returns has been seen before a plan is frozen."""
    flow = scene.flow
    return scene.cmd("run_component", {
        "component_ref": REPORT_REF, "environment": "lab", "isolation_profile": "dev:same-user",
        "inputs": {"baseline": flow.output(flow.job(baseline), "predictions")[0],
                   "candidate": flow.output(flow.job(candidate), "predictions")[0], "labels": scene.labels},
        "config": {"role": "baseline", "cluster": "11"}, **extra})["job_id"]


def test_a_manual_plan_over_labels_already_used_for_selection_is_refused_at_freeze(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    peek = _read_the_labels(scene, baseline, candidate)
    plan_id = scene.draft_plan(scene.fresh_draft(baseline, candidate))
    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": plan_id})
    assert err.value.details["reason_code"] == "selection-exposed" and err.value.details["jobs"] == [peek]
    assert scene.record("evidence_plan", plan_id)["status"] == "draft"


def test_a_manual_plan_rerunning_a_recipe_and_seed_already_seen_is_refused_at_freeze(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    plan_id = scene.draft_plan(scene.fresh_draft(baseline, candidate, seeds=[SEED]))
    with pytest.raises(Invalid) as err:
        scene.cmd("freeze_evidence_plan", {"plan_id": plan_id})
    assert err.value.details["reason_code"] == "seed-already-observed" and err.value.details["seeds"] == [SEED]
    assert set(err.value.details["jobs"]) == {baseline, candidate}
    assert scene.record("evidence_plan", plan_id)["status"] == "draft"


def test_a_manual_plan_on_fresh_data_and_fresh_seeds_still_freezes_eligible(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    plan_id = scene.draft_plan(scene.fresh_draft(baseline, candidate))
    scene.cmd("freeze_evidence_plan", {"plan_id": plan_id})
    plan = scene.record("evidence_plan", plan_id)
    assert plan["status"] == "frozen" and plan["body"]["confirmation"]["eligible"] is True


def test_labels_an_exploratory_plan_already_read_cannot_be_confirmed_through_assess_candidate(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    draft = scene.fresh_draft(baseline, candidate)
    del draft["data_provenance"]  # exploratory: it claims no data, so it reserves none, but its jobs read the labels
    exploratory = scene.launch_plan(draft)
    scene.run_plan_and_assess(exploratory)
    claims = scene.claims()
    receipt = scene.assess_candidate(scene.request(baseline, candidate, seeds=[31]))
    assert receipt["status"] == "refused" and receipt["exploratory"] is True, receipt
    (reason,) = [r for r in receipt["reasons"] if r["code"] == "selection-exposed"]
    assert reason["details"]["jobs"] and scene.claims() == claims


def test_the_view_pins_an_exploratory_pass_as_a_submitted_candidate(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    claim_id, _ = _file_claim(scene, inv)
    draft = scene.fresh_draft(baseline, candidate)
    del draft["data_provenance"]
    plan_id = scene.launch_plan(draft, claim_id=claim_id, investigation_id=inv)
    assessment = scene.run_plan_and_assess(plan_id)
    assert assessment["evaluation"]["outcome"] == "protocol-passed"
    state = scene.q("investigation_view", {"investigation_id": inv, "horizon": 0})["claim_state"]
    assert state["state"] == "candidate-submitted" and state["confirmed"] is False and state["plan_id"] == plan_id


def test_the_view_does_not_carry_an_assessment_over_to_a_revised_claim(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    claim_id, revision = _file_claim(scene, inv)
    plan_id = scene.launch_plan(scene.fresh_draft(baseline, candidate), claim_id=claim_id, investigation_id=inv)
    scene.run_plan_and_assess(plan_id)
    assert scene.q("investigation_view", {"investigation_id": inv, "horizon": 0})["claim_state"]["state"] == "protocol-passed"
    scene.cmd("file_claim", {"claim_id": claim_id, "supersedes_revision": revision, "investigation_id": inv, **CLAIM,
                             "kind": "improves"})
    state = scene.q("investigation_view", {"investigation_id": inv, "horizon": 0})["claim_state"]
    assert state["state"] == "candidate-submitted" and state["assessment_id"] is None


def _note(scene: Scene, inv: str, text: str, refs: list | None = None) -> None:
    scene.lock(inv, "", text=text, refs=refs)


@pytest.mark.parametrize("refs", [lambda job: [job], lambda job: [{"job_id": job}, "art_context"]])
def test_a_natural_language_lock_naming_one_job_in_evidence_refs_can_be_assessed(scene, refs):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    _note(scene, inv, "two features beat one on validation, so this is my candidate", refs(candidate))
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    assert receipt["status"] == "launched", receipt


def test_a_natural_language_lock_with_no_single_job_is_refused_with_the_format(scene):
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    inv = scene.investigation()
    _note(scene, inv, f"my candidate is {candidate}")  # a prose mention identifies nothing
    _note(scene, inv, "either of these", [baseline, candidate])
    claims = scene.claims()
    receipt = scene.assess_candidate(scene.request(baseline, candidate, investigation_id=inv))
    scene.assert_refused(receipt, "candidate-not-locked", claims=claims)
    message = next(r["message"] for r in receipt["reasons"] if r["code"] == "candidate-not-locked")
    assert '{"job_id"' in message and "evidence_refs" in message
