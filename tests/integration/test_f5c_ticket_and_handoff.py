"""F5c (review R8 #1 and #2, ADR 0017 addendum), against PostgreSQL. Temporal is not involved: commands are sent
directly, and the investigator activities run under ``temporalio.testing.ActivityEnvironment`` with an explicit
``workflow_run_id`` and ``activity_id``.

* R8 #1, ordered turn tickets. The coordinator gives every TurnWorkflow a ticket (a strictly increasing integer) and
  ``begin_turn`` carries it with the run id. The lab keeps the highest ticket that began or resumed a turn of the
  investigation (record ``turn_ticket``, under the investigation row lock) and refuses an older one
  (``superseded-run``), so a delayed ``begin_turn`` of a terminated run can neither take a turn from a newer run nor
  start a new turn after it. Callers without a run id are not fenced; a run without a ticket is refused once a
  ticketed run has begun; one ticket belongs to one run.
* R8 #2, the handoff and every effect check the locked turn and investigation. ``hand_off_decision`` is refused
  (``handed_off`` false) once the investigation is stopped or paused or the turn is closed, even when a handoff already
  exists. Each effect of a handed-off decision is sent with ``admission`` and is admitted in its own transaction
  (``EFFECT_NOT_ADMITTED``, nothing written, a replay of a committed effect still returns its receipt), so a stop that
  commits between two effects refuses the rest of the decision; ``execute_decision`` reports it as ``stopped``.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager

import pytest
from test_cancel_through_acceptance import EXECUTION, Recording, _env, _turn, _wf
from test_f5_turn_authorization import EXEC_B, RUN_A, RUN_B, _events, _record
from test_turn_concurrency import Background, held
from test_turn_recovery import setup

from eidos.application.service import COMMANDS, EFFECT_ADMISSION, Ctx
from eidos.domain.errors import EffectNotAdmitted

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

RUN_C = "run-C"
EXEC_A = EXECUTION  # "run-A/decide-slot-0"
EXEC_OWNER = f"{RUN_A}/exec"  # run-A's execute_decision activity; run-A is the run that began the turn in ``_turn``
STOP_OR_PAUSE = [("stop_investigation", "stopped"), ("pause_investigation", "paused")]
NOTE = {"rationale": "note", "calls": [{"tool": "record_note", "args": {"kind": "hypothesis", "text": "n"}}],
        "control": {}}
CLAIMS = {"rationale": "file two claims", "calls": [
    {"tool": "file_claim", "args": {"statement": "claim one", "kind": "improves", "scope": {"m": 1}}},
    {"tool": "file_claim", "args": {"statement": "claim two", "kind": "improves", "scope": {"m": 2}}}], "control": {}}


# -- helpers: turns and tickets ----------------------------------------------------------------------------------------
def _begin_turn(lab, pid, inv, run: str | None, ticket: int | None = None, key: str | None = None) -> dict:
    """The begin_turn command of a TurnWorkflow run (``run`` None: a caller that gives no run id, e.g. a tool)."""
    payload = {"investigation_id": inv, **({"run_id": run} if run else {}),
               **({"ticket": ticket} if ticket is not None else {})}
    return lab.cmd(_wf(lab, pid), "begin_turn", payload, project=pid, key=key)["result"]


def _auth(lab, pid, tid) -> dict | None:
    return _record(lab, pid, "turn_authorization", tid)


def _ticket(lab, pid, inv) -> dict | None:
    return _record(lab, pid, "turn_ticket", inv)


def _ticket_revision(lab, pid, inv) -> int:
    return lab.q(lab.admin, "get_record", {"kind": "turn_ticket", "id": inv}, project=pid)["revision"]


def _owner(tid: str, run: str, generation: int, previous: str | None) -> dict:
    return {"turn_id": tid, "run": run, "generation": generation, "previous_run": previous}


def _held(inv: str, ticket: int, run: str) -> dict:
    return {"investigation_id": inv, "ticket": ticket, "run": run}


def _turns_started(lab, pid, inv) -> list[str]:
    return [e["subject"] for e in _events(lab, pid, inv, "turn.started")]


def _start_with_turns_limit(lab, pid, turns: int = 5) -> str:
    """A second investigation of the project whose limits include ``turns``, so a started turn reserves that budget."""
    return lab.cmd(lab.admin, "start_investigation", {
        "question": "q", "charter_id": f"charter:s{pid}",
        "limits": {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100, "turns": turns},
        "investigator": {"runtime": "fixture", "settings": {"policy": "test-run-echo-then-wait",
                                                            "max_tokens_per_decision": 120_000}}},
        project=pid)["result"]["investigation_id"]


def _turns_budget(lab, pid, inv) -> dict:
    return lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["turns"]


def _begin_decision(lab, pid, tid, execution: str, attempt: int = 1, slot: int = 0) -> dict:
    return lab.cmd(_wf(lab, pid), "begin_decision", {"turn_id": tid, "slot": slot, "attempt": attempt,
                                                     "execution": execution}, project=pid)["result"]


def _accept_with(lab, pid, tid, proposal, *, epoch: int, execution: str, attempt: int = 1, slot: int = 0) -> dict:
    return lab.cmd(_wf(lab, pid), "accept_decision", {"turn_id": tid, "slot": slot, "proposal": proposal,
                                                      "usage_recorded": True, "attempt": attempt, "epoch": epoch,
                                                      "execution": execution}, project=pid)["result"]


def _accept(lab, pid, tid, proposal=CLAIMS, *, slot: int = 0, execution: str = EXEC_A) -> dict:
    """A decision accepted the way the decide activity does it (begin_decision, then accept_decision with its epoch)."""
    epoch = _begin_decision(lab, pid, tid, execution, slot=slot)["epoch"]
    out = _accept_with(lab, pid, tid, proposal, epoch=epoch, execution=execution, slot=slot)
    assert out["accepted"] is True, out
    return out


def _hand_off(lab, pid, did, execution: str, key: str | None = None) -> dict:
    return lab.cmd(_wf(lab, pid), "hand_off_decision", {"decision_id": did, "execution": execution}, project=pid,
                   key=key)["result"]


def _complete(lab, pid, tid, run: str, stop_reason: str = "calls-exhausted") -> dict:
    return lab.cmd(_wf(lab, pid), "complete_turn", {"turn_id": tid, "stop_reason": stop_reason,
                                                    "execution": f"{run}/complete"}, project=pid)["result"]


def _operator(lab, pid, inv, name: str) -> dict:
    return lab.cmd(lab.admin, name, {"investigation_id": inv}, project=pid)["result"]


# -- helpers: effects ----------------------------------------------------------------------------------------------------
def _admission(did: str, execution: str = EXEC_OWNER) -> dict:
    return {"decision_id": did, "execution": execution}


def _file_claim(lab, pid, inv, statement: str, *, admission: dict | None, key: str | None = None) -> dict:
    """``file_claim`` as the investigator sends it for a decision's call: through the service, with ``admission``."""
    return lab.svc.execute(lab.principal(f"inv-{pid}", ["investigator"], [pid]), "file_claim", project_id=pid,
                           command_key=key or uuid.uuid4().hex, admission=admission,
                           payload={"investigation_id": inv, "statement": statement, "kind": "improves",
                                    "scope": {"m": 1}})


