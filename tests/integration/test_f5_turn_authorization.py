"""F5 (ADR 0017 addendum "Turn and slot authorization"): which TurnWorkflow run may act on a turn, and which attempt of
a decision slot, against PostgreSQL. Temporal is not involved: commands are sent directly and the investigator
activities run under ``temporalio.testing.ActivityEnvironment`` with an explicit ``workflow_run_id`` and ``activity_id``.

The turn's owner is the TurnWorkflow run that began it (``begin_turn`` with ``run_id``). A resume by another run bumps
``turn_authorization.generation`` and revokes the accepted decisions of earlier runs that were never handed off
(``decision_handoff``, written by ``hand_off_decision``, the first step of ``execute_decision``).

* C1: a workflow terminated while its decide activity was accepting a decision, then a new execution of the turn. The
  acceptance may commit after the termination, before or after the resume; either way it is never executed, and every
  decision step of the old run (begin_decision, reserve_model_budget, accept_decision, hand_off_decision,
  complete_turn) is refused from the resume on.
* C2: a resumed run's decide activity starts again at attempt 1, so reservation refs carry the slot epoch
  (``model:<turn>:<slot>:<attempt>:e<epoch>``): reconciliation and late settlements never cross executions.
* C3: the slot's authorization never moves back: a delayed begin of an EARLIER attempt of the same execution is told it
  is stale and changes nothing.
"""

from __future__ import annotations

import pytest
from test_cancel_through_acceptance import (
    ACT,
    EXECUTION,
    Recording,
    _accept_payload,
    _begin,
    _decisions,
    _env,
    _jobs,
    _turn,
    _wf,
)
from test_turn_concurrency import Background, held
from test_turn_recovery import setup
from test_uncertain_model_usage import KNOWN, KNOWN_TOKENS, OTHER, RESERVED, Slot, _usage

from eidos.domain.errors import DomainError

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

RUN_A, RUN_B = "run-A", "run-B"
EXEC_A, EXEC_B = EXECUTION, f"{RUN_B}/{ACT}"  # EXECUTION is "run-A/decide-slot-0"


# -- helpers --------------------------------------------------------------------------------------------------------
def _resume_activity(acts, pid, inv, run, index: int = 2) -> dict:
    """The begin_turn activity of a (new) TurnWorkflow run: it resumes the open turn and takes its authorization."""
    return _env(1, run=run, activity_id="begin-turn").run(
        acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": index})


def _resume(lab, wf, pid, inv, run) -> dict:
    return lab.cmd(wf, "begin_turn", {"investigation_id": inv, "run_id": run}, project=pid)["result"]


def _record(lab, pid, kind, rid) -> dict | None:
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        rec = tx.get_record(kind, rid)
        return dict(rec.body) if rec is not None else None


def _events(lab, pid, inv, etype) -> list[dict]:
    return lab.q(lab.admin, "list_events", {"investigation_id": inv, "types": [etype]}, project=pid)["events"]


def _turn_view(lab, pid, tid) -> dict:
    return lab.q(lab.admin, "get_turn", {"turn_id": tid}, project=pid)


def _model_tokens(lab, pid, inv) -> dict:
    return lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]


def _revoked(lab, pid, tid) -> list[str]:
    return [d["id"] for d in _decisions(lab, pid, tid) if d.get("revoked")]


def _accepted(lab, pid, tid) -> list[dict]:
    return [d for d in _decisions(lab, pid, tid) if d["accepted"]]


def _spy(acts) -> None:
    acts.wf, acts.inv = Recording(acts.wf), Recording(acts.inv)


def _clear(acts) -> None:
    acts.wf.log.clear()
    acts.inv.log.clear()


def _reserve(lab, wf, pid, tid, execution, attempt, epoch, slot: int = 0) -> dict:
    return lab.cmd(wf, "reserve_model_budget", {"turn_id": tid, "slot": slot, "attempt": attempt, "epoch": epoch,
                                                "estimate": {"model_tokens": RESERVED}, "execution": execution},
                   project=pid)["result"]


