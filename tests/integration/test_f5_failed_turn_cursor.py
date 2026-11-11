"""F5 regression tests: the failed-turn policy (ADR 0019) against the real command service and PostgreSQL.

* A1: ``mark_needs_input`` returns the ``cursor`` of its needs-input wait (the seq of its ``investigation.needs_input``
  event) and stores it in the mark; marking again refreshes it to the current event horizon. ``check_news`` with
  ``{"operator_only": True, "after": cursor}`` counts only operator events after both the last turn's horizon and the
  cursor, so an operator event behind the mark never ends the wait (the old behaviour ended every wait at once).
* A2: ``record_turn_retry`` (and ``mark_needs_input`` with code ``turn-failed``) closes the investigation's last turn if
  it is still open (a turn workflow that timed out left it open): failed, ``stop_reason`` error, ``failure.abandoned``,
  open model reservations charged as uncertain usage, turns reservation settled. The next ``begin_turn`` is a NEW turn
  that reserves the turns budget, not a resumption of the abandoned one; a late ``accept_decision`` is refused.

Everything runs at the command-handler level (no Temporal): the activities are called directly with in-process clients.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import ActivityEnvironment
from test_turn_recovery import ShimClient

from eidos.domain.errors import BudgetExhausted
from eidos.investigator.activities import InvestigatorActivities

pytestmark = [pytest.mark.pg]

RESERVED = 120_000
LIMITS = {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100, "turns": 5}
FAILURE = {"error_type": "turn-workflow-failed", "retryable": True}


class World:
    """One project with a committed charter and one investigation (with a turns budget), driven by the same commands
    the coordinator's activities send."""

    def __init__(self, lab, *, limits: dict | None = None):
        self.lab = lab
        self.pid = f"p{uuid.uuid4().hex[:8]}"
        lab.cmd(lab.admin, "create_project", {"project_id": self.pid})
        self.charter = f"charter:s{self.pid}"
        lab.cmd(lab.admin, "commit_study_charter", {"charter": {"study_id": f"s{self.pid}", "question": "q",
                                                                "exploration_only": True}}, project=self.pid)
        self.inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "charter_id": self.charter,
                                                              "limits": limits or LIMITS}, project=self.pid)[
            "result"]["investigation_id"]
        self.wf = lab.principal(f"wf-{self.pid}", ["workflow"], [self.pid])
        self.scope = f"inv:{self.inv}"
        self.n = 0

    # -- commands as the coordinator and its activities send them ------------------------------------------------------
    def wfcmd(self, name: str, payload: dict) -> dict:
        return self.lab.cmd(self.wf, name, payload, project=self.pid)["result"]

    def begin(self) -> dict:
        return self.wfcmd("begin_turn", {"investigation_id": self.inv, "runtime_release": "test"})

    def reserve(self, tid: str, tokens: float = RESERVED, slot: int = 0, attempt: int = 1) -> dict:
        return self.wfcmd("reserve_model_budget", {"turn_id": tid, "slot": slot, "attempt": attempt,
                                                   "estimate": {"model_tokens": tokens}})

    def complete(self, tid: str, stop_reason: str = "calls-exhausted") -> dict:
        return self.wfcmd("complete_turn", {"turn_id": tid, "stop_reason": stop_reason})

    def retry(self, **extra) -> dict:
        return self.wfcmd("record_turn_retry", {"investigation_id": self.inv, "attempt": 1, "max_attempts": 2,
                                                "delay_seconds": 0, "failure": dict(FAILURE), **extra})

    def mark(self, code: str = "turn-failed", **extra) -> dict:
        return self.wfcmd("mark_needs_input", {"investigation_id": self.inv, "code": code, "reason": "r", **extra})

    # -- operator events -----------------------------------------------------------------------------------------------
    def steer(self) -> int:
        """An operator steer (it clears a needs-input mark); returns the seq of its event."""
        self.n += 1
        self.lab.cmd(self.lab.admin, "steer_investigation",
                     {"investigation_id": self.inv, "instruction": f"try something else {self.n}"}, project=self.pid)
        return self.events("investigation.steered")[-1]["seq"]

    def amend_charter(self) -> int:
        """A charter amendment: an operator event that does NOT clear a needs-input mark; returns its seq."""
        self.n += 1
        self.lab.cmd(self.lab.admin, "amend_study_charter", {
            "charter_id": self.charter, "rationale": "reframe",
            "charter": {"study_id": f"s{self.pid}", "question": f"q{self.n}", "exploration_only": True}},
            project=self.pid)
        return self.events("charter.amended")[-1]["seq"]

    # -- what the ledger says ------------------------------------------------------------------------------------------
    def events(self, *types: str) -> list[dict]:
        out = self.lab.q(self.lab.admin, "list_events", {"investigation_id": self.inv, "limit": 500,
                                                         **({"types": list(types)} if types else {})},
                         project=self.pid)["events"]
        return out

    def shown(self) -> dict:
        return self.lab.q(self.lab.admin, "get_investigation", {"investigation_id": self.inv}, project=self.pid)

    def turn(self, tid: str) -> dict:
        return self.lab.q(self.lab.admin, "get_turn", {"turn_id": tid}, project=self.pid)

    def budget(self, dim: str) -> dict:
        return self.lab.q(self.lab.admin, "budgets", {"investigation_id": self.inv}, project=self.pid)["budgets"][dim]

    def entries(self, dim: str, ref_prefix: str) -> list[dict]:
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            return [e for e in tx.budget_entries(self.scope)
                    if e["dimension"] == dim and str(e["ref"]).startswith(ref_prefix)]

    def open_reservations(self, tid: str) -> dict[str, float]:
        with self.lab.ledger.transaction() as tx:
            tx.project_id = self.pid
            return {k: v for k, v in tx.open_reservations(self.scope, f"model:{tid}:").items() if v > 0}

    # -- the check_news activity ---------------------------------------------------------------------------------------
    def activities(self) -> InvestigatorActivities:
        return InvestigatorActivities(ShimClient(self.lab, self.lab.principal(f"inv-{self.pid}", ["investigator"],
                                                                              [self.pid])),
                                      ShimClient(self.lab, self.wf))

    def news(self, *, after: int | None = None, operator_only: bool = True) -> dict:
        a = {"investigation_id": self.inv, "project_id": self.pid, "operator_only": operator_only,
             **({"after": after} if after is not None else {})}
        return ActivityEnvironment().run(self.activities().check_news, a)


