"""F5d (review R9), against PostgreSQL: an accepted decision whose control is ``conclude`` (or ``stop``) must not
overwrite an operator's stop or pause. Temporal is not involved: commands are sent directly and the investigator
activities run under ``temporalio.testing.ActivityEnvironment`` with an explicit ``workflow_run_id`` and
``activity_id`` (see test_f5c_ticket_and_handoff.py).

The conclusion of an investigation is applied by ``complete_turn`` (``_close_turn``). It used to put the investigation
record into ``concluded`` whenever the closing turn's control was conclude/stop, whatever state the investigation was
in, so a stop or pause that committed after the decision was accepted (before or after its effects ran) was lost.
Now the conclusion is applied only to an investigation that is still ``active``, checked under its row lock (lock
order: the turn row, then the investigation row, the order every other command uses):

* the investigation keeps the operator's status (stopped, paused) and gets no ``conclusion``;
* the turn closes as ``cancelled`` (a ``concluded`` stop reason becomes ``cancelled``) with
  ``result.control_refused == "investigation-<status>"``, and its ``turn.completed`` event carries the same field;
* the closing is idempotent: a second ``complete_turn`` finds the turn closed (``already_closed``) and changes nothing.

The TurnWorkflow part (an execution refused by a stop is a cancellation and the decision's control is never acted
on) is tested with Temporal in test_f5d_refused_execution.py. The sequences here are the ones that workflow produces:
a refused execution is followed by ``complete_turn`` with stop reason ``cancelled``; an execution that completed is
followed by ``complete_turn`` with stop reason ``concluded`` and the decision's control.
"""

from __future__ import annotations

import pytest
from test_cancel_through_acceptance import _env, _turn, _wf
from test_f5_turn_authorization import RUN_A, _events, _record, _turn_view
from test_f5c_ticket_and_handoff import CLAIMS, STOP_OR_PAUSE, _accept, _claims, _operator
from test_turn_concurrency import Background, held

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

ACTIONS = ["conclude", "stop"]
SUMMARY = "the decision's own conclusion"
SCOPE = {"m": 1}


def _control(action: str) -> dict:
    return {"action": action, "summary": SUMMARY, "scope": SCOPE}


def _with_calls(action: str) -> dict:
    """A decision with two effect calls whose control then concludes (or stops) the investigation."""
    return {"rationale": "file two claims, then conclude", "calls": CLAIMS["calls"], "control": _control(action)}


def _without_calls(action: str) -> dict:
    return {"rationale": "nothing to run; conclude", "calls": [], "control": _control(action)}


# -- helpers --------------------------------------------------------------------------------------------------------
def _investigation(lab, pid, inv) -> dict:
    return lab.q(lab.admin, "get_record", {"kind": "investigation", "id": inv}, project=pid)


def _complete(lab, pid, tid, stop_reason: str, control: dict, *, key: str | None = None, run: str = RUN_A) -> dict:
    """The lab command of the complete_turn activity, as the TurnWorkflow's run ``run`` (the turn's owner) sends it."""
    return lab.cmd(_wf(lab, pid), "complete_turn", {"turn_id": tid, "stop_reason": stop_reason, "control": control,
                                                    "receipts": [], "execution": f"{run}/complete"},
                   project=pid, key=key)


def _execute(acts, base, dec: str) -> dict:
    """The real execute_decision activity, as run-A's TurnWorkflow runs it."""
    return _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})


def _assert_conclusion_refused(lab, pid, inv, tid, status: str, control: dict) -> None:
    """The operator's state stands: nothing was concluded, and the turn closed as cancelled with the reason why."""
    rec = _investigation(lab, pid, inv)
    assert rec["status"] == status
    assert "conclusion" not in rec["body"]
    assert rec["body"]["control"]["status"] == status  # the operator's own control record is untouched

    turn = _turn_view(lab, pid, tid)
    assert turn["status"] == "cancelled"
    assert turn["result"]["stop_reason"] == "cancelled"
    assert turn["result"]["control_refused"] == f"investigation-{status}"
    assert turn["result"]["control"] == control  # what the decision asked for stays on record, unapplied

    (done,) = _events(lab, pid, inv, "turn.completed")
    assert done["subject"] == tid
    assert done["body"]["stop_reason"] == "cancelled" and done["body"]["status"] == "cancelled"
    assert done["body"]["control_refused"] == f"investigation-{status}"


def _assert_concluded(lab, pid, inv, tid, action: str) -> None:
    rec = _investigation(lab, pid, inv)
    assert rec["status"] == "concluded"
    assert rec["body"]["conclusion"] == {"turn_id": tid, "summary": SUMMARY, "scope": SCOPE, "action": action}
    turn = _turn_view(lab, pid, tid)
    assert turn["status"] == "completed" and turn["result"]["stop_reason"] == "concluded"
    assert "control_refused" not in turn["result"]
    (done,) = _events(lab, pid, inv, "turn.completed")
    assert done["body"]["stop_reason"] == "concluded" and "control_refused" not in done["body"]


