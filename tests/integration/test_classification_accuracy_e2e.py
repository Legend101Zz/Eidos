"""Two training jobs plus held-out predictions -> an assessor verdict on ordinary accuracy (ADR 0018), end to end.

Everything runs through the real command service on PostgreSQL, the real `AttemptRunner` (development profile
`dev:same-user`) and the real built-in components published from the repository directories:

* a project "training" component (standard library only: a nearest-centroid classifier on a synthetic 3-class
  dataset) runs twice, once per recipe, and writes held-out predictions;
* `eidos.templates.improves-under-budget@1.2.0` turns the two prediction artifacts plus the labels into an
  evidence-plan draft (its `classification` request path), which is compiled against the charter, frozen and
  launched;
* each arm runs `eidos.task.paired-classification-report@1.0.0` (it fails closed when the three inputs do not
  align) and the plan becomes `awaiting-assessment`;
* `AssessorActivities.assess_plan` runs `eidos.assessors.paired-ci@1.1.0` (metric `accuracy`) on the arms'
  `predictions` outputs and submits the assessment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment

from eidos.orchestration.activities import AssessorActivities
from eidos.runtime.runner import AttemptRunner

pytestmark = pytest.mark.pg

COMPONENTS = Path(__file__).resolve().parents[2] / "components"
REPORT = "eidos.task.paired-classification-report"
TEMPLATE = "eidos.templates.improves-under-budget"
ASSESSOR = "eidos.assessors.paired-ci"
TEMPLATE_REF = f"{TEMPLATE}@1.2.0"
ASSESSOR_REF = f"{ASSESSOR}@1.1.0"

N_HELDOUT = 360
DATA_SEED = 7
SEED = "11"
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
# recipe uses both features. Held-out examples depend on `data_seed` only, so both recipes predict the same ids.
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
    rows = [{"id": r["id"], "prediction": predict(r)} for r in held]
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
        lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER}, project=self.pid)
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

    def train(self, recipe: dict) -> str:
        """Run the project's training job for a recipe; returns its predictions artifact id."""
        job_id = self.lab.cmd(self.lab.admin, "run_component", {
            "component_ref": self.train_ref, "environment": "lab", "isolation_profile": "dev:same-user",
            "config": {**recipe, "data_seed": DATA_SEED, "n_heldout": N_HELDOUT}}, project=self.pid)["result"]["job_id"]
        done = self.run(job_id)
        assert done["status"] == "succeeded", done.get("terminal")
        artifact_id, rows = self.output(done, "predictions")
        assert len(rows) == N_HELDOUT
        return artifact_id

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

    def freeze_and_launch(self, draft_artifact_id: str) -> tuple[str, dict[str, list[str]]]:
        lab, pid = self.lab, self.pid
        plan = lab.cmd(lab.admin, "draft_evidence_plan", {
            "claim_id": self.claim_id, "charter_id": f"charter:{CHARTER['study_id']}", "template_ref": TEMPLATE_REF,
            "draft_artifact_id": draft_artifact_id}, project=pid)["result"]
        assert plan["obligations"] == []
        plan_id = plan["plan_id"]
        lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan_id}, project=pid)
        launched = lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)["result"]
        return plan_id, launched["jobs"]

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


def _run_plan_jobs(flow: Flow, jobs: dict[str, list[str]]) -> dict[str, dict]:
    return {job_id: flow.run(job_id) for arm in ("baseline", "candidate") for job_id in jobs[arm]}


