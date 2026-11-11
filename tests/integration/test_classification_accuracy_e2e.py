"""Two training jobs plus held-out predictions -> an assessor verdict on ordinary accuracy (ADR 0018), end to end.

Everything runs through the real command service on PostgreSQL, the real `AttemptRunner` (development profile
`dev:same-user`) and the real built-in components published from the repository directories:

* a project "training" component (standard library only: a nearest-centroid classifier on a synthetic 3-class
  dataset) runs twice, once per recipe, and writes held-out predictions;
* the two recorded training jobs only name the recipes (ADR 0021, "Prospective confirmation"): a plan cannot pin
  predictions that already exist, so `fresh_classification_draft` freezes the recipes, a fresh seed, the labels the
  charter attests as held-out and the rule, and the plan then trains both recipes anew;
* each arm runs `eidos.task.paired-classification-report@1.0.0` on the two NEW prediction outputs (it fails closed
  when the three inputs do not align) and the plan becomes `awaiting-assessment`;
* `AssessorActivities.assess_plan` runs `eidos.assessors.paired-ci@1.2.0` (metric `accuracy`) on the arms'
  `predictions` outputs and submits the assessment;
* `eidos.templates.improves-under-budget@1.2.0`'s classification path still pins existing predictions, so the plan
  built from it is refused at freeze.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment

from eidos.domain import assessment as rules
from eidos.domain.errors import Invalid
from eidos.orchestration.activities import AssessorActivities
from eidos.runtime.runner import AttemptRunner

pytestmark = pytest.mark.pg

COMPONENTS = Path(__file__).resolve().parents[2] / "components"
REPORT = "eidos.task.paired-classification-report"
TEMPLATE = "eidos.templates.improves-under-budget"
ASSESSOR = "eidos.assessors.paired-ci"
TEMPLATE_REF = f"{TEMPLATE}@1.2.0"
ASSESSOR_REF = f"{ASSESSOR}@1.2.0"

N_HELDOUT = 360
DATA_SEED = 7
SEED = "11"  # the seed the recorded recipe jobs ran: its results are already observed
FRESH_SEED = "21"  # a seed the plan has not run: its arms are trained after the plan is frozen
RULE_ID = "practical-accuracy-gain"
CHARTER = {
    "study_id": "cls-acc",
    "question": "Does the candidate recipe raise held-out accuracy on the toy 3-class task?",
    "population": "held-out examples",
    "comparator": "baseline",
    "metrics": {"accuracy": {"identity": "classification-report/1:accuracy", "direction": "increase",
                             "denominator": "held-out examples"}},
    "success_rules": [{"id": RULE_ID, "metric": "accuracy", "min_effect": 0.02,
                       "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                       "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95}}],
}
ISOLATION = {"profile": "dev:same-user", "controls": [], "waiver": "development lab: same-user isolation"}

# The project's own training component: deterministic nearest-centroid on three Gaussian blobs. The baseline
# recipe sees only the first feature, so classes 0 and 2 (equal on it) cannot be told apart; the candidate
# recipe uses both features. Held-out examples depend on `data_seed` only, so both recipes predict the same ids. A
# recipe with `drop_id` leaves that held-out example out of its predictions (a run that silently loses an example).
TRAIN_CODE = '''
import random

CENTERS = {0: (0.0, 0.0), 1: (3.0, 0.0), 2: (0.0, 3.0)}


def draw(rng, n):
    out = []
    for _ in range(n):
        label = rng.choice(sorted(CENTERS))
        cx, cy = CENTERS[label]
        out.append((label, rng.gauss(cx, 1.0), rng.gauss(cy, 1.0)))
    return out


def heldout(data_seed, n):
    rng = random.Random("heldout-%s" % data_seed)
    return [{"id": 1000 + i, "label": label, "x": x, "y": y} for i, (label, x, y) in enumerate(draw(rng, n))]


def run(inputs, config, *, reader=None, writer=None, context=None):
    features = int(config["features"])
    train = draw(random.Random("train-%s" % config["seed"]), int(config["n_train"]))
    centroids = {}
    for label in sorted(CENTERS):
        pts = [p[1:1 + features] for p in train if p[0] == label]
        centroids[label] = [sum(col) / len(pts) for col in zip(*pts)]

    def predict(row):
        point = (row["x"], row["y"])[:features]
        return min(centroids, key=lambda c: sum((a - b) ** 2 for a, b in zip(point, centroids[c])))

    held = heldout(config["data_seed"], int(config["n_heldout"]))
    rows = [{"id": r["id"], "prediction": predict(r)} for r in held if r["id"] != config.get("drop_id")]
    writer.stage_json("predictions", rows, schema="class-predictions/1")
    return {"examples": len(rows), "features": features}
'''
TRAIN_MANIFEST = {"id": "proj.nearest-centroid", "release": "1.0.0", "implements": "train-classifier/1",
                  "kind": "artifact-op", "entry": "m:run", "scientific_effects": ["change-weights", "observe"],
                  "output": {"predictions": "class-predictions/1"}}

BASELINE_RECIPE = {"features": 1, "n_train": 200, "seed": 11}
CANDIDATE_RECIPE = {"features": 2, "n_train": 200, "seed": 11}


def _train_module() -> SimpleNamespace:
    ns: dict = {}
    exec(TRAIN_CODE, ns)  # noqa: S102 - the test's own source, to derive the held-out labels independently
    return SimpleNamespace(**ns)


def _builtin(name: str) -> tuple[dict, dict]:
    """A built-in component's manifest and every other file, read from the repository directory."""
    root = COMPONENTS / name
    files = {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts and p.name != "eidos-component.json"}
    return json.loads((root / "eidos-component.json").read_text()), files