# =====================================================================================================================
# 1. the stop commits before the decision executes: execution is refused, the workflow completes the turn as cancelled
# =====================================================================================================================
@pytest.mark.parametrize("sent_control", ["none", "conclude"], ids=["workflow-sends-no-control", "control-also-sent"])
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_stop_or_pause_before_execution_skips_the_decision_and_the_cancelled_turn_never_concludes(
        lab, operator, status, sent_control):
    """Decision accepted (conclude, with calls); then the operator stops or pauses; execute_decision runs the real
    activity and executes nothing (``skipped``); the TurnWorkflow then completes the turn as ``cancelled``. The
    investigation stays stopped or paused with no conclusion, even if the decision's control is sent along."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    dec = _accept(lab, pid, tid, _with_calls("conclude"))["decision_id"]
    _operator(lab, pid, inv, operator)

    assert _execute(acts, base, dec) == {"summary": [], "skipped": f"investigation-{status}"}
    assert _claims(lab, pid) == [] and _record(lab, pid, "decision_receipts", dec) is None  # nothing executed

    control = _control("conclude") if sent_control == "conclude" else {}
    out = _complete(lab, pid, tid, "cancelled", control)["result"]
    assert out == {"turn_id": tid, "status": "cancelled"}
    rec = _investigation(lab, pid, inv)
    assert rec["status"] == status and "conclusion" not in rec["body"]
    turn = _turn_view(lab, pid, tid)
    assert turn["status"] == "cancelled" and turn["result"]["stop_reason"] == "cancelled"
    if sent_control == "conclude":  # defensive: the conclusion is refused whatever stop reason came with it
        assert turn["result"]["control_refused"] == f"investigation-{status}"
    else:
        assert "control_refused" not in turn["result"]
    assert _claims(lab, pid) == []


# =====================================================================================================================
# 2. the stop commits after the effects ran, before the turn is completed
# =====================================================================================================================
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_stop_or_pause_after_execution_before_completion_keeps_the_operators_state(lab, operator, status, action):
    """Decision (two effects, then conclude/stop) accepted and fully executed (effects committed, receipts recorded);
    the stop or pause then commits; the TurnWorkflow's complete_turn (reason ``concluded``) is refused its conclusion.
    The turn closes as cancelled, and the workflow can see that: the complete_turn activity reports ``cancelled``."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    dec = _accept(lab, pid, tid, _with_calls(action))["decision_id"]
    out = _execute(acts, base, dec)
    assert [r["ok"] for r in out["summary"]] == [True, True] and "stopped" not in out and "skipped" not in out
    assert _claims(lab, pid) == ["claim one", "claim two"] and _record(lab, pid, "decision_receipts", dec) is not None

    _operator(lab, pid, inv, operator)  # commits between the effects and the completion
    done = _env(1, activity_id="complete").run(acts.complete_turn, {**base, "stop_reason": "concluded",
                                                                    "control": _control(action), "receipts": []})
    assert done == {"status": "cancelled"}  # what TurnWorkflow turns into its own ``cancelled`` result

    _assert_conclusion_refused(lab, pid, inv, tid, status, _control(action))
    assert _claims(lab, pid) == ["claim one", "claim two"]  # the effects that ran are not undone: they have receipts

    # The operator keeps control: resuming gives an active investigation, still not concluded.
    _operator(lab, pid, inv, "resume_investigation")
    rec = _investigation(lab, pid, inv)
    assert rec["status"] == "active" and "conclusion" not in rec["body"]


# =====================================================================================================================
# 3. a decision with no calls: nothing to execute, so the stop races the completion itself
# =====================================================================================================================
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_stop_or_pause_before_completion_of_a_decision_without_calls_keeps_the_operators_state(
        lab, operator, status, action):
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    acc = _accept(lab, pid, tid, _without_calls(action))
    assert acc["calls"] == [] and acc["control"]["action"] == action

    _operator(lab, pid, inv, operator)  # the workflow never executes a decision without calls; it completes the turn
    out = _complete(lab, pid, tid, "concluded", _control(action))["result"]
    assert out == {"turn_id": tid, "status": "cancelled"}
    _assert_conclusion_refused(lab, pid, inv, tid, status, _control(action))


