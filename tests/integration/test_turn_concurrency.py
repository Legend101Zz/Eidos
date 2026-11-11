"""Concurrent transactions on one turn (F2b review of ADR 0017), against PostgreSQL.

Each test holds one command's transaction open (its handler has run, nothing is committed yet) while a second
command runs on another connection, then checks that the second one waited for the first and saw its effect:

* stop vs decision acceptance: accept_decision locks the turn row, then the investigation row; stop locks the
  investigation row. Whichever commits first, the other sees it: no decision is accepted after a stop.
* settlement vs settlement/release of the same model reservation: every read-then-settle of a turn's model
  reservations holds the turn row lock, so a reservation is released once and never frees another holder's budget.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager

import pytest
from test_turn_recovery import setup

from eidos.application.service import COMMANDS, Ctx
from eidos.domain.errors import DomainError, Invalid

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

BLOCKED_FOR = 1.0  # the second transaction must still be waiting after this long
CONCLUDE = {"rationale": "done", "calls": [], "control": {"action": "conclude", "summary": "s"}}


@contextmanager
def held(lab, principal, pid, name, payload):
    """Run command ``name``'s handler inside a transaction that stays open (locks held) until the block exits."""
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        ctx = Ctx(tx, principal, pid, uuid.uuid4().hex, lab.store, lab.svc.settings)
        yield COMMANDS[name].fn(ctx, payload)


class Background:
    """A command executed through the service on another thread (its own connection)."""

    def __init__(self, lab, principal, pid, name, payload):
        self.result: dict | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = lab.cmd(principal, name, payload, project=pid)["result"]
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def assert_waiting(self) -> None:
        self.thread.join(BLOCKED_FOR)
        assert self.thread.is_alive(), f"did not wait for the open transaction: {self.result or self.error}"

    def finish(self) -> dict:
        self.thread.join(30)
        assert not self.thread.is_alive(), "still blocked after the other transaction committed"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _turn(lab):
    pid, inv, _ = setup(lab)
    wf = lab.principal(f"wf2-{pid}", ["workflow"], [pid])
    b = lab.cmd(wf, "begin_turn", {"investigation_id": inv, "runtime_release": "test"}, project=pid)["result"]
    assert b["started"], b
    return pid, inv, wf, b["turn_id"]


def _accepted(lab, pid, tid):
    turn = lab.q(lab.admin, "get_turn", {"turn_id": tid}, project=pid)
    return [d for d in turn["decisions"] if d["accepted"]]


def _model_tokens(lab, pid, inv):
    return lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]


# -- finding 1: stop vs accept_decision ------------------------------------------------------------------------------
def test_accept_waits_for_an_uncommitted_stop_and_then_refuses(lab):
    """The reviewer's interleaving: the stop has run but not committed when the decision is proposed. Acceptance
    waits for the stop's investigation row lock and then sees the stopped investigation."""
    pid, inv, wf, tid = _turn(lab)
    with held(lab, lab.admin, pid, "stop_investigation", {"investigation_id": inv, "running_job_policy": "drain"}) as r:
        assert r["status"] == "stopped"
        accept = Background(lab, wf, pid, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": CONCLUDE,
                                                              "attempt": 1})
        accept.assert_waiting()
    out = accept.finish()
    assert out["accepted"] is False and out["reason"] == "investigation-stopped", out
    assert _accepted(lab, pid, tid) == []