class Flow:
    """One lab project with the real components published, a charter, a claim, a runner and an assessor."""

    def __init__(self, lab, tmp_path: Path):
        from test_command_path import _publish, _register_lab_env, project
        from test_turn_recovery import ShimClient

        self.lab = lab
        self.pid = project(lab)
        _register_lab_env(lab, self.pid)
        self.train_ref = _publish(lab, self.pid, TRAIN_MANIFEST, {"m.py": TRAIN_CODE})["component_ref"]
        for name in (REPORT, TEMPLATE, ASSESSOR):
            manifest, files = _builtin(name)
            _publish(lab, self.pid, manifest, files)
        for ref in (TEMPLATE_REF, ASSESSOR_REF):
            self._qualify_and_promote(ref)
        # The charter attests the held-out labels by digest: an operator committed these bytes as evaluator-owned data.
        self.labels_digest = lab.store.put_bytes(json.dumps(self.labels()).encode())[0]
        self.charter = {**CHARTER, "held_out": {"labels": [self.labels_digest]}}
        lab.cmd(lab.admin, "commit_study_charter", {"charter": self.charter}, project=self.pid)
        self.claim_id = lab.cmd(lab.admin, "file_claim", {
            "statement": "the candidate recipe raises held-out accuracy", "kind": "improves",
            "scope": {"task": "toy-3class"}}, project=self.pid)["result"]["claim_id"]
        launcher = lab.principal(f"svc-launcher-{self.pid}", ["launcher"], [self.pid])
        root = tmp_path / "labroot"
        self.runner = AttemptRunner(ShimClient(lab, launcher), lab.store, lab_root=root, launcher_id=launcher.id,
                                    api_port=1, credential_path=str(root / "config" / "secrets" / "launcher.token"))
        assessor = lab.principal(f"svc-assessor-{self.pid}", ["assessor"], [self.pid])
        token = tmp_path / "svc-assessor.token"
        token.write_text("token")
        self.assessor = AssessorActivities(ShimClient(lab, assessor), None, None, sys.executable,
                                           credential_path=str(token), api_port=1)

    def _qualify_and_promote(self, ref: str) -> None:
        lab, pid = self.lab, self.pid
        suite = {"target": {"kind": "assessor"}, "scope": ["improves"], "controls": [
            {"id": "p", "type": "planted", "input": "f1"}, {"id": "n", "type": "null", "input": "f2"}]}
        s = lab.cmd(lab.admin, "register_control_suite", {"suite": suite, "suite_id": f"suite:{ref}"},
                    project=pid)["result"]
        digest = lab.q(lab.admin, "describe_component", {"component_ref": ref}, project=pid)["body"]["digest"]
        lab.cmd(lab.admin, "submit_qualification", {
            "suite_id": s["suite_id"], "suite_digest": s["suite_digest"], "release_ref": ref,
            "release_digest": digest, "results": {"p": [{"detected": True}], "n": [{"detected": False}]}},
            project=pid)
        lab.cmd(lab.admin, "promote_protocol", {"release_ref": ref, "scope": ["improves"]}, project=pid)

    # -- artifacts and jobs --------------------------------------------------------------------------------
    def artifact(self, data, schema: str) -> str:
        raw = json.dumps(data).encode()
        digest, size = self.lab.store.put_bytes(raw)
        return self.lab.cmd(self.lab.admin, "register_artifact", {
            "digest": digest, "size": size, "schema": schema, "media_type": "application/json"},
            project=self.pid)["result"]["artifact_id"]

    def job(self, job_id: str) -> dict:
        return self.lab.q(self.lab.admin, "get_job", {"job_id": job_id}, project=self.pid)

    def output(self, job: dict, slot: str) -> tuple[str, object]:
        """A job's published output: (artifact id, parsed JSON or JSONL rows)."""
        out = next(o for o in job["outputs"] if o["slot"] == slot)
        text = Path(self.lab.store.path_for(out["digest"])).read_text()
        if out["media_type"] == "application/json":
            return out["artifact_id"], json.loads(text)
        return out["artifact_id"], [json.loads(line) for line in text.splitlines()]

    def run(self, job_id: str) -> dict:
        """Run an admitted job through the real runner; returns its final `get_job` view."""
        self.runner.run_job(job_id, self.pid, activity_attempt=1)
        return self.job(job_id)

    def train_job(self, recipe: dict) -> str:
        """Run the project's training job for a recipe; returns the job id (a recorded job: it names a recipe)."""
        job_id = self.lab.cmd(self.lab.admin, "run_component", {
            "component_ref": self.train_ref, "environment": "lab", "isolation_profile": "dev:same-user",
            "config": {**recipe, "data_seed": DATA_SEED, "n_heldout": N_HELDOUT}}, project=self.pid)["result"]["job_id"]
        done = self.run(job_id)
        assert done["status"] == "succeeded", done.get("terminal")
        _, rows = self.output(done, "predictions")
        assert len(rows) == N_HELDOUT - (1 if "drop_id" in recipe else 0)
        return job_id

    def train(self, recipe: dict) -> str:
        """Run the project's training job for a recipe; returns its predictions artifact id."""
        return self.output(self.job(self.train_job(recipe)), "predictions")[0]

    def labels(self) -> list[dict]:
        return [{"id": r["id"], "label": r["label"]} for r in _train_module().heldout(DATA_SEED, N_HELDOUT)]

    # -- plan ----------------------------------------------------------------------------------------------
    def draft(self, baseline: str, candidate: str, labels: str) -> dict:
        """Run the REAL template on a classification request; returns its `draft` output."""
        request = {"claim": {"kind": "improves", "statement": "the candidate recipe raises held-out accuracy",
                             "scope": {"task": "toy-3class"}},
                   "success_rule_id": RULE_ID, "assessor": {"ref": ASSESSOR_REF}, "required_isolation": ISOLATION,
                   "classification": {"labels": labels, "predictions": {"baseline": {SEED: baseline},
                                                                         "candidate": {SEED: candidate}}}}
        job_id = self.lab.cmd(self.lab.admin, "run_component", {
            "component_ref": TEMPLATE_REF, "environment": "lab", "isolation_profile": "dev:same-user",
            "inputs": {"request": self.artifact(request, "plan-request/1")}}, project=self.pid)["result"]["job_id"]
        done = self.run(job_id)
        assert done["status"] == "succeeded", done.get("terminal")
        assert done["terminal"]["receipt"]["open_obligations"] == []
        artifact_id, draft = self.output(done, "draft")
        return {"artifact_id": artifact_id, "draft": draft}

    def freeze_template_draft(self, draft_artifact_id: str) -> str:
        """Compile and freeze a draft the template produced; returns the plan id (the freeze may raise)."""
        lab, pid = self.lab, self.pid
        plan = lab.cmd(lab.admin, "draft_evidence_plan", {
            "claim_id": self.claim_id, "charter_id": f"charter:{CHARTER['study_id']}", "template_ref": TEMPLATE_REF,
            "draft_artifact_id": draft_artifact_id}, project=pid)["result"]
        assert plan["obligations"] == []
        lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan["plan_id"]}, project=pid)
        return plan["plan_id"]

    def fresh_draft(self, baseline_job: str, candidate_job: str, labels: str, *, seeds: list[str] | None = None,
                    provenance: dict | None = None) -> dict:
        """A plan draft whose arms do not exist yet: the recorded jobs' recipes, fresh seeds and the labels."""
        request = {"claim": {"kind": "improves", "statement": "the candidate recipe raises held-out accuracy",
                             "scope": {"task": "toy-3class"}},
                   "success_rule_id": RULE_ID, "assessor": {"ref": ASSESSOR_REF}, "required_isolation": ISOLATION,
                   "classification": {"labels": labels, "seeds": seeds or [FRESH_SEED], "slot": "predictions",
                                      "environment": "lab",
                                      "recipes": {"baseline": self.job(baseline_job)["lock"],
                                                  "candidate": self.job(candidate_job)["lock"]}}}
        draft = rules.fresh_classification_draft(request)
        draft["data_provenance"] = {"split": "held-out"} if provenance is None else provenance
        return draft

    def draft_plan(self, draft: dict) -> str:
        """Compile a draft inline against the flow's charter; returns the plan id."""
        plan = self.lab.cmd(self.lab.admin, "draft_evidence_plan", {
            "claim_id": self.claim_id, "charter_id": f"charter:{CHARTER['study_id']}", "template_ref": TEMPLATE_REF,
            "draft": draft}, project=self.pid)["result"]
        assert plan["obligations"] == []
        return plan["plan_id"]

    def freeze_and_launch(self, draft: dict) -> tuple[str, dict[str, list[str]]]:
        """Draft, freeze and launch a draft; returns the plan id and the jobs the launch admitted."""
        lab, pid = self.lab, self.pid
        plan_id = self.draft_plan(draft)
        lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan_id}, project=pid)
        launched = lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)["result"]
        return plan_id, launched["jobs"]

    def plan_body(self, plan_id: str) -> dict:
        return self.lab.q(self.lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=self.pid)["body"]

    def run_plan(self, plan_id: str) -> dict[str, dict]:
        """Run a launched plan to the end: each arm's report step is only admitted once the training steps of both
        arms have finished, so the plan's jobs grow while it runs. Returns {job id: final job view}."""
        done: dict[str, dict] = {}
        while pending := [j for j in self.plan_body(plan_id)["launch"]["steps"].values() if j not in done]:
            for job_id in pending:
                done[job_id] = self.run(job_id)
        return done

    def step_job(self, plan_id: str, step: str) -> dict:
        """The final view of one admitted step of a plan, by its key (``<arm>/<step id>``)."""
        return self.job(self.plan_body(plan_id)["launch"]["steps"][step])

    def plan_status(self, plan_id: str) -> str:
        return self.lab.q(self.lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id},
                          project=self.pid)["status"]

    def assess(self, plan_id: str) -> dict:
        """Run the assessment activity; returns the committed assessment record body (with its id)."""
        out = ActivityEnvironment().run(self.assessor.assess_plan, {"project_id": self.pid, "plan_id": plan_id})
        body = self.lab.q(self.lab.admin, "get_record", {"kind": "assessment", "id": out["assessment_id"]},
                          project=self.pid)["body"]
        assert body["outcome"] == out["outcome"]
        return {**body, "assessment_id": out["assessment_id"]}


