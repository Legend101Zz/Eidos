"""F2c (R3 finding 1): a decide attempt cancelled or timed out while its ``accept_decision`` is in progress, then
retried, against PostgreSQL.

Temporal can give up on a decide attempt (cancellation, start-to-close or heartbeat timeout) after the activity's last
cancellation check, while its ``accept_decision`` request is on the wire. The lab cannot see Temporal's decision, so
that acceptance may commit. Two durable rules make the outcome safe whatever the interleaving:

* every attempt first calls ``begin_decision``, which bumps the slot's epoch under the turn row lock; an earlier
  attempt's ``reserve_model_budget`` or ``accept_decision`` that arrives later is refused (``superseded-attempt``);
* a decision an earlier attempt of the SAME activity execution got accepted is revoked by the retry's
  ``begin_decision``: that attempt's result never reached the workflow (Temporal retried it), so it was never executed.
  A revoked decision is never recovered and never executed, and the retry yields ``attempt-revoked`` (the turn goes on
  to its next slot). A decision accepted by another run of the turn (a resumed turn) is still recovered.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import CancelledError
from temporalio.testing import ActivityEnvironment
from test_turn_concurrency import CONCLUDE, Background, held
from test_turn_recovery import setup

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

RUN, ACT = "run-A", "decide-slot-0"
EXECUTION = f"{RUN}/{ACT}"


def _env(attempt: int, run: str = RUN, activity_id: str = ACT) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = env.info.__class__(**{**env.info.__dict__, "attempt": attempt, "workflow_run_id": run,
                                     "activity_id": activity_id})
    return env


def _turn(lab):
    pid, inv, acts = setup(lab)
    env = _env(1)
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    return pid, inv, acts, base


def _decisions(lab, pid, tid):
    return lab.q(lab.admin, "get_turn", {"turn_id": tid}, project=pid)["decisions"]


def _jobs(lab, pid, inv):
    return lab.q(lab.admin, "list_jobs", {"investigation_id": inv}, project=pid)


class Recording:
    """Wraps a lab client: records each command name; runs ``on[name]`` just before that command is sent and
    ``after[name]`` once the lab has committed it (before the activity sees the response)."""

    def __init__(self, inner):
        self.inner, self.log, self.on, self.after = inner, [], {}, {}

    def command(self, name, payload=None, **kw):
        self.log.append(name)
        if name in self.on:
            self.on[name]()
        out = self.inner.command(name, payload, **kw)
        if name in self.after:
            self.after[name]()
        return out

    def __getattr__(self, name):
        return getattr(self.inner, name)


# -- the reviewer's sequence, through the decide activity ------------------------------------------------------------
def test_acceptance_committed_after_cancellation_is_revoked_by_the_retry_and_never_executed(lab):
    """Attempt 1's accept_decision is in progress when Temporal cancels the attempt; the request still reaches the lab
    and commits. The retry (attempt 2 of the same activity) must not recover and execute it: it revokes it, makes no
    model request, and the turn's next slot decides afresh."""
    pid, inv, acts, base = _turn(lab)
    env1 = _env(1)
    acts.wf = Recording(acts.wf)
    # Temporal cancels the attempt while its acceptance request is on the wire: the lab commits the acceptance, and
    # the cancellation is raised in the activity thread before the response is used (ActivityEnvironment.cancel
    # raises it in the running sync activity, as the worker does).
    acts.wf.after["accept_decision"] = env1.cancel
    with pytest.raises(CancelledError):
        env1.run(acts.decide, {**base, "slot": 0})
    accepted = [d for d in _decisions(lab, pid, base["turn_id"]) if d["accepted"]]
    assert len(accepted) == 1 and len(accepted[0]["body"]["calls"]) == 2  # committed after the cancellation
    late_id = accepted[0]["id"]

    acts.wf.log.clear()
    acts.wf.after.clear()
    retry = _env(2).run(acts.decide, {**base, "slot": 0})
    assert retry == {"accepted": False, "decision_id": late_id, "reason": "attempt-revoked", "n_calls": 0}
    assert acts.wf.log == ["begin_decision"]  # no reservation, no model request, no acceptance

    decs = {d["id"]: d for d in _decisions(lab, pid, base["turn_id"])}
    assert decs[late_id]["accepted"] and decs[late_id]["revoked"] is True
    # Even if a workflow asked for it, the revoked decision is never executed: no effect call, no receipts.
    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": late_id})
    assert out == {"summary": [], "skipped": "decision-revoked"}
    assert _jobs(lab, pid, inv) == []
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert tx.get_record("decision_receipts", late_id) is None
    with pytest.raises(Exception, match="revoked"):  # nor can receipts be recorded for it
        lab.cmd(_wf(lab, pid), "record_decision_receipts", {"decision_id": late_id, "receipts": []}, project=pid)
    events = lab.q(lab.admin, "list_events", {"investigation_id": inv, "types": ["decision.revoked"]},
                   project=pid)["events"]
    assert [e["subject"] for e in events] == [late_id]

    # A third attempt (or a later recovery) sees the revocation again; it never resurrects the decision.
    again = _env(3).run(acts.decide, {**base, "slot": 0})
    assert again["reason"] == "attempt-revoked" and again["n_calls"] == 0

    # The turn goes on: the next slot decides afresh (its own decision), and that one runs normally.
    nxt = _env(1, activity_id="decide-slot-1").run(acts.decide, {**base, "slot": 1})
    assert nxt["accepted"] and nxt["decision_id"] != late_id


