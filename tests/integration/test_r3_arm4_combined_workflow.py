"""The R3 merge, end to end: the arm-4 investigator workflow of ADR 0023 (incumbent and challengers, baseline identity),
ADR 0024 (``eidos.diagnose.baseline-errors`` and typed ``eidos.search.recipe-sweep`` templates) and ADRs 0021 and 0022
(a submitted candidate, selection frozen) working together in ONE investigation.

Runtime chosen: the lab's own commands and decisions, as ``test_incumbent_challengers`` and ``test_r14_selection_e2e``
drive them (real command service and ledger on PostgreSQL, ``begin_turn``, ``accept_decision``, ``record_decision_receipts``,
``complete_turn``), not the Temporal workflow and not a scripted model: there is no provider, no model call and no network.
The context is built by the real ``build_context`` activity, so the pinned facts are what a model would be handed.

The stages, in order, each a turn of the investigation:

1. the project's own command runs and the lab records it as the baseline (``investigation_baseline``), then its scorer;
2. a real ``eidos.diagnose.baseline-errors`` job (train, validation and the baseline's predictions) ranks the planted cause;
3. a real ``eidos.search.recipe-sweep@1.1.0`` job with a typed ``template`` ("quick") returns ranked evidence whose winner
   agrees with the diagnosis (both are real lab jobs through the attempt runner in a registered numpy environment);
4. the selection rule is recorded first (prospectively), the first candidate is locked (incumbent v1), and a challenger
   run after the rule and scored by the rule's scorer on the rule's data replaces it (v2, "beats-incumbent"); a worse
   and an unscored challenger are refused;
5. the candidate is submitted (a claim is filed: derived state "candidate-submitted"), an assessment request freezes the
   selection (a further re-lock, and further challenger compute, are refused) and the investigation concludes with it.
   The launched request is a ledger record written through the seam ``test_incumbent_challengers`` uses (``_request``):
   launching a real ``assess_candidate`` needs the attested-label Flow project of ``test_assess_candidate_e2e``, which has
   no project command, and ``test_a_launched_assess_candidate_freezes_the_selection`` covers that route.

Pinned facts (``context-policy/3``): the baseline, the incumbent, the remaining allowance and the diagnosis summary are
read from the packed context of a turn of the investigation.

Stages 2 and 3 run real components, which need an interpreter with numpy (``EIDOS_NUMPY_PYTHON``; CI's integration job
provides one): the full journey skips without it. The selection and submission stages are also run without the
components, and always run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cancel_through_acceptance import _env
from test_classification_accuracy_e2e import CHARTER, FRESH_SEED, ISOLATION, RULE_ID, Flow
from test_command_path import _publish
from test_diagnose_and_template_sweep_lab import GENERATE, _bundle
from test_f5d_conclude_race import _complete as _complete_turn
from test_incumbent_challengers import SCORER_RULE, Scene, _request, refusal
from test_r2_combined_workflow import FakeProvider, Journey, allow_investigator_to_freeze, call, decision, ref
from test_turn_recovery import ShimClient

from eidos.domain.errors import Invalid
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.context import POLICY_ID, pack
from eidos.runtime.runner import AttemptRunner

NUMPY_PY = os.environ.get("EIDOS_NUMPY_PYTHON", "")
needs_numpy = pytest.mark.skipif(not (NUMPY_PY and Path(NUMPY_PY).exists()),
                                 reason="set EIDOS_NUMPY_PYTHON to an interpreter with numpy")
pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

DIAGNOSE = "eidos.diagnose.baseline-errors"
SWEEP = "eidos.search.recipe-sweep"
RUN = {"environment": "numpy", "isolation_profile": "dev:same-user"}
TEMPLATE = {"name": "quick", "task_type": "classification", "families": ["linear", "knn"], "seeds": [7]}
CLAIM = {"statement": "the locked recipe raises validation accuracy over the baseline", "kind": "improves",
         "scope": {"task": "toy-4class", "split": "validation"}}
ESTIMATE = {"metric": "accuracy", "baseline": 0.61, "candidate": 0.85, "split": "validation"}
RULE_WITH_SCORER = SCORER_RULE  # {metric: accuracy, split: validation, higher_is_better, scorer: proj.scorer, data: ...}

# The scripted journey uses disjoint seed-defined groups. The trial gets ``correct`` tenths of a group's labels right.
# The selection scorer computes accuracy from its prediction input. The assessor uses independently registered labels.
ARM_CODE = """
def trial(inputs, config, *, reader=None, writer=None, context=None):
    seed = config.get("seed", 11)
    labels = [(i + seed) % 3 for i in range(120)]
    rows = [{"id": seed * 1000 + i, "prediction": label if i < 12 * int(config["correct"]) else (label + 1) % 3}
            for i, label in enumerate(labels)]
    writer.stage_json("predictions", rows, schema="class-predictions/1")
    return {"examples": len(rows)}