def _accuracy(rows: list[dict], labels: list[dict]) -> float:
    truth = {r["id"]: r["label"] for r in labels}
    return sum(truth[r["id"]] == r["prediction"] for r in rows) / len(rows)


def _predictions(flow: Flow, artifact_id: str) -> list[dict]:
    art = flow.lab.q(flow.lab.admin, "get_artifact", {"artifact_id": artifact_id}, project=flow.pid)
    return json.loads(Path(flow.lab.store.path_for(art["digest"])).read_text())


def _run_plan(flow: Flow, plan_id: str) -> dict[str, dict]:
    return flow.run_plan(plan_id)


# -----------------------------------------------------------------------------------------------------------
def _fresh_predictions(flow: Flow, plan_id: str, role: str, seed: str = FRESH_SEED) -> list[dict]:
    """The predictions a plan's fresh training step of one arm published."""
    return flow.output(flow.step_job(plan_id, f"{role}/{role}.s{seed}.train"), "predictions")[1]


def test_two_recorded_recipes_and_attested_heldout_labels_reach_an_accuracy_verdict(lab, tmp_path):
    flow = Flow(lab, tmp_path)
    baseline_job, candidate_job = flow.train_job(BASELINE_RECIPE), flow.train_job(CANDIDATE_RECIPE)
    labels = flow.artifact(flow.labels(), "class-labels/1")
    recorded = {role: _predictions(flow, flow.output(flow.job(job), "predictions")[0])
                for role, job in (("baseline", baseline_job), ("candidate", candidate_job))}
    assert _accuracy(recorded["candidate"], flow.labels()) - _accuracy(recorded["baseline"], flow.labels()) > 0.1

    draft = flow.fresh_draft(baseline_job, candidate_job, labels)
    assert [a["id"] for a in draft["arms"]] == ["baseline", "candidate"] and draft["cluster_by"] is None
    for arm in draft["arms"]:
        (run,) = arm["runs"]
        train, report = run["jobs"]  # a fresh training step, then the report that compares both arms' new predictions
        assert train["id"] == f"{arm['id']}.s{FRESH_SEED}.train" and train["intermediate"] is True
        assert train["config"]["seed"] == int(FRESH_SEED) and train["config"]["features"] == (
            1 if arm["id"] == "baseline" else 2)
        assert report["component"] == f"{REPORT}@1.0.0" and report["config"] == {"role": arm["id"], "cluster": FRESH_SEED}
        assert report["inputs"] == {"baseline": f"baseline.s{FRESH_SEED}.train.predictions",
                                    "candidate": f"candidate.s{FRESH_SEED}.train.predictions", "labels": labels}
    plan_id, jobs = flow.freeze_and_launch(draft)
    plan = flow.plan_body(plan_id)
    assert plan["confirmation"]["eligible"] is True and plan["confirmation"]["reasons"] == []
    assert plan["confirmation"]["data_keys"] == [f"labels:{flow.labels_digest}"]
    # Launch admits the training steps only; the recorded jobs are not among them (nothing observed is reused).
    assert {arm: len(ids) for arm, ids in jobs.items()} == {"baseline": 1, "candidate": 1}
    assert not {j for ids in jobs.values() for j in ids} & {baseline_job, candidate_job}

    finished = _run_plan(flow, plan_id)
    assert len(finished) == 4  # per arm: the training step and its report step
    for done in finished.values():
        assert done["status"] == "succeeded", done.get("terminal")
    for arm in ("baseline", "candidate"):
        done = flow.step_job(plan_id, f"{arm}/{arm}.s{FRESH_SEED}.report")
        assert done["terminal"]["receipt"]["role"] == arm and done["terminal"]["receipt"]["examples"] == N_HELDOUT
        _, rows = flow.output(done, "predictions")
        assert {r["role"] for r in rows} == {arm} and len(rows) == N_HELDOUT
    assert flow.plan_status(plan_id) == "awaiting-assessment"
    base_acc = _accuracy(_fresh_predictions(flow, plan_id, "baseline"), flow.labels())
    cand_acc = _accuracy(_fresh_predictions(flow, plan_id, "candidate"), flow.labels())
    assert cand_acc - base_acc > 0.1  # the recipes really differ on the held-out set under the fresh seed too

    assessment = flow.assess(plan_id)
    ev = assessment["evaluation"]
    assert assessment["outcome"] == "protocol-passed", assessment
    assert assessment["conformance"] == "conformant" and assessment["plan_id"] == plan_id
    assert ev["metric"] == "accuracy" and ev["direction"] == "increase" and ev["min_effect"] == 0.02
    assert ev["effect"]["units"] == N_HELDOUT and ev["units_by_role"] == {"baseline": N_HELDOUT,
                                                                           "candidate": N_HELDOUT}
    assert ev["effect"]["ci_low"] >= 0.02 and ev["effect"]["ci_low"] <= ev["effect"]["estimate"]
    assert ev["effect"]["estimate"] == pytest.approx(cand_acc - base_acc)  # the assessor's gain is the true gain
    assert ev["clusters"] == [FRESH_SEED] and ev["assessor"] == ASSESSOR_REF and ev["cluster_by"] is None
    assert ev["unbounded_fields"] == [] and assessment["assessor"]["ref"] == ASSESSOR_REF
    assert flow.plan_status(plan_id) == "assessed"
    claim = lab.q(lab.admin, "get_record", {"kind": "claim", "id": flow.claim_id}, project=flow.pid)
    assert claim["status"] == "assessed" and claim["labels"]["outcome"] == "protocol-passed"
    assert lab.q(lab.admin, "get_claim", {"claim_id": flow.claim_id}, project=flow.pid)["claim_state"]["confirmed"] is True


