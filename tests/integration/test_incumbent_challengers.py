"""ADR 0023, incumbent and challengers, end to end against PostgreSQL (no model, no network).

The lab side of the exploration policy, through the real command service:

* the selection rule (metric, split, direction, and since R14 the scorer component and the selection data) is fixed once
  recorded (a ``selection_rule`` note or the ``selection`` of the first lock) and may only name validation or training
  cross-validation data; a lock that names a job with no recorded rule is refused, as is a rule first given with a
  re-lock;
* the first lock is incumbent version 1 with evidence read from recorded job receipts (the rule's scorer run on the
  rule's data); a re-lock must beat the incumbent on the rule (a worse or equal challenger, one with no recorded
  metric, an unscored incumbent, or a challenger admitted before the rule is refused and the incumbent stays), a better
  one is version 2 with ``previous`` and an ``incumbent.changed`` event;
* the selection freezes once an evidence plan is frozen or an assessment requested (assessor feedback never selects);
* the baseline of an investigation is recorded as a durable record by the launcher's terminal hook, and the packed
  context reads it instead of guessing from recent jobs;
* ``begin_turn`` records the exploration state in the turn spec and ``accept_decision`` refuses new challenger compute
  once exploration has stopped (finalize phase), while reproduction is still accepted;
* ``mark_needs_input`` accepts ``provider-unavailable``.

Jobs are admitted with ``run_component`` and driven through the trusted launcher's commands (``start_attempt``,
``publish_outputs``, ``finish_attempt``) with receipts of the test's choosing, so the terminal hook that records the
baseline runs for every job. A job's recorded metric is what selection reads; the Flow-based test at the end launches a
real ``assess_candidate`` to freeze the selection.
"""

from __future__ import annotations

import json
import uuid

import pytest
from test_assess_candidate_e2e import Scene as AssessScene
from test_classification_accuracy_e2e import BASELINE_RECIPE, CANDIDATE_RECIPE
from test_command_path import _publish, _register_lab_env
from test_f5_turn_authorization import RUN_A
from test_f5c_ticket_and_handoff import _begin_decision, _begin_turn, _complete

from eidos.domain.assessment import candidate_lock_job_id
from eidos.domain.errors import Invalid, NotFound
from eidos.investigator.context import pack

pytestmark = pytest.mark.pg

COMMAND_REF = "eidos.project.command"
SELECTION_DATA = {"config": {"split": "validation"}}
# R14 #2: the rule names the scorer component and the selection data it reads before any result is known. The default
# scorer is the trial itself (its receipt carries the metric); SCORER_RULE scores through the scorer component.
RULE = {"metric": "accuracy", "split": "validation", "direction": "higher_is_better", "scorer": "proj.trial",
        "data": SELECTION_DATA}
SCORER_RULE = {**RULE, "scorer": "proj.scorer"}
LIMITS = {"model_tokens": 1e6, "compute_seconds": 1e6, "trials": 1000}

COMMAND = {"id": COMMAND_REF, "release": "1.0.0", "implements": "project-command/1", "kind": "artifact-op",
           "entry": "m:run", "scientific_effects": ["observe"], "optional_inputs": {"source": "source-bundle/1"},
           "output": {"predictions": "class-predictions/1"}}
TRIAL = {"id": "proj.trial", "release": "1.0.0", "implements": "trial/1", "kind": "artifact-op", "entry": "m:run",
         "scientific_effects": ["observe"], "output": {"predictions": "class-predictions/1"}}
INFER = {"id": "proj.infer", "release": "1.0.0", "implements": "infer/1", "kind": "artifact-op", "entry": "m:run",
         "scientific_effects": ["observe"], "input": {"predictions": "class-predictions/1"},
         "output": {"predictions": "class-predictions/1"}}
SCORER = {"id": "proj.scorer", "release": "1.0.0", "implements": "scorer/1", "kind": "artifact-op", "entry": "m:run",
          "scientific_effects": ["observe"], "input": {"predictions": "class-predictions/1"},
          "output": {"report": "score-report/1"}}
CODE = {"m.py": "def run(i, c):\n    return {}\n"}


def rule_text(**override) -> str:
    return json.dumps({**RULE, **override})