def _claims(lab, pid) -> list[str]:
    return sorted(c["title"] for c in lab.q(lab.admin, "list_records", {"kind": "claim"}, project=pid))


def _claim_events(lab, pid, inv) -> int:
    return len(_events(lab, pid, inv, "claim.filed"))


def _refusal(lab, pid, inv, admission: dict, statement: str = "refused") -> EffectNotAdmitted:
    with pytest.raises(EffectNotAdmitted) as err:
        _file_claim(lab, pid, inv, statement, admission=admission)
    assert err.value.code == "EFFECT_NOT_ADMITTED" and err.value.http_status == 409
    return err.value


def _once(fn):
    done: list[bool] = []

    def run() -> None:
        if not done:
            done.append(True)
            fn()
    return run


class BackgroundCall(Background):
    """``fn`` run on another thread (its own connection), like ``Background`` runs a command."""

    def __init__(self, fn):
        self.result, self.error = None, None

        def run() -> None:
            try:
                self.result = fn()
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


@contextmanager
def _held_effect(lab, pid, inv, admission: dict, statement: str):
    """The effect's admission and handler run inside a transaction that stays open (locks held) until the block exits."""
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        ctx = Ctx(tx, lab.principal(f"inv-{pid}", ["investigator"], [pid]), pid, uuid.uuid4().hex, lab.store,
                  lab.svc.settings)
        for admit in EFFECT_ADMISSION:
            admit(ctx, admission)
        yield COMMANDS["file_claim"].fn(ctx, {"investigation_id": inv, "statement": statement, "kind": "improves",
                                              "scope": {"m": 1}})


# =====================================================================================================================
# R8 #1: ordered turn tickets
# =====================================================================================================================
def test_delayed_begin_turn_of_the_older_run_is_refused_after_the_newer_run_resumed_the_turn(lab):
    """Run A (ticket 100) begins the turn and is terminated; run B (ticket 200) resumes it and owns it. A's delayed
    begin_turn then returns ``superseded-run`` and changes nothing: not the owner, not the ticket, no event."""
    pid, inv, _ = setup(lab)
    a = _begin_turn(lab, pid, inv, RUN_A, 100)
    tid = a["turn_id"]
    assert a["started"] is True and "resumed" not in a
    assert a["authorization"] == _owner(tid, RUN_A, 1, None) and _ticket(lab, pid, inv) == _held(inv, 100, RUN_A)

    b = _begin_turn(lab, pid, inv, RUN_B, 200)
    assert b["started"] is True and b["resumed"] is True and b["turn_id"] == tid
    assert b["authorization"] == _owner(tid, RUN_B, 2, RUN_A)
    assert _ticket(lab, pid, inv) == _held(inv, 200, RUN_B)
    ticket_revision = _ticket_revision(lab, pid, inv)

    late = _begin_turn(lab, pid, inv, RUN_A, 100)  # a new command key: not a cached receipt
    assert late == {"started": False, "reason": "superseded-run", "superseded": "stale-ticket", "ticket": 100,
                    "current_ticket": 200}
    assert _auth(lab, pid, tid) == _owner(tid, RUN_B, 2, RUN_A)
    assert _ticket(lab, pid, inv) == _held(inv, 200, RUN_B) and _ticket_revision(lab, pid, inv) == ticket_revision
    assert _turns_started(lab, pid, inv) == [tid]
    assert [e["body"]["generation"] for e in _events(lab, pid, inv, "turn.resumed")] == [2]  # B's resume only