def test_a_candidate_run_missing_one_heldout_id_fails_closed_and_cannot_pass(lab, tmp_path):
    flow = Flow(lab, tmp_path)
    dropped = 1000 + 17
    baseline_job = flow.train_job(BASELINE_RECIPE)
    candidate_job = flow.train_job({**CANDIDATE_RECIPE, "drop_id": dropped})  # the recipe loses an example on every run
    labels = flow.artifact(flow.labels(), "class-labels/1")

    # Planning cannot see the ids; the fresh training steps publish them and the report steps check the alignment.
    plan_id, jobs = flow.freeze_and_launch(flow.fresh_draft(baseline_job, candidate_job, labels))
    finished = _run_plan(flow, plan_id)
    assert len(finished) == 4
    for arm in ("baseline", "candidate"):
        assert flow.step_job(plan_id, f"{arm}/{arm}.s{FRESH_SEED}.train")["status"] == "succeeded"
        done = flow.step_job(plan_id, f"{arm}/{arm}.s{FRESH_SEED}.report")  # each arm checks the whole alignment
        assert done["status"] == "failed", (arm, done.get("terminal"))
        assert "predictions" not in {o["slot"] for o in done["outputs"]}  # no rows are published for an arm
        message = json.dumps(done["terminal"]["error"])
        assert "do not align" in message and "candidate lacks 1 labelled ids" in message and str(dropped) in message
    assert flow.plan_status(plan_id) == "awaiting-assessment"  # every job ended; the evidence is incomplete

    inputs = lab.q(lab.admin, "assessment_inputs", {"plan_id": plan_id}, project=flow.pid)
    assert {arm: v["status"] for arm, v in inputs["arms"].items()} == {"baseline": "incomplete",
                                                                       "candidate": "incomplete"}
    assert not [o for v in inputs["arms"].values() for o in v["outputs"] if o["slot"] == "predictions"]
    # The assessment is still recorded (ADR 0018: an incomplete arm is not a contract violation): no paired units,
    # conformance incomplete, outcome inconclusive. A protocol-passed verdict is impossible.
    assessment = flow.assess(plan_id)
    assert assessment["outcome"] == "inconclusive", assessment
    assert assessment["conformance"] == "incomplete"
    assert assessment["evaluation"]["effect"] is None and assessment["evaluation"]["units_by_role"] == {}
    assert flow.plan_status(plan_id) == "assessed"
    claim = lab.q(lab.admin, "get_record", {"kind": "claim", "id": flow.claim_id}, project=flow.pid)
    assert claim["labels"]["outcome"] == "inconclusive"