# -- C1: termination during acceptance, then a new execution ------------------------------------------------------------
def test_acceptance_committed_by_a_terminated_run_is_revoked_by_the_resume_and_never_executed(lab):
    """Run-A's decide reaches accept_decision and the acceptance commits (its workflow was terminated meanwhile, so
    nobody ever executes the decision). Run-B resumes the turn: the decision is revoked at the resume; run-B never
    recovers it, and run-A's execute_decision for it executes nothing."""
    pid, inv, acts, base = _turn(lab)  # run-A begins the turn through the begin_turn activity
    tid = base["turn_id"]
    assert _record(lab, pid, "turn_authorization", tid) == {"turn_id": tid, "run": RUN_A, "generation": 1,
                                                            "previous_run": None}
    _spy(acts)
    first = _env(1).run(acts.decide, {**base, "slot": 0})
    assert first["accepted"] is True and first["n_calls"] == 2
    old = first["decision_id"]
    assert _revoked(lab, pid, tid) == []

    b = _resume_activity(acts, pid, inv, RUN_B, index=2)
    assert b["started"] is True and b["resumed"] is True and b["turn_id"] == tid
    auth = _record(lab, pid, "turn_authorization", tid)
    assert auth == {"turn_id": tid, "run": RUN_B, "generation": 2, "previous_run": RUN_A}

    decs = {d["id"]: d for d in _decisions(lab, pid, tid)}
    assert decs[old]["accepted"] is True and decs[old]["revoked"] is True
    revoked = _events(lab, pid, inv, "decision.revoked")
    assert [(e["subject"], e["body"]["reason"]) for e in revoked] == [(old, "superseded-execution")]
    resumed = _events(lab, pid, inv, "turn.resumed")
    assert [(e["subject"], e["body"]["generation"], e["body"]["revoked_decisions"]) for e in resumed] == [(tid, 2, [old])]

    # Run-B's slot 0 is not recovered: the revoked decision still occupies the slot (a decision is never updated, and
    # the slot accepts no other), so the slot yields nothing and the turn goes on; slot 1 decides afresh.
    _clear(acts)
    again = _env(1, run=RUN_B).run(acts.decide, {**base, "slot": 0})
    assert again == {"accepted": False, "decision_id": old, "reason": "attempt-revoked", "n_calls": 0}
    assert acts.wf.log == ["begin_decision"] and acts.inv.log == []  # no reservation, no model request, no acceptance
    nxt = _env(1, run=RUN_B, activity_id="decide-slot-1").run(acts.decide, {**base, "slot": 1})
    assert nxt["accepted"] is True and nxt["decision_id"] != old and nxt.get("reason") != "recovered"
    assert nxt["n_calls"] == 2  # decided afresh: the revoked decision is not part of what this turn did

    # Run-A's execute_decision (a zombie, or an activity that outlived its workflow) executes nothing.
    _clear(acts)
    out = _env(1, run=RUN_A, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": old})
    assert out == {"summary": [], "skipped": "decision-revoked"}
    assert acts.inv.log == [] and "hand_off_decision" not in acts.wf.log
    assert _jobs(lab, pid, inv) == []
    assert _record(lab, pid, "decision_handoff", old) is None
    assert _record(lab, pid, "decision_receipts", old) is None
    wf = _wf(lab, pid)
    for run, why in ((RUN_A, "superseded-execution"), (RUN_B, "decision-revoked")):  # nor can anyone hand it off
        h = lab.cmd(wf, "hand_off_decision", {"decision_id": old, "execution": f"{run}/exec"}, project=pid)["result"]
        assert h == {"decision_id": old, "handed_off": False, "reason": why}

    # The owner executes its own fresh decision, exactly once.
    ex = _env(1, run=RUN_B, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": nxt["decision_id"]})
    assert [r["ok"] for r in ex["summary"]] == [True, True]
    assert len(_jobs(lab, pid, inv)) == 1
    assert _record(lab, pid, "decision_handoff", nxt["decision_id"])["run"] == RUN_B


def test_decide_of_a_run_that_lost_the_turn_begins_nothing_and_leaves_the_slot_alone(lab):
    """A zombie decide attempt of run-A after run-B resumed (here begun with the SAME index: the begin key is per run):
    begin_decision returns ``superseded`` without touching the slot's epoch; the activity stops with no reservation and
    no model request."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    assert _resume_activity(acts, pid, inv, RUN_B, index=1)["resumed"] is True  # same index as run-A's begin
    assert _record(lab, pid, "turn_authorization", tid)["run"] == RUN_B
    wf = _wf(lab, pid)
    eb = _begin(lab, wf, pid, tid, 1, execution=EXEC_B)["epoch"]
    slot = _record(lab, pid, "decision_attempt", f"{tid}:0")
    assert slot["execution"] == EXEC_B and slot["epoch"] == eb == 1

    _spy(acts)
    out = _env(1).run(acts.decide, {**base, "slot": 0})  # run-A
    assert out == {"accepted": False, "decision_id": None, "reason": "superseded:superseded-execution", "n_calls": 0}
    assert acts.wf.log == ["begin_decision"] and acts.inv.log == []
    assert _record(lab, pid, "decision_attempt", f"{tid}:0") == slot  # the epoch did not move
    # Run-B's attempt is unaffected.
    ok = _env(1, run=RUN_B).run(acts.decide, {**base, "slot": 0})
    assert ok["accepted"] is True and ok["n_calls"] == 2


def test_run_that_lost_the_turn_cannot_hand_off_or_execute_a_decision_of_the_new_run(lab):
    """The hand-off is the owner's: a zombie run-A (an old execute_decision activity) is refused even for a decision
    run-B accepted and handed off, executes no effect, and gets the same answer for any retry."""
    pid, inv, acts, base = _turn(lab)
    wf = _wf(lab, pid)
    _resume_activity(acts, pid, inv, RUN_B)
    dec = _env(1, run=RUN_B).run(acts.decide, {**base, "slot": 0})["decision_id"]
    _spy(acts)
    skipped = {"summary": [], "skipped": "superseded-execution"}

    assert _env(1, run=RUN_A, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec}) == skipped
    assert acts.inv.log == [] and acts.wf.log == ["hand_off_decision"]
    assert _jobs(lab, pid, inv) == [] and _record(lab, pid, "decision_handoff", dec) is None

    ex = _env(1, run=RUN_B, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert [r["ok"] for r in ex["summary"]] == [True, True] and len(_jobs(lab, pid, inv)) == 1
    assert _record(lab, pid, "decision_handoff", dec)["run"] == RUN_B

    # The owner has handed it off; run-A is still refused (owner check first), by command and by activity.
    _clear(acts)
    h = lab.cmd(wf, "hand_off_decision", {"decision_id": dec, "execution": f"{RUN_A}/exec2"}, project=pid)["result"]
    assert h == {"decision_id": dec, "handed_off": False, "reason": "superseded-execution"}
    assert _env(1, run=RUN_A, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec}) == skipped
    assert acts.inv.log == [] and len(_jobs(lab, pid, inv)) == 1
    # The owner's repeated hand-off is idempotent.
    again = lab.cmd(wf, "hand_off_decision", {"decision_id": dec, "execution": f"{RUN_B}/exec2"}, project=pid)["result"]
    assert again == {"decision_id": dec, "handed_off": True, "by": RUN_B}


def test_acceptance_of_the_old_run_waiting_on_the_resume_is_refused(lab):
    """The race order the other way, with real concurrent transactions: run-B's begin_turn (the resume) holds the turn
    row and commits first; run-A's accept_decision, blocked on that row, then sees the new owner and is refused. Nothing
    is accepted for the slot by run-A."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]  # run-A begins slot 0 (and its decide is now proposing)
    with held(lab, wf, pid, "begin_turn", {"investigation_id": inv, "run_id": RUN_B}) as r:
        assert r["resumed"] is True and r["authorization"]["generation"] == 2
        accept = Background(lab, wf, pid, "accept_decision", _accept_payload(tid, 1, e1))
        accept.assert_waiting()
    out = accept.finish()
    assert out["accepted"] is False and out["reason"] == "superseded-execution", out
    assert _accepted(lab, pid, tid) == [] and _revoked(lab, pid, tid) == []
    stored = [d for d in _decisions(lab, pid, tid) if d["id"] == out["decision_id"]]
    assert len(stored) == 1 and stored[0]["accepted"] is False and stored[0]["body"]["reason"] == "superseded-execution"
    assert [e["body"]["reason"] for e in _events(lab, pid, inv, "decision.unapplied")] == ["superseded-execution"]
    # Run-B decides the slot normally.
    eb = _begin(lab, wf, pid, tid, 1, execution=EXEC_B)
    assert "revoked" not in eb and "decision" not in eb and eb["epoch"] == e1 + 1
    ok = lab.cmd(wf, "accept_decision", _accept_payload(tid, 1, eb["epoch"], EXEC_B), project=pid)["result"]
    assert ok["accepted"] is True


def test_resume_waiting_on_the_old_runs_uncommitted_acceptance_revokes_it(lab):
    """The first order, with real concurrent transactions: run-A's acceptance has run but not committed when run-B
    resumes; begin_turn waits for the turn row, then sees the accepted decision (never handed off) and revokes it."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    with held(lab, wf, pid, "accept_decision", _accept_payload(tid, 1, e1)) as r:
        assert r["accepted"] is True, r
        resume = Background(lab, wf, pid, "begin_turn", {"investigation_id": inv, "run_id": RUN_B})
        resume.assert_waiting()
    out = resume.finish()
    assert out["resumed"] is True and out["authorization"]["run"] == RUN_B
    assert _revoked(lab, pid, tid) == [r["decision_id"]]
    assert [(e["subject"], e["body"]["reason"]) for e in _events(lab, pid, inv, "decision.revoked")] == [
        (r["decision_id"], "superseded-execution")]
    h = lab.cmd(wf, "hand_off_decision", {"decision_id": r["decision_id"], "execution": f"{RUN_A}/exec"},
                project=pid)["result"]
    assert h["handed_off"] is False


def test_turn_begun_without_a_run_id_is_claimed_by_the_first_resume_and_its_unhanded_decision_is_revoked_at_begin(lab):
    """A turn that has no authorization record (begun before F5, or by a caller that gives no run id) has no owner: the
    first run to begin it claims it without revoking anything. Its first begin_decision then revokes a decision that
    another execution accepted and never handed off (the fallback of the resume-time revocation)."""
    pid, inv, acts = setup(lab)
    wf = _wf(lab, pid)
    b = lab.cmd(wf, "begin_turn", {"investigation_id": inv, "runtime_release": "test"}, project=pid)["result"]
    tid = b["turn_id"]
    assert "authorization" not in b and _record(lab, pid, "turn_authorization", tid) is None
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]  # run-A, unchecked: the turn has no owner yet
    acc = lab.cmd(wf, "accept_decision", _accept_payload(tid, 1, e1), project=pid)["result"]
    assert acc["accepted"] is True

    r = _resume(lab, wf, pid, inv, RUN_B)  # the first claim
    assert r["resumed"] is True and r["authorization"] == {"turn_id": tid, "run": RUN_B, "generation": 1,
                                                           "previous_run": None}
    assert _revoked(lab, pid, tid) == []  # nothing is revoked by a first claim
    out = _begin(lab, wf, pid, tid, 1, execution=EXEC_B)
    assert out["revoked"] == acc["decision_id"] and "decision" not in out and out["epoch"] == e1 + 1
    assert _revoked(lab, pid, tid) == [acc["decision_id"]]
    assert [(e["subject"], e["body"]["reason"]) for e in _events(lab, pid, inv, "decision.revoked")] == [
        (acc["decision_id"], "superseded-execution")]
    h = lab.cmd(wf, "hand_off_decision", {"decision_id": acc["decision_id"], "execution": f"{RUN_A}/exec"},
                project=pid)["result"]
    assert h["handed_off"] is False and _record(lab, pid, "decision_handoff", acc["decision_id"]) is None


@pytest.mark.parametrize("executed", [False, True], ids=["handed-off-only", "executed"])
def test_decision_handed_off_before_the_resume_is_recovered_by_the_new_run_and_not_revoked(lab, executed):
    """Once run-A handed the decision off (it may have run part of it), the resumed turn recovers it: run-B's decide
    returns it with reason "recovered", it is never revoked, and the owner continues it (one admission, not two)."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    old = _env(1).run(acts.decide, {**base, "slot": 0})["decision_id"]
    if executed:
        ex = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": old})
        assert [r["ok"] for r in ex["summary"]] == [True, True] and len(_jobs(lab, pid, inv)) == 1
    else:  # run-A died right after the hand-off, before any effect
        h = lab.cmd(wf, "hand_off_decision", {"decision_id": old, "execution": f"{RUN_A}/exec"}, project=pid)["result"]
        assert h == {"decision_id": old, "handed_off": True, "by": RUN_A}
        assert _jobs(lab, pid, inv) == []

    assert _resume_activity(acts, pid, inv, RUN_B)["resumed"] is True
    assert _revoked(lab, pid, tid) == [] and _events(lab, pid, inv, "decision.revoked") == []
    assert [e["body"]["revoked_decisions"] for e in _events(lab, pid, inv, "turn.resumed")] == [[]]

    rec = _env(1, run=RUN_B).run(acts.decide, {**base, "slot": 0})
    assert rec["accepted"] is True and rec["reason"] == "recovered" and rec["decision_id"] == old
    assert rec["n_calls"] == 2
    out = _env(1, run=RUN_B, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": old})
    assert [r["ok"] for r in out["summary"]] == [True, True]
    if executed:
        assert all(r["replayed"] for r in out["summary"])  # the persisted command keys: nothing runs twice
    assert len(_jobs(lab, pid, inv)) == 1
    assert _record(lab, pid, "decision_handoff", old)["run"] == RUN_A  # the original hand-off stands
    assert _record(lab, pid, "decision_receipts", old) is not None


def test_run_that_handed_off_and_then_lost_the_turn_executes_nothing_more(lab):
    """Run-A handed the decision off, then lost the turn to run-B before any effect: a retry of run-A's execution is
    refused (the owner check comes first), while run-B, the owner, continues the handed-off decision."""
    pid, inv, acts, base = _turn(lab)
    wf = _wf(lab, pid)
    old = _env(1).run(acts.decide, {**base, "slot": 0})["decision_id"]
    assert lab.cmd(wf, "hand_off_decision", {"decision_id": old, "execution": f"{RUN_A}/exec"},
                   project=pid)["result"]["handed_off"] is True
    _resume_activity(acts, pid, inv, RUN_B)
    _spy(acts)
    out = _env(1, run=RUN_A, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": old})
    assert out == {"summary": [], "skipped": "superseded-execution"}
    assert acts.inv.log == [] and _jobs(lab, pid, inv) == []
    h = lab.cmd(wf, "hand_off_decision", {"decision_id": old, "execution": f"{RUN_A}/exec3"}, project=pid)["result"]
    assert h == {"decision_id": old, "handed_off": False, "reason": "superseded-execution"}
    ex = _env(1, run=RUN_B, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": old})
    assert [r["ok"] for r in ex["summary"]] == [True, True] and len(_jobs(lab, pid, inv)) == 1


def test_complete_turn_of_a_run_that_lost_the_turn_leaves_it_open_and_the_owner_closes_it(lab):
    """Run-A's late complete_turn (its workflow's ``finally``) after run-B resumed does not close the turn, nor settle
    its model reservations under the new owner; run-B's completion does, once."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    assert _resume_activity(acts, pid, inv, RUN_B, index=1)["resumed"] is True
    eb = _begin(lab, wf, pid, tid, 1, execution=EXEC_B)["epoch"]
    _reserve(lab, wf, pid, tid, EXEC_B, 1, eb)  # run-B's decision is in flight
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert tx.open_reservations(f"inv:{inv}", f"model:{tid}:") == {"model_tokens": RESERVED}

    r = lab.cmd(wf, "complete_turn", {"turn_id": tid, "stop_reason": "cancelled", "execution": f"{RUN_A}/complete"},
                project=pid)["result"]
    assert r == {"turn_id": tid, "status": "open", "superseded": "superseded-execution"}
    assert _env(1, run=RUN_A, activity_id="complete").run(acts.complete_turn, {**base, "stop_reason": "cancelled"}) \
        == {"status": "open"}
    assert _turn_view(lab, pid, tid)["status"] == "open" and _events(lab, pid, inv, "turn.completed") == []
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert tx.open_reservations(f"inv:{inv}", f"model:{tid}:") == {"model_tokens": RESERVED}  # still run-B's
    assert _model_tokens(lab, pid, inv)["uncertain"] == 0

    done = _env(1, run=RUN_B, activity_id="complete").run(acts.complete_turn, {**base, "stop_reason": "waiting"})
    assert done == {"status": "completed"}
    view = _turn_view(lab, pid, tid)
    assert view["status"] == "completed" and view["result"]["stop_reason"] == "waiting"
    assert view["result"]["uncertain_model_reservations"] == {"model_tokens": RESERVED}  # run-B's, closed by run-B
    assert len(_events(lab, pid, inv, "turn.completed")) == 1


def test_reserve_model_budget_of_a_run_that_lost_the_turn_raises_and_reserves_nothing(lab):
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]  # run-A begins slot 0, then its workflow is terminated
    _resume(lab, wf, pid, inv, RUN_B)
    for epoch in (e1, None):  # with the epoch of its begin_decision, and as a caller that reports attempts only
        with pytest.raises(DomainError) as err:
            _reserve(lab, wf, pid, tid, EXEC_A, 1, epoch)
        assert err.value.details["reason"] == "superseded-execution"
    assert _model_tokens(lab, pid, inv)["reserved"] == 0
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert tx.open_reservations(f"inv:{inv}", f"model:{tid}:") == {}  # no reservation entry was ever written
    # The owner reserves normally (its begin_decision moved the epoch on).
    eb = _begin(lab, wf, pid, tid, 1, execution=EXEC_B)["epoch"]
    rr = _reserve(lab, wf, pid, tid, EXEC_B, 1, eb)
    assert rr["reservation_ref"] == f"model:{tid}:0:1:e{eb}" and rr["reserved"] == {"model_tokens": RESERVED}
    assert _model_tokens(lab, pid, inv)["reserved"] == RESERVED


def test_begin_decision_of_the_old_run_waiting_on_the_resume_is_refused_without_bumping_the_epoch(lab):
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    before = _record(lab, pid, "decision_attempt", f"{tid}:0")
    with held(lab, wf, pid, "begin_turn", {"investigation_id": inv, "run_id": RUN_B}):
        late = Background(lab, wf, pid, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 2,
                                                           "execution": EXEC_A})
        late.assert_waiting()
    out = late.finish()
    assert out == {"turn_id": tid, "slot": 0, "superseded": "superseded-execution"}
    assert _record(lab, pid, "decision_attempt", f"{tid}:0") == before and before["epoch"] == e1


# -- C2: two executions both at attempt 1 ------------------------------------------------------------------------------
def _claim(s: Slot, run: str) -> dict:
    """``run`` begins (or resumes) the slot's turn: it owns the turn from now on."""
    r = s.lab.cmd(s.wf, "begin_turn", {"investigation_id": s.inv, "run_id": run}, project=s.pid)["result"]
    assert r["started"] and r["turn_id"] == s.tid
    return r


def _begin_slot(s: Slot, execution: str, attempt: int, slot: int = 0) -> dict:
    return s.lab.cmd(s.wf, "begin_decision", {"turn_id": s.tid, "slot": slot, "attempt": attempt,
                                              "execution": execution}, project=s.pid)["result"]


def _ref_entries(s: Slot, ref: str) -> list[tuple[str, float]]:
    return [(e["kind"], float(e["amount"])) for e in s.raw_entries(ref=ref)]


def _settle_ref(s: Slot, ref: str, usage: dict) -> dict:
    return s.lab.cmd(s.wf, "record_model_usage", {"turn_id": s.tid, "usage": usage, "reserved": {"model_tokens": RESERVED},
                                                  "reservation_ref": ref}, project=s.pid)["result"]


def _two_executions(lab) -> tuple[Slot, str, str]:
    """Run-A begins slot 0 (epoch 1) and reserves, then dies; run-B resumes the turn, begins slot 0 (epoch 2, attempt 1
    again) and reserves. Returns the slot and the two reservation refs."""
    s = Slot(lab)
    s.hold()
    assert _claim(s, RUN_A)["started"]
    ea = _begin_slot(s, EXEC_A, 1)
    assert ea["epoch"] == 1
    ra = _reserve(lab, s.wf, s.pid, s.tid, EXEC_A, 1, ea["epoch"])
    ref_a = f"model:{s.tid}:0:1:e1"
    assert ra == {"reserved": {"model_tokens": RESERVED}, "reservation_ref": ref_a}
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": 0, "uncertain": 0}

    assert _claim(s, RUN_B)["resumed"] is True
    eb = _begin_slot(s, EXEC_B, 1)  # attempt 1 again, but another execution: not stale
    assert "superseded" not in eb and eb["epoch"] == 2
    rb = _reserve(lab, s.wf, s.pid, s.tid, EXEC_B, 1, eb["epoch"])
    ref_b = f"model:{s.tid}:0:1:e2"
    assert rb == {"reserved": {"model_tokens": RESERVED}, "reservation_ref": ref_b}
    assert ref_a != ref_b
    return s, ref_a, ref_b


def test_two_executions_at_attempt_1_reserve_under_distinct_refs_and_settle_independently(lab):
    s, ref_a, ref_b = _two_executions(lab)
    # Run-B's reserve reconciled run-A's open reservation as provisional uncertain usage, under run-A's ref only.
    assert _ref_entries(s, ref_a) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.raw_entries(ref=ref_a)[1]["detail"] == {"provisional": True, "reconciled_by": ref_b}
    assert _ref_entries(s, ref_b) == [("reserve", RESERVED)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": RESERVED, "uncertain": RESERVED}
    assert s.open_reservations() == {"model_tokens": RESERVED}  # run-B's only

    # Run-B closes the turn: its open reservation is charged as provisional, under ITS ref; run-A's is not touched.
    done = s.lab.cmd(s.wf, "complete_turn", {"turn_id": s.tid, "stop_reason": "calls-exhausted",
                                             "execution": f"{RUN_B}/complete"}, project=s.pid)["result"]
    assert done["status"] == "completed"
    assert s.turn_result()["uncertain_model_reservations"] == {"model_tokens": RESERVED}
    assert _ref_entries(s, ref_a) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert _ref_entries(s, ref_b) == [("reserve", RESERVED), ("possible-usage", RESERVED)]
    assert s.raw_entries(ref=ref_b)[1]["detail"] == {"provisional": True, "reconciled_by": "complete_turn"}
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": 2 * RESERVED, "uncertain": 2 * RESERVED}
    assert s.open_reservations() == {"model_tokens": 0.0}

    # Run-A's late settlement (its request finished at the provider after all): replaces only run-A's provisional charge.
    _settle_ref(s, ref_a, _usage(KNOWN))
    assert _ref_entries(s, ref_a) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                                      ("usage", KNOWN_TOKENS)]
    assert _ref_entries(s, ref_b) == [("reserve", RESERVED), ("possible-usage", RESERVED)]  # untouched
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": RESERVED + KNOWN_TOKENS, "uncertain": RESERVED}

    # Run-B's own late settlement then replaces its own, again nothing else.
    _settle_ref(s, ref_b, _usage({"input_tokens": 1000, "output_tokens": 0}))
    assert _ref_entries(s, ref_b) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                                      ("usage", 1000)]
    assert _ref_entries(s, ref_a) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                                      ("usage", KNOWN_TOKENS)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS + 1000, "uncertain": 0}


def test_late_settlement_of_the_old_execution_never_releases_the_new_executions_open_reservation(lab):
    s, ref_a, ref_b = _two_executions(lab)
    _settle_ref(s, ref_a, _usage(KNOWN))  # run-A's request reports while run-B's decision is still in flight
    assert _ref_entries(s, ref_a) == [("reserve", RESERVED), ("possible-usage", RESERVED), ("correction", -RESERVED),
                                      ("usage", KNOWN_TOKENS)]
    assert _ref_entries(s, ref_b) == [("reserve", RESERVED)]
    assert s.open_reservations() == {"model_tokens": RESERVED}
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER + RESERVED, "used": KNOWN_TOKENS, "uncertain": 0}
    # Run-B settles normally: its own usage only, its own remainder released.
    _settle_ref(s, ref_b, _usage({"input_tokens": 1000, "output_tokens": 0}))
    assert _ref_entries(s, ref_b) == [("reserve", RESERVED), ("usage", 1000), ("release", RESERVED - 1000)]
    assert s.budget() == {"limit_amount": 1e6, "reserved": OTHER, "used": KNOWN_TOKENS + 1000, "uncertain": 0}
    assert s.open_reservations() == {"model_tokens": 0.0}


# -- C3: a delayed begin of an earlier attempt --------------------------------------------------------------------------
def test_delayed_begin_of_an_earlier_attempt_is_stale_and_changes_nothing(lab):
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    first = lab.cmd(wf, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 1, "execution": EXEC_A},
                    project=pid, key="begin-attempt-1")
    assert first["result"]["epoch"] == 1
    assert _begin(lab, wf, pid, tid, 2)["epoch"] == 2
    before = _record(lab, pid, "decision_attempt", f"{tid}:0")
    assert before["attempt"] == 2 and before["epoch"] == 2 and before["execution"] == EXEC_A

    # A delayed duplicate of attempt 1's begin request under another idempotency key (not a cached receipt).
    late = _begin(lab, wf, pid, tid, 1)
    assert late == {"turn_id": tid, "slot": 0, "superseded": "stale-attempt", "current_attempt": 2}
    assert _record(lab, pid, "decision_attempt", f"{tid}:0") == before  # still attempt 2, epoch 2, same started_at
    # The same request replayed under its ORIGINAL key is served from the receipt cache: still no side effect, and the
    # epoch it returns (1) is refused at the reservation and the acceptance (the F2c rule).
    replay = lab.cmd(wf, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 1, "execution": EXEC_A},
                     project=pid, key="begin-attempt-1")
    assert replay["result"]["epoch"] == 1
    assert _record(lab, pid, "decision_attempt", f"{tid}:0") == before
    with pytest.raises(DomainError) as err:
        _reserve(lab, wf, pid, tid, EXEC_A, 1, 1)
    assert err.value.details["reason"] == "superseded-attempt"
    stale = lab.cmd(wf, "accept_decision", _accept_payload(tid, 1, 1), project=pid)["result"]
    assert stale["accepted"] is False and stale["reason"] == "superseded-attempt"
    assert _accepted(lab, pid, tid) == []

    # Attempt 2 can still reserve and accept, with epoch 2.
    rr = _reserve(lab, wf, pid, tid, EXEC_A, 2, 2)
    assert rr["reservation_ref"] == f"model:{tid}:0:2:e2"
    ok = lab.cmd(wf, "accept_decision", _accept_payload(tid, 2, 2), project=pid)["result"]
    assert ok["accepted"] is True
    attempt = _accepted(lab, pid, tid)[0]["body"]["attempt"]
    assert attempt == {"execution": EXEC_A, "attempt": 2, "epoch": 2}