def test_old_run_that_lost_the_turn_is_refused_at_every_decision_step_and_the_new_run_proceeds(lab):
    """After B took the turn by ticket, A's begin_decision, accept_decision, hand_off_decision and complete_turn are
    all refused as ``superseded-execution``, and B's own steps work."""
    pid, inv, _ = setup(lab)
    tid = _begin_turn(lab, pid, inv, RUN_A, 100)["turn_id"]
    ea = _begin_decision(lab, pid, tid, EXEC_A)["epoch"]  # A's decide is proposing when its workflow is terminated
    assert _begin_turn(lab, pid, inv, RUN_B, 200)["resumed"] is True
    assert _begin_turn(lab, pid, inv, RUN_A, 100)["started"] is False  # A's delayed begin does not change that

    late = _accept_with(lab, pid, tid, NOTE, epoch=ea, execution=EXEC_A)
    assert late["accepted"] is False and late["reason"] == "superseded-execution"
    assert _begin_decision(lab, pid, tid, EXEC_A, attempt=2) == {"turn_id": tid, "slot": 0,
                                                                  "superseded": "superseded-execution"}
    eb = _begin_decision(lab, pid, tid, EXEC_B)
    assert "superseded" not in eb and eb["epoch"] == ea + 1
    dec = _accept_with(lab, pid, tid, NOTE, epoch=eb["epoch"], execution=EXEC_B)
    assert dec["accepted"] is True

    assert _hand_off(lab, pid, dec["decision_id"], f"{RUN_A}/exec") == {
        "decision_id": dec["decision_id"], "handed_off": False, "reason": "superseded-execution"}
    assert _record(lab, pid, "decision_handoff", dec["decision_id"]) is None
    assert _complete(lab, pid, tid, RUN_A) == {"turn_id": tid, "status": "open", "superseded": "superseded-execution"}
    assert _events(lab, pid, inv, "turn.completed") == []

    assert _hand_off(lab, pid, dec["decision_id"], f"{RUN_B}/exec") == {
        "decision_id": dec["decision_id"], "handed_off": True, "by": RUN_B}
    assert _complete(lab, pid, tid, RUN_B) == {"turn_id": tid, "status": "completed"}
    assert _auth(lab, pid, tid) == _owner(tid, RUN_B, 2, RUN_A)


def test_older_ticket_arriving_after_the_newer_run_created_the_turn_is_refused_and_starts_no_second_turn(lab):
    """The reverse order: B (ticket 200) begins first and creates the turn, then A (ticket 100) arrives. A is refused:
    no new turn, no ownership change, no second reservation of the turns budget."""
    pid, _, _ = setup(lab)
    inv = _start_with_turns_limit(lab, pid)
    b = _begin_turn(lab, pid, inv, RUN_B, 200)
    tid = b["turn_id"]
    assert b["started"] is True and "resumed" not in b and b["authorization"] == _owner(tid, RUN_B, 1, None)
    budget = _turns_budget(lab, pid, inv)
    assert budget["reserved"] == 1

    late = _begin_turn(lab, pid, inv, RUN_A, 100)
    assert late == {"started": False, "reason": "superseded-run", "superseded": "stale-ticket", "ticket": 100,
                    "current_ticket": 200}
    assert _turns_started(lab, pid, inv) == [tid] and _events(lab, pid, inv, "turn.resumed") == []
    assert _auth(lab, pid, tid) == _owner(tid, RUN_B, 1, None) and _ticket(lab, pid, inv) == _held(inv, 200, RUN_B)
    assert _turns_budget(lab, pid, inv) == budget
    # Not even after the turn closed: A still starts nothing (see the next test).
    assert _complete(lab, pid, tid, RUN_B)["status"] == "completed"
    assert _begin_turn(lab, pid, inv, RUN_A, 100)["started"] is False
    assert _turns_started(lab, pid, inv) == [tid]


def test_delayed_begin_turn_after_the_newer_run_completed_its_turn_never_starts_a_new_turn(lab):
    """A delayed begin_turn of an old run that arrives when the newer run's turn is already closed would, unfenced, find
    no open turn and START one (and reserve the turns budget). It starts nothing; a run with a newer ticket still can."""
    pid, _, _ = setup(lab)
    inv = _start_with_turns_limit(lab, pid)
    tid = _begin_turn(lab, pid, inv, RUN_A, 100)["turn_id"]
    assert _begin_turn(lab, pid, inv, RUN_B, 200)["resumed"] is True
    assert _complete(lab, pid, tid, RUN_B)["status"] == "completed"
    budget = _turns_budget(lab, pid, inv)
    assert budget["reserved"] == 0 and budget["used"] == 1

    late = _begin_turn(lab, pid, inv, RUN_A, 100)
    assert late["started"] is False and late["reason"] == "superseded-run" and late["superseded"] == "stale-ticket"
    assert _turns_started(lab, pid, inv) == [tid]  # still one turn
    assert _turns_budget(lab, pid, inv) == budget  # nothing reserved
    assert lab.q(lab.admin, "get_turn", {"turn_id": tid}, project=pid)["status"] == "completed"

    nxt = _begin_turn(lab, pid, inv, RUN_C, 300)  # the fence is an order, not a lock: a newer run begins turn 2
    assert nxt["started"] is True and "resumed" not in nxt and nxt["turn_id"] != tid
    assert _turns_started(lab, pid, inv) == [tid, nxt["turn_id"]]
    assert _turns_budget(lab, pid, inv)["reserved"] == 1 and _ticket(lab, pid, inv) == _held(inv, 300, RUN_C)