# -- the scene ----------------------------------------------------------------------------------------------------------
class Scene:
    """One project with a project command, a trial and a scorer component, a charter, and shorthands for the lab."""

    def __init__(self, lab, *, held_out: dict | None = None):
        self.lab = lab
        self.pid = f"p{uuid.uuid4().hex[:8]}"
        lab.cmd(lab.admin, "create_project", {"project_id": self.pid, "budgets": {"compute_seconds": 1e7, "trials": 1000}})
        _register_lab_env(lab, self.pid)
        self.command = _publish(lab, self.pid, COMMAND, CODE)["component_ref"]
        self.trial = _publish(lab, self.pid, TRIAL, CODE)["component_ref"]
        self.infer = _publish(lab, self.pid, INFER, CODE)["component_ref"]
        self.scorer = _publish(lab, self.pid, SCORER, CODE)["component_ref"]
        lab.cmd(lab.admin, "commit_study_charter", {"charter": {
            "study_id": f"s{self.pid}", "question": "q", "exploration_only": True,
            **({"held_out": held_out} if held_out else {})}}, project=self.pid)
        self.launcher = lab.principal(f"launcher-{self.pid}", ["launcher"], [self.pid])
        self.wf = lab.principal(f"wf-{self.pid}", ["workflow"], [self.pid])
        self.n = 0

    # -- plumbing
    def cmd(self, name: str, payload: dict) -> dict:
        return self.lab.cmd(self.lab.admin, name, payload, project=self.pid)["result"]

    def q(self, name: str, params: dict | None = None) -> dict:
        return self.lab.q(self.lab.admin, name, params or {}, project=self.pid)

    def investigation(self, *, limits: dict | None = None, **investigator) -> str:
        return self.cmd("start_investigation", {
            "question": "which recipe is best?", "charter_id": f"charter:s{self.pid}", "environment": "lab",
            "limits": limits or LIMITS, "investigator": {"runtime": "fixture",
                                                         "settings": {"max_tokens_per_decision": 120_000},
                                                         **investigator}})["investigation_id"]

    # -- jobs, through the launcher
    def job(self, inv: str | None, *, ref: str | None = None, purpose: str = "open-search", metrics: dict | None = None,
            inputs: dict | None = None, status: str = "succeeded", wall_seconds: int = 60,
            config: dict | None = None, finish: bool = True, slot: str | None = None) -> str:
        """Admit a run of ``ref`` (the trial component by default) and finish it through the launcher: it publishes one
        output and reports ``metrics`` in its receipt. Returns the job id. The job's configuration names the validation
        split unless ``config`` says otherwise (the default selection data of RULE). ``finish=False`` leaves the job
        admitted, a challenger still running: ``finish_job`` ends it later."""
        job = self.cmd("run_component", {"component_ref": ref or self.trial, "environment": "lab", "purpose": purpose,
                                         "inputs": inputs or {}, "wall_seconds": wall_seconds,
                                         "config": {"split": "validation", **(config or {})},
                                         **({"investigation_id": inv} if inv else {})})["job_id"]
        if finish:
            self.finish_job(job, ref=ref, metrics=metrics, status=status, slot=slot)
        return job

    def finish_job(self, job: str, *, ref: str | None = None, metrics: dict | None = None, status: str = "succeeded",
                   slot: str | None = None) -> None:
        """Drive an admitted job to its terminal state through the launcher (``ref``: the component it runs)."""
        self.n += 1
        started = self.lab.cmd(self.launcher, "start_attempt", {"job_id": job}, project=self.pid)["result"]
        attempt = {"job_id": job, "attempt_id": started["attempt_id"], "epoch": started["epoch"]}
        if status == "succeeded":
            slot = slot or ("report" if ref == self.scorer else "predictions")
            schema = "score-report/1" if slot == "report" else "class-predictions/1"
            digest, size = self.lab.store.put_bytes(json.dumps({"job": job, "n": self.n}).encode())
            manifest = {"wire": "eidos-wire/1", **attempt, "status": "succeeded",
                        "outputs": [{"slot": slot, "filename": f"{slot}.json", "digest": digest, "size": size,
                                     "media_type": "application/json", "schema": schema}]}
            self.lab.cmd(self.launcher, "publish_outputs", {**attempt, "manifest": manifest}, project=self.pid)
        receipt = {"metrics": metrics} if metrics else {}
        self.lab.cmd(self.launcher, "finish_attempt", {
            **attempt, "status": status, "receipt": receipt,
            **({"error": {"category": "runtime", "message": "the run failed"}, "final": True} if status != "succeeded"
               else {})}, project=self.pid)
        assert self.q("get_job", {"job_id": job})["status"] == status

    def output(self, job: str, slot: str = "predictions") -> str:
        return next(o["artifact_id"] for o in self.q("get_job", {"job_id": job})["outputs"] if o["slot"] == slot)

    def scored(self, inv: str, job: str, metrics: dict, **kw) -> str:
        """A scorer job that reads ``job``'s predictions and reports ``metrics`` (``kw``: more of ``job``'s arguments,
        e.g. the ``config`` naming the data it scored)."""
        return self.job(inv, ref=self.scorer, inputs={"predictions": self.output(job)}, metrics=metrics, **kw)

    def accuracy_job(self, inv: str, value: float, *, metric: str = "accuracy") -> str:
        return self.job(inv, metrics={metric: value})

    # -- notes
    def note(self, inv: str, kind: str, text: str) -> dict:
        return self.cmd("record_note", {"investigation_id": inv, "kind": kind, "text": text})

    def rule(self, inv: str, **override) -> dict:
        return self.note(inv, "selection_rule", rule_text(**override))

    def lock(self, inv: str, job: str, **extra) -> dict:
        return self.note(inv, "candidate_lock", json.dumps({"job_id": job, **extra}))

    def notes(self, inv: str, kind: str | None = None) -> list[dict]:
        notes = self.q("list_records", {"kind": "note", "parent_id": inv, "summary": False, "limit": 1000})
        return [n for n in notes if kind is None or n["body"]["kind"] == kind]

    def events(self, inv: str, *types: str) -> list[dict]:
        return self.q("list_events", {"investigation_id": inv, "types": list(types), "limit": 500})["events"]

    def view(self, inv: str) -> dict:
        horizon = self.q("project_summary")["horizon"]
        return self.q("investigation_view", {"investigation_id": inv, "horizon": horizon})

    def locked_job(self, inv: str) -> str | None:
        """The job of the latest valid lock, as the lab reads it."""
        pinned = [n for n in self.view(inv)["pinned_notes"] if n["kind"] == "candidate_lock"]
        return candidate_lock_job_id(pinned[-1]) if pinned else None

    # -- turns, as the TurnWorkflow's run sends them
    def begin(self, inv: str) -> dict:
        out = _begin_turn(self.lab, self.pid, inv, RUN_A)
        assert out["started"] is True, out
        return out

    def finish_turn(self, turn_id: str) -> None:
        _complete(self.lab, self.pid, turn_id, RUN_A)

    def round(self, inv: str, work) -> dict:
        """One turn whose accepted decision admitted the jobs and recorded the notes ``work()`` returns as
        ``(job ids, note ids)`` after the turn began (the receipts an executed decision records); the turn is then
        closed. Returns the exploration state the turn began with."""
        turn = self.begin(inv)
        jobs, notes = work()
        decision = self.propose(turn["turn_id"], 0, {"rationale": "r", "purpose": "open-search", "calls": [
            {"tool": "record_note", "args": {"kind": "hypothesis", "text": "the round"}}]})
        assert decision["accepted"] is True, decision
        receipts = [{"tool": "run_component", "ok": True, "result": {"job_id": j}} for j in jobs] + \
                   [{"tool": "record_note", "ok": True, "result": {"note_id": n}} for n in notes]
        self.lab.cmd(self.wf, "record_decision_receipts", {"decision_id": decision["decision_id"], "receipts": receipts},
                     project=self.pid)
        self.finish_turn(turn["turn_id"])
        return turn["spec"]["exploration"]

    def propose(self, turn_id: str, slot: int, proposal: dict) -> dict:
        """A proposal sent the way the decide activity sends it; returns the raw acceptance (it may be a refusal)."""
        execution = f"{RUN_A}/decide-slot-{slot}"
        epoch = _begin_decision(self.lab, self.pid, turn_id, execution, slot=slot)["epoch"]
        return self.lab.cmd(self.wf, "accept_decision", {"turn_id": turn_id, "slot": slot, "proposal": proposal,
                                                         "usage_recorded": True, "attempt": 1, "epoch": epoch,
                                                         "execution": execution}, project=self.pid)["result"]

    # -- ledger writes the lab commands do not offer to a test
    def put(self, kind: str, rid: str, body: dict, *, status: str, parent_id: str | None = None) -> None:
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            tx.put_record(kind, rid, self.pid, body, created_by=self.lab.admin.id, status=status, parent_id=parent_id)


@pytest.fixture()
def scene(lab):
    return Scene(lab)


def refusal(excinfo) -> str:
    assert isinstance(excinfo.value, Invalid)
    return excinfo.value.message