# -- A1: the needs-input cursor ----------------------------------------------------------------------------------------

def test_stale_operator_news_does_not_end_a_new_needs_input_wait(lab):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    w.complete(t1)
    horizon = w.shown()["turns"][0]["horizon"]
    stale = w.steer()  # an operator event after the last turn's horizon, before the failure
    assert stale > horizon

    r = w.mark("turn-failed", details={"turn_id": t1, "failure": dict(FAILURE), "retryable": False})
    assert r["already"] is False
    cursor = r["cursor"]
    assert cursor > stale
    # The cursor is the seq of the mark's own event and is stored in the mark (label ``needs_input.cursor``).
    assert [e["seq"] for e in w.events("investigation.needs_input")] == [cursor]
    assert r["needs_input"]["cursor"] == cursor and w.shown()["needs_input"]["cursor"] == cursor

    # The old coordinator checked from the last turn's horizon: the stale steer was news on every check.
    assert w.news()["news"] is True
    behind = w.news(after=cursor)
    assert behind["news"] is False and behind["after"] == cursor and behind["after_horizon"] == horizon

    # A new operator event after the cursor ends the wait.
    fresh = w.steer()
    assert fresh > cursor
    assert w.news(after=cursor)["news"] is True


def test_check_news_counts_only_events_after_both_the_horizon_and_the_cursor(lab):
    w = World(lab)
    old = w.steer()
    t1 = w.begin()["turn_id"]  # its horizon is past the steer
    w.complete(t1)
    horizon = w.shown()["turns"][0]["horizon"]
    assert horizon >= old
    # An ``after`` behind the horizon cannot make older events news again: the later of the two applies.
    r = w.news(after=old - 1)
    assert r["news"] is False and r["after"] == horizon
    # ``after`` is only a lower bound; operator events beyond it still count, and without it the horizon applies.
    later = w.amend_charter()
    assert later > horizon
    assert w.news(after=old - 1)["news"] is True
    assert w.news()["news"] is True
    assert w.news(after=later)["news"] is False