def test_same_run_and_ticket_again_returns_the_same_turn_without_changing_its_generation(lab):
    pid, inv, _ = setup(lab)
    first = _begin_turn(lab, pid, inv, RUN_B, 200, key="begin-1")
    tid = first["turn_id"]
    assert first["started"] is True and "resumed" not in first
    revision = _ticket_revision(lab, pid, inv)

    again = _begin_turn(lab, pid, inv, RUN_B, 200)  # a retry under another key
    assert again["started"] is True and again["resumed"] is True and again["turn_id"] == tid
    assert again["authorization"] == first["authorization"] == _owner(tid, RUN_B, 1, None)
    replay = lab.cmd(_wf(lab, pid), "begin_turn", {"investigation_id": inv, "run_id": RUN_B, "ticket": 200},
                     project=pid, key="begin-1")
    assert replay["replayed"] is True and replay["result"]["turn_id"] == tid  # and under the original key
    assert _auth(lab, pid, tid) == _owner(tid, RUN_B, 1, None)
    assert _ticket_revision(lab, pid, inv) == revision  # the ticket is not rewritten
    assert _turns_started(lab, pid, inv) == [tid] and _events(lab, pid, inv, "turn.resumed") == []


def test_resumed_run_repeating_its_own_begin_keeps_its_generation_and_revokes_nothing_more(lab):
    pid, inv, _ = setup(lab)
    tid = _begin_turn(lab, pid, inv, RUN_A, 100)["turn_id"]
    assert _begin_turn(lab, pid, inv, RUN_B, 200)["authorization"] == _owner(tid, RUN_B, 2, RUN_A)
    again = _begin_turn(lab, pid, inv, RUN_B, 200)
    assert again["resumed"] is True and again["authorization"] == _owner(tid, RUN_B, 2, RUN_A)
    assert [e["body"]["generation"] for e in _events(lab, pid, inv, "turn.resumed")] == [2]


def test_run_without_a_ticket_is_refused_once_a_ticketed_run_began_and_callers_without_a_run_id_are_not_fenced(lab):
    pid, inv, _ = setup(lab)
    tid = _begin_turn(lab, pid, inv, RUN_A)["turn_id"]  # no ticket has begun a turn yet: nothing to compare with
    assert _ticket(lab, pid, inv) is None and _auth(lab, pid, tid) == _owner(tid, RUN_A, 1, None)
    assert _begin_turn(lab, pid, inv, RUN_B, 200)["authorization"] == _owner(tid, RUN_B, 2, RUN_A)

    unticketed = _begin_turn(lab, pid, inv, RUN_C)  # a turn workflow started before tickets existed
    assert unticketed == {"started": False, "reason": "superseded-run", "superseded": "unticketed-run",
                          "current_ticket": 200}
    assert _auth(lab, pid, tid) == _owner(tid, RUN_B, 2, RUN_A) and _ticket(lab, pid, inv) == _held(inv, 200, RUN_B)

    # No run id (tests, tools): never fenced, takes no ownership, records no ticket, even with an old ticket.
    for ticket in (None, 1):
        anon = _begin_turn(lab, pid, inv, None, ticket)
        assert anon["started"] is True and anon["resumed"] is True and anon["turn_id"] == tid
        assert "authorization" not in anon
    assert _auth(lab, pid, tid) == _owner(tid, RUN_B, 2, RUN_A) and _ticket(lab, pid, inv) == _held(inv, 200, RUN_B)
    assert _turns_started(lab, pid, inv) == [tid]


def test_same_ticket_from_a_different_run_is_refused_while_the_holder_keeps_it(lab):
    pid, inv, _ = setup(lab)
    tid = _begin_turn(lab, pid, inv, RUN_A, 100)["turn_id"]
    other = _begin_turn(lab, pid, inv, RUN_B, 100)
    assert other == {"started": False, "reason": "superseded-run", "superseded": "ticket-held-by-another-run",
                     "ticket": 100}
    assert _auth(lab, pid, tid) == _owner(tid, RUN_A, 1, None) and _ticket(lab, pid, inv) == _held(inv, 100, RUN_A)
    assert _begin_turn(lab, pid, inv, RUN_A, 100)["authorization"] == _owner(tid, RUN_A, 1, None)  # the holder: fine
    assert _begin_turn(lab, pid, inv, RUN_B, 101)["authorization"] == _owner(tid, RUN_B, 2, RUN_A)  # a newer one: fine
    assert _begin_turn(lab, pid, inv, RUN_A, 100)["superseded"] == "stale-ticket"  # and now A is the old one


def test_begin_turn_activity_sends_its_ticket_and_reports_a_superseded_run_as_not_started(lab):
    """Through the activity: the ticket in its input reaches the lab; an older (or missing) ticket is ``started`` false
    with the lab's reason, and nothing is begun."""
    pid, inv, acts = setup(lab)
    args = {"investigation_id": inv, "project_id": pid}
    b = _env(1, run=RUN_B, activity_id="begin-turn").run(acts.begin_turn, {**args, "index": 2, "ticket": 200})
    assert b["started"] is True and b["resumed"] is False
    assert _ticket(lab, pid, inv) == _held(inv, 200, RUN_B)
    assert _auth(lab, pid, b["turn_id"]) == _owner(b["turn_id"], RUN_B, 1, None)

    for index, ticket in ((1, {"ticket": 100}), (3, {})):  # the begin key is per run and index: one payload each
        out = _env(1, run=RUN_A, activity_id="begin-turn").run(acts.begin_turn, {**args, "index": index, **ticket})
        assert out == {"started": False, "reason": "superseded-run"}
    assert _turns_started(lab, pid, inv) == [b["turn_id"]]
    assert _auth(lab, pid, b["turn_id"]) == _owner(b["turn_id"], RUN_B, 1, None)


