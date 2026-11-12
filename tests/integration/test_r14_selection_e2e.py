"""R14, selection and exploration semantics, end to end against PostgreSQL (no model, no network).

One group of tests per review finding, through the real command service (the lab's ``record_note``, ``begin_turn``,
``accept_decision``, ``reserve_model_budget`` and the launcher's job commands), on the Scene of
``test_incumbent_challengers``:

* #1 the selection rule is recorded before the challenger round: a re-lock needs recorded evidence for both the
  incumbent and the challenger, the challenger admitted after the rule, and a strict improvement;
* #2 evidence has provenance: the rule names its scorer and its selection data, a lock cannot nominate another scorer,
  a receipt from a scorer run on other data is no evidence, and attested held-out data is never selection data;
* #3 the incumbent's reproduction is judged against a prospective criterion (same recipe, another seed, scored within
  the tolerance) and the conclusion records the result;
* #4 an exploratory turn ends where the hold-back begins (wall and model tokens), at the turn's start and live;
* #6 a challenger round counts as unsuccessful only once its challengers are terminal and scored, and scoring a
  pending challenger is still accepted when exploration has stopped;
* #7 the baseline is the project's own command or a job a ``baseline`` note names, never a reproduction job.

(#5, the retry bounds, is covered by ``tests/unit/test_r14_retry_bounds.py``.)
"""

from __future__ import annotations

import json
import time

import pytest
from test_command_path import _publish
from test_f5_turn_authorization import RUN_A
from test_f5c_ticket_and_handoff import _begin_decision
from test_f5d_conclude_race import _complete as _complete_turn
from test_incumbent_challengers import (
    CODE,
    RULE,
    SCORER,
    Scene,
    _baseline,
    _legacy_lock,
    _source_artifact,
    refusal,
)

from eidos.domain.errors import BudgetExhausted, Invalid, NotFound
from eidos.domain.turns import turn_boundary

pytestmark = pytest.mark.pg

TOKEN_LIMITS = {"model_tokens": 10_000, "compute_seconds": 1e6, "trials": 1000}  # hold-back 15%: 1500; 8500 to explore
LABELLED_SCORER = {**SCORER, "id": "proj.lscorer", "input": {"predictions": "class-predictions/1",
                                                              "labels": "class-labels/1"}}


@pytest.fixture()
def scene(lab):
    return Scene(lab)


def _calls(ref: str, **args) -> list[dict]:
    return [{"tool": "run_component", "args": {"component_ref": ref, "environment": "lab", **args}}]


def _challenger(scene: Scene) -> dict:
    return {"rationale": "r", "purpose": "open-search", "calls": _calls(scene.trial)}


def _first_lock_round(scene: Scene, inv: str, value: float, *, via_scorer: bool = False) -> str:
    """The turn that finds and locks the first candidate (the rule is already recorded): returns its job."""
    held: dict[str, str] = {}

    def work():
        if via_scorer:
            held["job"] = scene.job(inv, config={"depth": 1})
            scene.scored(inv, held["job"], {"accuracy": value})
        else:
            held["job"] = scene.job(inv, metrics={"accuracy": value}, config={"depth": 1})
        return [held["job"]], [scene.lock(inv, held["job"])["note_id"]]
    scene.round(inv, work)
    return held["job"]


def _round(scene: Scene, inv: str, depth: int, **job) -> str:
    """A turn whose accepted decision admitted one challenger of its own recipe (``job``: the job's arguments)."""
    held: dict[str, str] = {}

    def work():
        held["job"] = scene.job(inv, config={"depth": depth}, **job)
        return [held["job"]], []
    scene.round(inv, work)
    return held["job"]


def _exploration(scene: Scene, inv: str) -> dict:
    """The exploration state a turn beginning now records (the turn is closed again)."""
    turn = scene.begin(inv)
    scene.finish_turn(turn["turn_id"])
    return turn["spec"]["exploration"]


def _reproduction_note(scene: Scene, inv: str, note_id: str) -> dict:
    notes = scene.notes(inv, "candidate_lock")
    return next(n for n in notes if n["id"] == note_id)["body"]["incumbent"]["reproduction"]


def _conclusion(scene: Scene, inv: str) -> dict:
    """Conclude the investigation through a turn (the way a model's conclude control does) and return the conclusion
    its record keeps."""
    control = {"action": "conclude", "summary": "the incumbent is the result"}
    turn = scene.begin(inv)
    out = scene.propose(turn["turn_id"], 0, {"rationale": "r", "purpose": "open-search", "calls": [], "control": control})
    assert out["accepted"] is True, out
    _complete_turn(scene.lab, scene.pid, turn["turn_id"], "concluded", control)
    body = scene.q("get_investigation", {"investigation_id": inv})
    assert body["status"] == "concluded", body["status"]
    return body["body"]["conclusion"]