def test_marking_again_refreshes_the_cursor_past_an_operator_event_that_keeps_the_mark(lab):
    w = World(lab)
    first = w.mark("turn-failed", details={"failure": dict(FAILURE)})
    assert first["already"] is False
    # A charter amendment ends nothing: the investigation keeps its mark (only steer, resume and stop clear it).
    amended = w.amend_charter()
    assert amended > first["cursor"]
    assert w.shown()["needs_input"]["cursor"] == first["cursor"]
    assert w.news(after=first["cursor"])["news"] is True  # behind the stale cursor it still looks like news

    again = w.mark("coordinator-stopped", reason="the next wait")
    assert again["already"] is True
    assert again["cursor"] >= amended and again["cursor"] > first["cursor"]
    assert again["needs_input"]["cursor"] == again["cursor"] == w.shown()["needs_input"]["cursor"]
    # Refreshing keeps the original mark (code, reason) and appends no second needs-input event.
    assert again["needs_input"]["code"] == "turn-failed"
    assert len(w.events("investigation.needs_input")) == 1
    assert w.news(after=again["cursor"])["news"] is False
    # ... and an operator event after the refreshed cursor is news again.
    assert w.amend_charter() > again["cursor"]
    assert w.news(after=again["cursor"])["news"] is True


def test_charter_attachment_is_an_operator_event_that_keeps_the_mark(lab):
    """The other event the task names: attaching a charter does not clear the mark either, so the refresh matters."""
    w = World(lab)
    first = w.mark("coordinator-stopped")
    lab.cmd(lab.admin, "commit_study_charter", {"charter": {"study_id": f"t{w.pid}", "question": "q2",
                                                            "exploration_only": True},
                                                "investigation_id": w.inv}, project=w.pid)
    attached = w.events("investigation.charter_attached")[-1]["seq"]
    assert attached > first["cursor"] and w.shown()["needs_input"]
    assert w.news(after=first["cursor"])["news"] is True
    again = w.mark("coordinator-stopped")
    assert again["already"] is True and again["cursor"] >= attached
    assert w.news(after=again["cursor"])["news"] is False


def test_the_needs_input_activity_returns_the_cursor_the_workflow_waits_on(lab):
    """``_after_failed_turn`` reads ``cursor`` from the activity's result: it must come through the activity."""
    w = World(lab)
    acts, env = w.activities(), ActivityEnvironment()
    r = env.run(acts.mark_needs_input, {"investigation_id": w.inv, "project_id": w.pid, "turn_counter": 3,
                                        "code": "turn-failed", "reason": "r",
                                        "details": {"failure": dict(FAILURE)}})
    assert r["already"] is False and isinstance(r["cursor"], int)
    assert [e["seq"] for e in w.events("investigation.needs_input")] == [r["cursor"]]
    assert w.news(after=r["cursor"])["news"] is False


# -- A2: an abandoned turn is closed, never resumed -------------------------------------------------------------------

def test_record_turn_retry_closes_the_abandoned_turn_and_the_retry_is_a_new_turn(lab):
    w = World(lab)
    b1 = w.begin()
    t1 = b1["turn_id"]
    assert b1["started"] and b1["spec"]["seq"] == 1 and "resumed" not in b1
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (1.0, 0.0)
    w.reserve(t1)  # a decide attempt reserved model budget, then the turn workflow timed out
    assert w.open_reservations(t1) == {"model_tokens": RESERVED}

    # No failed_turn_id: the child's timeout never produced a result, so the coordinator does not know the turn.
    retry = w.retry()
    assert retry["closed_abandoned_turn"] == t1 and retry["failed_turn_id"] == t1
    assert retry["failure"] == FAILURE and retry["attempt"] == 1 and retry["max_attempts"] == 2

    turn = w.turn(t1)
    assert turn["status"] == "failed"
    assert turn["result"]["stop_reason"] == "error"
    assert turn["result"]["failure"] == {**FAILURE, "abandoned": True}
    # The open model reservation was charged as uncertain usage (never released as zero), nothing stays open.
    assert turn["result"]["uncertain_model_reservations"] == {"model_tokens": RESERVED}
    assert w.open_reservations(t1) == {}
    tokens = w.budget("model_tokens")
    assert (tokens["reserved"], tokens["used"], tokens["uncertain"]) == (0.0, float(RESERVED), float(RESERVED))
    [charge] = [e for e in w.entries("model_tokens", f"model:{t1}:") if e["kind"] == "possible-usage"]
    assert float(charge["amount"]) == RESERVED and charge["detail"]["provisional"] is True
    # The turns reservation was settled like any closing turn.
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (0.0, 1.0)
    # The ledger shows what happened: turn.completed (failed, with the failure) and the scheduled retry.
    assert [e["body"]["status"] for e in w.events("turn.completed")] == ["failed"]
    assert w.events("turn.completed")[0]["body"]["failure"] == {**FAILURE, "abandoned": True}
    assert w.events("investigation.turn_retry_scheduled")[0]["body"]["closed_abandoned_turn"] == t1

    # The retry is a NEW turn (seq 2, not resumed) that reserves the turns budget again.
    b2 = w.begin()
    assert b2["started"] and "resumed" not in b2
    assert b2["turn_id"] == f"turn_{w.inv}_2" != t1 and b2["spec"]["seq"] == 2
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (1.0, 1.0)
    assert w.turn(b2["turn_id"])["status"] == "open"
    # begin_turn stays idempotent for the open new turn (a retried activity resumes it, as before).
    again = w.begin()
    assert again["resumed"] is True and again["turn_id"] == b2["turn_id"]

    # A late acceptance from the abandoned workflow is refused, and so is its late completion.
    late = w.wfcmd("accept_decision", {"turn_id": t1, "slot": 0, "proposal": {"rationale": "late", "calls": [],
                                                                               "control": {"action": "conclude"}}})
    assert late["accepted"] is False and late["reason"] == "turn-closed"
    done = w.complete(t1)
    assert done["already_closed"] is True and done["status"] == "failed"
    assert w.turn(t1)["status"] == "failed" and w.turn(b2["turn_id"])["status"] == "open"
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (1.0, 1.0)