@pytest.mark.parametrize("turn_exists", [False, True], ids=["turn-created-by-newer", "turn-resumed-by-newer"])
def test_delayed_begin_turn_waiting_on_the_newer_runs_uncommitted_begin_is_refused_once_it_commits(lab, turn_exists):
    """Real concurrent transactions: the newer run's begin_turn has run but not committed when the older run's delayed
    begin_turn arrives. It waits for the locks the newer one holds, then sees the newer ticket and is refused."""
    pid, inv, _ = setup(lab)
    wf = _wf(lab, pid)
    if turn_exists:
        _begin_turn(lab, pid, inv, RUN_A, 100)
    with held(lab, wf, pid, "begin_turn", {"investigation_id": inv, "run_id": RUN_B, "ticket": 200}) as r:
        assert r["started"] is True and r.get("resumed", False) is turn_exists
        late = Background(lab, wf, pid, "begin_turn", {"investigation_id": inv, "run_id": RUN_A, "ticket": 100})
        late.assert_waiting()
    out = late.finish()
    assert out["started"] is False and out["superseded"] == "stale-ticket" and out["current_ticket"] == 200
    tid = r["turn_id"]
    assert _turns_started(lab, pid, inv) == [tid]  # one turn: created by the first begin, then resumed or refused
    assert _auth(lab, pid, tid)["run"] == RUN_B and _ticket(lab, pid, inv) == _held(inv, 200, RUN_B)


# =====================================================================================================================
# R8 #2: the handoff checks the locked turn and investigation
# =====================================================================================================================
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_hand_off_after_the_operator_stopped_or_paused_is_refused_and_the_activity_executes_nothing(
        lab, operator, status):
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    _operator(lab, pid, inv, operator)  # the stop commits before the handoff

    assert _hand_off(lab, pid, dec, EXEC_OWNER) == {"decision_id": dec, "handed_off": False,
                                                    "reason": f"investigation-{status}"}
    assert _record(lab, pid, "decision_handoff", dec) is None
    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out == {"summary": [], "skipped": f"investigation-{status}"}
    assert _claims(lab, pid) == [] and _record(lab, pid, "decision_handoff", dec) is None
    assert _record(lab, pid, "decision_receipts", dec) is None


@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_existing_handoff_is_not_returned_once_the_investigation_stopped_or_paused(lab, operator, status):
    """The handoff was granted first; then the stop. A repeated hand_off_decision (a retried activity sends a new key
    per attempt) must be refused, not answered from the existing handoff."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    assert _hand_off(lab, pid, dec, EXEC_OWNER) == {"decision_id": dec, "handed_off": True, "by": RUN_A}
    _operator(lab, pid, inv, operator)

    for execution in (EXEC_OWNER, f"{RUN_A}/exec-retry"):
        assert _hand_off(lab, pid, dec, execution) == {"decision_id": dec, "handed_off": False,
                                                       "reason": f"investigation-{status}"}
    assert _record(lab, pid, "decision_handoff", dec)["run"] == RUN_A  # what was recorded is not rewritten
    out = _env(2, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})  # a retry of the activity
    assert out == {"summary": [], "skipped": f"investigation-{status}"}
    assert _claims(lab, pid) == [] and _record(lab, pid, "decision_receipts", dec) is None


@pytest.mark.parametrize("handed_off_before", [False, True], ids=["new-handoff", "existing-handoff"])
@pytest.mark.parametrize("stop_reason, status", [("calls-exhausted", "completed"), ("cancelled", "cancelled"),
                                                 ("error", "failed")])
def test_hand_off_after_the_turn_closed_is_refused_with_the_turn_status(lab, handed_off_before, stop_reason, status):
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    dec = _accept(lab, pid, tid)["decision_id"]
    if handed_off_before:
        assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    assert _complete(lab, pid, tid, RUN_A, stop_reason)["status"] == status

    assert _hand_off(lab, pid, dec, EXEC_OWNER) == {"decision_id": dec, "handed_off": False, "reason": f"turn-{status}"}
    assert (_record(lab, pid, "decision_handoff", dec) is not None) is handed_off_before
    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out == {"summary": [], "skipped": f"turn-{status}"} and _claims(lab, pid) == []


def test_hand_off_waiting_on_an_uncommitted_stop_is_refused_when_the_stop_commits(lab):
    """Real concurrent transactions: the stop has run but not committed when the handoff arrives. The handoff takes the
    turn row, then waits for the investigation row the stop holds, and is refused once the stop committed."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    with held(lab, lab.admin, pid, "stop_investigation", {"investigation_id": inv}) as r:
        assert r["status"] == "stopped"
        handoff = Background(lab, _wf(lab, pid), pid, "hand_off_decision", {"decision_id": dec,
                                                                           "execution": EXEC_OWNER})
        handoff.assert_waiting()
    assert handoff.finish() == {"decision_id": dec, "handed_off": False, "reason": "investigation-stopped"}
    assert _record(lab, pid, "decision_handoff", dec) is None