# =====================================================================================================================
# #1 the rule is recorded before the challenger round
# =====================================================================================================================
def test_a_worse_unscored_challenger_cannot_replace_a_scored_incumbent_and_a_worse_scored_one_does_not_beat_it(scene):
    """The reviewer's case: naming a challenger in a lock must not move the incumbent unless the recorded evidence says
    it is better. Neither a challenger that reported nothing nor one that scored worse replaces a scored incumbent."""
    inv = scene.investigation()
    scene.rule(inv)
    best = scene.accuracy_job(inv, 0.90)
    scene.lock(inv, best)
    unscored, worse = scene.job(inv), scene.accuracy_job(inv, 0.50)
    for challenger, message in ((unscored, "no recorded"), (worse, "does not beat the incumbent")):
        with pytest.raises(Invalid) as caught:
            scene.lock(inv, challenger)
        assert message in refusal(caught)
    assert scene.locked_job(inv) == best
    assert len(scene.notes(inv, "candidate_lock")) == 1 and len(scene.events(inv, "incumbent.changed")) == 1


def test_a_relock_while_the_incumbent_is_unscored_is_refused_until_the_incumbent_is_scored(scene):
    """The old "incumbent-unscored" basis let a re-lock through when the incumbent had no evidence. Now the incumbent
    must be scored before any challenger can replace it, whatever the challenger scored."""
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer")
    incumbent = scene.job(inv)  # the first lock is recorded even though nothing scored it yet
    first = scene.lock(inv, incumbent)
    assert first["incumbent"]["version"] == 1 and first["incumbent"]["evidence"] is None
    challenger = scene.job(inv)
    scene.scored(inv, challenger, {"accuracy": 0.99})
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, challenger)
    assert "incumbent" in refusal(caught) and "score it before any challenger can replace it" in refusal(caught)
    details = caught.value.details
    assert details["incumbent"]["job_id"] == incumbent and details["incumbent"]["evidence"] is None
    assert scene.locked_job(inv) == incumbent and len(scene.events(inv, "incumbent.changed")) == 1

    scene.scored(inv, incumbent, {"accuracy": 0.60})  # now both are scored under the rule: 0.99 beats 0.60
    out = scene.lock(inv, challenger)
    assert out["incumbent"]["version"] == 2 and out["incumbent"]["basis"] == "beats-incumbent"


@pytest.mark.parametrize("rule_in_the_lock", [pytest.param(False, id="rule-note"), pytest.param(True, id="rule-in-lock")])
def test_a_challenger_admitted_before_the_rule_was_recorded_is_refused_even_when_it_is_better(scene, rule_in_the_lock):
    inv = scene.investigation()
    first, early = scene.accuracy_job(inv, 0.60), scene.accuracy_job(inv, 0.95)  # both admitted before the rule
    if rule_in_the_lock:
        scene.lock(inv, first, selection=RULE)
    else:
        scene.rule(inv)
        scene.lock(inv, first)
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, early)
    assert "admitted before the selection rule was recorded" in refusal(caught)
    assert caught.value.details["job_id"] == early and scene.locked_job(inv) == first
    late = scene.accuracy_job(inv, 0.95)  # the same result from a round after the rule
    assert scene.lock(inv, late)["incumbent"]["version"] == 2


def test_a_rule_first_given_with_a_relock_is_refused_even_when_it_favours_the_challenger(scene):
    """A lock that predates the rule requirement names an incumbent with no recorded rule. A rule that first arrives
    with the re-lock (in the lock or as a note after it) was chosen after the results were visible."""
    inv = scene.investigation()
    incumbent, better = scene.accuracy_job(inv, 0.50), scene.accuracy_job(inv, 0.90)
    legacy = _legacy_lock(scene, inv, incumbent)
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, better, selection=RULE)
    assert "before the challenger round" in refusal(caught) and caught.value.details["incumbent"] == incumbent
    with pytest.raises(Invalid):
        scene.lock(inv, better)  # and with no rule at all
    assert [n["id"] for n in scene.notes(inv, "candidate_lock")] == [legacy]
    assert scene.notes(inv, "selection_rule") == [] and scene.events(inv, "incumbent.changed") == []