def test_equal_recipes_have_zero_gain_and_the_protocol_fails(lab, tmp_path):
    flow = Flow(lab, tmp_path)
    baseline_job, candidate_job = flow.train_job(CANDIDATE_RECIPE), flow.train_job(CANDIDATE_RECIPE)
    labels = flow.artifact(flow.labels(), "class-labels/1")
    plan_id, _ = flow.freeze_and_launch(flow.fresh_draft(baseline_job, candidate_job, labels))
    for done in _run_plan(flow, plan_id).values():
        assert done["status"] == "succeeded", done.get("terminal")
    assert flow.plan_status(plan_id) == "awaiting-assessment"
    # Equal recipes under one seed train the same model: the two arms' fresh predictions are identical.
    assert _fresh_predictions(flow, plan_id, "baseline") == _fresh_predictions(flow, plan_id, "candidate")

    assessment = flow.assess(plan_id)
    effect = assessment["evaluation"]["effect"]
    assert assessment["outcome"] == "protocol-failed", assessment
    assert assessment["conformance"] == "conformant"
    assert effect["estimate"] == 0 and effect["ci_high"] < 0.02 and effect["units"] == N_HELDOUT
    claim = lab.q(lab.admin, "get_record", {"kind": "claim", "id": flow.claim_id}, project=flow.pid)
    assert claim["labels"]["outcome"] == "protocol-failed"
    state = lab.q(lab.admin, "get_claim", {"claim_id": flow.claim_id}, project=flow.pid)["claim_state"]
    assert state["state"] == "not-supported" and state["confirmed"] is False


