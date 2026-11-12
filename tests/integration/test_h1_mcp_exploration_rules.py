"""H1: ADR 0023's finalize phase and exploration deadline for work admitted OUTSIDE a decision, against PostgreSQL.

``accept_decision`` has always refused new ``open-search`` / ``hypothesis-test`` compute once exploration has stopped.
The same rule now holds at the lab's job admission (``exploration.check_admission``, called from ``admit_job``), so the
MCP ``run_batch`` path (ADR 0025, the shared ``BatchRunner``) and direct commands cannot start new challengers after the
selection is frozen, at a perfect score, at the search limit or in the hold-back:

* the real MCP tools (``run_batch``, ``submit_candidate``) over the real LabClient/FastAPI app on the real ledger;
* a refused call is an ``INVALID`` receipt naming the stop ("exploration has stopped (<stop>)"), and starts nothing;
* ``reproduction`` (and ``confirmation``) work is never refused: that is what the finalize phase is for;
* a job that reads an output of a pending challenger (it finishes an admitted round) is still admitted;
* while exploring, an exploratory job's wall is capped where the hold-back begins;
* an effect of an accepted decision (``Ctx.admission`` set) is checked when the decision is accepted, not again here;
* the selection rules live in ``record_note``, which ``submit_candidate`` uses.

The scene, the ledger writes that freeze a selection, and the round helpers are those of ``test_incumbent_challengers``
and ``test_r14_selection_e2e``; the lab is driven through the same command service they use.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from test_f5_turn_authorization import RUN_A
from test_f5c_ticket_and_handoff import _accept, _admission, _begin_turn, _hand_off
from test_incumbent_challengers import RULE, Scene, _plan, _request
from test_mcp_normalized_receipts import lab_client_over_http
from test_r14_selection_e2e import TOKEN_LIMITS, _challenger, _exploration, _first_lock_round, _round

from eidos.application.handlers.exploration import check_admission
from eidos.domain.errors import Invalid
from eidos.mcp.gateway import ToolFailure, call_tool

pytestmark = pytest.mark.pg


@pytest.fixture()
def scene(lab):
    return Scene(lab)


@pytest.fixture()
def client(lab):
    return lab_client_over_http(lab)


# -- helpers ------------------------------------------------------------------------------------------------------------
def _batch(client, scene: Scene, inv: str, key: str, ref: str, **args) -> dict:
    """``run_batch`` of one ``run_component`` call of ``ref`` in ``inv`` (``args``: the call's arguments, e.g. the
    purpose, inputs and wall_seconds); returns the batch result. Nothing waits: no job is run here."""
    call = {"tool": "run_component", "args": {"component_ref": ref, "environment": "lab", **args}}
    return call_tool(client, "run_batch", {"project": scene.pid, "command_key": key, "investigation_id": inv,
                                           "wait_seconds": 0, "calls": [call]})


def _only(out: dict) -> dict:
    assert out["status"] == "done" and len(out["calls"]) == 1, out
    return out["calls"][0]


def _admitted(receipt: dict) -> str:
    assert receipt["ok"] is True and receipt["tool"] == "run_component", receipt
    return receipt["result"]["job_id"]


def _stopped(receipt: dict, stop: str) -> None:
    """The batch receipt of a call the lab refused because exploration has stopped for ``stop``."""
    assert receipt["ok"] is False, receipt
    error = receipt["error"]
    assert error["code"] == "INVALID", error
    assert f"exploration has stopped ({stop})" in error["message"], error
    assert "reproduction" in error["next_action"], error


def _jobs_of(scene: Scene, inv: str, purpose: str | None = None) -> list[dict]:
    jobs = scene.q("list_jobs", {"investigation_id": inv, "limit": 1000})
    return [j for j in jobs if purpose is None or j["purpose"] == purpose]


def _lock_wall(scene: Scene, job: str) -> int:
    return scene.q("get_job", {"job_id": job})["lock"]["limits"]["wall_seconds"]


class _Clock:
    """The ``time`` module the exploration check reads, with a settable offset: wall time passing without waiting."""

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return time.time() + self.offset


# =====================================================================================================================
# 1. run_batch after the selection is frozen
# =====================================================================================================================
FREEZES = [pytest.param(_plan, "frozen", id="evidence-plan-frozen"),
           pytest.param(_request, "launched", id="assessment-requested")]


@pytest.mark.parametrize(("freeze", "status"), FREEZES)
def test_run_batch_refuses_a_new_challenger_once_the_selection_is_frozen_but_admits_a_reproduction(
        scene, client, freeze, status):
    inv = scene.investigation()
    incumbent = scene.accuracy_job(inv, 0.70)
    scene.lock(inv, incumbent, selection=RULE)
    before = _batch(client, scene, inv, "b-open-before", scene.trial, purpose="open-search")
    _admitted(_only(before))  # while exploring, the same batch is admitted
    freeze(scene, inv, status)
    assert _exploration(scene, inv)["stop"] == "selection-frozen"
    jobs = len(_jobs_of(scene, inv))

    for purpose in ("open-search", "hypothesis-test"):
        refused = _only(_batch(client, scene, inv, f"b-{purpose}", scene.trial, purpose=purpose))
        _stopped(refused, "selection-frozen")
    default_purpose = _only(_batch(client, scene, inv, "b-default", scene.trial))  # run_component's default is open-search
    _stopped(default_purpose, "selection-frozen")
    assert len(_jobs_of(scene, inv)) == jobs  # a refused call admitted nothing

    for purpose in ("reproduction", "confirmation"):
        job = _admitted(_only(_batch(client, scene, inv, f"b-{purpose}", scene.trial, purpose=purpose)))
        assert [j["purpose"] for j in _jobs_of(scene, inv) if j["id"] == job] == [purpose]
    assert len(_jobs_of(scene, inv)) == jobs + 2


def test_a_refused_batch_call_is_a_receipt_the_same_key_replays_and_records_why(scene, client):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    _request(scene, inv, "launched")
    first = _only(_batch(client, scene, inv, "b-refused", scene.trial, purpose="open-search"))
    _stopped(first, "selection-frozen")
    again = _only(_batch(client, scene, inv, "b-refused", scene.trial, purpose="open-search"))
    _stopped(again, "selection-frozen")  # a failed command stores nothing: the retry is judged again, and refused again
    assert [j for j in _jobs_of(scene, inv) if j["purpose"] == "open-search" and j["status"] == "admitted"] == []


# =====================================================================================================================
# 2. a perfect score on the recorded rule ends exploration
# =====================================================================================================================
def test_run_batch_refuses_a_new_challenger_once_the_incumbent_is_at_the_rules_perfect_score(scene, client):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 1.0), selection=RULE)  # accuracy's ceiling is 1.0 (BOUNDED_METRICS)
    state = _exploration(scene, inv)
    assert state["phase"] == "finalize" and state["stop"] == "ceiling-perfect-score"

    _stopped(_only(_batch(client, scene, inv, "b-perfect", scene.trial, purpose="open-search")), "ceiling-perfect-score")
    reproduction = _only(_batch(client, scene, inv, "b-perfect-repro", scene.trial, purpose="reproduction"))
    assert _admitted(reproduction)


def test_an_incumbent_just_below_the_ceiling_still_explores(scene, client):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.99), selection=RULE)
    assert _exploration(scene, inv)["stop"] is None
    assert _admitted(_only(_batch(client, scene, inv, "b-near", scene.trial, purpose="open-search")))


def test_a_rules_own_ceiling_ends_exploration_for_a_metric_with_no_default_ceiling(scene, client):
    inv = scene.investigation()
    rule = {**RULE, "metric": "loss", "direction": "lower_is_better", "ceiling": 0.05}
    scene.lock(inv, scene.job(inv, metrics={"loss": 0.05}), selection=rule)
    _stopped(_only(_batch(client, scene, inv, "b-loss", scene.trial, purpose="open-search")), "ceiling-perfect-score")


# direct commands outside any decision (the CLI, a script) are held to the same stops
def _stop_frozen(scene: Scene, inv: str) -> None:
    _request(scene, inv, "launched")


def _stop_ceiling(scene: Scene, inv: str) -> None:
    scene.lock(inv, scene.accuracy_job(inv, 1.0), selection=RULE)


@pytest.mark.parametrize(("stop", "settings"), [
    pytest.param("selection-frozen", {}, id="selection-frozen"),
    pytest.param("ceiling-perfect-score", {}, id="perfect-score"),
    pytest.param("search-limit", {"exploration": {"patience_rounds": 0, "max_rounds": 0}}, id="search-limit"),
    pytest.param("token-holdback", {"limits": TOKEN_LIMITS, "settings": {"max_tokens_per_decision": 8_000}},
                 id="token-holdback")])
def test_a_direct_run_component_outside_a_decision_is_refused_in_every_finalize_stop(scene, stop, settings):
    inv = scene.investigation(**settings)
    if stop == "ceiling-perfect-score":
        _stop_ceiling(scene, inv)
    else:
        if stop == "token-holdback":
            assert _exploration(scene, inv)["phase"] == "baseline"
        scene.lock(inv, scene.job(inv, metrics={"accuracy": 0.70}), selection=RULE)
        if stop == "selection-frozen":
            _stop_frozen(scene, inv)
        elif stop == "token-holdback":
            # Establish the incumbent before a real ledger reservation clips the next exploratory decision.
            with scene.lab.ledger.transaction() as tx:
                tx.project_id = scene.pid
                tx.reserve(f"inv:{inv}", "model_tokens", 1_000, "fixture:reserve")
    assert _exploration(scene, inv)["stop"] == stop
    for purpose in ("open-search", "hypothesis-test"):
        with pytest.raises(Invalid) as caught:
            scene.job(inv, purpose=purpose)
        assert f"exploration has stopped ({stop})" in caught.value.message
        details = caught.value.details
        assert details["stop"] == stop and details["purpose"] == purpose and details["incumbent"]["version"] == 1
    assert scene.job(inv, purpose="reproduction")  # finalization work


def test_direct_work_is_refused_when_patience_is_spent(scene):
    inv = scene.investigation(exploration={"patience_rounds": 1})
    scene.rule(inv)
    _first_lock_round(scene, inv, 0.70)
    _round(scene, inv, 2, metrics={"accuracy": 0.50})  # a scored challenger round that does not improve
    assert _exploration(scene, inv)["stop"] == "ceiling-no-improvement"
    with pytest.raises(Invalid) as caught:
        scene.job(inv)
    assert "exploration has stopped (ceiling-no-improvement)" in caught.value.message


def test_in_the_baseline_phase_nothing_is_refused_and_work_outside_an_investigation_is_unchecked(scene, client):
    inv = scene.investigation(exploration={"patience_rounds": 0, "max_rounds": 0})  # a limit, but no incumbent yet
    assert _admitted(_only(_batch(client, scene, inv, "b-base", scene.trial, purpose="open-search")))
    assert scene.job(None, purpose="open-search")  # a job of no investigation has no exploration to stop


def test_the_wall_holdback_refuses_a_new_challenger_in_a_batch_but_not_a_reproduction(scene, client, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("eidos.application.handlers.exploration.time", clock)
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000)
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    wall = scene.q("get_investigation", {"investigation_id": inv})["body"]["wall"]
    until = wall["deadline_unix"] - wall["conclude_threshold_seconds"] - 540.0  # 15% of the wall is held back

    assert _admitted(_only(_batch(client, scene, inv, "b-before", scene.trial, purpose="open-search")))
    clock.offset = until - time.time() + 5  # the hold-back has begun; the reserve for the conclusion has not
    _stopped(_only(_batch(client, scene, inv, "b-after", scene.trial, purpose="open-search")), "wall-holdback")
    assert _admitted(_only(_batch(client, scene, inv, "b-after-repro", scene.trial, purpose="reproduction")))


# =====================================================================================================================
# 3. scoring a pending challenger is still admitted
# =====================================================================================================================
def test_run_batch_still_admits_a_job_that_reads_a_pending_challengers_output_when_exploration_has_stopped(
        scene, client):
    inv = scene.investigation(exploration={"max_rounds": 1})
    scene.rule(inv, scorer="proj.scorer")
    incumbent = _first_lock_round(scene, inv, 0.70, via_scorer=True)
    pending = _round(scene, inv, 2)  # admitted and succeeded, but nothing has scored it yet
    state = _exploration(scene, inv)
    assert state["phase"] == "finalize" and state["stop"] == "search-limit"
    assert state["pending_challengers"] == [pending] and state["pending_outputs"] == [scene.output(pending)]

    def score(key: str, artifact: str, purpose: str = "open-search") -> dict:
        return _only(_batch(client, scene, inv, key, scene.scorer, purpose=purpose, inputs={"predictions": artifact},
                            config={"split": "validation"}))  # the rule's selection data

    scoring = _admitted(score("b-score-pending", scene.output(pending)))  # finishes the admitted round
    assert [j["id"] for j in _jobs_of(scene, inv, "open-search")].count(scoring) == 1
    assert _admitted(score("b-score-pending-2", scene.output(pending), "hypothesis-test"))  # the rule is by artifact

    # anything else that starts exploratory compute is refused: a new challenger, scoring what is not pending
    _stopped(_only(_batch(client, scene, inv, "b-new", scene.trial, purpose="open-search")), "search-limit")
    _stopped(score("b-score-incumbent", scene.output(incumbent)), "search-limit")

    scene.finish_job(scoring, ref=scene.scorer, metrics={"accuracy": 0.50})  # the round is now evaluated
    state = _exploration(scene, inv)
    assert state["pending_challengers"] == [] and "pending_outputs" not in state
    _stopped(score("b-score-again", scene.output(pending)), "search-limit")


# =====================================================================================================================
# 4. while exploring, an exploratory job's wall ends where the hold-back begins
# =====================================================================================================================
def test_an_exploratory_job_outside_a_decision_is_capped_at_the_exploration_deadline(scene, client):
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000)
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    wall = scene.q("get_investigation", {"investigation_id": inv})["body"]["wall"]
    hold = 540.0  # 15% of the 3600 s wall limit (between the policy's 120 s and 900 s)
    until = wall["deadline_unix"] - wall["conclude_threshold_seconds"] - hold
    assert wall["wall_seconds"] == 3600 and wall["conclude_threshold_seconds"] == 540.0

    started = time.time()
    capped = _admitted(_only(_batch(client, scene, inv, "b-wall", scene.trial, purpose="open-search", wall_seconds=3000)))
    ended = time.time()
    # the investigation's reserve alone would leave ~3060 s: the hold-back leaves ~2520 s, and the lock records it
    cap = _lock_wall(scene, capped)
    assert until - ended - 1 <= cap <= until - started < 3000, (cap, until - started)
    assert cap > 2420
    hypothesis = _admitted(_only(_batch(client, scene, inv, "b-wall-h", scene.trial, purpose="hypothesis-test",
                                        wall_seconds=3000)))
    assert _lock_wall(scene, hypothesis) <= until - started

    short = _admitted(_only(_batch(client, scene, inv, "b-short", scene.trial, purpose="open-search", wall_seconds=60)))
    assert _lock_wall(scene, short) == 60  # a cap never raises a job's own limit

    # finalization work may use the hold-back: only the conclusion's reserve limits it
    repro = _admitted(_only(_batch(client, scene, inv, "b-repro-wall", scene.trial, purpose="reproduction",
                                   wall_seconds=3000)))
    assert _lock_wall(scene, repro) == 3000

    # a direct command outside a decision is capped the same way
    direct = scene.job(inv, purpose="open-search", wall_seconds=3000, finish=False)
    assert _lock_wall(scene, direct) <= wall["deadline_unix"] - wall["conclude_threshold_seconds"] - hold - started


def test_without_a_wall_limit_nothing_is_capped_while_exploring(scene, client):
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    job = _admitted(_only(_batch(client, scene, inv, "b-nowall", scene.trial, purpose="open-search", wall_seconds=3000)))
    assert _lock_wall(scene, job) == 3000


# =====================================================================================================================
# 5. an effect of an accepted decision is not checked again here
# =====================================================================================================================
class _NoLedger:
    """A transaction that fails the test when it is touched: the admission check must not even read the state."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"check_admission read the ledger ({name}) for an effect of an accepted decision")