# =====================================================================================================================
# #2 evidence has provenance
# =====================================================================================================================
def test_a_lock_cannot_nominate_a_scorer_other_than_the_one_the_rule_fixed(scene):
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer")
    job = scene.job(inv)
    honest = scene.scored(inv, job, {"accuracy": 0.60})
    flattering = scene.scored(inv, job, {"accuracy": 0.99})  # a second run of the same scorer, read after the first
    for nominated in (flattering, "job_made_up", job):
        with pytest.raises(Invalid) as caught:
            scene.lock(inv, job, scored_by=nominated)
        assert "cannot nominate its scorer" in refusal(caught)
        assert caught.value.details["evidence"]["scored_by"] == honest
    assert scene.notes(inv, "candidate_lock") == []

    out = scene.lock(inv, job, scored_by=honest)  # naming the rule's own evidence is not a nomination
    assert out["incumbent"]["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.60, "scored_by": honest}


def test_a_challenger_nominating_a_scorer_is_judged_by_the_rules_evidence_not_the_nominated_one(scene):
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer")
    incumbent = scene.job(inv)
    scene.scored(inv, incumbent, {"accuracy": 0.70})
    scene.lock(inv, incumbent)
    challenger = scene.job(inv)
    honest = scene.scored(inv, challenger, {"accuracy": 0.60})
    flattering = scene.scored(inv, challenger, {"accuracy": 0.95})
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, challenger, scored_by=flattering)
    assert "cannot nominate its scorer" in refusal(caught)
    with pytest.raises(Invalid) as caught:  # and without a nomination the first scorer's 0.60 is the evidence
        scene.lock(inv, challenger)
    assert "does not beat the incumbent" in refusal(caught) and caught.value.details["challenger"]["scored_by"] == honest


def test_a_receipt_from_a_scorer_run_on_another_split_is_not_evidence(scene):
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer")  # data: config split == validation
    incumbent = scene.job(inv)
    scene.scored(inv, incumbent, {"accuracy": 0.60})
    scene.lock(inv, incumbent)
    challenger = scene.job(inv)
    scene.scored(inv, challenger, {"accuracy": 0.95}, config={"split": "test"})  # scored on other data
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, challenger)
    assert "no recorded" in refusal(caught) and caught.value.details["rule"]["data"] == {"config": {"split": "validation"}}
    assert scene.locked_job(inv) == incumbent

    good = scene.scored(inv, challenger, {"accuracy": 0.80})  # the same scorer on the rule's data
    out = scene.lock(inv, challenger)
    assert out["incumbent"]["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.80, "scored_by": good}


def test_a_receipt_from_a_scorer_run_on_another_input_artifact_is_not_evidence(lab):
    scene = Scene(lab)
    labelled = _publish(lab, scene.pid, LABELLED_SCORER, CODE)["component_ref"]
    validation = _labels(scene, b'{"labels": "validation"}')
    other = _labels(scene, b'{"labels": "something else"}')
    rule = {**RULE, "scorer": "proj.lscorer", "data": {"inputs": {"labels": validation}}}

    def score(inv: str, job: str, labels: str, value: float) -> str:
        return scene.job(inv, ref=labelled, slot="report", metrics={"accuracy": value},
                         inputs={"predictions": scene.output(job), "labels": labels})

    inv = scene.investigation()
    scene.note(inv, "selection_rule", json.dumps(rule))
    incumbent = scene.job(inv)
    score(inv, incumbent, validation, 0.60)
    scene.lock(inv, incumbent)
    challenger = scene.job(inv)
    score(inv, challenger, other, 0.95)  # scored against other labels
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, challenger)
    assert "no recorded" in refusal(caught)
    good = score(inv, challenger, validation, 0.80)
    assert scene.lock(inv, challenger)["incumbent"]["evidence"]["scored_by"] == good


def _labels(scene: Scene, content: bytes) -> str:
    digest, size = scene.lab.store.put_bytes(content)
    return scene.cmd("register_artifact", {"digest": digest, "size": size, "schema": "class-labels/1",
                                           "media_type": "application/json"})["artifact_id"]


@pytest.mark.parametrize("data", [pytest.param({"config": {"split": "confirmation"}}, id="split-key"),
                                  pytest.param({"config": {"fold": "confirmation", "x": 1}}, id="any-key"),
                                  pytest.param({"config": {"split": "validation", "fold": "confirmation"}}, id="mixed")])
def test_a_rule_whose_data_names_a_split_the_charter_attests_as_held_out_is_refused(lab, data):
    scene = Scene(lab, held_out={"splits": ["confirmation"]})
    inv = scene.investigation()
    for record in (lambda: scene.rule(inv, data=data),
                   lambda: scene.lock(inv, scene.accuracy_job(inv, 0.7), selection={**RULE, "data": data})):
        with pytest.raises(Invalid) as caught:
            record()
        assert "attested held-out data" in refusal(caught)
        assert caught.value.details["held_out_splits"] == ["confirmation"]
    assert scene.notes(inv) == []

    scene.rule(inv)  # a rule on the validation split is fine under the same charter
    assert len(scene.notes(inv, "selection_rule")) == 1


