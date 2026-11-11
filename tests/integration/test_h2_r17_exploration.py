"""H2: R17 findings 1-3 on ADR 0023's exploration rules, against PostgreSQL (no model, no network).

* Finding 1, one round history: investigator turns, MCP ``run_batch`` batches and locks recorded outside a decision
  (``submit_candidate``) share one numbered history (``turn_log``), so ``max_rounds`` and patience stop work admitted
  over MCP as they stop decisions. A batch is one round however many calls or resumptions it takes (each call is judged
  by the state as its batch began); the limits stop the NEXT batch, and a replayed batch is never refused.
* Finding 2, ``measurement-development`` is a challenger purpose: once exploration has stopped it is refused like
  ``open-search`` (MCP admission and the decision path), except to finish a pending round (it reads a pending output).
* Finding 3, the hold-back is protected before the first lock too: a wall or token allowance already spent with no
  candidate locked is phase ``finalize`` with ``candidate: "none"``, new exploratory work is refused with a message that
  says no candidate was locked, ``reproduction`` stays admitted, and while still exploring (phase ``baseline``) an
  exploratory job's wall is capped where the hold-back begins.

The scene, the round helpers and the batch helpers are those of ``test_incumbent_challengers``, ``test_r14_selection_e2e``
and ``test_h1_mcp_exploration_rules``; the real MCP tools run over the real LabClient/FastAPI app on the real ledger.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from test_h1_mcp_exploration_rules import _admitted, _batch, _Clock, _jobs_of, _lock_wall, _only, _stopped
from test_incumbent_challengers import RULE, Scene, _request
from test_mcp_normalized_receipts import lab_client_over_http
from test_r14_selection_e2e import TOKEN_LIMITS, _exploration, _first_lock_round, _round

from eidos.application.handlers.exploration import turn_log
from eidos.application.handlers.investigations import _check_exploration_phase, _live_exploration_stop
from eidos.application.service import Ctx
from eidos.domain import turns as turn_rules
from eidos.domain.errors import Invalid
from eidos.mcp.gateway import ToolFailure, call_tool

pytestmark = pytest.mark.pg

SCOPE = {"task": "demo", "population": "visible validation"}
VALIDATION = {"split": "validation"}


@pytest.fixture()
def scene(lab):
    return Scene(lab)


@pytest.fixture()
def client(lab):
    return lab_client_over_http(lab)


# -- helpers ------------------------------------------------------------------------------------------------------------
def _lock(client, scene: Scene, inv: str, key: str, job: str, **extra) -> dict:
    """``submit_candidate`` of ``job`` (``extra``: e.g. the ``selection`` rule): the lock is a note outside a decision."""
    return call_tool(client, "submit_candidate", {"project": scene.pid, "command_key": key, "investigation_id": inv,
                                                  "job_id": job, "statement": f"{job} improves", "scope": SCOPE, **extra})


def _two_calls(scene: Scene) -> list[dict]:
    return [{"tool": "run_component", "args": {"component_ref": scene.trial, "environment": "lab", "purpose": "open-search",
                                               "config": {**VALIDATION, "depth": depth}}} for depth in (1, 2)]


def _run_two_call_batch(client, scene: Scene, inv: str, key: str) -> tuple[dict, list[str]]:
    """One batch of two challenger calls. The batch runs one job at a time, so it first stops after call 0 (``pending``);
    once that job is done the same key resumes it: call 0 replays and call 1 is admitted. Returns (the result, the jobs)."""
    args = {"project": scene.pid, "command_key": key, "investigation_id": inv, "wait_seconds": 0, "calls": _two_calls(scene)}
    first = call_tool(client, "run_batch", args)
    assert first["status"] == "pending" and first["next_call"] == 1, first
    job = _admitted(first["calls"][0])
    scene.finish_job(job, metrics={"accuracy": 0.50})
    done = call_tool(client, "run_batch", args)
    assert done["status"] == "done" and len(done["calls"]) == 2, done
    assert done["calls"][0].get("replayed") is True
    return done, [job, _admitted(done["calls"][1])]


def _log(scene: Scene, inv: str) -> tuple[list[dict], dict[str, int]]:
    with scene.lab.ledger.transaction() as tx:
        tx.project_id = scene.pid
        return turn_log(Ctx(tx, scene.lab.admin, scene.pid, None, scene.lab.store, {}), inv)


def _stopped_before_any_lock(receipt: dict, stop: str) -> None:
    """The batch receipt of a call refused in the finalize phase of an investigation that has no candidate."""
    assert receipt["ok"] is False, receipt
    error = receipt["error"]
    assert error["code"] == "INVALID", error
    assert f"exploration has stopped ({stop}) before any candidate was locked" in error["message"], error
    assert error["details"]["stop"] == stop and error["details"]["candidate"] == "none", error
    assert "conclude" in error["next_action"].lower(), error


def _clocked(monkeypatch) -> _Clock:
    """Wall time that can be moved: the admission check and the turn's start read the same clock."""
    clock = _Clock()
    monkeypatch.setattr("eidos.application.handlers.exploration.time", clock)
    monkeypatch.setattr("eidos.application.handlers.investigations.time", clock)
    return clock