# =====================================================================================================================
# 1. the selection rule is fixed
# =====================================================================================================================
def test_a_selection_rule_is_recorded_once_and_a_second_one_is_refused_as_fixed(scene):
    inv = scene.investigation()
    first = scene.rule(inv)
    assert first["note_id"] and "incumbent" not in first  # a rule is not a lock: no incumbent comes of it

    for rewrite in (RULE, {**RULE, "direction": "lower_is_better"}, {**RULE, "metric": "f1"}):
        with pytest.raises(Invalid) as caught:
            scene.note(inv, "selection_rule", json.dumps(rewrite))
        assert "fixed" in refusal(caught)
    assert [n["id"] for n in scene.notes(inv, "selection_rule")] == [first["note_id"]]  # the refusals recorded nothing
    assert [n["id"] for n in scene.view(inv)["pinned_notes"] if n["kind"] == "selection_rule"] == [first["note_id"]]


def test_a_rule_a_lock_carried_is_fixed_for_a_later_rule_note(scene):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.7), selection=RULE)
    with pytest.raises(Invalid) as caught:
        scene.rule(inv)
    assert "fixed" in refusal(caught)


def test_a_lock_cannot_change_the_recorded_rule(scene):
    inv = scene.investigation()
    first, second = scene.accuracy_job(inv, 0.7), scene.accuracy_job(inv, 0.9)
    scene.rule(inv)
    scene.lock(inv, first, selection=RULE)  # restating the rule is not a change
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, second, selection={**RULE, "direction": "lower_is_better"})
    assert "fixed" in refusal(caught)
    assert scene.locked_job(inv) == first


@pytest.mark.parametrize("bad", [
    pytest.param({"split": "test"}, id="test-split"), pytest.param({"split": "held-out"}, id="held-out-split"),
    pytest.param({"direction": "sideways"}, id="direction"), pytest.param({"metric": ""}, id="no-metric"),
    pytest.param({"max_rounds": -1}, id="negative-rounds"), pytest.param({"scorer": None}, id="no-scorer"),
    pytest.param({"scorer": " "}, id="blank-scorer"), pytest.param({"data": None}, id="no-data"),
    pytest.param({"data": {}}, id="empty-data"), pytest.param({"data": {"config": {}}}, id="empty-config-data"),
    pytest.param({"data": {"labels": "x"}}, id="unknown-data-key"),
    pytest.param({"reproduce_within": -0.1}, id="negative-tolerance")])
def test_an_invalid_selection_rule_is_refused_and_records_no_rule(scene, bad):
    inv = scene.investigation()
    with pytest.raises(Invalid):
        scene.rule(inv, **bad)
    with pytest.raises(Invalid):
        scene.lock(inv, scene.accuracy_job(inv, 0.7), selection={**RULE, **bad})
    assert scene.notes(inv) == []
    scene.rule(inv)  # nothing was fixed by the refusals
    assert len(scene.notes(inv, "selection_rule")) == 1


def test_a_rule_that_is_not_json_is_refused(scene):
    with pytest.raises(Invalid) as caught:
        scene.note(scene.investigation(), "selection_rule", "pick the most accurate one")
    assert "JSON" in refusal(caught)


# =====================================================================================================================
# 2. the first lock is incumbent version 1
# =====================================================================================================================
def test_the_first_lock_records_incumbent_version_1_with_evidence_from_the_jobs_receipt(scene):
    inv = scene.investigation()
    job = scene.accuracy_job(inv, 0.7)
    out = scene.lock(inv, job, selection=RULE)
    assert out["incumbent"] == {"version": 1, "basis": "first-lock", "evidence": {
        "metric": "accuracy", "split": "validation", "value": 0.7, "scored_by": job}}

    (note,) = scene.notes(inv, "candidate_lock")
    assert note["id"] == out["note_id"] and note["body"]["incumbent"]["job_id"] == job
    assert note["body"]["incumbent"]["rule"] == RULE and "previous" not in note["body"]["incumbent"]
    (event,) = scene.events(inv, "incumbent.changed")
    assert event["subject"] == out["note_id"]
    assert (event["body"]["version"], event["body"]["job_id"], event["body"]["basis"]) == (1, job, "first-lock")
    # the view the context is built from carries the incumbent on the pinned lock
    (pinned,) = [n for n in scene.view(inv)["pinned_notes"] if n["kind"] == "candidate_lock"]
    assert pinned["incumbent"]["version"] == 1 and pinned["incumbent"]["evidence"]["value"] == 0.7


def test_the_first_lock_reads_its_evidence_from_the_scorer_that_read_its_outputs(scene):
    inv = scene.investigation()
    job = scene.job(inv)  # the trial itself reports no metric
    scorer = scene.scored(inv, job, {"accuracy": 0.82})
    out = scene.lock(inv, job, selection=SCORER_RULE)
    assert out["incumbent"]["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.82, "scored_by": scorer}


def test_a_scorers_receipt_is_not_evidence_when_the_rule_names_another_scorer(scene):
    inv = scene.investigation()
    job = scene.job(inv)
    scene.scored(inv, job, {"accuracy": 0.82})  # the rule's scorer is the trial itself, which reported nothing
    assert scene.lock(inv, job, selection=RULE)["incumbent"]["evidence"] is None


def test_a_scorer_ref_with_a_release_matches_that_release_only(scene):
    inv = scene.investigation()
    job = scene.job(inv)
    scene.scored(inv, job, {"accuracy": 0.82})
    other_release = scene.scorer.split("@")[0] + "@9.9.9"
    assert "@" in scene.scorer and scene.lock(inv, job, selection={**RULE, "scorer": other_release})["incumbent"][
        "evidence"] is None


def test_evidence_is_never_the_number_the_model_typed(scene):
    inv = scene.investigation()
    job = scene.accuracy_job(inv, 0.7)
    out = scene.lock(inv, job, selection=RULE, accuracy=0.99, evidence={"value": 0.99})
    assert out["incumbent"]["evidence"]["value"] == 0.7


def test_a_first_lock_with_no_selection_rule_is_refused_and_records_nothing(scene):
    """R14: the rule comes first. A lock that names a job without any recorded rule (a prior note or its own
    ``selection``) is refused: there would be no way to say which result selected it."""
    inv = scene.investigation()
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, scene.accuracy_job(inv, 0.7))
    assert "selection rule" in refusal(caught) and "recorded first" in refusal(caught)
    assert scene.notes(inv) == [] and scene.events(inv, "incumbent.changed") == []
    assert scene.locked_job(inv) is None


def test_a_lock_that_carries_the_rule_records_it_and_the_first_lock_is_accepted(scene):
    inv = scene.investigation()
    job = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, job, selection=RULE)
    assert scene.locked_job(inv) == job
    assert len(scene.notes(inv, "candidate_lock")) == 1


def test_a_lock_that_names_no_job_is_recorded_as_before_with_no_incumbent(scene):  # even without a rule
    inv = scene.investigation()
    out = scene.note(inv, "candidate_lock", "the recipe with the deeper tree")
    assert "incumbent" not in out and scene.events(inv, "incumbent.changed") == []