def test_attested_held_out_labels_are_neither_selection_data_nor_evidence(lab):
    content = b'{"labels": "held out"}'
    digest, _ = lab.store.put_bytes(content)
    scene = Scene(lab, held_out={"labels": [digest]})
    held = _labels(scene, content)
    labelled = _publish(lab, scene.pid, LABELLED_SCORER, CODE)["component_ref"]
    open_labels = _labels(scene, b'{"labels": "validation"}')
    inv = scene.investigation()
    with pytest.raises(Invalid) as caught:
        scene.rule(inv, data={"inputs": {"labels": held}})
    assert "attested held-out data" in refusal(caught) and caught.value.details["held_out_artifacts"] == [held]
    assert scene.notes(inv) == []

    rule = {**RULE, "scorer": "proj.lscorer"}  # data: config split == validation
    job = scene.job(inv)
    scene.job(inv, ref=labelled, slot="report", metrics={"accuracy": 0.99},
              inputs={"predictions": scene.output(job), "labels": held})  # a scorer that read the held-out labels
    assert scene.lock(inv, job, selection=rule)["incumbent"]["evidence"] is None
    good = scene.job(inv, ref=labelled, slot="report", metrics={"accuracy": 0.70},
                     inputs={"predictions": scene.output(job), "labels": open_labels})
    assert scene.lock(inv, job)["incumbent"]["evidence"]["scored_by"] == good


# =====================================================================================================================
# #3 reproduction
# =====================================================================================================================
def test_the_incumbent_is_reproduced_only_by_a_same_recipe_other_seed_job_scored_within_tolerance(scene):
    inv = scene.investigation()
    scene.rule(inv)
    recipe = {"depth": 1}
    incumbent = scene.job(inv, metrics={"accuracy": 0.80}, config={**recipe, "seed": 1})
    scene.lock(inv, incumbent)

    def refused_as_not_reproduced() -> dict:
        with pytest.raises(Invalid) as caught:
            scene.lock(inv, incumbent, reproduced=True)
        assert "not reproduced" in refusal(caught)
        return caught.value.details["reproduction"]

    assert refused_as_not_reproduced()["attempts"] == []  # nothing has run again yet

    unscored = scene.job(inv, config={**recipe, "seed": 2})
    short = scene.job(inv, metrics={"accuracy": 0.70}, config={**recipe, "seed": 3})  # 0.10 short of 0.80, tolerance 0.02
    # not attempts at all: the same seed again, another recipe, a failed run
    scene.job(inv, metrics={"accuracy": 0.80}, config={**recipe, "seed": 1})
    scene.job(inv, metrics={"accuracy": 0.80}, config={"depth": 2, "seed": 2})
    scene.job(inv, metrics={"accuracy": 0.80}, config={**recipe, "seed": 4}, status="failed")
    rep = refused_as_not_reproduced()
    assert rep["criterion"] == {"metric": "accuracy", "split": "validation", "direction": "higher_is_better",
                                "within": 0.02}
    assert rep["incumbent_value"] == 0.80 and rep["reproduced_by"] is None
    assert [(a["job_id"], a["status"]) for a in rep["attempts"]] == [(unscored, "unscored"), (short, "not-reproduced")]
    assert _exploration(scene, inv)["incumbent"]["reproduced_by"] is None

    reproduced = scene.job(inv, metrics={"accuracy": 0.79}, config={**recipe, "seed": 6})  # within 0.02
    out = scene.lock(inv, incumbent, reproduced=True)
    assert out["incumbent"]["basis"] == "same-incumbent" and out["incumbent"]["version"] == 1
    rep = _reproduction_note(scene, inv, out["note_id"])
    assert rep["reproduced_by"] == reproduced and rep["attempts"][-1] == {
        "job_id": reproduced, "value": 0.79, "scored_by": reproduced, "status": "reproduced"}
    assert _exploration(scene, inv)["incumbent"]["reproduced_by"] == reproduced