# =====================================================================================================================
# Finding 1: MCP rounds count toward the stopping rules
# =====================================================================================================================
def test_an_mcp_batch_is_one_round_and_the_search_limit_stops_the_next_batch(scene, client):
    inv = scene.investigation()
    incumbent = scene.accuracy_job(inv, 0.70)
    _lock(client, scene, inv, "s-1", incumbent, selection={**RULE, "max_rounds": 1})  # the first lock, over MCP
    assert _exploration(scene, inv)["rounds"] == 0

    _, jobs = _run_two_call_batch(client, scene, inv, "b-round")  # both calls admitted: one round, judged as it began
    state = _exploration(scene, inv)
    assert state["rounds"] == 1 and state["max_rounds"] == 1 and state["stop"] == "search-limit"
    assert {j["id"] for j in _jobs_of(scene, inv, "open-search")} >= set(jobs)

    count = len(_jobs_of(scene, inv))
    _stopped(_only(_batch(client, scene, inv, "b-next", scene.trial, purpose="open-search", config=VALIDATION)),
             "search-limit")
    assert len(_jobs_of(scene, inv)) == count  # the refused call started nothing

    again = call_tool(client, "run_batch", {"project": scene.pid, "command_key": "b-round", "investigation_id": inv,
                                            "wait_seconds": 0, "calls": _two_calls(scene)})  # the first batch, replayed
    assert again["status"] == "done" and all(c["replayed"] for c in again["calls"]), again
    assert [_admitted(c) for c in again["calls"]] == jobs  # a replay is not judged again
    assert _exploration(scene, inv)["rounds"] == 1 and len(_jobs_of(scene, inv)) == count
    assert _admitted(_only(_batch(client, scene, inv, "b-repro", scene.trial, purpose="reproduction")))


def test_a_direct_mcp_run_component_is_its_own_round_and_the_search_limit_stops_the_next(scene, client):
    inv = scene.investigation()
    _lock(client, scene, inv, "s-1", scene.accuracy_job(inv, 0.70), selection={**RULE, "max_rounds": 1})

    def direct(key: str, depth: int) -> dict:  # run_component itself, not run_batch
        return call_tool(client, "run_component", {"project": scene.pid, "command_key": key, "investigation_id": inv,
                                                   "component_ref": scene.trial, "environment": "lab",
                                                   "purpose": "open-search", "config": {**VALIDATION, "depth": depth}})
    job = direct("d-1", 1)["result"]["job_id"]  # round 1 is admitted
    state = _exploration(scene, inv)
    assert state["rounds"] == 1 and state["stop"] == "search-limit"
    log, _ = _log(scene, inv)
    assert [e["jobs"] for e in log if e["source"] == "direct" and job in e["jobs"]] == [[job]]  # its own round entry

    count = len(_jobs_of(scene, inv))
    with pytest.raises(ToolFailure) as caught:
        direct("d-2", 2)
    assert caught.value.code == "INVALID" and "exploration has stopped (search-limit)" in caught.value.message
    assert len(_jobs_of(scene, inv)) == count