# =====================================================================================================================
# 3 and 4. a challenger replaces the incumbent only by beating it on the rule
# =====================================================================================================================
DIRECTIONS = [
    pytest.param("higher_is_better", "accuracy", 0.80, 0.70, 0.90, id="higher-is-better"),
    pytest.param("lower_is_better", "loss", 0.40, 0.55, 0.25, id="lower-is-better")]


@pytest.mark.parametrize(("direction", "metric", "incumbent", "worse", "better"), DIRECTIONS)
def test_a_worse_challenger_is_refused_and_the_incumbent_stays(scene, direction, metric, incumbent, worse, better):
    inv = scene.investigation()
    rule = {**RULE, "metric": metric, "direction": direction}
    scene.rule(inv, metric=metric, direction=direction)
    a = scene.accuracy_job(inv, incumbent, metric=metric)
    scene.lock(inv, a)
    for value in (worse, incumbent):  # worse, and equal: a tie keeps the incumbent
        challenger = scene.accuracy_job(inv, value, metric=metric)
        with pytest.raises(Invalid) as caught:
            scene.lock(inv, challenger, selection=rule)  # restating the recorded rule is not a change
        assert "does not beat the incumbent" in refusal(caught)
        details = caught.value.details
        assert details["challenger"]["value"] == value and details["incumbent"]["job_id"] == a
        assert scene.locked_job(inv) == a
    assert len(scene.notes(inv, "candidate_lock")) == 1  # the refusals recorded nothing
    assert len(scene.events(inv, "incumbent.changed")) == 1


@pytest.mark.parametrize(("direction", "metric", "incumbent", "worse", "better"), DIRECTIONS)
def test_a_better_challenger_becomes_incumbent_version_2_with_the_previous_one_named(
        scene, direction, metric, incumbent, worse, better):
    inv = scene.investigation()
    scene.rule(inv, metric=metric, direction=direction)
    a = scene.accuracy_job(inv, incumbent, metric=metric)
    first = scene.lock(inv, a)
    c = scene.accuracy_job(inv, better, metric=metric)
    out = scene.lock(inv, c)  # the rule is already fixed: the lock need not restate it
    assert out["incumbent"]["version"] == 2 and out["incumbent"]["basis"] == "beats-incumbent"
    assert out["incumbent"]["evidence"] == {"metric": metric, "split": "validation", "value": better, "scored_by": c}
    assert scene.locked_job(inv) == c

    body = next(n for n in scene.notes(inv, "candidate_lock") if n["id"] == out["note_id"])["body"]["incumbent"]
    assert body["previous"]["job_id"] == a and body["previous"]["note_id"] == first["note_id"]
    assert body["previous"]["evidence"]["value"] == incumbent
    changed = scene.events(inv, "incumbent.changed")
    assert [e["body"]["version"] for e in changed] == [1, 2]
    assert changed[-1]["subject"] == out["note_id"] and changed[-1]["body"]["job_id"] == c
    assert changed[-1]["body"]["previous"]["job_id"] == a and changed[-1]["body"]["basis"] == "beats-incumbent"


def test_the_incumbent_is_the_latest_valid_lock_through_several_changes(scene):
    inv = scene.investigation()
    scene.rule(inv)
    a, b, c, d = (scene.accuracy_job(inv, v) for v in (0.60, 0.70, 0.65, 0.80))  # all admitted after the rule
    scene.lock(inv, a)
    assert scene.lock(inv, b)["incumbent"]["version"] == 2
    with pytest.raises(Invalid):  # c beats a but not the incumbent b
        scene.lock(inv, c)
    out = scene.lock(inv, d)
    assert out["incumbent"]["version"] == 3 and out["incumbent"]["evidence"]["value"] == 0.80
    assert scene.locked_job(inv) == d
    assert [e["body"]["job_id"] for e in scene.events(inv, "incumbent.changed")] == [a, b, d]


def test_relocking_the_incumbent_is_not_a_new_version_and_makes_no_change_event(scene):
    inv = scene.investigation()
    a = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a, selection=RULE)
    out = scene.lock(inv, a)
    assert out["incumbent"]["version"] == 1 and out["incumbent"]["basis"] == "same-incumbent"
    assert len(scene.events(inv, "incumbent.changed")) == 1


# =====================================================================================================================
# 5. a challenger with no recorded metric
# =====================================================================================================================
def test_a_challenger_with_no_recorded_metric_is_refused(scene):
    inv = scene.investigation()
    a = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a, selection=RULE)
    unscored = scene.job(inv)
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, unscored)
    assert "no recorded" in refusal(caught) and "accuracy" in refusal(caught) and "proj.trial" in refusal(caught)
    assert caught.value.details["job_id"] == unscored and caught.value.details["incumbent"]["job_id"] == a
    assert scene.locked_job(inv) == a


def test_a_challenger_that_reports_another_metric_or_failed_has_no_recorded_evidence(scene):
    inv = scene.investigation()
    a = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a, selection=RULE)
    for challenger in (scene.accuracy_job(inv, 0.95, metric="f1_something_else"),
                       scene.job(inv, metrics={"accuracy": 0.95}, status="failed")):
        with pytest.raises(Invalid) as caught:
            scene.lock(inv, challenger)
        assert "no recorded" in refusal(caught)
    assert scene.locked_job(inv) == a


def test_a_challenger_scored_by_a_job_that_read_its_outputs_has_recorded_evidence(scene):
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer")
    a = scene.job(inv)
    scene.scored(inv, a, {"accuracy": 0.7})
    scene.lock(inv, a)
    challenger = scene.job(inv)
    with pytest.raises(Invalid):
        scene.lock(inv, challenger)  # not scored yet
    scorer = scene.scored(inv, challenger, {"accuracy": 0.9})
    out = scene.lock(inv, challenger)
    assert out["incumbent"]["version"] == 2 and out["incumbent"]["evidence"]["scored_by"] == scorer


# =====================================================================================================================
# 6. the rule comes first: no lock without it, no re-lock under a rule chosen after the results (R14 #1)
# =====================================================================================================================
def _legacy_lock(scene, inv: str, job: str) -> str:
    """A lock recorded before R14: it names a job, carries no rule and no incumbent record."""
    nid = f"note_legacy{uuid.uuid4().hex[:6]}"
    scene.put("note", nid, {"investigation_id": inv, "kind": "candidate_lock", "text": json.dumps({"job_id": job}),
                            "status": "open"}, status="open", parent_id=inv)
    return nid


def test_without_a_selection_rule_the_first_lock_is_refused_so_nothing_is_there_to_relock(scene):
    """The old behaviour (a re-lock without a rule allowed and recorded as "unverified-no-selection-rule") is gone: a
    lock that names a job needs the rule recorded first, so a worse job cannot replace a better one by being named."""
    inv = scene.investigation()
    a, worse = scene.accuracy_job(inv, 0.9), scene.accuracy_job(inv, 0.5)
    for job in (a, worse):
        with pytest.raises(Invalid) as caught:
            scene.lock(inv, job)
        assert "selection rule" in refusal(caught) and "recorded first" in refusal(caught)
    assert scene.notes(inv) == [] and scene.locked_job(inv) is None and scene.events(inv, "incumbent.changed") == []