def test_the_conclusion_records_whether_the_incumbent_was_reproduced_and_by_what(scene):
    inv = scene.investigation()
    scene.rule(inv)
    incumbent = scene.job(inv, metrics={"accuracy": 0.80}, config={"depth": 1, "seed": 1})
    scene.lock(inv, incumbent)
    repro = scene.job(inv, metrics={"accuracy": 0.79}, config={"depth": 1, "seed": 2})  # within 0.02 of 0.80
    conclusion = _conclusion(scene, inv)["incumbent"]
    assert conclusion["job_id"] == incumbent and conclusion["version"] == 1
    assert conclusion["reproduced"] is True and conclusion["reproduced_by"] == repro
    assert conclusion["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.80, "scored_by": incumbent}
    assert conclusion["rule"] == RULE and conclusion["reproduction"]["reproduced_by"] == conclusion["reproduced_by"]


def test_a_conclusion_never_records_an_unreproduced_incumbent_as_reproduced(scene):
    inv = scene.investigation()
    scene.rule(inv)
    incumbent = scene.job(inv, metrics={"accuracy": 0.80}, config={"depth": 1, "seed": 1})
    scene.lock(inv, incumbent)
    scene.job(inv, metrics={"accuracy": 0.60}, config={"depth": 1, "seed": 2})  # a run that fell short
    conclusion = _conclusion(scene, inv)["incumbent"]
    assert conclusion["job_id"] == incumbent and conclusion["reproduced"] is False and conclusion["reproduced_by"] is None
    assert [a["status"] for a in conclusion["reproduction"]["attempts"]] == ["not-reproduced"]


def test_a_reproduction_is_judged_by_the_rules_scorer_and_its_own_tolerance(scene):
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer", reproduce_within=0.15)  # the rule widens the tolerance to 0.15
    incumbent = scene.job(inv, config={"depth": 1, "seed": 1})
    scene.scored(inv, incumbent, {"accuracy": 0.80})
    scene.lock(inv, incumbent)
    repeat = scene.job(inv, config={"depth": 1, "seed": 2})  # the trial reports nothing: only the scorer's receipt counts
    with pytest.raises(Invalid):
        scene.lock(inv, incumbent, reproduced=True)
    scorer = scene.scored(inv, repeat, {"accuracy": 0.70})  # 0.10 short, inside 0.15
    out = scene.lock(inv, incumbent, reproduced=True)
    rep = _reproduction_note(scene, inv, out["note_id"])
    assert rep["criterion"]["within"] == 0.15 and rep["reproduced_by"] == repeat
    assert rep["attempts"][0]["scored_by"] == scorer


def test_a_lower_is_better_reproduction_may_not_be_worse_by_more_than_the_tolerance(scene):
    inv = scene.investigation()
    scene.rule(inv, metric="loss", direction="lower_is_better")
    incumbent = scene.job(inv, metrics={"loss": 0.40}, config={"depth": 1, "seed": 1})
    scene.lock(inv, incumbent)
    scene.job(inv, metrics={"loss": 0.45}, config={"depth": 1, "seed": 2})  # 0.05 worse than 0.40
    with pytest.raises(Invalid):
        scene.lock(inv, incumbent, reproduced=True)
    better = scene.job(inv, metrics={"loss": 0.30}, config={"depth": 1, "seed": 3})  # lower is fine
    assert scene.lock(inv, incumbent, reproduced=True)["incumbent"]["basis"] == "same-incumbent"
    assert _exploration(scene, inv)["incumbent"]["reproduced_by"] == better


def test_an_unscored_incumbent_is_never_reproduced(scene):
    inv = scene.investigation()
    scene.rule(inv, scorer="proj.scorer")
    incumbent = scene.job(inv, config={"depth": 1, "seed": 1})
    scene.lock(inv, incumbent)  # nothing scored the incumbent
    repeat = scene.job(inv, config={"depth": 1, "seed": 2})
    scene.scored(inv, repeat, {"accuracy": 0.99})
    with pytest.raises(Invalid) as caught:
        scene.lock(inv, incumbent, reproduced=True)
    rep = caught.value.details["reproduction"]
    assert rep["incumbent_value"] is None and [a["status"] for a in rep["attempts"]] == ["incumbent-unscored"]


# =====================================================================================================================
# #4 an exploratory turn ends where the hold-back begins
# =====================================================================================================================
def test_an_explore_turn_with_a_wall_limit_ends_where_the_holdback_begins(scene):
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000)
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    turn = scene.begin(inv)
    spec = turn["spec"]
    completion = spec["completion"]
    assert spec["exploration"]["phase"] == "explore"
    hold = spec["exploration"]["allowance"]["holdback_seconds"]
    assert hold == 540.0 and completion["conclude_threshold_seconds"] == 360.0  # 15% of the wall, 10% for the conclusion
    until = completion["exploration_deadline_unix"]
    assert until == pytest.approx(completion["deadline_unix"] - 360.0 - 540.0, abs=0.01)
    assert turn_boundary(completion) == until  # retries and activities end there too
    # the decision deadline is the time left until then: without the cap it would be min(5000, remaining - 360) = ~3200
    assert 2500 <= spec["limits"]["deadline_seconds"] <= until - time.time()
    assert scene.q("get_turn", {"turn_id": turn["turn_id"]})["spec"]["completion"]["exploration_deadline_unix"] == until


def test_a_finalize_turn_may_use_the_holdback_so_it_has_no_exploration_deadline(scene):
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000, exploration={"patience_rounds": 0, "max_rounds": 0})
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    spec = scene.begin(inv)["spec"]
    assert spec["exploration"]["phase"] == "finalize" and "exploration_deadline_unix" not in spec["completion"]
    assert turn_boundary(spec["completion"]) == spec["completion"]["deadline_unix"] - 360.0
    assert spec["limits"]["deadline_seconds"] > 3000  # not capped at the hold-back


class _Clock:
    """The lab's ``time`` module, with a settable offset: a turn's wall time passing without waiting for it."""

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return time.time() + self.offset