def score(inputs, config, *, reader=None, writer=None, context=None):
    rows = reader.json("predictions")
    accuracy = sum(1 for r in rows if r["prediction"] == (r["id"] % 1000 + r["id"] // 1000) % 3) / len(rows)
    writer.stage_json("report", {"accuracy": accuracy}, schema="score-report/1")
    return {"metrics": {"accuracy": accuracy}}
"""
ARM_COMMAND = {"id": "eidos.project.command", "release": "1.1.0", "implements": "project-command/1", "kind": "artifact-op",
               "entry": "m:trial", "scientific_effects": ["observe"], "optional_inputs": {"source": "source-bundle/1"},
               "output": {"predictions": "class-predictions/1"}}
ARM_TRIAL = {"id": "proj.arm4-trial", "release": "1.0.0", "implements": "trial/1", "kind": "artifact-op", "entry": "m:trial",
             "scientific_effects": ["observe"], "output": {"predictions": "class-predictions/1"}}
ARM_SCORER = {"id": "proj.arm4-scorer", "release": "1.0.0", "implements": "scorer/1", "kind": "artifact-op",
              "entry": "m:score", "scientific_effects": ["observe"], "input": {"predictions": "class-predictions/1"},
              "output": {"report": "score-report/1"}}
SCORED_RULE = {**SCORER_RULE, "scorer": "proj.arm4-scorer"}  # the scorer of the scripted journey, on the validation split
ON_VALIDATION = {"split": "validation"}  # the selection data the rule names: the scorer jobs' configuration

# The baseline's validation predictions: the true labels with 30 percent of them wrong (ids as in GENERATE).
PREDICTIONS = """
v = np.load(sys.argv[1] + "/validation.npz")
r = np.random.default_rng(9)
y = v["y"]
pred = np.where(r.random(len(y)) < 0.3, (y + 1 + r.integers(0, 3, len(y))) % 4, y)
np.savez(sys.argv[1] + "/predictions.npz", ids=v["ids"], predictions=pred)
"""


# -- the scene: the incumbent/challenger scene of test_incumbent_challengers, plus the two real components ------------------
class ArmScene(Scene):
    """One project with a project command, trial and scorer (synthetic launcher jobs of chosen receipts), and, with
    ``tmp_path``, a numpy environment, the published diagnose and sweep components, the data and a real attempt runner."""

    def __init__(self, lab, tmp_path: Path | None = None):
        super().__init__(lab)
        self.refs: dict[str, str] = {}
        self.arts: dict[str, str] = {}
        self.investigator = lab.principal(f"inv-{self.pid}", ["investigator"], [self.pid])
        if tmp_path is None:
            return
        self.cmd("register_environment", {"name": "numpy", "python_executable": NUMPY_PY, "project_root": str(tmp_path),
                                          "python_version": "3.12", "installed": {}})
        self.refs = {name: _publish(lab, self.pid, *_bundle(name))["component_ref"] for name in (DIAGNOSE, SWEEP)}
        subprocess.run([NUMPY_PY, "-c", "import sys, numpy as np\n" + GENERATE + PREDICTIONS, str(tmp_path)],
                       check=True, timeout=120)
        for name, schema in (("train", "task-array/1"), ("validation", "task-array/1"), ("predictions", "prediction-array/1")):
            digest, size = lab.store.put_bytes((tmp_path / f"{name}.npz").read_bytes())
            self.arts[name] = self.cmd("register_artifact", {"digest": digest, "size": size, "schema": schema,
                                                             "media_type": "application/x-npz"})["artifact_id"]
        root = tmp_path / "labroot"
        self.runner = AttemptRunner(ShimClient(lab, self.launcher), lab.store, lab_root=root, launcher_id=self.launcher.id,
                                    api_port=1, credential_path=str(root / "config" / "secrets" / "launcher.token"))
        self.flow = SimpleNamespace(runner=self.runner)  # what test_r2_combined_workflow.Journey runs admitted jobs with

    def publish_project_components(self) -> dict[str, str]:
        """The project's own runnable command, trial and scorer (the synthetic ones of the incumbent scene only answer to
        a launcher's chosen receipts): the real runner executes these, so a scripted model's calls produce real receipts."""
        return {key: _publish(self.lab, self.pid, manifest, {"m.py": ARM_CODE})["component_ref"]
                for key, manifest in (("command", ARM_COMMAND), ("trial", ARM_TRIAL), ("scorer", ARM_SCORER))}

    def component_job(self, inv: str, name: str, config: dict, ports: tuple[str, ...]) -> dict:
        """Admit a real component job of the investigation and run it through the attempt runner; returns its final view."""
        job_id = self.cmd("run_component", {"component_ref": self.refs[name], **RUN, "purpose": "open-search",
                                            "config": config, "inputs": {p: self.arts[p] for p in ports},
                                            "investigation_id": inv})["job_id"]
        self.runner.run_job(job_id, self.pid, activity_attempt=1)
        return self.q("get_job", {"job_id": job_id})

    def artifact_json(self, job: dict, slot: str):
        digest = next(o["digest"] for o in job["outputs"] if o["slot"] == slot)
        return json.loads(Path(self.lab.store.path_for(digest)).read_text())

    def packed_context(self, inv: str, turn_id: str) -> dict:
        """The context a model would be handed for this open turn, as the real build_context activity packs and stores it."""
        acts = InvestigatorActivities(ShimClient(self.lab, self.investigator), ShimClient(self.lab, self.wf))
        done = _env(1, activity_id="context").run(acts.build_context, {"turn_id": turn_id, "project_id": self.pid,
                                                                       "investigation_id": inv})
        record = self.q("get_turn", {"turn_id": turn_id})["context"]
        digest = self.q("get_artifact", {"artifact_id": done["context_artifact_id"]})["digest"]
        return {"text": Path(self.lab.store.path_for(digest)).read_text(), "policy": record["policy"]}

    def receipt(self, inv: str, job_id: str) -> dict:
        """A job's recorded receipt, as the investigation view (and so the packed context) carries it."""
        return next(j for j in self.view(inv)["jobs"] if j["id"] == job_id)["receipt"]

    def baseline(self, inv: str) -> dict:
        return self.q("get_record", {"kind": "investigation_baseline", "id": inv})

    def conclude(self, inv: str, control: dict) -> dict:
        """A turn whose accepted decision concludes with ``control``, closed as the workflow closes it; returns the conclusion."""
        turn = self.begin(inv)
        out = self.propose(turn["turn_id"], 0, {"rationale": "r", "purpose": "open-search", "calls": [], "control": control})
        assert out["accepted"] is True, out
        _complete_turn(self.lab, self.pid, turn["turn_id"], "concluded", control)
        body = self.q("get_investigation", {"investigation_id": inv})
        assert body["status"] == "concluded", body["status"]
        return body["body"]["conclusion"]


def pinned_facts(text: str) -> str:
    return text.split("## Pinned facts", 1)[1].split("## Tools", 1)[0]


# -- the stages ------------------------------------------------------------------------------------------------------------
def stage_baseline(scene: ArmScene, inv: str) -> tuple[str, str]:
    """1. The project's own command runs; the lab records it as the baseline, and its scorer (the job that read its outputs)."""
    held: dict[str, str] = {}

    def work():
        held["base"] = scene.job(inv, ref=scene.command, metrics={"accuracy": 0.60})
        return [held["base"]], []

    assert scene.round(inv, work)["phase"] == "baseline"  # no incumbent yet
    base = held["base"]
    record = scene.baseline(inv)
    assert record["status"] == "recorded" and record["parent_id"] == inv
    assert record["body"] == {"investigation_id": inv, "job_id": base, "component": scene.command, "environment": "lab",
                              "purpose": "open-search", "metrics": {"accuracy": 0.6}, "basis": "automatic", "scored_by": None}
    (event,) = scene.events(inv, "baseline.recorded")
    assert event["subject"] == base and event["body"] == {"job_id": base}

    scorer = scene.scored(inv, base, {"accuracy": 0.61})  # the baseline's outputs, scored: recorded without a note
    record = scene.baseline(inv)
    assert record["status"] == "scored" and record["body"]["job_id"] == base
    assert record["body"]["scored_by"] == {"job_id": scorer, "metrics": {"accuracy": 0.61}}
    return base, scorer


def stage_diagnosis(scene: ArmScene, inv: str) -> dict:
    """2. eidos.diagnose.baseline-errors on train, validation and the baseline's predictions runs and succeeds."""
    held: dict[str, dict] = {}

    def work():
        held["job"] = scene.component_job(inv, DIAGNOSE, {"seed": 3}, ("train", "validation", "predictions"))
        return [held["job"]["id"]], []

    scene.round(inv, work)
    job = held["job"]
    assert job["status"] == "succeeded", job.get("terminal")
    assert job["investigation_id"] == inv and job["purpose"] == "open-search"
    assert "report" in [o["slot"] for o in job["outputs"]]
    receipt = scene.receipt(inv, job["id"])
    assert receipt["top_hypothesis"] == "per-example-additive-offset" and receipt["hypotheses"][0] == "per-example-additive-offset"
    report = scene.artifact_json(job, "report")
    assert report["schema"] == "baseline-diagnosis/1" and report["exploratory"] is True and report["claim"] is None
    assert report["hypotheses"][0]["cause"] == "per-example-additive-offset"
    assert {"train", "validation", "predictions"} <= set(report["provenance"]["inputs"])
    assert "baseline_validation_error_rate" in receipt["metrics"]  # it read the baseline's predictions
    return job


def stage_sweep(scene: ArmScene, inv: str) -> tuple[dict, dict]:
    """3. A broad typed sweep (config.template) runs in one call and returns ranked evidence; returns (job, best recipe)."""
    held: dict[str, dict] = {}

    def work():
        held["job"] = scene.component_job(inv, SWEEP, {"template": TEMPLATE}, ("train", "validation"))
        return [held["job"]["id"]], []

    scene.round(inv, work)
    job = held["job"]
    assert job["status"] == "succeeded", job.get("terminal")
    assert job["lock"]["component"]["ref"] == scene.refs[SWEEP] == "eidos.search.recipe-sweep@1.1.0"
    assert job["lock"]["config"] == {"template": TEMPLATE}  # the typed template is the job's whole configuration
    table, best = scene.artifact_json(job, "evidence_table"), scene.artifact_json(job, "best_recipe")
    assert table["counts"]["evaluated"] == table["counts"]["usable"] == 8
    assert table["counts"]["finalists_cross_validated"] == 3
    assert best["status"] == "found" and best["exploratory"] is True
    assert best["recipe"]["parameters"]["preprocess"] in ("per_example_center", "per_example_standardize")
    assert scene.receipt(inv, job["id"])["metrics"]["recipes_usable"] == 8  # what the pinned sweep line and a later lock can read
    return job, best


def stage_selection(scene: ArmScene, inv: str, recipe: dict) -> dict:
    """4. The rule first; the first candidate locked; a challenger of a round after the rule re-locks only by beating it."""
    held: dict[str, str] = {}

    def first_candidate():  # turn A: the rule, then the candidate (informed by the sweep's winner) and its score, then the lock
        rule = scene.rule(inv, scorer="proj.scorer")
        assert rule["note_id"] and "incumbent" not in rule  # a rule fixes nothing to select yet
        held["a"] = scene.job(inv, config={"recipe": recipe})
        held["a_score"] = scene.scored(inv, held["a"], {"accuracy": 0.70})
        out = scene.lock(inv, held["a"])
        assert out["incumbent"] == {"version": 1, "basis": "first-lock", "evidence": {
            "metric": "accuracy", "split": "validation", "value": 0.70, "scored_by": held["a_score"]}}
        held["lock_1"] = out["note_id"]
        return [held["a"]], [out["note_id"]]

    assert scene.round(inv, first_candidate)["phase"] == "baseline"
    assert scene.locked_job(inv) == held["a"]

    def challenger_round():  # turn B: a worse, an unscored and a better challenger, all admitted after the rule
        worse = scene.job(inv, config={"recipe": {**recipe, "variant": "worse"}})
        scene.scored(inv, worse, {"accuracy": 0.60})
        unscored = scene.job(inv, config={"recipe": {**recipe, "variant": "unscored"}})  # no scorer ran on it
        better = scene.job(inv, config={"recipe": {**recipe, "variant": "better"}})
        held["b_score"] = scene.scored(inv, better, {"accuracy": 0.85})
        for challenger, message in ((worse, "does not beat the incumbent"), (unscored, "no recorded")):
            with pytest.raises(Invalid) as caught:
                scene.lock(inv, challenger)
            assert message in refusal(caught) and caught.value.details["incumbent"]["job_id"] == held["a"]
        assert scene.locked_job(inv) == held["a"] and len(scene.events(inv, "incumbent.changed")) == 1  # nothing changed

        out = scene.lock(inv, better)  # the rule is fixed: the lock need not restate it
        assert out["incumbent"]["version"] == 2 and out["incumbent"]["basis"] == "beats-incumbent"
        assert out["incumbent"]["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.85,
                                                "scored_by": held["b_score"]}
        held["b"], held["lock_2"] = better, out["note_id"]
        return [worse, unscored, better], [out["note_id"]]

    assert scene.round(inv, challenger_round)["phase"] == "explore"  # the turn began with incumbent v1
    assert scene.locked_job(inv) == held["b"]
    body = next(n for n in scene.notes(inv, "candidate_lock") if n["id"] == held["lock_2"])["body"]["incumbent"]
    assert body["previous"]["job_id"] == held["a"] and body["previous"]["note_id"] == held["lock_1"]
    assert body["previous"]["evidence"]["value"] == 0.70 and body["rule"] == RULE_WITH_SCORER
    changed = scene.events(inv, "incumbent.changed")
    assert [(e["body"]["version"], e["body"]["job_id"]) for e in changed] == [(1, held["a"]), (2, held["b"])]
    assert changed[-1]["body"]["basis"] == "beats-incumbent" and changed[-1]["body"]["previous"]["job_id"] == held["a"]
    return held


# =====================================================================================================================
# the acceptance journey: baseline, diagnosis, typed sweep, rule + lock + challenger, submit and freeze, pinned facts
# =====================================================================================================================
@needs_numpy
def test_the_arm4_workflow_baseline_diagnosis_sweep_selection_and_submission(lab, tmp_path):
    scene = ArmScene(lab, tmp_path)
    inv = scene.investigation()

    base, base_scorer = stage_baseline(scene, inv)  # 1
    diagnosis = stage_diagnosis(scene, inv)  # 2
    sweep, best = stage_sweep(scene, inv)  # 3
    assert best["recipe"]["parameters"]["preprocess"] in ("per_example_center", "per_example_standardize")  # as diagnosed
    held = stage_selection(scene, inv, best["recipe"]["parameters"])  # 4
    assert scene.baseline(inv)["body"]["job_id"] == base  # the later jobs did not change the recorded baseline

    # the pinned facts of a turn begun now (policy 3): baseline, incumbent, allowance, diagnosis, sweep, lock
    turn = scene.begin(inv)
    state = turn["spec"]["exploration"]
    assert state["phase"] == "explore" and state["versions"] == 2 and state["rule"] == RULE_WITH_SCORER
    assert state["incumbent"]["job_id"] == held["b"] and state["incumbent"]["version"] == 2
    assert state["rounds"] == 1 and state["rounds_without_improvement"] == 0
    context = scene.packed_context(inv, turn["turn_id"])
    assert context["policy"] == POLICY_ID == "context-policy/3"
    facts = pinned_facts(context["text"])
    assert f"- Baseline (recorded): job {base} (eidos.project.command@" in facts and f"scored by {base_scorer}: " in facts
    assert f'metrics {{"accuracy": 0.6}}; scored by {base_scorer}: {{"accuracy": 0.61}}' in facts
    assert (f"- Incumbent v2: job {held['b']}, accuracy 0.85 (scored by {held['b_score']}); not reproduced yet" in facts
            and json.dumps(RULE_WITH_SCORER, sort_keys=True) in facts)
    exploration = next(line for line in facts.splitlines() if line.startswith("- Exploration:"))
    assert exploration.startswith("- Exploration: keep improving. 1 challenger round(s), 0 without improvement")
    assert "Allowance left: 850000 model tokens (after 150000 held back)." in exploration  # 1e6 limit, 15 percent held back
    assert f"- Diagnosis {diagnosis['id']} (succeeded): report " in facts and "top hypothesis per-example-additive-offset" in facts
    assert f"- Sweep {sweep['id']} (succeeded): " in facts and "best_recipe" in facts and "evidence_table" in facts
    lock = next(line for line in facts.splitlines() if line.startswith("- Current lock"))
    assert lock.startswith(f"- Current lock ({held['lock_2']}, 2 recorded): ") and held["b"] in lock
    assert "Baseline: none recorded yet" not in facts

    submit(scene, inv, turn, held)  # 5


def submit(scene: ArmScene, inv: str, open_turn: dict, held: dict[str, str]) -> None:
    """5. Submit the candidate (claim filed: derived state candidate-submitted), freeze the selection, conclude with it."""
    claim_id = scene.cmd("file_claim", {**CLAIM, "investigation_id": inv})["claim_id"]
    assert scene.q("get_record", {"kind": "claim", "id": claim_id})["labels"]["state"] == "candidate-submitted"
    late = scene.job(inv, config={"recipe": "late"})  # a challenger of a round after the rule, better than the incumbent
    scene.scored(inv, late, {"accuracy": 0.95})
    scene.finish_turn(open_turn["turn_id"])

    _request(scene, inv, "launched")  # an assessment requested (a ledger seam, see the module docstring): selection frozen
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, late)  # better than the incumbent, but the selection is frozen
    assert "frozen" in refusal(caught) and caught.value.details["job_id"] == late
    assert scene.locked_job(inv) == held["b"] and len(scene.events(inv, "incumbent.changed")) == 2
    again = scene.lock(inv, held["b"])  # naming the incumbent again changes nothing and is not refused
    assert again["incumbent"]["version"] == 2 and again["incumbent"]["basis"] == "same-incumbent"

    turn = scene.begin(inv)
    state = turn["spec"]["exploration"]
    assert state["phase"] == "finalize" and state["stop"] == "selection-frozen" and state["incumbent"]["job_id"] == held["b"]
    refused = scene.propose(turn["turn_id"], 0, {"rationale": "r", "purpose": "open-search", "calls": [
        {"tool": "run_component", "args": {"component_ref": scene.trial, "environment": "lab"}}]})
    assert refused["accepted"] is False and "exploration has stopped" in refused["error"]["message"]
    text = scene.packed_context(inv, turn["turn_id"])["text"]
    facts = pinned_facts(text)
    assert "- Exploration: STOPPED, the selection is frozen" in facts and f"- Incumbent v2: job {held['b']}" in facts
    assert "- Candidate state: " in facts and "- Route: no held-out data is attested in the charter" in facts
    scene.finish_turn(turn["turn_id"])

    control = {"action": "conclude", "summary": "the incumbent is the result",
               "claim": {"disposition": "candidate", "point_estimate": ESTIMATE}}
    conclusion = scene.conclude(inv, control)
    claim_state = conclusion["claim_state"]
    assert claim_state["state"] == "candidate-submitted" and claim_state["confirmed"] is False, claim_state
    assert claim_state["label"] == "candidate submitted for confirmation" and claim_state["claim_id"] == claim_id
    assert claim_state["requested_disposition"] == "candidate" and "downgraded_from" not in claim_state
    assert claim_state["assessment_id"] is None and claim_state["point_estimate"]["source"] == "investigator"


def test_selection_and_submission_without_the_components(lab):
    """The stages that need no numpy: baseline, rule, incumbent v1 and v2, refusals, pinned facts, submission and freeze."""
    scene = ArmScene(lab)
    inv = scene.investigation()
    base, base_scorer = stage_baseline(scene, inv)
    held = stage_selection(scene, inv, {"preprocess": "per_example_center", "family": "linear"})

    turn = scene.begin(inv)
    assert turn["spec"]["exploration"]["phase"] == "explore" and turn["spec"]["exploration"]["versions"] == 2
    context = scene.packed_context(inv, turn["turn_id"])
    assert context["policy"] == POLICY_ID
    facts = pinned_facts(context["text"])
    assert f"- Baseline (recorded): job {base}" in facts and f"scored by {base_scorer}" in facts
    assert f"- Incumbent v2: job {held['b']}, accuracy 0.85" in facts and "Allowance left: " in facts
    assert "- Diagnosis " not in facts and "- Sweep " not in facts  # no component ran in this variant
    assert pack({**scene.view(inv), "exploration": turn["spec"]["exploration"]}, [], policy=POLICY_ID)["text"].count(
        "- Incumbent v2") == 1
    submit(scene, inv, turn, held)


# =====================================================================================================================
# the same workflow, driven by a scripted fake provider through the real decide/execute activities (test_r2 harness)
# =====================================================================================================================
LAB = {"environment": "lab", "isolation_profile": "dev:same-user"}


def job_of(prompt: str, component: str, tag: str) -> str:
    """The id of the succeeded job of ``component`` whose configuration carries ``tag``, as the model reads it in the Jobs
    section of its own context (the lock a model writes names a job it saw there)."""
    for line in prompt.splitlines():
        try:
            row = json.loads(line) if line.startswith('{"') else {}
        except ValueError:
            continue
        if row.get("component") == component and (row.get("config") or {}).get("tag") == tag and row.get("status") == "succeeded":
            return row["id"]
    raise AssertionError(f"no succeeded {component} job tagged {tag!r} among the jobs of this context")


def lock(component: str, tag: str, **control):
    """A decision (a function of the prompt the model got) that locks the job it finds in its context as the candidate."""
    return lambda prompt: decision(call("record_note", kind="candidate_lock",
                                        text=json.dumps({"job_id": job_of(prompt, component, tag)})), **control)


@needs_numpy
def test_a_scripted_fake_provider_drives_the_arm4_workflow_through_the_real_activities(lab, tmp_path):
    scene = ArmScene(lab, tmp_path)
    refs = scene.publish_project_components()
    sources = {name: scene.arts[name] for name in ("train", "validation")}
    confirmation = Flow(lab, tmp_path, project_id=scene.pid)
    allow_investigator_to_freeze(scene)
    fresh_seed = int(FRESH_SEED)
    labels = confirmation.artifact([{"id": fresh_seed * 1000 + i, "label": (i + fresh_seed) % 3}
                                    for i in range(120)], "class-labels/1")
    labels_digest = scene.q("get_artifact", {"artifact_id": labels})["digest"]
    charter = {**CHARTER, "study_id": "r4-full-journey", "held_out": {"labels": [labels_digest]}}
    scene.cmd("commit_study_charter", {"charter": charter})
    charter_id = "charter:r4-full-journey"

    def trial(tag: str, correct: int, seed: int = 11) -> dict:
        return call("run_component", component_ref=refs["trial"], **LAB,
                    config={"correct": correct, "tag": tag, "seed": seed})

    def scorer(tag: str, index: int) -> dict:  # reads the predictions of the call at ``index`` of its own decision
        return call("run_component", component_ref=refs["scorer"], **LAB, config={**ON_VALIDATION, "tag": f"{tag}-score"},
                    inputs={"predictions": ref(index, "outputs.predictions")})

    script = [
        # 1. the baseline: the project's own command, then its scorer (it names the command's predictions with a $ref)
        decision(call("run_component", component_ref=refs["command"], **LAB, config={"correct": 6, "tag": "baseline"}),
                 scorer("baseline", 0), action="wait_for_jobs"),
        # 2. the diagnosis, on train, validation and the baseline's predictions
        decision(call("run_component", component_ref=scene.refs[DIAGNOSE], **RUN, config={"seed": 3},
                      inputs={**sources, "predictions": scene.arts["predictions"]}), action="wait_for_jobs"),
        # 3. one typed template sweep
        decision(call("run_component", component_ref=scene.refs[SWEEP], **RUN, config={"template": TEMPLATE}, inputs=sources),
                 action="wait_for_jobs"),
        # 4. the selection rule first; then the first candidate and its score (admitted after the rule)
        decision(call("record_note", kind="selection_rule", text=json.dumps(SCORED_RULE)), trial("first", 7),
                 scorer("first", 1), action="wait_for_jobs"),
        # 5. the first candidate is locked (a function of the context it was scored in); a challenger round follows
        lock(refs["trial"], "first"),
        decision(trial("challenger", 9), scorer("challenger", 0), action="wait_for_jobs"),
        # 6. the scored challenger is better: re-lock it
        lock(refs["trial"], "challenger", action="wait_for_jobs"),
    ]
    provider = FakeProvider(*script)
    journey = Journey(scene, provider, charter_id)
    inv = journey.inv
    assert scene.q("get_investigation", {"investigation_id": inv})["body"]["investigator"]["finalization"] == "autonomous"
    assert [journey.turn()["stop_reason"] for _ in range(6)] == ["waiting"] * 6
    receipts = {i: journey.receipts(i) for i in range(len(script))}
    assert all(r["ok"] for rs in receipts.values() for r in rs), [r for rs in receipts.values() for r in rs if not r["ok"]]

    # 1. the baseline: recorded by the lab from the first project-command job, scored by the job that read its predictions
    base, base_scorer = receipts[0][0]["result"]["job_id"], receipts[0][1]["result"]["job_id"]
    assert receipts[0][1]["depends_on"] == [0] and receipts[0][1]["resolved"][0]["ref"] == "call[0].outputs.predictions"
    record = scene.baseline(inv)
    assert record["status"] == "scored" and record["body"]["job_id"] == base and record["body"]["basis"] == "automatic"
    assert record["body"]["scored_by"] == {"job_id": base_scorer, "metrics": {"accuracy": 0.6}}

    # 2. the diagnosis job succeeded and ranked the planted offset first
    diagnosis = scene.q("get_job", {"job_id": receipts[1][0]["result"]["job_id"]})
    assert diagnosis["status"] == "succeeded" and diagnosis["lock"]["component"]["ref"] == scene.refs[DIAGNOSE]
    assert {"train", "validation", "predictions"} <= set(diagnosis["lock"]["inputs"])
    report = scene.artifact_json(diagnosis, "report")
    assert report["hypotheses"][0]["cause"] == "per-example-additive-offset" and report["claim"] is None

    # 3. the template sweep: ranked evidence in one call, and its winner agrees with the diagnosis
    sweep = scene.q("get_job", {"job_id": receipts[2][0]["result"]["job_id"]})
    assert sweep["status"] == "succeeded" and sweep["lock"]["config"] == {"template": TEMPLATE}
    table, best = scene.artifact_json(sweep, "evidence_table"), scene.artifact_json(sweep, "best_recipe")
    assert table["counts"]["evaluated"] == table["counts"]["usable"] == 8 and best["status"] == "found"
    assert best["recipe"]["parameters"]["preprocess"] in ("per_example_center", "per_example_standardize")

    # 4. the rule came first; the first candidate is incumbent v1; the challenger, run after the rule, is v2
    (rule,) = scene.notes(inv, "selection_rule")
    assert rule["id"] == receipts[3][0]["result"]["note_id"] and json.loads(rule["body"]["text"]) == SCORED_RULE
    first, first_score = receipts[3][1]["result"]["job_id"], receipts[3][2]["result"]["job_id"]
    challenger, challenger_score = receipts[5][0]["result"]["job_id"], receipts[5][1]["result"]["job_id"]
    assert receipts[5][1]["depends_on"] == [0]  # the score names the challenger's predictions with a $ref
    assert journey.proposal(5)["calls"][1]["args"]["inputs"]["predictions"] == ref(0, "outputs.predictions")
    assert receipts[4][0]["result"]["incumbent"] == {"version": 1, "basis": "first-lock", "evidence": {
        "metric": "accuracy", "split": "validation", "value": 0.7, "scored_by": first_score}}
    second = receipts[6][0]["result"]["incumbent"]
    assert second["version"] == 2 and second["basis"] == "beats-incumbent"
    assert second["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.9, "scored_by": challenger_score}
    assert scene.locked_job(inv) == challenger
    changed = scene.events(inv, "incumbent.changed")
    assert [(e["body"]["version"], e["body"]["job_id"]) for e in changed] == [(1, first), (2, challenger)]
    assert changed[-1]["body"]["basis"] == "beats-incumbent" and changed[-1]["body"]["previous"]["job_id"] == first
    jobs = {j["id"]: j for j in scene.view(inv)["jobs"]}
    assert jobs[challenger]["receipt"]["examples"] == 120 and "metrics" not in jobs[challenger]["receipt"]  # the trial scores nothing
    assert jobs[challenger_score]["receipt"]["metrics"] == {"accuracy": 0.9}  # the rule's scorer does

    # 5. Reproduce the selected recipe under a different seed in the ordinary finalization window.
    assert scene.cmd("enter_finalization", {
        "investigation_id": inv, "trigger": "exploration-boundary",
    })["mode"] == "ordinary"
    provider.script.append({
        **decision(trial("challenger", 9, seed=12), scorer("reproduction", 0), action="wait_for_jobs"),
        "purpose": "reproduction",
    })
    assert journey.turn()["stop_reason"] == "waiting"
    reproduction = journey.receipts(7)
    assert all(r["ok"] for r in reproduction), reproduction
    reproduced_job = reproduction[0]["result"]["job_id"]

    # 6. The provider sees the safe route and requests a held-out assessment with no hidden artifact ID.
    def assess_from_context(prompt: str) -> dict:
        assert "Route: held-out data is attested in the charter" in prompt
        assert labels not in prompt and labels_digest not in prompt
        return {
            **decision(call("assess_candidate", success_rule_id=RULE_ID, charter_id=charter_id,
                            baseline={"job_id": base}, candidate={"job_id": challenger}, seeds=[fresh_seed],
                            data_provenance={"split": "held-out"}, required_isolation=ISOLATION,
                            claim={"statement": CLAIM["statement"], "scope": CLAIM["scope"]}),
                       action="wait_for_jobs"),
            "purpose": "confirmation",
        }

    provider.script.append(assess_from_context)
    assert journey.turn()["stop_reason"] == "waiting"
    request = journey.receipts(8)[0]
    assert request["ok"] and request["result"]["status"] == "launched", request
    assert "labels" not in journey.proposal(8)["calls"][0]["args"]
    plan_id = request["result"]["plan_id"]
    confirmation.run_plan(plan_id)
    assessment = confirmation.assess(plan_id)
    assert assessment["outcome"] == "protocol-passed", assessment

    provider.script.append(decision(action="conclude", claim={"disposition": "confirmed"}))
    assert journey.turn()["stop_reason"] == "concluded"
    state = journey.conclusion()["claim_state"]
    assert state["state"] == "protocol-passed" and state["confirmed"] is True, state
    assert state["claim_id"] == request["result"]["claim_id"] and state["assessment_id"] == assessment["assessment_id"]
    shown = scene.q("get_investigation", {"investigation_id": inv})
    assert shown["needs_input"] is None and not scene.events(inv, "investigation.finalization_started")
    assert len(provider.prompts) == len(provider.script)
    assert all(labels_digest not in prompt and labels not in prompt for prompt in provider.prompts)
    assert assessment["assessment_id"] not in provider.prompts[-1]
    assert "outcome protocol-passed" not in provider.prompts[-1] and "health intact" not in provider.prompts[-1]
    assert "Independent assessment recorded. Its outcome is sealed" in provider.prompts[-1]

    # the pinned facts of the last turn's context (the model's own prompt): baseline, incumbent v2, allowance, diagnosis
    facts = pinned_facts(provider.prompts[-1])
    assert (f"- Baseline (recorded): job {base} (eidos.project.command@1.1.0, environment lab), metrics null; "
            f'scored by {base_scorer}: {{"accuracy": 0.6}}') in facts
    assert f"- Incumbent v2: job {challenger}, accuracy 0.9 (scored by {challenger_score}); reproduced by {reproduced_job}" in facts
    assert f"- Selection rule: {json.dumps(SCORED_RULE, sort_keys=True)}" in facts
    exploration = next(line for line in facts.splitlines() if line.startswith("- Exploration:"))
    assert "Exploration" in exploration and "keep improving" not in exploration, exploration
    assert f"- Diagnosis {diagnosis['id']} (succeeded): report " in facts and "top hypothesis per-example-additive-offset" in facts
    assert f"- Sweep {sweep['id']} (succeeded): " in facts and "evidence_table" in facts
    assert next(line for line in facts.splitlines() if line.startswith("- Current lock")).startswith(
        f"- Current lock ({receipts[6][0]['result']['note_id']}, 2 recorded): ")