def test_stop_waiting_on_an_uncommitted_handoff_commits_after_it_and_the_effects_are_then_refused(lab):
    """The other order: the handoff has been granted but not committed when the stop arrives. The stop waits (the
    decision is handed off, a linearizable history); the effects are then refused at their own admission."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    with held(lab, _wf(lab, pid), pid, "hand_off_decision", {"decision_id": dec, "execution": EXEC_OWNER}) as r:
        assert r == {"decision_id": dec, "handed_off": True, "by": RUN_A}
        stop = Background(lab, lab.admin, pid, "stop_investigation", {"investigation_id": inv})
        stop.assert_waiting()
    assert stop.finish()["status"] == "stopped"
    assert _record(lab, pid, "decision_handoff", dec)["run"] == RUN_A
    assert _refusal(lab, pid, inv, _admission(dec)).details["reason"] == "investigation-stopped"
    assert _claims(lab, pid) == []


# =====================================================================================================================
# R8 #2: effect admission
# =====================================================================================================================
@pytest.mark.parametrize("operator, status", STOP_OR_PAUSE)
def test_effect_of_a_handed_off_decision_is_admitted_while_active_and_refused_after_a_stop_or_pause_writing_nothing(
        lab, operator, status):
    pid, inv, acts, base = _turn(lab)
    acc = _accept(lab, pid, base["turn_id"])
    dec, keys = acc["decision_id"], [c["command_key"] for c in acc["calls"]]
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    adm = _admission(dec)

    first = _file_claim(lab, pid, inv, "claim one", admission=adm, key=keys[0])
    assert first["status"] == "committed" and first["replayed"] is False
    assert _claims(lab, pid) == ["claim one"] and _claim_events(lab, pid, inv) == 1

    _operator(lab, pid, inv, operator)  # the stop commits between the decision's two effects
    err = _refusal(lab, pid, inv, adm, "claim two")
    assert err.details == {"reason": f"investigation-{status}", "decision_id": dec}
    with pytest.raises(EffectNotAdmitted):  # with the decision's own key for that call, too
        _file_claim(lab, pid, inv, "claim two", admission=adm, key=keys[1])
    assert _claims(lab, pid) == ["claim one"] and _claim_events(lab, pid, inv) == 1  # nothing was written

    # A replay of the effect that had already committed returns its receipt, it is not an error.
    again = _file_claim(lab, pid, inv, "claim one", admission=adm, key=keys[0])
    assert again["replayed"] is True and again["result"] == first["result"]
    assert _claims(lab, pid) == ["claim one"]


def test_refused_effect_leaves_its_command_key_unused_so_the_same_key_commits_after_resume(lab):
    pid, inv, acts, base = _turn(lab)
    acc = _accept(lab, pid, base["turn_id"])
    dec, key = acc["decision_id"], acc["calls"][0]["command_key"]
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    _operator(lab, pid, inv, "pause_investigation")
    with pytest.raises(EffectNotAdmitted):
        _file_claim(lab, pid, inv, "claim one", admission=_admission(dec), key=key)
    _operator(lab, pid, inv, "resume_investigation")
    out = _file_claim(lab, pid, inv, "claim one", admission=_admission(dec), key=key)
    assert out["status"] == "committed" and out["replayed"] is False  # the refusal claimed nothing under the key
    assert _claims(lab, pid) == ["claim one"] and _claim_events(lab, pid, inv) == 1


def test_effect_of_an_accepted_decision_that_was_not_handed_off_is_refused_as_not_handed_off(lab):
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    err = _refusal(lab, pid, inv, _admission(dec))
    assert err.details == {"reason": "not-handed-off", "decision_id": dec}
    assert _claims(lab, pid) == [] and _claim_events(lab, pid, inv) == 0
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True  # and once handed off it is admitted
    assert _file_claim(lab, pid, inv, "claim one", admission=_admission(dec))["status"] == "committed"


def test_effect_without_an_accepted_decision_is_refused_as_not_accepted(lab):
    pid, inv, acts, base = _turn(lab)
    for did in ("dec_does_not_exist", ""):
        err = _refusal(lab, pid, inv, _admission(did))
        assert err.details == {"reason": "not-accepted", "decision_id": did}
    assert _claims(lab, pid) == []


def test_effect_of_a_run_that_lost_the_turn_is_refused_while_the_new_owner_continues_the_handed_off_decision(lab):
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    assert _begin_turn(lab, pid, inv, RUN_B)["resumed"] is True  # run-B takes the turn; a handed-off decision is kept

    err = _refusal(lab, pid, inv, _admission(dec, EXEC_OWNER))  # run-A's effect
    assert err.details == {"reason": "superseded-execution", "decision_id": dec}
    assert _claims(lab, pid) == []
    out = _file_claim(lab, pid, inv, "claim one", admission=_admission(dec, f"{RUN_B}/exec"))  # run-B continues it
    assert out["status"] == "committed"
    assert _claims(lab, pid) == ["claim one"]


def test_effect_of_a_revoked_decision_is_refused_for_the_old_run_and_for_the_new_owner(lab):
    """Run-A accepted a decision and never handed it off; run-B's resume revoked it. Run-A's effect for it is refused as
    a lost turn, run-B's as revoked (even if somebody recorded a handoff for it first)."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    assert _begin_turn(lab, pid, inv, RUN_B)["resumed"] is True
    assert [e["subject"] for e in _events(lab, pid, inv, "decision.revoked")] == [dec]

    assert _refusal(lab, pid, inv, _admission(dec, EXEC_OWNER)).details["reason"] == "superseded-execution"
    assert _refusal(lab, pid, inv, _admission(dec, f"{RUN_B}/exec")).details["reason"] == "decision-revoked"
    assert _claims(lab, pid) == [] and _claim_events(lab, pid, inv) == 0