def test_after_the_exploration_deadline_an_explore_turn_refuses_new_challengers_but_not_reproduction(scene, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("eidos.application.handlers.investigations.time", clock)
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000)
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    turn = scene.begin(inv)
    tid, until = turn["turn_id"], turn["spec"]["completion"]["exploration_deadline_unix"]
    assert turn["spec"]["exploration"]["phase"] == "explore"
    assert scene.propose(tid, 0, _challenger(scene))["accepted"] is True  # before the deadline

    clock.offset = until - time.time() + 5  # the hold-back has begun, the turn has not ended
    refused = scene.propose(tid, 1, _challenger(scene))
    assert refused["accepted"] is False and refused["reason"] == "invalid"
    assert "exploration has stopped" in refused["error"]["message"] and "wall-holdback" in refused["error"]["message"]
    assert refused["error"]["details"]["stop"] == "wall-holdback" and refused["error"]["details"]["calls"] == [0]
    proposal = {**_challenger(scene), "purpose": "reproduction"}
    proposal["calls"][0]["args"]["purpose"] = "reproduction"
    repro = scene.propose(tid, 2, proposal)
    assert repro["accepted"] is True  # reproduction is what the hold-back is for
    concluding = scene.propose(tid, 3, {"rationale": "r", "calls": [], "control": {"action": "conclude", "summary": "s"}})
    assert concluding["accepted"] is True


@pytest.mark.parametrize(("decision_tokens", "phase", "stop"), [
    pytest.param(9_000, "finalize", "token-holdback", id="decision-does-not-fit"),
    pytest.param(8_501, "finalize", "token-holdback", id="one-token-too-many"),
    pytest.param(8_500, "explore", None, id="exactly-fits"),
    pytest.param(1_000, "explore", None, id="fits")])
def test_a_turn_begins_in_finalize_when_one_decision_does_not_fit_before_the_token_holdback(
        scene, decision_tokens, phase, stop):
    inv = scene.investigation(limits=TOKEN_LIMITS, settings={"max_tokens_per_decision": decision_tokens})
    # The token hold-back protects the phase before the first lock as well.
    pre = _exploration(scene, inv)
    assert (pre["phase"], pre["stop"]) == (("finalize", "token-holdback") if stop else ("baseline", None))
    candidate = scene.job(inv, metrics={"accuracy": 0.70}, purpose="reproduction")
    if stop:
        with pytest.raises(Invalid, match="no new or replacement lock"):
            scene.lock(inv, candidate, selection=RULE)
    else:
        scene.lock(inv, candidate, selection=RULE)
    state = scene.begin(inv)["spec"]["exploration"]
    assert state["allowance"]["holdback_tokens"] == 1500 and state["allowance"]["tokens_left"] == 8500
    assert (state["phase"], state["stop"]) == (phase, stop)
    assert scene.locked_job(inv) == (None if stop else candidate)


def _reserve(scene: Scene, turn_id: str, slot: int, tokens: float) -> dict:
    """begin_decision and reserve_model_budget, as the decide activity sends them."""
    execution = f"{RUN_A}/decide-slot-{slot}"
    epoch = _begin_decision(scene.lab, scene.pid, turn_id, execution, slot=slot)["epoch"]
    return scene.lab.cmd(scene.wf, "reserve_model_budget", {
        "turn_id": turn_id, "slot": slot, "attempt": 1, "epoch": epoch, "estimate": {"model_tokens": tokens},
        "execution": execution}, project=scene.pid)["result"]


def _reserved(scene: Scene, inv: str) -> float:
    return scene.q("budgets", {"investigation_id": inv})["budgets"]["model_tokens"]["reserved"]


def test_an_explore_decision_that_does_not_fit_before_the_token_holdback_is_refused_when_it_reserves(scene):
    inv = scene.investigation(limits=TOKEN_LIMITS, settings={"max_tokens_per_decision": 1_000})
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    turn = scene.begin(inv)
    tid = turn["turn_id"]
    assert turn["spec"]["exploration"]["phase"] == "explore"

    with pytest.raises(BudgetExhausted) as caught:  # 9 000 would eat into the 1 500 kept for finalization
        _reserve(scene, tid, 0, 9_000)
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert caught.value.details["reason"] == "exploration-holdback"
    assert caught.value.details["estimate"] == 9_000 and caught.value.details["tokens_left"] == 8_500
    assert _reserved(scene, inv) == 0  # nothing was reserved

    assert _reserve(scene, tid, 1, 5_000)["reservation_ref"]  # fits
    with pytest.raises(BudgetExhausted) as caught:  # as of now: another decision of this turn has reserved since
        _reserve(scene, tid, 2, 4_000)
    assert caught.value.details["tokens_left"] == 3_500 and _reserved(scene, inv) == 5_000