# =====================================================================================================================
# 4. control: no operator action, the conclusion is applied
# =====================================================================================================================
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("with_calls", [False, True], ids=["no-calls", "with-calls"])
def test_active_investigation_still_concludes(lab, action, with_calls):
    """Guards against over-refusal: the same sequences with no stop or pause conclude the investigation."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    proposal = _with_calls(action) if with_calls else _without_calls(action)
    dec = _accept(lab, pid, tid, proposal)["decision_id"]
    if with_calls:
        out = _execute(acts, base, dec)
        assert [r["ok"] for r in out["summary"]] == [True, True] and "stopped" not in out and "skipped" not in out
        assert _claims(lab, pid) == ["claim one", "claim two"]
        done = _env(1, activity_id="complete").run(acts.complete_turn, {**base, "stop_reason": "concluded",
                                                                        "control": _control(action), "receipts": []})
        assert done == {"status": "completed"}
    else:
        assert _complete(lab, pid, tid, "concluded", _control(action))["result"] == {"turn_id": tid,
                                                                                    "status": "completed"}
    _assert_concluded(lab, pid, inv, tid, action)


def test_other_controls_are_not_refused_by_a_paused_investigation(lab):
    """The refusal is only for conclude/stop. A turn that ends waiting (or without any control) closes as usual under
    a pause: its stop reason and status are not rewritten and no ``control_refused`` appears."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    _operator(lab, pid, inv, "pause_investigation")
    out = _complete(lab, pid, tid, "waiting", {"action": "wait_for_jobs"})["result"]
    assert out == {"turn_id": tid, "status": "completed"}
    turn = _turn_view(lab, pid, tid)
    assert turn["result"]["stop_reason"] == "waiting" and "control_refused" not in turn["result"]
    assert _investigation(lab, pid, inv)["status"] == "paused"


# =====================================================================================================================
# 5. a redelivered completion
# =====================================================================================================================
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_redelivered_completion_after_the_refusal_changes_nothing(lab, operator, status, action):
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    _accept(lab, pid, tid, _without_calls(action))
    _operator(lab, pid, inv, operator)
    first = _complete(lab, pid, tid, "concluded", _control(action), key="complete-1")
    assert first["result"] == {"turn_id": tid, "status": "cancelled"} and first["replayed"] is False
    before = (_investigation(lab, pid, inv), _turn_view(lab, pid, tid), _events(lab, pid, inv, "turn.completed"))

    # The same command key again (a redelivered activity of the same run): the receipt of the first, nothing re-run.
    again = _complete(lab, pid, tid, "concluded", _control(action), key="complete-1")
    assert again["replayed"] is True and again["result"] == first["result"]
    # A different key (another run's completion, or an activity sent under a new key): the turn is already closed.
    other = _complete(lab, pid, tid, "concluded", _control(action), key="complete-2")["result"]
    assert other == {"turn_id": tid, "status": "cancelled", "already_closed": True}
    # And a late completion that says the turn completed normally does not reopen or rewrite it either.
    late = _complete(lab, pid, tid, "calls-exhausted", {})["result"]
    assert late == {"turn_id": tid, "status": "cancelled", "already_closed": True}

    after = (_investigation(lab, pid, inv), _turn_view(lab, pid, tid), _events(lab, pid, inv, "turn.completed"))
    assert after == before
    assert after[0]["status"] == status and "conclusion" not in after[0]["body"]
    assert len(after[2]) == 1  # one turn.completed event, however often the completion was delivered


def test_redelivered_completion_after_a_conclusion_changes_nothing(lab):
    """The same idempotence for the normal outcome: the conclusion is recorded once; a redelivered completion (another
    key) finds the turn closed and rewrites neither the status nor the conclusion."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    _accept(lab, pid, tid, _without_calls("conclude"))
    assert _complete(lab, pid, tid, "concluded", _control("conclude"))["result"]["status"] == "completed"
    _assert_concluded(lab, pid, inv, tid, "conclude")
    again = _complete(lab, pid, tid, "concluded", _control("conclude"), key="another")["result"]
    assert again == {"turn_id": tid, "status": "completed", "already_closed": True}
    _assert_concluded(lab, pid, inv, tid, "conclude")


# =====================================================================================================================
# the same race with real concurrent transactions
# =====================================================================================================================
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_completion_waiting_on_an_uncommitted_stop_or_pause_is_refused_when_it_commits(lab, operator, status):
    """The stop has run but not committed when the conclusion arrives. complete_turn takes the turn row, then waits for
    the investigation row the stop holds, and sees the stop once it committed: it refuses the conclusion, rather than
    having read ``active`` before the wait."""
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    _accept(lab, pid, tid, _without_calls("conclude"))
    payload = {"turn_id": tid, "stop_reason": "concluded", "control": _control("conclude"), "receipts": [],
               "execution": f"{RUN_A}/complete"}
    with held(lab, lab.admin, pid, operator, {"investigation_id": inv}) as r:
        assert r["status"] == status
        complete = Background(lab, _wf(lab, pid), pid, "complete_turn", payload)
        complete.assert_waiting()
    assert complete.finish() == {"turn_id": tid, "status": "cancelled"}
    _assert_conclusion_refused(lab, pid, inv, tid, status, _control("conclude"))