@pytest.mark.parametrize("stop_reason, status", [("calls-exhausted", "completed"), ("cancelled", "cancelled"),
                                                 ("error", "failed")])
def test_effect_of_a_decision_whose_turn_closed_is_refused_with_the_turn_status(lab, stop_reason, status):
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    dec = _accept(lab, pid, tid)["decision_id"]
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    assert _complete(lab, pid, tid, RUN_A, stop_reason)["status"] == status
    err = _refusal(lab, pid, inv, _admission(dec))
    assert err.details == {"reason": f"turn-{status}", "decision_id": dec}
    assert _claims(lab, pid) == [] and _claim_events(lab, pid, inv) == 0


def test_command_without_admission_is_not_gated_by_the_effect_check(lab):
    """Only the effects of a decision carry ``admission``: an operator's command after a stop is handled as before
    (so the refusals above come from the admission check, not from the handler)."""
    pid, inv, acts, base = _turn(lab)
    _operator(lab, pid, inv, "stop_investigation")
    out = lab.cmd(lab.admin, "file_claim", {"investigation_id": inv, "statement": "operator claim",
                                            "kind": "improves", "scope": {"m": 1}}, project=pid)["result"]
    assert out["claim_id"] and _claims(lab, pid) == ["operator claim"]


def test_stop_waits_for_an_effect_being_admitted_and_the_effect_commits_first(lab):
    """Real concurrent transactions: an effect's admission holds the turn and investigation rows until the effect
    commits, so a stop that arrives meanwhile waits and the effect is committed whole before it. (The event sequence
    serializes this order too, so it is the next test, the stop committing first, that depends on the row lock.)"""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    with _held_effect(lab, pid, inv, _admission(dec), "claim one") as r:
        assert r["claim_id"]
        stop = Background(lab, lab.admin, pid, "stop_investigation", {"investigation_id": inv})
        stop.assert_waiting()
    assert stop.finish()["status"] == "stopped"
    assert _claims(lab, pid) == ["claim one"]
    assert _refusal(lab, pid, inv, _admission(dec), "claim two").details["reason"] == "investigation-stopped"


def test_effect_waiting_on_an_uncommitted_stop_is_refused_when_the_stop_commits(lab):
    """The other order: the stop has run but not committed when the effect arrives. The admission waits for the
    investigation row, sees the stop and refuses the effect; nothing is written."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    assert _hand_off(lab, pid, dec, EXEC_OWNER)["handed_off"] is True
    with held(lab, lab.admin, pid, "stop_investigation", {"investigation_id": inv}) as r:
        assert r["status"] == "stopped"
        effect = BackgroundCall(lambda: _file_claim(lab, pid, inv, "claim one", admission=_admission(dec)))
        effect.assert_waiting()
    with pytest.raises(EffectNotAdmitted) as err:
        effect.finish()
    assert err.value.details == {"reason": "investigation-stopped", "decision_id": dec}
    assert _claims(lab, pid) == [] and _claim_events(lab, pid, inv) == 0


# =====================================================================================================================
# R8 #2 through the execute_decision activity
# =====================================================================================================================
OPERATOR_REASONS = [("stop_investigation", "investigation-stopped"), ("pause_investigation", "investigation-paused")]


def _interrupt(lab, pid, inv, tid, what: str) -> None:
    if what == "complete_turn":
        _complete(lab, pid, tid, RUN_A)
    else:
        _operator(lab, pid, inv, what)


@pytest.mark.parametrize("operator, why", OPERATOR_REASONS)
def test_stop_between_two_effects_commits_the_first_and_refuses_the_second_through_the_activity(lab, operator, why):
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    acts.inv = Recording(acts.inv)  # the stop commits right after the first effect command committed
    acts.inv.after["file_claim"] = _once(lambda: _interrupt(lab, pid, inv, base["turn_id"], operator))

    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out["stopped"] == why
    assert out["summary"] == [{"tool": "file_claim", "ok": True, "replayed": False, "code": None},
                              {"tool": "file_claim", "ok": False, "replayed": None, "code": "EFFECT_NOT_ADMITTED"}]
    assert "nothing_outstanding" not in out
    assert acts.inv.log == ["file_claim", "file_claim"]  # the second call was sent, and refused
    assert _claims(lab, pid) == ["claim one"] and _claim_events(lab, pid, inv) == 1

    receipts = _record(lab, pid, "decision_receipts", dec)["receipts"]  # the refused call is part of the record
    assert [(r["tool"], r["index"], r["ok"]) for r in receipts] == [("file_claim", 0, True), ("file_claim", 1, False)]
    assert receipts[1]["code"] == "EFFECT_NOT_ADMITTED"
    assert receipts[1]["error"]["details"] == {"reason": why, "decision_id": dec}
    assert _record(lab, pid, "decision_handoff", dec)["run"] == RUN_A  # the handoff itself was granted

    # A retry of the activity (its result got lost) executes nothing more: the handoff is refused.
    acts.inv.log.clear()
    retry = _env(2, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert retry == {"summary": [], "skipped": why}
    assert acts.inv.log == [] and _claims(lab, pid) == ["claim one"]


def test_turn_closed_between_two_effects_refuses_the_second_through_the_activity(lab):
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    dec = _accept(lab, pid, tid)["decision_id"]
    acts.inv = Recording(acts.inv)
    acts.inv.after["file_claim"] = _once(lambda: _interrupt(lab, pid, inv, tid, "complete_turn"))
    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out["stopped"] == "turn-completed"
    assert [(r["ok"], r["code"]) for r in out["summary"]] == [(True, None), (False, "EFFECT_NOT_ADMITTED")]
    assert _claims(lab, pid) == ["claim one"]
    assert _record(lab, pid, "decision_receipts", dec)["receipts"][1]["error"]["details"]["reason"] == "turn-completed"


@pytest.mark.parametrize("operator, why", OPERATOR_REASONS)
def test_stop_before_the_handoff_through_the_activity_hands_off_nothing_and_executes_nothing(lab, operator, why):
    """The activity read the decision and the open turn; the stop commits just before its hand_off_decision."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    acts.wf = Recording(acts.wf)
    acts.inv = Recording(acts.inv)
    acts.wf.on["hand_off_decision"] = lambda: _interrupt(lab, pid, inv, base["turn_id"], operator)

    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out == {"summary": [], "skipped": why}
    assert acts.wf.log == ["hand_off_decision"] and acts.inv.log == []
    assert _record(lab, pid, "decision_handoff", dec) is None and _record(lab, pid, "decision_receipts", dec) is None
    assert _claims(lab, pid) == []