def test_a_finalize_decision_may_reserve_the_holdback(scene):
    inv = scene.investigation(limits=TOKEN_LIMITS, settings={"max_tokens_per_decision": 1_000},
                              exploration={"patience_rounds": 0, "max_rounds": 0})
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    turn = scene.begin(inv)
    assert turn["spec"]["exploration"]["phase"] == "finalize"
    assert _reserve(scene, turn["turn_id"], 0, 9_000)["reservation_ref"]
    assert _reserved(scene, inv) == 9_000


def test_once_the_token_allowance_is_used_an_explore_turn_admits_no_new_challenger(scene):
    inv = scene.investigation(limits=TOKEN_LIMITS, settings={"max_tokens_per_decision": 1_000})
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    turn = scene.begin(inv)
    tid = turn["turn_id"]
    assert turn["spec"]["exploration"]["phase"] == "explore"
    execution = f"{RUN_A}/decide-slot-0"
    epoch = _begin_decision(scene.lab, scene.pid, tid, execution, slot=0)["epoch"]
    scene.lab.cmd(scene.wf, "reserve_model_budget", {  # this decision takes all that was left before the hold-back
        "turn_id": tid, "slot": 0, "attempt": 1, "epoch": epoch, "estimate": {"model_tokens": 8_500},
        "execution": execution}, project=scene.pid)
    refused = scene.lab.cmd(scene.wf, "accept_decision", {
        "turn_id": tid, "slot": 0, "proposal": _challenger(scene), "usage_recorded": True, "attempt": 1, "epoch": epoch,
        "execution": execution}, project=scene.pid)["result"]
    assert refused["accepted"] is False and refused["reason"] == "invalid"
    assert refused["error"]["details"]["stop"] == "token-holdback"
    assert "exploration has stopped" in refused["error"]["message"]


# =====================================================================================================================
# #6 a round is evaluated only when its challengers are
# =====================================================================================================================
def test_rounds_whose_challengers_are_still_running_do_not_stop_exploration_until_they_are_scored(scene):
    inv = scene.investigation(exploration={"patience_rounds": 3})
    scene.rule(inv)
    _first_lock_round(scene, inv, 0.70)
    running = [_round(scene, inv, depth, finish=False) for depth in (2, 3, 4)]

    state = _exploration(scene, inv)
    assert state["rounds"] == 3 and state["rounds_without_improvement"] == 0
    assert state["pending_challengers"] == running
    assert state["stop"] is None and state["phase"] == "explore"  # three rounds, none of them evaluated

    scene.finish_job(running[0], metrics={"accuracy": 0.50})
    scene.finish_job(running[1], metrics={"accuracy": 0.60})
    state = _exploration(scene, inv)
    assert state["rounds_without_improvement"] == 2 and state["pending_challengers"] == [running[2]]
    assert state["stop"] is None and state["phase"] == "explore"

    scene.finish_job(running[2], metrics={"accuracy": 0.65})  # terminal and scored, and none improves: now it stops
    state = _exploration(scene, inv)
    assert (state["rounds"], state["rounds_without_improvement"]) == (3, 3) and state["pending_challengers"] == []
    assert state["stop"] == "ceiling-no-improvement" and state["phase"] == "finalize"


def test_a_failed_challenger_is_evaluated_but_a_succeeded_one_without_evidence_is_still_pending(scene):
    inv = scene.investigation(exploration={"patience_rounds": 3})
    scene.rule(inv, scorer="proj.scorer")
    _first_lock_round(scene, inv, 0.70, via_scorer=True)
    failed = _round(scene, inv, 2, finish=False)
    unscored = _round(scene, inv, 3, finish=False)
    scene.finish_job(failed, status="failed")
    scene.finish_job(unscored)  # succeeded, but no run of the rule's scorer has read its outputs yet
    state = _exploration(scene, inv)
    assert state["rounds"] == 2 and state["rounds_without_improvement"] == 1  # the failed round only
    assert state["pending_challengers"] == [unscored] and state["stop"] is None

    scene.scored(inv, unscored, {"accuracy": 0.50})
    state = _exploration(scene, inv)
    assert state["rounds_without_improvement"] == 2 and state["pending_challengers"] == []