def test_patience_counts_an_mcp_round_once_it_is_scored_and_stops_the_next_batch(scene, client):
    inv = scene.investigation(exploration={"patience_rounds": 1})
    _lock(client, scene, inv, "s-1", scene.accuracy_job(inv, 0.70), selection=RULE)
    challenger = _admitted(_only(_batch(client, scene, inv, "b-round", scene.trial, purpose="open-search",
                                        config={**VALIDATION, "depth": 2})))
    state = _exploration(scene, inv)  # the round is admitted, but not evaluated: exploration goes on
    assert state["stop"] is None and state["rounds"] == 1 and state["pending_challengers"] == [challenger]
    assert state["rounds_without_improvement"] == 0

    scene.finish_job(challenger, metrics={"accuracy": 0.50})  # scored, and it does not beat the incumbent's 0.70
    state = _exploration(scene, inv)
    assert state["stop"] == "ceiling-no-improvement" and state["rounds_without_improvement"] == 1
    _stopped(_only(_batch(client, scene, inv, "b-next", scene.trial, purpose="open-search",
                          config={**VALIDATION, "depth": 3})), "ceiling-no-improvement")


def test_the_shared_history_numbers_batches_and_locks_in_ledger_order(scene, client):
    inv = scene.investigation()
    before = scene.accuracy_job(inv, 0.70)  # work outside a decision, before any lock
    lock = _lock(client, scene, inv, "s-1", before, selection=RULE)
    _, jobs = _run_two_call_batch(client, scene, inv, "b-one")
    later = _admitted(_only(_batch(client, scene, inv, "b-two", scene.trial, purpose="open-search",
                                   config={**VALIDATION, "depth": 3})))

    log, note_turns = _log(scene, inv)
    assert [(e["turn"], e["source"], e["jobs"]) for e in log] == [
        (1, "direct", [before]), (2, "note", []), (3, "batch", jobs), (4, "batch", [later])]
    assert log[2]["batch"] == "b-one" and log[3]["batch"] == "b-two" and log[1]["note"] == lock["lock_note_id"]
    assert note_turns == {lock["lock_note_id"]: 2}  # the rounds are counted from the lock's own entry


# =====================================================================================================================
# Finding 2: measurement-development is a challenger purpose
# =====================================================================================================================
@pytest.mark.parametrize("stop", ["selection-frozen", "search-limit"])
def test_run_batch_refuses_measurement_development_once_exploration_has_stopped(scene, client, stop):
    inv = scene.investigation(**({"exploration": {"patience_rounds": 0, "max_rounds": 0}} if stop == "search-limit" else {}))
    scene.lock(inv, scene.accuracy_job(inv, 0.70), selection=RULE)
    if stop == "selection-frozen":
        _request(scene, inv, "launched")
    assert _exploration(scene, inv)["stop"] == stop
    count = len(_jobs_of(scene, inv))
    refused = _only(_batch(client, scene, inv, "b-measure", scene.trial, purpose="measurement-development"))
    _stopped(refused, stop)
    assert refused["error"]["details"]["purpose"] == "measurement-development" and len(_jobs_of(scene, inv)) == count
    assert _admitted(_only(_batch(client, scene, inv, "b-repro", scene.trial, purpose="reproduction")))