def test_a_relock_without_a_recorded_rule_is_refused_whatever_the_challenger_scored(scene):
    """A lock that predates the rule requirement (it names a job and records no rule) cannot be re-locked by a better
    challenger either: nothing was fixed before the results."""
    inv = scene.investigation()
    a, better = scene.accuracy_job(inv, 0.5), scene.accuracy_job(inv, 0.9)
    legacy = _legacy_lock(scene, inv, a)
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, better)
    assert "selection rule" in refusal(caught)
    assert [n["id"] for n in scene.notes(inv, "candidate_lock")] == [legacy] and scene.events(inv, "incumbent.changed") == []


def test_a_rule_given_first_with_a_relock_is_refused(scene):
    """The rule must exist before any lock. A rule that arrives with a re-lock was chosen when the results were visible,
    even if it would let the challenger win."""
    inv = scene.investigation()
    a, better = scene.accuracy_job(inv, 0.5), scene.accuracy_job(inv, 0.9)
    legacy = _legacy_lock(scene, inv, a)
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, better, selection=RULE)
    assert "before the challenger round" in refusal(caught) and caught.value.details["incumbent"] == a
    assert [n["id"] for n in scene.notes(inv, "candidate_lock")] == [legacy]
    assert scene.notes(inv, "selection_rule") == [] and scene.events(inv, "incumbent.changed") == []


def test_a_rule_recorded_after_a_lock_that_names_no_job_binds_every_later_change(scene):
    """A lock that names no job fixes nothing; a rule recorded after it comes before any lock that names a job, so it
    binds every later change (the old test recorded the rule after unverified locks)."""
    inv = scene.investigation()
    scene.note(inv, "candidate_lock", "the recipe with the deeper tree")
    scene.rule(inv)
    a, b = scene.accuracy_job(inv, 0.6), scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a)
    out = scene.lock(inv, b)
    assert out["incumbent"]["basis"] == "beats-incumbent"
    body = next(n for n in scene.notes(inv, "candidate_lock") if n["id"] == out["note_id"])["body"]["incumbent"]
    assert body["previous"]["job_id"] == a and body["previous"]["evidence"]["value"] == 0.6 and body["rule"] == RULE
    with pytest.raises(Invalid):
        scene.lock(inv, scene.accuracy_job(inv, 0.65))


# =====================================================================================================================
# 7. the selection freeze
# =====================================================================================================================
def _plan(scene, inv: str, status: str, *, for_inv: str | None = None) -> None:
    scene.put("evidence_plan", f"plan_{uuid.uuid4().hex[:8]}", {"investigation_id": for_inv or inv}, status=status)


def _request(scene, inv: str, status: str) -> None:
    scene.put("assessment_request", f"req_{uuid.uuid4().hex[:8]}", {"investigation_id": inv}, status=status, parent_id=inv)


FREEZING = [pytest.param(_plan, "frozen", id="plan-frozen"), pytest.param(_plan, "executing", id="plan-executing"),
            pytest.param(_plan, "awaiting-assessment", id="plan-awaiting-assessment"),
            pytest.param(_request, "launched", id="assessment-launched")]
NOT_FREEZING = [pytest.param(_plan, "draft", id="plan-draft"), pytest.param(_plan, "cancelled", id="plan-cancelled"),
                pytest.param(_request, "refused", id="assessment-refused")]


@pytest.mark.parametrize(("write", "status"), FREEZING)
def test_after_a_plan_is_frozen_or_an_assessment_requested_no_lock_names_another_job(scene, write, status):
    inv = scene.investigation()
    a, b = scene.accuracy_job(inv, 0.7), scene.accuracy_job(inv, 0.9)  # b would even beat the incumbent
    scene.lock(inv, a, selection=RULE)
    write(scene, inv, status)
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, b)
    assert "frozen" in refusal(caught) and caught.value.details["job_id"] == b
    assert scene.locked_job(inv) == a and len(scene.events(inv, "incumbent.changed")) == 1


def test_the_freeze_holds_with_the_rule_recorded_as_a_note_too(scene):
    inv = scene.investigation()
    scene.rule(inv)
    a, b = scene.accuracy_job(inv, 0.7), scene.accuracy_job(inv, 0.9)
    scene.lock(inv, a)
    _plan(scene, inv, "frozen")
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, b)
    assert "frozen" in refusal(caught)


@pytest.mark.parametrize(("write", "status"), NOT_FREEZING)
def test_a_draft_or_cancelled_plan_or_a_refused_request_does_not_freeze_the_selection(scene, write, status):
    inv = scene.investigation()
    a = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a, selection=RULE)
    b = scene.accuracy_job(inv, 0.9)  # a challenger of a round after the rule
    write(scene, inv, status)
    assert scene.lock(inv, b)["incumbent"]["version"] == 2


def test_another_investigations_frozen_plan_does_not_freeze_this_one(scene):
    inv, other = scene.investigation(), scene.investigation()
    a = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a, selection=RULE)
    b = scene.accuracy_job(inv, 0.9)
    _plan(scene, other, "frozen")
    assert scene.lock(inv, b)["incumbent"]["version"] == 2


def test_relocking_the_incumbent_itself_after_the_freeze_changes_nothing_and_is_not_refused(scene):
    inv = scene.investigation()
    a = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, a, selection=RULE)
    _plan(scene, inv, "frozen")
    out = scene.lock(inv, a)
    assert out["incumbent"]["version"] == 1 and out["incumbent"]["basis"] == "same-incumbent"


def test_a_launched_assess_candidate_freezes_the_selection(lab, tmp_path):
    """The real route: a locked candidate is assessed (a plan is frozen and launched); a lock naming another job is then
    refused, and the incumbent the assessment is about stays."""
    flow = AssessScene(lab, tmp_path)
    baseline, candidate = flow.job(BASELINE_RECIPE), flow.job(CANDIDATE_RECIPE)
    inv = flow.investigation()
    flow.cmd("record_note", {"investigation_id": inv, "kind": "candidate_lock",
                             "text": json.dumps({"job_id": candidate, "selection": RULE})})
    receipt = flow.assess_candidate(flow.request(baseline, candidate, investigation_id=inv))
    assert receipt["status"] == "launched", receipt
    with pytest.raises(Invalid) as caught:  # through the command, as the lab checks it (flow.lock writes the note directly)
        flow.cmd("record_note", {"investigation_id": inv, "kind": "candidate_lock", "text": json.dumps({"job_id": baseline})})
    assert "frozen" in caught.value.message
    latest = [n for n in flow.q("investigation_view", {"investigation_id": inv, "horizon": flow.q("project_summary")["horizon"]}
                                )["pinned_notes"] if n["kind"] == "candidate_lock"][-1]
    assert candidate_lock_job_id(latest) == candidate