# -----------------------------------------------------------------------------------------------------------
def test_two_training_jobs_and_heldout_predictions_reach_an_accuracy_verdict(lab, tmp_path):
    flow = Flow(lab, tmp_path)
    baseline = flow.train(BASELINE_RECIPE)
    candidate = flow.train(CANDIDATE_RECIPE)
    labels = flow.artifact(flow.labels(), "class-labels/1")
    base_acc = _accuracy(_predictions(flow, baseline), flow.labels())
    cand_acc = _accuracy(_predictions(flow, candidate), flow.labels())
    assert cand_acc - base_acc > 0.1  # the recipes really differ on the held-out set

    made = flow.draft(baseline, candidate, labels)
    draft = made["draft"]
    assert [a["id"] for a in draft["arms"]] == ["baseline", "candidate"] and draft["cluster_by"] is None
    jobs_planned = {a["id"]: [j for r in a["runs"] for j in r["jobs"]] for a in draft["arms"]}
    for arm, (spec,) in jobs_planned.items():
        assert spec["component"] == f"{REPORT}@1.0.0" and spec["config"] == {"role": arm, "cluster": SEED}
        assert spec["inputs"] == {"baseline": baseline, "candidate": candidate, "labels": labels}
    plan_id, jobs = flow.freeze_and_launch(made["artifact_id"])
    assert {arm: len(ids) for arm, ids in jobs.items()} == {"baseline": 1, "candidate": 1}

    finished = _run_plan_jobs(flow, jobs)
    for arm, (job_id,) in jobs.items():
        done = finished[job_id]
        assert done["status"] == "succeeded", done.get("terminal")
        assert done["terminal"]["receipt"]["role"] == arm and done["terminal"]["receipt"]["examples"] == N_HELDOUT
        _, rows = flow.output(done, "predictions")
        assert {r["role"] for r in rows} == {arm} and len(rows) == N_HELDOUT
    assert flow.plan_status(plan_id) == "awaiting-assessment"

    assessment = flow.assess(plan_id)
    ev = assessment["evaluation"]
    assert assessment["outcome"] == "protocol-passed", assessment
    assert assessment["conformance"] == "conformant" and assessment["plan_id"] == plan_id
    assert ev["metric"] == "accuracy" and ev["direction"] == "increase" and ev["min_effect"] == 0.02
    assert ev["effect"]["units"] == N_HELDOUT and ev["units_by_role"] == {"baseline": N_HELDOUT,
                                                                           "candidate": N_HELDOUT}
    assert ev["effect"]["ci_low"] >= 0.02 and ev["effect"]["ci_low"] <= ev["effect"]["estimate"]
    assert ev["effect"]["estimate"] == pytest.approx(cand_acc - base_acc)  # the assessor's gain is the true gain
    assert ev["clusters"] == [SEED] and ev["assessor"] == ASSESSOR_REF and ev["cluster_by"] is None
    assert ev["unbounded_fields"] == [] and assessment["assessor"]["ref"] == ASSESSOR_REF
    assert flow.plan_status(plan_id) == "assessed"
    claim = lab.q(lab.admin, "get_record", {"kind": "claim", "id": flow.claim_id}, project=flow.pid)
    assert claim["status"] == "assessed" and claim["labels"]["outcome"] == "protocol-passed"


def test_a_candidate_missing_one_heldout_id_fails_closed_and_cannot_pass(lab, tmp_path):
    flow = Flow(lab, tmp_path)
    baseline = flow.train(BASELINE_RECIPE)
    candidate_rows = _predictions(flow, flow.train(CANDIDATE_RECIPE))
    dropped = candidate_rows.pop(17)["id"]
    short_candidate = flow.artifact(candidate_rows, "class-predictions/1")
    labels = flow.artifact(flow.labels(), "class-labels/1")

    made = flow.draft(baseline, short_candidate, labels)  # planning cannot see the ids; the report jobs do
    plan_id, jobs = flow.freeze_and_launch(made["artifact_id"])
    finished = _run_plan_jobs(flow, jobs)
    for arm, (job_id,) in jobs.items():  # each arm checks the whole three-way alignment, so both refuse
        done = finished[job_id]
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
    baseline = flow.train(CANDIDATE_RECIPE)
    candidate = flow.train(CANDIDATE_RECIPE)
    labels = flow.artifact(flow.labels(), "class-labels/1")
    made = flow.draft(baseline, candidate, labels)
    plan_id, jobs = flow.freeze_and_launch(made["artifact_id"])
    for done in _run_plan_jobs(flow, jobs).values():
        assert done["status"] == "succeeded", done.get("terminal")
    assert flow.plan_status(plan_id) == "awaiting-assessment"

    assessment = flow.assess(plan_id)
    effect = assessment["evaluation"]["effect"]
    assert assessment["outcome"] == "protocol-failed", assessment
    assert assessment["conformance"] == "conformant"
    assert effect["estimate"] == 0 and effect["ci_high"] < 0.02 and effect["units"] == N_HELDOUT
    claim = lab.q(lab.admin, "get_record", {"kind": "claim", "id": flow.claim_id}, project=flow.pid)
    assert claim["labels"]["outcome"] == "protocol-failed"