def test_retry_that_begins_while_the_old_acceptance_waits_refuses_it(lab):
    """The other interleaving, through the activity: the retry's begin_decision commits while attempt 1's
    accept_decision is still in progress, so the acceptance arrives after the epoch moved and is refused."""
    pid, inv, acts, base = _turn(lab)
    acts.wf = Recording(acts.wf)
    retry_begin: dict = {}

    def retry_begins_first():
        retry_begin.update(acts.wf.inner.command("begin_decision", {"turn_id": base["turn_id"], "slot": 0,
                                                                    "attempt": 2, "execution": EXECUTION},
                                                 project=pid, key="retry-begin")["result"])
    acts.wf.on["accept_decision"] = retry_begins_first
    first = _env(1).run(acts.decide, {**base, "slot": 0})
    assert retry_begin["epoch"] == 2 and "revoked" not in retry_begin and "decision" not in retry_begin
    assert first["accepted"] is False and first["reason"] == "superseded-attempt", first
    assert [d for d in _decisions(lab, pid, base["turn_id"]) if d["accepted"]] == []


# -- the same race at the lab, with real concurrent transactions -----------------------------------------------------
def _begin(lab, wf, pid, tid, attempt, execution=EXECUTION):
    return lab.cmd(wf, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": attempt, "execution": execution},
                   project=pid)["result"]


def _accept_payload(tid, attempt, epoch, execution=EXECUTION):
    return {"turn_id": tid, "slot": 0, "proposal": CONCLUDE, "usage_recorded": True, "attempt": attempt,
            "epoch": epoch, "execution": execution}


def _wf(lab, pid):
    return lab.principal(f"wf-{pid}", ["workflow"], [pid])