def test_run_batch_admits_measurement_development_only_to_finish_a_pending_round(scene, client):
    inv = scene.investigation(exploration={"max_rounds": 1})
    scene.rule(inv, scorer="proj.scorer")
    incumbent = _first_lock_round(scene, inv, 0.70, via_scorer=True)
    pending = _round(scene, inv, 2)
    state = _exploration(scene, inv)
    assert state["stop"] == "search-limit" and state["pending_outputs"] == [scene.output(pending)]

    def measure(key: str, artifact: str) -> dict:
        return _only(_batch(client, scene, inv, key, scene.scorer, purpose="measurement-development",
                            inputs={"predictions": artifact}, config=VALIDATION))
    job = _admitted(measure("b-pending", scene.output(pending)))  # it reads the pending challenger's output
    assert [j["id"] for j in _jobs_of(scene, inv, "measurement-development")] == [job]
    _stopped(measure("b-incumbent", scene.output(incumbent)), "search-limit")  # anything else is new exploration
    _stopped(_only(_batch(client, scene, inv, "b-new", scene.trial, purpose="measurement-development")), "search-limit")


# the decision path: _check_exploration_phase is pure over the turn spec's recorded exploration state
def _decision(purpose: str, *calls: dict, **control: Any) -> dict:
    return {"purpose": purpose, "calls": list(calls), "control": control}


def _call(tool: str = "run_component", **inputs: str) -> dict:
    return {"tool": tool, "args": {"component_ref": "proj.scorer", **({"inputs": inputs} if inputs else {})}}


FINALIZE = {"phase": "finalize", "stop": "search-limit", "pending_outputs": ["art_pending"],
            "incumbent": {"version": 1, "job_id": "job_1"}}


@pytest.mark.parametrize("tool", turn_rules.JOB_TOOLS)
def test_a_decision_with_purpose_measurement_development_is_refused_in_the_finalize_phase(tool):
    with pytest.raises(Invalid) as caught:
        _check_exploration_phase({"spec": {"exploration": FINALIZE}}, _decision("measurement-development", _call(tool)))
    assert "exploration has stopped (search-limit)" in caught.value.message
    assert caught.value.details["calls"] == [0] and caught.value.details["purpose"] == "measurement-development"


def test_a_measurement_development_decision_that_finishes_a_pending_round_or_starts_nothing_is_accepted():
    turn = {"spec": {"exploration": FINALIZE}}
    _check_exploration_phase(turn, _decision("measurement-development", _call(predictions="art_pending")))
    _check_exploration_phase(turn, _decision("measurement-development", {"tool": "record_note", "args": {}}))
    _check_exploration_phase(turn, _decision("reproduction", _call()))
    _check_exploration_phase({"spec": {"exploration": {**FINALIZE, "phase": "explore", "stop": None}}},
                             _decision("measurement-development", _call()))  # still exploring: nothing to refuse


# =====================================================================================================================
# Finding 3: the hold-back before the first lock
# =====================================================================================================================
def test_the_wall_holdback_before_any_lock_refuses_a_new_challenger_but_not_a_reproduction(scene, client, monkeypatch):
    clock = _clocked(monkeypatch)
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000)  # no lock: the baseline phase
    wall = scene.q("get_investigation", {"investigation_id": inv})["body"]["wall"]
    until = wall["deadline_unix"] - wall["conclude_threshold_seconds"] - 540.0  # 15% of the wall is held back
    assert _admitted(_only(_batch(client, scene, inv, "b-before", scene.trial, purpose="open-search")))
    assert _exploration(scene, inv)["phase"] == "baseline"

    clock.offset = until - time.time() + 5  # the hold-back has begun; the reserve for the conclusion has not
    state = _exploration(scene, inv)
    assert (state["phase"], state["stop"], state["candidate"], state["incumbent"]) == ("finalize", "wall-holdback", "none", None)
    count = len(_jobs_of(scene, inv))
    for purpose in ("open-search", "hypothesis-test", "measurement-development"):
        _stopped_before_any_lock(_only(_batch(client, scene, inv, f"b-{purpose}", scene.trial, purpose=purpose)),
                                 "wall-holdback")
    assert len(_jobs_of(scene, inv)) == count
    assert _admitted(_only(_batch(client, scene, inv, "b-repro", scene.trial, purpose="reproduction")))