def test_the_retry_cannot_exceed_the_turns_budget_when_the_abandoned_turn_used_one(lab):
    """The resumed turn used to run for free; now each retry costs a turn, so the budget bounds the retries."""
    w = World(lab, limits={**LIMITS, "turns": 2})
    t1 = w.begin()["turn_id"]
    w.retry()  # closes t1: turns used 1
    t2 = w.begin()
    assert t2["started"] and t2["spec"]["seq"] == 2
    w.retry()  # closes t2: turns used 2 of 2
    assert w.turn(t2["turn_id"])["status"] == "failed"
    assert w.turn(t1)["status"] == "failed"
    with pytest.raises(BudgetExhausted):
        w.begin()
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (0.0, 2.0)


def test_record_turn_retry_through_the_activity_closes_the_turn_and_begin_turn_starts_a_new_one(lab):
    w = World(lab)
    acts, env = w.activities(), ActivityEnvironment()
    base = {"investigation_id": w.inv, "project_id": w.pid}
    b1 = env.run(acts.begin_turn, {**base, "index": 1})
    assert b1["started"] and b1["resumed"] is False
    r = env.run(acts.record_turn_retry, {**base, "turn_counter": 1, "failed_turn_id": None, "attempt": 1,
                                         "max_attempts": 2, "delay_seconds": 0, "failure": dict(FAILURE)})
    assert r["closed_abandoned_turn"] == b1["turn_id"]
    b2 = env.run(acts.begin_turn, {**base, "index": 2})
    assert b2["started"] and b2["resumed"] is False and b2["turn_id"] != b1["turn_id"]
    assert [t["status"] for t in w.shown()["turns"]] == ["failed", "open"]


def test_marking_turn_failed_closes_an_open_turn_and_a_later_begin_turn_starts_a_new_one(lab):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    w.reserve(t1)
    details = {"turn_id": t1, "retryable": False,
               "failure": {"error_type": "TimeoutError", "retryable": False, "message": "turn workflow timed out"}}
    r = w.mark("turn-failed", details=details)
    assert r["already"] is False and r["cursor"] > 0

    turn = w.turn(t1)
    assert turn["status"] == "failed" and turn["result"]["stop_reason"] == "error"
    # The coordinator's own failure details are kept; the turn is marked as abandoned.
    assert turn["result"]["failure"] == {**details["failure"], "abandoned": True}
    assert turn["result"]["uncertain_model_reservations"] == {"model_tokens": RESERVED}
    assert w.open_reservations(t1) == {}
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (0.0, 1.0)
    assert w.shown()["needs_input"]["code"] == "turn-failed" and w.shown()["turns"][0]["stop_reason"] == "error"

    # An operator resumes (clears the mark); the next turn is new, not the abandoned one.
    w.lab.cmd(w.lab.admin, "resume_investigation", {"investigation_id": w.inv}, project=w.pid)
    assert not w.shown()["needs_input"]
    b2 = w.begin()
    assert b2["started"] and "resumed" not in b2 and b2["turn_id"] == f"turn_{w.inv}_2" and b2["spec"]["seq"] == 2
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (1.0, 1.0)
    assert w.turn(t1)["status"] == "failed"