def test_acceptance_in_progress_then_retry_begins_the_retry_waits_and_revokes(lab):
    """accept_decision of attempt 1 has run but not committed when attempt 2 begins: begin_decision waits for the turn
    row, then sees the accepted decision of its own earlier attempt and revokes it."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    with held(lab, wf, pid, "accept_decision", _accept_payload(tid, 1, e1)) as r:
        assert r["accepted"] is True, r
        begin = Background(lab, wf, pid, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 2,
                                                            "execution": EXECUTION})
        begin.assert_waiting()
    out = begin.finish()
    assert out["revoked"] == r["decision_id"] and out["epoch"] == e1 + 1 and "decision" not in out
    assert [d.get("revoked") for d in _decisions(lab, pid, tid) if d["accepted"]] == [True]


def test_retry_begun_first_then_the_old_acceptance_is_refused(lab):
    """The opposite commit order: attempt 2's begin_decision holds the turn row when attempt 1's acceptance arrives;
    the acceptance waits, then sees the bumped epoch and is refused. Nothing is accepted for the slot."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    with held(lab, wf, pid, "begin_decision", {"turn_id": tid, "slot": 0, "attempt": 2, "execution": EXECUTION}) as r:
        assert r["epoch"] == e1 + 1
        accept = Background(lab, wf, pid, "accept_decision", _accept_payload(tid, 1, e1))
        accept.assert_waiting()
    out = accept.finish()
    assert out["accepted"] is False and out["reason"] == "superseded-attempt", out
    assert [d for d in _decisions(lab, pid, tid) if d["accepted"]] == []
    # The current attempt is still accepted normally.
    ok = lab.cmd(wf, "accept_decision", _accept_payload(tid, 2, e1 + 1), project=pid)["result"]
    assert ok["accepted"] is True


def test_superseded_attempt_cannot_reserve(lab):
    """A zombie attempt that reaches reserve_model_budget after a later attempt began reserves nothing (and so cannot
    reconcile the live attempt's reservation)."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    e2 = _begin(lab, wf, pid, tid, 2)["epoch"]
    live = lab.cmd(wf, "reserve_model_budget", {"turn_id": tid, "slot": 0, "attempt": 2, "epoch": e2,
                                                "estimate": {"model_tokens": 120_000}}, project=pid)["result"]
    with pytest.raises(Exception, match="superseded"):
        lab.cmd(wf, "reserve_model_budget", {"turn_id": tid, "slot": 0, "attempt": 1, "epoch": e1,
                                             "estimate": {"model_tokens": 120_000}}, project=pid)
    bud = lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]
    assert bud["reserved"] == live["reserved"]["model_tokens"] == 120_000 and bud["uncertain"] == 0


def test_decision_accepted_by_another_run_is_recovered_not_revoked(lab):
    """A resumed turn (its earlier workflow run was terminated) recovers the decision that run got accepted: that run
    may have executed it. Only attempts of the same activity execution revoke each other."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1, execution="run-old/decide")["epoch"]
    acc = lab.cmd(wf, "accept_decision", _accept_payload(tid, 1, e1, "run-old/decide"), project=pid)["result"]
    out = _begin(lab, wf, pid, tid, 1, execution="run-new/decide")
    assert out["decision"]["id"] == acc["decision_id"] and "revoked" not in out


def test_executed_decision_is_never_revoked(lab):
    """Defence in depth: a decision with recorded receipts has run; a later attempt recovers it, never revokes it."""
    pid, inv, acts, base = _turn(lab)
    tid, wf = base["turn_id"], _wf(lab, pid)
    e1 = _begin(lab, wf, pid, tid, 1)["epoch"]
    acc = lab.cmd(wf, "accept_decision", _accept_payload(tid, 1, e1), project=pid)["result"]
    lab.cmd(wf, "record_decision_receipts", {"decision_id": acc["decision_id"], "receipts": []}, project=pid)
    out = _begin(lab, wf, pid, tid, 2)
    assert out["decision"]["id"] == acc["decision_id"] and "revoked" not in out


def test_without_the_revocation_the_retry_would_recover_the_late_decision(lab):
    """Control for the first test: the same acceptance seen by an attempt of a DIFFERENT execution is recovered with
    its calls, i.e. the revocation is what keeps the cancelled attempt's decision from being executed."""
    pid, inv, acts, base = _turn(lab)
    first = _env(1).run(acts.decide, {**base, "slot": 0})
    assert first["accepted"]
    other = _env(1, run="run-B").run(acts.decide, {**base, "slot": 0})
    assert other["reason"] == "recovered" and other["decision_id"] == first["decision_id"] and other["n_calls"] == 2