@pytest.mark.parametrize(("per_decision", "stop"), [pytest.param(9_000, "token-holdback", id="decision-does-not-fit"),
                                                    pytest.param(8_000, None, id="decision-fits")])
def test_the_token_holdback_before_any_lock(scene, client, per_decision, stop):
    # 10 000 model tokens, 1 500 held back: one decision of 9 000 does not fit in what is left, one of 8 000 does
    inv = scene.investigation(limits=TOKEN_LIMITS, settings={"max_tokens_per_decision": per_decision})
    state = _exploration(scene, inv)
    refused = _only(_batch(client, scene, inv, "b-open", scene.trial, purpose="open-search"))
    if stop is None:
        assert state["phase"] == "baseline" and "candidate" not in state and _admitted(refused)
        return
    assert (state["phase"], state["stop"], state["candidate"]) == ("finalize", stop, "none")
    _stopped_before_any_lock(refused, stop)
    assert _admitted(_only(_batch(client, scene, inv, "b-repro", scene.trial, purpose="reproduction")))


def test_an_exploratory_job_before_any_lock_is_capped_at_the_exploration_deadline(scene, client):
    inv = scene.investigation(wall_seconds=3600, turn_deadline_seconds=5000)
    wall = scene.q("get_investigation", {"investigation_id": inv})["body"]["wall"]
    until = wall["deadline_unix"] - wall["conclude_threshold_seconds"] - 540.0  # 15% of the 3600 s wall
    assert _exploration(scene, inv)["phase"] == "baseline"

    started = time.time()
    capped = _admitted(_only(_batch(client, scene, inv, "b-wall", scene.trial, purpose="open-search", wall_seconds=3000)))
    ended = time.time()
    assert until - ended - 1 <= _lock_wall(scene, capped) <= until - started < 3000  # not the reserve's ~3240 s
    short = _admitted(_only(_batch(client, scene, inv, "b-short", scene.trial, purpose="open-search", wall_seconds=60)))
    assert _lock_wall(scene, short) == 60  # a cap never raises a job's own limit
    repro = _admitted(_only(_batch(client, scene, inv, "b-repro", scene.trial, purpose="reproduction", wall_seconds=3000)))
    assert _lock_wall(scene, repro) == 3000  # finalization work may use the hold-back


# the decision path
def test_a_decision_in_the_finalize_phase_before_any_lock_is_refused_as_having_no_candidate():
    state = {"phase": "finalize", "stop": "wall-holdback", "incumbent": None, "candidate": "none"}
    with pytest.raises(Invalid) as caught:
        _check_exploration_phase({"spec": {"exploration": state}}, _decision("open-search", _call()))
    assert "before any candidate was locked" in caught.value.message and "wall-holdback" in caught.value.message
    assert caught.value.details["candidate"] == "none" and caught.value.details["incumbent"] is None
    _check_exploration_phase({"spec": {"exploration": state}}, _decision("reproduction", _call()))


def test_the_live_holdback_stops_a_turn_that_began_before_the_first_lock():
    spec = {"exploration": {"phase": "baseline", "stop": None, "incumbent": None},
            "completion": {"exploration_deadline_unix": time.time() - 1}}
    turn = {"spec": spec}
    assert _live_exploration_stop(None, None, turn) == "wall-holdback"  # the deadline passed: no ledger read needed
    with pytest.raises(Invalid) as caught:
        _check_exploration_phase(turn, _decision("open-search", _call()), live_stop="wall-holdback")
    assert "before any candidate was locked" in caught.value.message and caught.value.details["candidate"] == "none"
    assert _live_exploration_stop(None, None, {"spec": {**spec, "exploration": {"phase": "finalize"}}}) is None