# =====================================================================================================================
# 8. baseline identity
# =====================================================================================================================
def _baseline(scene, inv: str) -> dict:
    return scene.q("get_record", {"kind": "investigation_baseline", "id": inv})


def test_the_first_succeeded_project_command_with_no_source_is_recorded_as_the_baseline(scene):
    inv = scene.investigation()
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    source = _source_artifact(scene)
    # none of these is the baseline: an authored source, another component, a failure, a job of no investigation
    scene.job(inv, ref=scene.command, inputs={"source": source}, metrics={"accuracy": 0.1})
    scene.job(inv, metrics={"accuracy": 0.2})
    scene.job(inv, ref=scene.command, status="failed")
    scene.job(None, ref=scene.command)
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    assert scene.events(inv, "baseline.recorded") == []

    job = scene.job(inv, ref=scene.command, metrics={"accuracy": 0.6})
    rec = _baseline(scene, inv)
    assert rec["status"] == "recorded" and rec["parent_id"] == inv
    assert rec["body"] == {"investigation_id": inv, "job_id": job, "component": scene.command, "environment": "lab",
                           "purpose": "open-search", "metrics": {"accuracy": 0.6}, "basis": "automatic",
                           "scored_by": None}
    (event,) = scene.events(inv, "baseline.recorded")
    assert event["subject"] == job and event["body"] == {"job_id": job}


def _source_artifact(scene) -> str:
    digest, size = scene.lab.store.put_bytes(b'{"files": {}}')
    return scene.cmd("register_artifact", {"digest": digest, "size": size, "schema": "source-bundle/1",
                                           "media_type": "application/json"})["artifact_id"]


def test_a_reproduction_job_is_not_the_baseline_whatever_its_purpose(scene):
    """R14 #7: purpose "reproduction" also reproduces candidates, so it never makes a job the baseline; only the
    project's own command (no authored source) or a job a "baseline" note names does."""
    inv = scene.investigation()
    scene.job(inv, purpose="hypothesis-test", metrics={"accuracy": 0.5})
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    scene.job(inv, purpose="reproduction", metrics={"accuracy": 0.6})
    scene.job(inv, ref=scene.command, purpose="reproduction", inputs={"source": _source_artifact(scene)})
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    assert scene.events(inv, "baseline.recorded") == []
    # the project's own command is the baseline even when its job's purpose is reproduction
    base = scene.job(inv, ref=scene.command, purpose="reproduction", metrics={"accuracy": 0.6})
    rec = _baseline(scene, inv)
    assert rec["body"]["job_id"] == base and rec["body"]["basis"] == "automatic"


def test_a_job_with_metrics_that_read_the_baseline_outputs_is_recorded_as_its_scorer(scene):
    inv = scene.investigation()
    base = scene.job(inv, ref=scene.command)  # reports no metrics itself
    assert _baseline(scene, inv)["body"]["metrics"] is None
    scene.job(inv, metrics={"accuracy": 0.9})  # metrics, but it never read the baseline's outputs
    assert _baseline(scene, inv)["status"] == "recorded"
    scorer = scene.scored(inv, base, {"accuracy": 0.61})
    rec = _baseline(scene, inv)
    assert rec["status"] == "scored" and rec["body"]["job_id"] == base
    assert rec["body"]["scored_by"] == {"job_id": scorer, "metrics": {"accuracy": 0.61}}
    (event,) = scene.events(inv, "baseline.scored")
    assert event["subject"] == scorer and event["body"] == {"baseline_job_id": base, "job_id": scorer}
    # a later scorer does not replace the first
    scene.scored(inv, base, {"accuracy": 0.99})
    assert _baseline(scene, inv)["body"]["scored_by"]["job_id"] == scorer
    assert len(scene.events(inv, "baseline.scored")) == 1


def test_a_scorer_that_reads_the_baseline_through_another_job_scores_it(scene):
    inv = scene.investigation()
    base = scene.job(inv, ref=scene.command)
    middle = scene.job(inv, ref=scene.infer, inputs={"predictions": scene.output(base)})  # reads it, no metrics
    assert _baseline(scene, inv)["status"] == "recorded"
    final = scene.scored(inv, middle, {"accuracy": 0.7})  # reads what the middle job produced
    assert _baseline(scene, inv)["body"]["scored_by"]["job_id"] == final


def test_a_later_baseline_looking_job_does_not_change_the_recorded_baseline(scene):
    inv = scene.investigation()
    base = scene.job(inv, ref=scene.command, metrics={"accuracy": 0.6})
    before = _baseline(scene, inv)
    scene.job(inv, ref=scene.command, metrics={"accuracy": 0.8})
    scene.job(inv, purpose="reproduction", metrics={"accuracy": 0.7})
    after = _baseline(scene, inv)
    assert after["body"]["job_id"] == base and after["revision"] == before["revision"] and after["body"] == before["body"]
    assert len(scene.events(inv, "baseline.recorded")) == 1


def test_each_investigation_has_its_own_baseline(scene):
    one, two = scene.investigation(), scene.investigation()
    a = scene.job(one, ref=scene.command)
    b = scene.job(two, ref=scene.command)
    assert _baseline(scene, one)["body"]["job_id"] == a and _baseline(scene, two)["body"]["job_id"] == b


def test_the_view_and_the_packed_context_state_the_recorded_baseline(scene):
    inv = scene.investigation()
    before = pack(scene.view(inv), [])["text"]
    assert scene.view(inv)["baseline_record"] is None and "Baseline: none recorded yet" in before

    # recent noise that the old guess would have picked up: a later reproduction-purpose job is not the baseline
    base = scene.job(inv, ref=scene.command, metrics={"accuracy": 0.6})
    scene.job(inv, purpose="reproduction", metrics={"accuracy": 0.99})
    view = scene.view(inv)
    assert view["baseline_record"]["job_id"] == base and view["baseline_record"]["scored_by"] is None
    text = pack(view, [])["text"]
    assert f"Baseline (recorded): job {base}" in text and "Baseline: none recorded yet" not in text
    assert "not scored yet" in text

    scorer = scene.scored(inv, base, {"accuracy": 0.61})
    view = scene.view(inv)
    assert view["baseline_record"]["scored_by"]["job_id"] == scorer
    text = pack(view, [])["text"]
    assert f"Baseline (recorded): job {base}" in text and f"scored by {scorer}" in text
    assert "Baseline: none recorded yet" not in text


def test_an_explicit_baseline_note_overrides_the_recorded_baseline_in_the_context(scene):
    inv = scene.investigation()
    scene.job(inv, ref=scene.command, metrics={"accuracy": 0.6})
    other = scene.job(inv, metrics={"accuracy": 0.5})
    scene.note(inv, "baseline", f"the baseline is {other}")
    text = pack(scene.view(inv), [])["text"]
    assert f"Baseline: job {other}" in text and "Baseline (recorded)" not in text