def test_decide_attempt_whose_begin_arrives_late_stops_as_stale_and_the_later_attempt_proceeds(lab):
    """Through the activity: attempt 2's begin_decision overtook attempt 1's (delayed) one. Attempt 1 stops with no
    reservation and no model request; attempt 2's decide activity then runs normally."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    assert _begin(lab, wf, pid, tid, 2)["epoch"] == 1
    before = _record(lab, pid, "decision_attempt", f"{tid}:0")
    _spy(acts)
    out = _env(1).run(acts.decide, {**base, "slot": 0})
    assert out == {"accepted": False, "decision_id": None, "reason": "superseded:stale-attempt", "n_calls": 0}
    assert acts.wf.log == ["begin_decision"] and acts.inv.log == []
    assert _record(lab, pid, "decision_attempt", f"{tid}:0") == before
    ok = _env(2).run(acts.decide, {**base, "slot": 0})
    assert ok["accepted"] is True and ok["n_calls"] == 2
    assert _accepted(lab, pid, tid)[0]["body"]["attempt"]["attempt"] == 2


def test_stale_begin_waits_for_the_later_attempts_uncommitted_begin_and_is_then_refused(lab):
    """Real concurrent transactions: attempt 2's begin_decision has run but not committed when attempt 1's delayed begin
    arrives; it waits for the turn row, then sees attempt 2 and is told it is stale. The slot stays at attempt 2."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    with held(lab, wf, pid, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 2, "execution": EXEC_A}) as r:
        assert r["epoch"] == e1 + 1
        late = Background(lab, wf, pid, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 1,
                                                           "execution": EXEC_A})
        late.assert_waiting()
    out = late.finish()
    assert out == {"turn_id": tid, "slot": 0, "superseded": "stale-attempt", "current_attempt": 2}
    rec = _record(lab, pid, "decision_attempt", f"{tid}:0")
    assert rec["attempt"] == 2 and rec["epoch"] == e1 + 1
    ok = lab.cmd(wf, "accept_decision", _accept_payload(tid, 2, e1 + 1), project=pid)["result"]
    assert ok["accepted"] is True


def test_a_resuming_runs_first_attempt_is_not_stale_against_the_old_runs_later_attempt(lab):
    """Staleness is per execution: the resumed run's decide starts again at attempt 1 and must still begin, while the
    old run's later attempts are refused as a lost turn (not as stale)."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    assert _begin(lab, wf, pid, tid, 3)["epoch"] == 1  # run-A got to its third attempt of slot 0
    _resume(lab, wf, pid, inv, RUN_B)
    b = _begin(lab, wf, pid, tid, 1, execution=EXEC_B)
    assert "superseded" not in b and b["epoch"] == 2
    rec = _record(lab, pid, "decision_attempt", f"{tid}:0")
    assert rec["execution"] == EXEC_B and rec["attempt"] == 1 and rec["epoch"] == 2
    zombie = _begin(lab, wf, pid, tid, 4)  # run-A
    assert zombie == {"turn_id": tid, "slot": 0, "superseded": "superseded-execution"}
    assert _record(lab, pid, "decision_attempt", f"{tid}:0") == rec