def test_in_a_finalize_turn_scoring_a_pending_challenger_is_accepted_and_a_new_challenger_is_not(scene):
    inv = scene.investigation(exploration={"max_rounds": 1})
    scene.rule(inv, scorer="proj.scorer")
    incumbent = _first_lock_round(scene, inv, 0.70, via_scorer=True)
    pending = _round(scene, inv, 2)  # succeeded, nothing has scored it
    turn = scene.begin(inv)
    state, tid = turn["spec"]["exploration"], turn["turn_id"]
    assert state["phase"] == "finalize" and state["stop"] == "search-limit"
    assert state["pending_challengers"] == [pending] and state["pending_outputs"] == [scene.output(pending)]

    def read(artifact, ref=scene.scorer):
        return _calls(ref, inputs={"predictions": artifact})

    scoring = scene.propose(tid, 0, {"rationale": "r", "purpose": "open-search", "calls": read(scene.output(pending))})
    assert scoring["accepted"] is True, scoring

    chained = [*_calls(scene.infer, inputs={"predictions": scene.output(pending)}),
               *_calls(scene.scorer, inputs={"predictions": {"$ref": "call[0].outputs.predictions"}})]
    assert scene.propose(tid, 1, {"rationale": "r", "purpose": "open-search", "calls": chained})["accepted"] is True

    for slot, calls in ((2, _calls(scene.trial)),  # a new challenger
                        (3, read(scene.output(incumbent))),  # scoring something that is not pending
                        (4, [*_calls(scene.trial), *_calls(scene.scorer, inputs={
                            "predictions": {"$ref": "call[0].outputs.predictions"}})])):  # a chain from a new job
        refused = scene.propose(tid, slot, {"rationale": "r", "purpose": "open-search", "calls": calls})
        assert refused["accepted"] is False and refused["reason"] == "invalid", (slot, refused)
        assert "exploration has stopped" in refused["error"]["message"]
    assert scene.propose(tid, 5, {"rationale": "r", "purpose": "reproduction",
                                  "calls": _calls(scene.trial, purpose="reproduction")})["accepted"]

    scene.finish_turn(tid)
    scene.scored(inv, pending, {"accuracy": 0.50})  # scoring it finishes its round
    state = _exploration(scene, inv)
    assert state["pending_challengers"] == [] and state["rounds_without_improvement"] == 1


# =====================================================================================================================
# #7 the baseline is explicit
# =====================================================================================================================
@pytest.mark.parametrize("how", [pytest.param("text", id="in-the-text"), pytest.param("ref", id="evidence-ref"),
                                 pytest.param("job-ref", id="evidence-ref-object")])
def test_a_baseline_note_naming_a_succeeded_job_records_it_as_the_designated_baseline(scene, how):
    inv = scene.investigation()
    first = scene.job(inv, metrics={"accuracy": 0.50})  # a trial, not the project's command: no automatic baseline
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    refs = {"text": {}, "ref": {"evidence_refs": [first]}, "job-ref": {"evidence_refs": [{"job_id": first}]}}[how]
    scene.cmd("record_note", {"investigation_id": inv, "kind": "baseline",
                              "text": f"the baseline is {first}" if how == "text" else "the reference run", **refs})
    rec = _baseline(scene, inv)
    assert rec["body"] == {"investigation_id": inv, "job_id": first, "component": scene.trial, "environment": "lab",
                           "purpose": "open-search", "metrics": {"accuracy": 0.50}, "basis": "designated",
                           "scored_by": None}
    (event,) = scene.events(inv, "baseline.recorded")
    assert event["subject"] == first

    other = scene.job(inv, metrics={"accuracy": 0.60})
    scene.note(inv, "baseline", f"actually {other}")  # the first record stands
    assert _baseline(scene, inv)["body"]["job_id"] == first and len(scene.events(inv, "baseline.recorded")) == 1


def test_a_baseline_note_records_nothing_for_a_failed_job_and_the_baseline_when_a_named_job_succeeds(scene):
    inv = scene.investigation()
    failed = scene.job(inv, status="failed")
    scene.note(inv, "baseline", f"the baseline is {failed}")
    with pytest.raises(NotFound):
        _baseline(scene, inv)

    running = scene.job(inv, metrics=None, finish=False)
    scene.note(inv, "baseline", f"the baseline is {running}")  # named before it has succeeded
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    scene.finish_job(running, metrics={"accuracy": 0.55})  # the terminal hook records it
    rec = _baseline(scene, inv)
    assert rec["body"]["job_id"] == running and rec["body"]["basis"] == "designated"
    assert rec["body"]["metrics"] == {"accuracy": 0.55}


def test_a_reproduction_purpose_job_is_not_recorded_as_the_baseline_unless_a_note_names_it(scene):
    inv = scene.investigation()
    repro = scene.job(inv, purpose="reproduction", metrics={"accuracy": 0.60})
    scene.job(inv, ref=scene.command, purpose="reproduction", inputs={"source": _source_artifact(scene)})
    with pytest.raises(NotFound):
        _baseline(scene, inv)
    assert scene.events(inv, "baseline.recorded") == []
    scene.note(inv, "baseline", f"the reference is {repro}")  # an explicit designation is the only other way
    rec = _baseline(scene, inv)
    assert rec["body"]["job_id"] == repro and rec["body"]["basis"] == "designated"
    assert rec["body"]["purpose"] == "reproduction"