def test_stop_waits_for_an_uncommitted_acceptance(lab):
    """The other order: a decision being accepted holds the investigation row, so a stop waits until it is committed
    (accepted before the stop, a linearizable history) instead of committing in the middle of it. (Event sequencing
    alone already serializes this order; the reviewer's order is the test above, which fails without the lock.)"""
    pid, inv, wf, tid = _turn(lab)
    with held(lab, wf, pid, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": CONCLUDE}) as r:
        assert r["accepted"] is True, r
        stop = Background(lab, lab.admin, pid, "stop_investigation", {"investigation_id": inv})
        stop.assert_waiting()
    assert stop.finish()["status"] == "stopped"
    assert [d["id"] for d in _accepted(lab, pid, tid)] == [r["decision_id"]]


def test_accept_after_a_committed_stop_is_refused(lab):
    pid, inv, wf, tid = _turn(lab)
    lab.cmd(lab.admin, "stop_investigation", {"investigation_id": inv}, project=pid)
    out = lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": CONCLUDE}, project=pid)["result"]
    assert out["accepted"] is False and out["reason"] == "investigation-stopped"


def test_complete_turn_and_accept_take_the_same_lock_order(lab):
    """complete_turn (conclude) locks the turn then the investigation, like accept_decision: run against an open
    acceptance it waits on the turn row, and no deadlock occurs; the decision accepted first stays the only one."""
    pid, inv, wf, tid = _turn(lab)
    with held(lab, wf, pid, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": CONCLUDE}) as r:
        assert r["accepted"]
        done = Background(lab, wf, pid, "complete_turn", {"turn_id": tid, "stop_reason": "concluded",
                                                          "control": {"action": "conclude", "summary": "s"}})
        done.assert_waiting()
    assert done.finish()["status"] == "completed"
    assert lab.q(lab.admin, "get_record", {"kind": "investigation", "id": inv}, project=pid)["status"] == "concluded"


# -- finding 3: overlapping settlement and release of one reservation ------------------------------------------------
OTHER = 300_000.0  # budget held by another reservation of the same investigation and dimension
ENVELOPE = 120_000.0


def _reserved_turn(lab):
    pid, inv, wf, tid = _turn(lab)
    rr = lab.cmd(wf, "reserve_model_budget", {"turn_id": tid, "slot": 0, "attempt": 1,
                                              "estimate": {"model_tokens": ENVELOPE}}, project=pid)["result"]
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        tx.reserve(f"inv:{inv}", "model_tokens", OTHER, "test:other-holder")
    assert abs(_model_tokens(lab, pid, inv)["reserved"] - (OTHER + ENVELOPE)) < 1
    return pid, inv, wf, tid, rr["reservation_ref"]


def _settle(tid, ref, tokens=1000):
    return {"turn_id": tid, "usage": {"input_tokens": tokens, "output_tokens": 0}, "reserved": {"model_tokens": ENVELOPE},
            "reservation_ref": ref}


def _assert_released_once(lab, pid, inv, tid, used):
    b = _model_tokens(lab, pid, inv)
    assert abs(b["reserved"] - OTHER) < 1, b  # the other holder's budget is untouched
    assert abs(b["used"] - used) < 1, b
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert tx.open_reservations(f"inv:{inv}", f"model:{tid}:") == {"model_tokens": 0.0}


def test_complete_turn_waits_for_an_uncommitted_settlement(lab):
    """record_model_usage has settled the reservation but not committed; complete_turn's release of the turn's
    open reservations waits for it and then finds nothing open."""
    pid, inv, wf, tid, ref = _reserved_turn(lab)
    with held(lab, wf, pid, "record_model_usage", _settle(tid, ref)):
        done = Background(lab, wf, pid, "complete_turn", {"turn_id": tid, "stop_reason": "calls-exhausted"})
        done.assert_waiting()
    assert done.finish()["status"] == "completed"
    _assert_released_once(lab, pid, inv, tid, used=1000)


def test_late_settlement_waits_for_an_uncommitted_turn_release(lab):
    """complete_turn has released the reservation but not committed; a late settlement from the decide attempt waits,
    then charges its actual usage without releasing the reservation a second time."""
    pid, inv, wf, tid, ref = _reserved_turn(lab)
    with held(lab, wf, pid, "complete_turn", {"turn_id": tid, "stop_reason": "calls-exhausted"}):
        late = Background(lab, wf, pid, "record_model_usage", _settle(tid, ref))
        late.assert_waiting()
    assert late.finish()["recorded"]
    _assert_released_once(lab, pid, inv, tid, used=1000)


def test_two_overlapping_settlements_of_one_reservation_release_it_once(lab):
    """Two settlements of the same reservation (e.g. a retried record_model_usage under another command key)."""
    pid, inv, wf, tid, ref = _reserved_turn(lab)
    with held(lab, wf, pid, "record_model_usage", _settle(tid, ref, 1000)):
        second = Background(lab, wf, pid, "record_model_usage", _settle(tid, ref, 500))
        second.assert_waiting()
    assert second.finish()["recorded"]
    _assert_released_once(lab, pid, inv, tid, used=1500)  # both actual charges stay on the ledger


def test_retry_reservation_waits_for_the_earlier_attempts_settlement(lab):
    """reserve_model_budget for attempt 2 releases attempt 1's open reservation; run against attempt 1's uncommitted
    settlement it waits and then releases nothing of attempt 1."""
    pid, inv, wf, tid, ref = _reserved_turn(lab)
    with held(lab, wf, pid, "record_model_usage", _settle(tid, ref)):
        retry = Background(lab, wf, pid, "reserve_model_budget", {"turn_id": tid, "slot": 0, "attempt": 2,
                                                                  "estimate": {"model_tokens": ENVELOPE}})
        retry.assert_waiting()
    assert retry.finish()["reserved"] == {"model_tokens": ENVELOPE}
    b = _model_tokens(lab, pid, inv)
    assert abs(b["reserved"] - (OTHER + ENVELOPE)) < 1, b  # attempt 2's envelope + the other holder; nothing freed twice
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert tx.open_reservations(f"inv:{inv}", f"model:{tid}:0:1") == {"model_tokens": 0.0}


def test_record_model_usage_refuses_to_settle_another_turns_reservation(lab):
    """The turn lock only serializes this turn's reservations, so a reservation of another turn is refused."""
    pid, inv, wf, tid, ref = _reserved_turn(lab)
    with pytest.raises((Invalid, DomainError)) as err:
        lab.cmd(wf, "record_model_usage", {**_settle(tid, ref), "reservation_ref": "model:turn_other_1:0:1"},
                project=pid)
    assert "reservation_ref" in str(err.value)
    # usage without a reservation keeps its default ref and is accepted as before
    assert lab.cmd(wf, "record_model_usage", {"turn_id": tid, "usage": {"input_tokens": 5}}, project=pid)["result"]