def test_check_admission_skips_an_effect_of_an_accepted_decision_and_work_that_is_never_refused():
    lock = {"inputs": {}}
    decision = SimpleNamespace(admission={"decision_id": "dec_1", "execution": "run-A/exec"}, tx=_NoLedger())
    for purpose in ("open-search", "hypothesis-test", "reproduction"):
        assert check_admission(decision, "inv_1", purpose, lock) is None  # no deadline, no refusal, no ledger read

    direct = SimpleNamespace(admission=None, tx=_NoLedger())
    assert check_admission(direct, None, "open-search", lock) is None  # no investigation: nothing to stop
    for purpose in ("reproduction", "confirmation", "qualification", "build", "assessment", "export-check"):
        assert check_admission(direct, "inv_1", purpose, lock) is None  # purposes the finalize phase never refuses
    # H2 (R17 #2): measurement-development is a challenger purpose, so it is checked against the ledger like the others
    with pytest.raises(AssertionError, match="read the ledger"):
        check_admission(direct, "inv_1", "measurement-development", lock)


def test_an_effect_of_a_decision_accepted_before_the_freeze_is_admitted_while_a_direct_call_is_refused(scene):
    """The decision path is checked when the decision is accepted: the effect of a decision accepted while exploring
    runs after the selection is frozen, with its ``admission``; the same command without it is refused."""
    inv = scene.investigation()
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    turn = _begin_turn(scene.lab, scene.pid, inv, RUN_A)
    assert turn["started"] is True and turn["spec"]["exploration"]["phase"] == "explore"
    decision = _accept(scene.lab, scene.pid, turn["turn_id"], _challenger(scene))
    handed = _hand_off(scene.lab, scene.pid, decision["decision_id"], f"{RUN_A}/exec")
    assert handed["handed_off"] is True

    _request(scene, inv, "launched")  # the freeze commits after the decision was accepted
    payload = {"component_ref": scene.trial, "environment": "lab", "purpose": "open-search", "investigation_id": inv,
               "config": {"split": "validation"}}
    with pytest.raises(Invalid) as caught:
        scene.lab.svc.execute(scene.lab.admin, "run_component", project_id=scene.pid, command_key="direct-1",
                              payload=payload)
    assert "exploration has stopped (selection-frozen)" in caught.value.message

    receipt = scene.lab.svc.execute(scene.lab.admin, "run_component", project_id=scene.pid, command_key="effect-1",
                                    payload=payload, admission=_admission(decision["decision_id"], f"{RUN_A}/exec"))
    assert receipt["status"] == "committed" and receipt["result"]["job_id"]
    assert [j["id"] for j in _jobs_of(scene, inv, "open-search")].count(receipt["result"]["job_id"]) == 1


