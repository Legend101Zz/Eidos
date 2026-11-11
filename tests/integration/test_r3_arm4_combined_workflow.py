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

import pytest
from test_cancel_through_acceptance import _env
from test_command_path import _publish
from test_diagnose_and_template_sweep_lab import GENERATE, _bundle
from test_f5d_conclude_race import _complete as _complete_turn
from test_incumbent_challengers import SCORER_RULE, Scene, _request, refusal
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