# =====================================================================================================================
# 9. the exploration state in the turn spec, and the finalize phase
# =====================================================================================================================
def test_begin_turn_records_the_baseline_phase_before_any_lock_and_the_explore_phase_after_one(scene):
    inv = scene.investigation()
    first = scene.begin(inv)
    state = first["spec"]["exploration"]
    assert state["phase"] == "baseline" and state["stop"] is None and state["incumbent"] is None
    assert state["versions"] == 0 and state["rounds"] == 0 and state["rule"] is None
    assert state["policy"] == "exploration-policy/2" and state["patience_rounds"] == 3
    assert scene.q("get_turn", {"turn_id": first["turn_id"]})["spec"]["exploration"] == state  # persisted with the turn
    scene.finish_turn(first["turn_id"])

    job = scene.accuracy_job(inv, 0.7)
    lock = scene.lock(inv, job, selection=RULE)
    second = scene.begin(inv)
    state = second["spec"]["exploration"]
    assert state["phase"] == "explore" and state["stop"] is None and state["versions"] == 1
    assert state["rule"] == RULE and state["rounds"] == 0 and state["rounds_without_improvement"] == 0
    assert state["incumbent"]["job_id"] == job and state["incumbent"]["version"] == 1
    assert state["incumbent"]["note_id"] == lock["note_id"] and state["incumbent"]["basis"] == "first-lock"
    assert state["incumbent"]["evidence"]["value"] == 0.7 and state["incumbent"]["reproduced_by"] is None
    # the first turn's recorded state is immutable
    assert scene.q("get_turn", {"turn_id": first["turn_id"]})["spec"]["exploration"]["phase"] == "baseline"


def test_a_resumed_turn_keeps_the_exploration_state_it_began_with(scene):
    inv = scene.investigation()
    first = scene.begin(inv)
    scene.lock(inv, scene.accuracy_job(inv, 0.7), selection=RULE)  # recorded while the turn is open
    resumed = _begin_turn(scene.lab, scene.pid, inv, RUN_A)
    assert resumed["resumed"] is True and resumed["spec"]["exploration"] == first["spec"]["exploration"]
    assert resumed["spec"]["exploration"]["phase"] == "baseline"


def _calls(ref: str) -> list[dict]:
    return [{"tool": "run_component", "args": {"component_ref": ref, "environment": "lab"}}]


STOPS = [
    pytest.param("search-limit", {"exploration": {"patience_rounds": 0, "max_rounds": 0}}, {}, 0.7, id="config-search-limit"),
    pytest.param("search-limit", {}, {"max_rounds": 0}, 0.7, id="rule-search-limit"),
    pytest.param("ceiling-perfect-score", {}, {}, 1.0, id="perfect-score")]


@pytest.mark.parametrize(("stop", "config", "rule", "value"), STOPS)
def test_when_exploration_has_stopped_a_challenger_decision_is_refused_and_reproduction_is_accepted(
        scene, stop, config, rule, value):
    inv = scene.investigation(**config)
    job = scene.accuracy_job(inv, value)
    scene.lock(inv, job, selection={**RULE, **rule})
    turn = scene.begin(inv)
    state = turn["spec"]["exploration"]
    assert state["phase"] == "finalize" and state["stop"] == stop and state["incumbent"]["job_id"] == job
    tid = turn["turn_id"]

    refused = scene.propose(tid, 0, {"rationale": "r", "purpose": "open-search", "calls": _calls(scene.trial)})
    assert refused["accepted"] is False and refused["reason"] == "invalid"
    assert "exploration has stopped" in refused["error"]["message"] and stop in refused["error"]["message"]
    assert refused["error"]["details"]["stop"] == stop and refused["error"]["details"]["calls"] == [0]
    assert refused["error"]["details"]["incumbent"]["job_id"] == job

    for slot, purpose in enumerate(("hypothesis-test", None), start=1):  # a missing purpose is open-search
        body = {"rationale": "r", "calls": _calls(scene.trial), **({"purpose": purpose} if purpose else {})}
        again = scene.propose(tid, slot, body)
        assert again["accepted"] is False and again["reason"] == "invalid", again
        assert "exploration has stopped" in again["error"]["message"]

    accepted = scene.propose(tid, 3, {"rationale": "r", "purpose": "reproduction", "calls": _calls(scene.trial)})
    assert accepted["accepted"] is True and [c["tool"] for c in accepted["calls"]] == ["run_component"]
    assert [d["accepted"] for d in scene.q("get_turn", {"turn_id": tid})["decisions"]].count(True) == 1


def test_finalize_refuses_each_job_starting_tool_but_not_notes_or_waiting(scene):
    inv = scene.investigation(exploration={"patience_rounds": 0, "max_rounds": 0})
    scene.lock(inv, scene.accuracy_job(inv, 0.7), selection=RULE)
    turn = scene.begin(inv)
    tid = turn["turn_id"]
    assert turn["spec"]["exploration"]["phase"] == "finalize"
    note = {"tool": "record_note", "args": {"kind": "hypothesis", "text": "what to try next"}}
    ok = scene.propose(tid, 0, {"rationale": "r", "purpose": "open-search", "calls": [note]})
    assert ok["accepted"] is True  # nothing that starts compute
    mixed = scene.propose(tid, 1, {"rationale": "r", "purpose": "open-search", "calls": [note, *_calls(scene.trial)]})
    assert mixed["accepted"] is False and mixed["error"]["details"]["calls"] == [1]  # names the offending call


def test_a_conclusion_is_still_accepted_when_exploration_has_stopped(scene):
    inv = scene.investigation(exploration={"patience_rounds": 0, "max_rounds": 0})
    scene.lock(inv, scene.accuracy_job(inv, 0.7), selection=RULE)
    tid = scene.begin(inv)["turn_id"]
    out = scene.propose(tid, 0, {"rationale": "r", "purpose": "open-search", "calls": [],
                                 "control": {"action": "conclude", "summary": "the incumbent is the result"}})
    assert out["accepted"] is True


def test_while_exploration_is_open_a_challenger_decision_is_accepted(scene):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.7), selection=RULE)
    turn = scene.begin(inv)
    assert turn["spec"]["exploration"]["phase"] == "explore"
    out = scene.propose(turn["turn_id"], 0, {"rationale": "r", "purpose": "open-search", "calls": _calls(scene.trial)})
    assert out["accepted"] is True


def test_the_search_limit_does_not_apply_before_there_is_an_incumbent(scene):
    inv = scene.investigation(exploration={"patience_rounds": 0, "max_rounds": 0})
    turn = scene.begin(inv)  # no lock yet: the limit applies to rounds after the first lock
    assert turn["spec"]["exploration"]["phase"] == "baseline"
    out = scene.propose(turn["turn_id"], 0, {"rationale": "r", "purpose": "open-search", "calls": _calls(scene.trial)})
    assert out["accepted"] is True