def test_the_template_route_over_existing_predictions_is_refused_at_freeze(lab, tmp_path):
    """The template's classification path pins the predictions of recorded jobs. Those results exist before the plan
    does, so the plan is drafted (the draft is only a record) but never frozen (ADR 0021, prospective confirmation)."""
    flow = Flow(lab, tmp_path)
    baseline, candidate = flow.train(BASELINE_RECIPE), flow.train(CANDIDATE_RECIPE)
    labels = flow.artifact(flow.labels(), "class-labels/1")
    made = flow.draft(baseline, candidate, labels)  # the real template, over predictions that already exist
    spec = made["draft"]["arms"][0]["runs"][0]["jobs"][0]
    assert spec["inputs"] == {"baseline": baseline, "candidate": candidate, "labels": labels}

    with pytest.raises(Invalid) as err:
        flow.freeze_template_draft(made["artifact_id"])
    assert err.value.details["reason_code"] == "observed-evidence"
    pinned = {(i["arm"], i["port"]) for i in err.value.details["inputs"]}
    assert {("baseline", "baseline"), ("baseline", "candidate"), ("candidate", "baseline"),
            ("candidate", "candidate")} <= pinned
    (plan,) = lab.q(lab.admin, "list_records", {"kind": "evidence_plan"}, project=flow.pid)
    assert plan["status"] == "draft"  # nothing was frozen, so nothing can be launched or assessed