# =====================================================================================================================
# 6. the selection rules apply to submit_candidate over MCP (record_note)
# =====================================================================================================================
SCOPE = {"task": "demo", "population": "visible validation"}


def _submit(client, scene: Scene, inv: str, key: str, job: str) -> dict:
    return call_tool(client, "submit_candidate", {"project": scene.pid, "command_key": key, "investigation_id": inv,
                                                  "job_id": job, "statement": f"{job} improves", "scope": SCOPE})


def test_submit_candidate_follows_the_selection_rules_and_the_freeze(scene, client):
    inv = scene.investigation()
    scene.rule(inv)
    incumbent = scene.accuracy_job(inv, 0.70)
    first = _submit(client, scene, inv, "s-1", incumbent)
    assert first["claim"]["state"] == "candidate-submitted" and scene.locked_job(inv) == incumbent

    worse, better, unreached = (scene.accuracy_job(inv, v) for v in (0.60, 0.85, 0.95))
    with pytest.raises(ToolFailure) as caught:
        _submit(client, scene, inv, "s-worse", worse)
    assert caught.value.code == "INVALID" and "does not beat the incumbent" in caught.value.message
    assert scene.locked_job(inv) == incumbent  # the refused submission moved nothing

    second = _submit(client, scene, inv, "s-better", better)
    note = next(n for n in scene.notes(inv, "candidate_lock") if n["id"] == second["lock_note_id"])
    assert note["body"]["incumbent"]["version"] == 2 and note["body"]["incumbent"]["basis"] == "beats-incumbent"
    assert json.loads(note["body"]["text"]) == {"job_id": better} and scene.locked_job(inv) == better

    _request(scene, inv, "launched")  # an assessment was requested: the selection is frozen
    with pytest.raises(ToolFailure) as frozen:
        _submit(client, scene, inv, "s-frozen", unreached)
    assert frozen.value.code == "INVALID" and "the selection is frozen" in frozen.value.message
    assert scene.locked_job(inv) == better
    # the frozen selection also ends exploration: new challengers are refused at admission, whoever asks
    _stopped(_only(_batch(client, scene, inv, "b-after-submit", scene.trial, purpose="open-search")), "selection-frozen")