def test_a_frozen_selection_ends_exploration(scene):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.7), selection=RULE)
    _plan(scene, inv, "frozen")
    state = scene.begin(inv)["spec"]["exploration"]
    assert state["phase"] == "finalize" and state["stop"] == "selection-frozen"


def test_the_packed_context_of_a_finalizing_investigation_says_exploration_has_stopped(scene):
    inv = scene.investigation(exploration={"patience_rounds": 0, "max_rounds": 0})
    job = scene.accuracy_job(inv, 0.7)
    scene.lock(inv, job, selection=RULE)
    state = scene.begin(inv)["spec"]["exploration"]
    text = pack({**scene.view(inv), "exploration": state}, [])["text"]
    assert "Exploration: STOPPED" in text and "declared search limit" in text
    assert f"Incumbent v1: job {job}" in text and "Selection rule:" in text


def _recipe(depth: int) -> dict:
    return {"depth": depth}


def test_challenger_rounds_are_counted_from_the_turns_that_admitted_them_and_patience_ends_exploration(scene):
    inv = scene.investigation(exploration={"patience_rounds": 2})
    held = {}

    def first():
        held["a"] = scene.job(inv, metrics={"accuracy": 0.70}, config=_recipe(1))
        held["lock"] = scene.lock(inv, held["a"], selection=RULE)["note_id"]
        return [held["a"]], [held["lock"]]

    assert scene.round(inv, first)["phase"] == "baseline"  # turn 1 found and locked the first candidate
    state = scene.round(inv, lambda: ([scene.job(inv, metrics={"accuracy": 0.60}, config=_recipe(2))], []))
    assert (state["phase"], state["rounds"], state["rounds_without_improvement"]) == ("explore", 0, 0)
    state = scene.round(inv, lambda: ([scene.job(inv, metrics={"accuracy": 0.65}, config=_recipe(3))], []))
    assert (state["phase"], state["rounds"], state["rounds_without_improvement"]) == ("explore", 1, 1)
    final = scene.begin(inv)["spec"]["exploration"]
    assert (final["rounds"], final["rounds_without_improvement"]) == (2, 2)
    assert final["phase"] == "finalize" and final["stop"] == "ceiling-no-improvement" and final["patience_rounds"] == 2


def test_a_new_incumbent_restarts_the_count_of_rounds_without_improvement(scene):
    inv = scene.investigation(exploration={"patience_rounds": 2})

    def lock(depth: int, value: float):
        def work():
            job = scene.job(inv, metrics={"accuracy": value}, config=_recipe(depth))
            return [job], [scene.lock(inv, job, selection=RULE)["note_id"]]
        return work

    def challenger(depth: int, value: float):
        return lambda: ([scene.job(inv, metrics={"accuracy": value}, config=_recipe(depth))], [])

    scene.round(inv, lock(1, 0.70))
    scene.round(inv, challenger(2, 0.60))
    state = scene.round(inv, lock(3, 0.90))  # a challenger that beats the incumbent and is locked in this very turn
    assert (state["rounds"], state["rounds_without_improvement"]) == (1, 1)
    state = scene.round(inv, challenger(4, 0.80))
    assert (state["phase"], state["rounds"], state["rounds_without_improvement"], state["versions"]) == ("explore", 2, 0, 2)
    state = scene.round(inv, challenger(5, 0.85))
    assert (state["phase"], state["rounds"], state["rounds_without_improvement"]) == ("explore", 3, 1)
    final = scene.begin(inv)["spec"]["exploration"]
    assert (final["rounds"], final["rounds_without_improvement"]) == (4, 2)
    assert final["phase"] == "finalize" and final["stop"] == "ceiling-no-improvement"
    assert final["incumbent"]["version"] == 2 and final["incumbent"]["evidence"]["value"] == 0.90


def test_reproducing_the_incumbent_is_not_a_challenger_round(scene):
    inv = scene.investigation(exploration={"patience_rounds": 1})
    held = {}

    def first():
        held["a"] = scene.job(inv, metrics={"accuracy": 0.70}, config=_recipe(1))
        return [held["a"]], [scene.lock(inv, held["a"], selection=RULE)["note_id"]]

    scene.round(inv, first)

    def repro():  # the incumbent's recipe under another seed: finalization work, whatever purpose the model declared
        held["r"] = scene.job(inv, metrics={"accuracy": 0.69}, config={**_recipe(1), "seed": 2})
        return [held["r"]], []

    scene.round(inv, repro)
    state = scene.begin(inv)["spec"]["exploration"]
    assert state["rounds"] == 0 and state["phase"] == "explore"  # patience is 1, and no challenger round happened
    assert state["incumbent"]["reproduced_by"] == held["r"]


# =====================================================================================================================
# 10. the provider is unavailable
# =====================================================================================================================
def test_a_provider_unavailable_mark_is_accepted_and_visible_on_the_investigation(scene):
    inv = scene.investigation()
    out = scene.lab.cmd(scene.wf, "mark_needs_input", {
        "investigation_id": inv, "code": "provider-unavailable", "reason": "the model provider is out of quota",
        "details": {"failure": {"error_type": "QuotaExhausted", "retryable": False}}}, project=scene.pid)["result"]
    assert out["already"] is False and out["needs_input"]["code"] == "provider-unavailable"
    shown = scene.q("get_investigation", {"investigation_id": inv})
    mark = shown["labels"]["needs_input"]
    assert mark["code"] == "provider-unavailable" and mark["reason"] == "the model provider is out of quota"
    assert mark["details"]["failure"]["error_type"] == "QuotaExhausted" and mark["cursor"] == out["cursor"]
    assert shown["needs_input"] == mark
    (event,) = scene.events(inv, "investigation.needs_input")
    assert event["body"]["code"] == "provider-unavailable"


def test_a_provider_unavailable_mark_closes_the_turn_the_failed_child_left_open(scene):
    inv = scene.investigation()
    tid = scene.begin(inv)["turn_id"]
    scene.lab.cmd(scene.wf, "mark_needs_input", {"investigation_id": inv, "code": "provider-unavailable", "reason": "r"},
                  project=scene.pid)
    turn = scene.q("get_turn", {"turn_id": tid})
    assert turn["status"] != "open" and turn["result"]["stop_reason"] == "error"


def test_an_unknown_needs_input_code_is_still_refused(scene):
    inv = scene.investigation()
    with pytest.raises(Invalid):
        scene.lab.cmd(scene.wf, "mark_needs_input", {"investigation_id": inv, "code": "provider-on-fire"}, project=scene.pid)
    assert scene.q("get_investigation", {"investigation_id": inv})["needs_input"] is None