def test_turn_closed_before_the_handoff_through_the_activity_executes_nothing(lab):
    pid, inv, acts, base = _turn(lab)
    tid = base["turn_id"]
    dec = _accept(lab, pid, tid)["decision_id"]
    acts.wf = Recording(acts.wf)
    acts.wf.on["hand_off_decision"] = lambda: _interrupt(lab, pid, inv, tid, "complete_turn")
    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out == {"summary": [], "skipped": "turn-completed"}
    assert _record(lab, pid, "decision_handoff", dec) is None and _claims(lab, pid) == []


@pytest.mark.parametrize("operator, why", OPERATOR_REASONS)
def test_stop_right_after_the_handoff_through_the_activity_refuses_the_first_effect(lab, operator, why):
    """The other ordering: the handoff is granted and the stop commits before the first effect is sent. The handoff
    stands, but no effect is admitted; the refused call is recorded."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    acts.wf = Recording(acts.wf)
    acts.wf.after["hand_off_decision"] = lambda: _interrupt(lab, pid, inv, base["turn_id"], operator)

    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out["stopped"] == why
    assert out["summary"] == [{"tool": "file_claim", "ok": False, "replayed": None, "code": "EFFECT_NOT_ADMITTED"}]
    assert _claims(lab, pid) == [] and _claim_events(lab, pid, inv) == 0
    assert _record(lab, pid, "decision_handoff", dec)["run"] == RUN_A
    receipts = _record(lab, pid, "decision_receipts", dec)["receipts"]
    assert [(r["index"], r["ok"], r["code"]) for r in receipts] == [(0, False, "EFFECT_NOT_ADMITTED")]
    assert receipts[0]["error"]["details"]["reason"] == why


def test_run_that_loses_the_turn_after_its_handoff_executes_no_effect_and_the_new_owner_continues(lab):
    """Run-B resumes the turn right after run-A's handoff: run-A's first effect is refused as ``superseded-execution``
    and the activity returns ``skipped`` without recording receipts (they are the owner's to record)."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    acts.wf = Recording(acts.wf)
    acts.wf.after["hand_off_decision"] = lambda: _begin_turn(lab, pid, inv, RUN_B)

    out = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert out == {"summary": [], "skipped": "superseded-execution"}
    assert _claims(lab, pid) == [] and _record(lab, pid, "decision_receipts", dec) is None
    assert _record(lab, pid, "decision_handoff", dec)["run"] == RUN_A

    acts.wf.after.clear()
    ex = _env(1, run=RUN_B, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert [r["ok"] for r in ex["summary"]] == [True, True] and "stopped" not in ex
    assert _claims(lab, pid) == ["claim one", "claim two"]


def test_retry_after_resume_of_a_decision_stopped_between_its_effects_completes_it_and_records_its_receipts(lab):
    """FAILS TODAY (review R8 #2 follow-up, see the report): the first attempt of execute_decision was cut by a pause
    between its two effects and recorded its receipts (``[ok, refused]``) under the key ``receipts:<decision>``. After
    the operator resumed, a retry of the activity (its first result was lost) is handed off again (the handoff stands
    and the investigation is active), replays call 0 and runs call 1, which the refusal had left uncommitted; then
    ``_finish_decision`` records ``[ok, ok]`` under the SAME key with another payload and the activity dies with
    COMMAND_KEY_CONFLICT instead of returning its summary."""
    pid, inv, acts, base = _turn(lab)
    dec = _accept(lab, pid, base["turn_id"])["decision_id"]
    acts.inv = Recording(acts.inv)
    acts.inv.after["file_claim"] = _once(lambda: _interrupt(lab, pid, inv, base["turn_id"], "pause_investigation"))
    first = _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert first["stopped"] == "investigation-paused" and _claims(lab, pid) == ["claim one"]
    _operator(lab, pid, inv, "resume_investigation")

    retry = _env(2, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": dec})
    assert [(r["ok"], r["replayed"]) for r in retry["summary"]] == [(True, True), (True, False)]
    assert _claims(lab, pid) == ["claim one", "claim two"]