def test_marking_turn_failed_without_failure_details_still_closes_the_open_turn(lab):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    w.mark("turn-failed")  # no details at all
    turn = w.turn(t1)
    assert turn["status"] == "failed"
    assert turn["result"]["failure"] == {"error_type": "turn-workflow-failed", "retryable": True, "abandoned": True}


def test_marking_again_closes_the_next_abandoned_turn_and_refreshes_the_cursor(lab):
    """An amendment ended the first wait without clearing the mark; the coordinator's next turn failed and was left
    open too. The second mark closes it (A2) and moves the cursor past the amendment (A1)."""
    w = World(lab)
    t1 = w.begin()["turn_id"]
    first = w.mark("turn-failed", details={"failure": dict(FAILURE)})
    assert w.turn(t1)["status"] == "failed"
    amended = w.amend_charter()
    t2 = w.begin()
    assert t2["started"] and t2["spec"]["seq"] == 2  # a mark is a label: the investigation is still active
    again = w.mark("turn-failed", details={"failure": dict(FAILURE)})
    assert again["already"] is True and again["cursor"] >= amended and again["cursor"] > first["cursor"]
    assert w.turn(t2["turn_id"])["status"] == "failed"
    assert w.turn(t2["turn_id"])["result"]["failure"]["abandoned"] is True
    assert w.news(after=again["cursor"])["news"] is False


@pytest.mark.parametrize("code", ["refused-proposals", "coordinator-stopped"])
def test_other_needs_input_codes_leave_an_open_turn_alone(lab, code):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    r = w.mark(code)
    assert r["cursor"] > 0
    assert w.turn(t1)["status"] == "open"
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (1.0, 0.0)


# -- A2: nothing to close ---------------------------------------------------------------------------------------------

def test_record_turn_retry_is_a_noop_when_the_last_turn_is_already_closed(lab):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    failure = {"activity": "decide", "error_type": "MODEL_CALL_FAILED", "retryable": True}
    w.wfcmd("complete_turn", {"turn_id": t1, "stop_reason": "error", "failure": failure})
    before = w.turn(t1)
    turns_before = w.budget("turns")
    retry = w.retry(failed_turn_id=t1, failure=failure)
    assert "closed_abandoned_turn" not in retry and retry["failed_turn_id"] == t1 and retry["failure"] == failure
    after = w.turn(t1)
    assert after["status"] == "failed" and after["result"] == before["result"]  # not closed a second time
    assert "abandoned" not in after["result"]["failure"]
    assert w.budget("turns") == turns_before
    assert len(w.events("turn.completed")) == 1
    assert "closed_abandoned_turn" not in w.events("investigation.turn_retry_scheduled")[0]["body"]
    # The next turn is new, as before.
    assert w.begin()["spec"]["seq"] == 2


def test_record_turn_retry_with_no_turn_at_all_is_a_noop(lab):
    w = World(lab)
    retry = w.retry()
    assert "closed_abandoned_turn" not in retry and retry["failed_turn_id"] is None
    assert w.events("turn.completed") == []


def test_record_turn_retry_on_a_completed_turn_does_not_touch_it(lab):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    w.complete(t1)
    retry = w.retry(failed_turn_id=t1)
    assert "closed_abandoned_turn" not in retry
    assert w.turn(t1)["status"] == "completed" and w.turn(t1)["result"]["stop_reason"] == "calls-exhausted"
    assert (w.budget("turns")["reserved"], w.budget("turns")["used"]) == (0.0, 1.0)


def test_an_explicit_failed_turn_id_is_kept_when_an_open_turn_is_also_closed(lab):
    w = World(lab)
    t1 = w.begin()["turn_id"]
    retry = w.retry(failed_turn_id="turn_reported_by_the_child")
    assert retry["failed_turn_id"] == "turn_reported_by_the_child" and retry["closed_abandoned_turn"] == t1
    assert w.turn(t1)["status"] == "failed"
