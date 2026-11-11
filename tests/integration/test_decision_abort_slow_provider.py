"""ADR 0017 (I3): stopping an investigation, or the decision deadline, abandons the in-flight model request.

A slow OpenAI-compatible provider (a local HTTP server that holds every request open) stands in for a reasoning
model. The real decide activity runs the real pydantic-ai runtime against it; the tests check that the request's
connection is closed promptly, that its usage is recorded as uncertain and the reservation is settled, and that no
decision from the abandoned request is ever applied. The lab-side refusals (stopped investigation, superseded
attempt, attempt past its deadline) are checked directly as well. No live model is called.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from temporalio.testing import ActivityEnvironment

from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.runtime import output_cap

pytestmark = [pytest.mark.pg]

DECISION = {"rationale": "conclude", "calls": [{"tool": "record_note", "args": {"kind": "observation", "text": "late"}}],
            "control": {"action": "conclude", "summary": "late decision"}}


class SlowProvider:
    """``/v1/chat/completions`` that holds each request for ``delay`` seconds, then answers ``reply``; it records when
    a request arrived and whether the client closed the connection first."""

    def __init__(self, delay: float, reply: dict):
        self.delay, self.reply = delay, reply
        self.arrived, self.closed = threading.Event(), threading.Event()
        self.arrived_at: float | None = None
        self.closed_at: float | None = None
        self.answered = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.arrived_at = time.monotonic()
                outer.arrived.set()
                end = time.monotonic() + outer.delay
                while time.monotonic() < end:
                    readable, _, _ = select.select([self.connection], [], [], 0.05)
                    if readable:
                        try:
                            peek = self.connection.recv(1, socket.MSG_PEEK)
                        except OSError:
                            peek = b""
                        if not peek:  # the client hung up: the request was abandoned
                            outer.closed_at = time.monotonic()
                            outer.closed.set()
                            self.close_connection = True
                            return
                body = json.dumps({
                    "id": "cmpl-slow", "object": "chat.completion", "created": int(time.time()), "model": "slow-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": json.dumps(outer.reply)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                outer.answered += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def provider():
    servers: list[SlowProvider] = []

    def make(delay: float, reply: dict | None = None) -> SlowProvider:
        servers.append(SlowProvider(delay, reply or DECISION))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


def _setup(lab, url: str, *, turn_deadline_seconds: int = 600):
    from test_turn_recovery import ShimClient, setup
    pid, _, _ = setup(lab)  # project, environment, component and charter of the turn-recovery tests
    inv = lab.cmd(lab.admin, "start_investigation", {
        "question": "q", "charter_id": f"charter:s{pid}", "limits": {"model_tokens": 1e6, "trials": 100},
        "investigator": {"runtime": "pydantic-ai", "model": "openai-compatible:slow-model",
                         "turn_deadline_seconds": turn_deadline_seconds,
                         "settings": {"max_tokens_per_decision": 120_000}}}, project=pid)["result"]["investigation_id"]
    overrides = {"base_url": url, "api_key_env": "EIDOS_TEST_NO_SUCH_KEY", "output_mode": "json",
                 "request_timeout": 60, "abort_poll_seconds": 0.2}
    acts = InvestigatorActivities(ShimClient(lab, lab.principal(f"inv-{pid}", ["investigator"], [pid])),
                                  ShimClient(lab, lab.principal(f"wf-{pid}", ["workflow"], [pid])),
                                  runtime_overrides=overrides)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    return pid, inv, acts, base


def _decide_in_thread(acts, base) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run() -> None:
        try:
            out["result"] = ActivityEnvironment().run(acts.decide, {**base, "slot": 0})
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out["error"] = exc
        out["done_at"] = time.monotonic()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _usage_events(lab, pid, inv):
    ev = lab.q(lab.admin, "list_events", {"investigation_id": inv, "types": ["model.usage"], "limit": 50}, project=pid)
    return [e["body"]["usage"] for e in ev["events"]]


def _model_tokens(lab, pid, inv) -> tuple[dict, list[dict]]:
    budget = lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        entries = [e for e in tx.budget_entries(f"inv:{inv}") if e["dimension"] == "model_tokens"]
    return budget, entries


def _decisions(lab, pid, turn_id):
    return lab.q(lab.admin, "get_turn", {"turn_id": turn_id}, project=pid)["decisions"]


RESERVED = 120_000  # max_tokens_per_decision of ``_setup``: what the decide activity reserves


def _assert_charged_at_the_abandoned_requests_worst_case(budget, entries, usage, turn_id) -> int:
    """F2c: the abandoned request is charged at what it was admitted with (input estimate + output cap), as possible
    usage, never as zero; the rest of the reservation is released. Exact amounts; returns the worst case."""
    abandoned = usage["aborted"]["abandoned_request"]
    assert abandoned["input_estimate"] > 0
    assert abandoned["output_cap"] == output_cap({})  # ``_setup`` configures no max_output_tokens: the default cap
    worst = abandoned["input_estimate"] + abandoned["output_cap"]
    assert 0 < worst < RESERVED
    # The usage carries the same estimate (no price is configured, so no dollars); the lab charged exactly that.
    assert usage["in_flight_estimate"] == {"input_tokens": abandoned["input_estimate"],
                                           "output_tokens": abandoned["output_cap"], "basis": "admitted-worst-case"}
    ref = f"model:{turn_id}:0:1:e1"  # F5 (C2): the first begin_decision epoch is part of the ref
    assert [(e["kind"], float(e["amount"])) for e in entries if e["ref"] == ref] == [
        ("reserve", RESERVED), ("possible-usage", worst), ("release", RESERVED - worst)]
    assert budget == {"limit_amount": 1e6, "reserved": 0, "used": worst, "uncertain": worst}
    return worst


def test_stop_abandons_the_in_flight_request_and_applies_nothing(lab, provider):
    slow = provider(delay=30)
    pid, inv, acts, base = _setup(lab, slow.url)
    thread, out = _decide_in_thread(acts, base)
    assert slow.arrived.wait(20), "the decide activity never reached the provider"
    stopped_at = time.monotonic()
    lab.cmd(lab.admin, "stop_investigation", {"investigation_id": inv}, project=pid)
    thread.join(10)
    assert not thread.is_alive(), "decide kept waiting for the provider after stop_investigation"
    assert "error" not in out, out.get("error")
    assert out["result"] == {"accepted": False, "reason": "investigation-stopped", "n_calls": 0}
    assert out["done_at"] - stopped_at < 5
    # The provider saw the connection closed promptly and never got to answer.
    assert slow.closed.wait(5) and slow.closed_at is not None and slow.closed_at - stopped_at < 5
    assert slow.answered == 0
    # Usage: recorded, uncertain (the abandoned request's bill is unknown), with what was abandoned.
    [usage] = _usage_events(lab, pid, inv)
    assert usage["uncertain"] is True and usage["calls"] == 0
    assert usage["aborted"]["reason"] == "investigation-stopped" and usage["aborted"]["in_flight"] is True
    assert usage["aborted"]["abandoned_request"]["output_cap"] > 0
    # The 120k reservation is settled: nothing left reserved, a possible-usage entry and a release under its ref.
    budget, entries = _model_tokens(lab, pid, inv)
    assert budget["reserved"] == 0, budget
    kinds = {(e["kind"], e["ref"]) for e in entries}
    ref = f"model:{base['turn_id']}:0:1:e1"  # F5 (C2): with the slot epoch
    assert ("reserve", ref) in kinds and ("possible-usage", ref) in kinds and ("release", ref) in kinds
    # ... and the amounts: the possible usage is the abandoned request's admitted worst case, not zero, and it is all
    # the budget's uncertain total.
    worst = _assert_charged_at_the_abandoned_requests_worst_case(budget, entries, usage, base["turn_id"])
    [possible] = [e for e in entries if e["kind"] == "possible-usage"]
    assert float(possible["amount"]) == worst > 0 and possible["detail"]["in_flight_estimate"] is True
    assert budget["uncertain"] == worst
    assert _decisions(lab, pid, base["turn_id"]) == []  # nothing proposed, nothing accepted


def test_decision_deadline_abandons_the_in_flight_request(lab, provider):
    slow = provider(delay=30)
    pid, inv, acts, base = _setup(lab, slow.url, turn_deadline_seconds=4)  # aborted at 4 s - 10% = 3.6 s
    started = time.monotonic()
    thread, out = _decide_in_thread(acts, base)
    thread.join(15)
    assert not thread.is_alive(), "decide outlived its deadline"
    assert "error" not in out, out.get("error")
    assert out["done_at"] - started < 4  # before the activity's own timeout (the turn deadline)
    assert slow.closed.wait(5) and slow.answered == 0
    # The outcome is the explicit no-effect decision-deadline proposal; with nothing outstanding the lab refuses it
    # as a wait for nothing (ADR 0007), so it ends unaccepted either way. Nothing from the request is applied.
    assert out["result"]["n_calls"] == 0
    decisions = _decisions(lab, pid, base["turn_id"])
    assert not any(d["accepted"] and d["body"].get("calls") for d in decisions)
    [usage] = _usage_events(lab, pid, inv)
    assert usage["uncertain"] is True and usage["aborted"] == {
        "reason": "decision-deadline", "in_flight": True, "abandoned_request": usage["aborted"]["abandoned_request"]}
    assert "decision deadline" in usage["limit_reached"]
    budget, entries = _model_tokens(lab, pid, inv)
    assert budget["reserved"] == 0, budget
    # The abandoned request is charged at its admitted worst case (possible usage), the rest of the reservation released.
    worst = _assert_charged_at_the_abandoned_requests_worst_case(budget, entries, usage, base["turn_id"])
    assert budget["uncertain"] == worst > 0


def test_a_response_that_arrives_in_time_is_still_accepted(lab, provider):
    """Control: the abort machinery does not disturb a normal decision."""
    fast = provider(delay=0)
    pid, inv, acts, base = _setup(lab, fast.url)
    d = ActivityEnvironment().run(acts.decide, {**base, "slot": 0})
    assert d["accepted"] and d["n_calls"] == 1 and d["control"]["action"] == "conclude", d
    [usage] = _usage_events(lab, pid, inv)
    assert usage["calls"] == 1 and "aborted" not in usage and not usage.get("uncertain")
    budget, _ = _model_tokens(lab, pid, inv)
    assert budget["used"] == 120 and budget["reserved"] == 0


def test_stop_after_the_response_arrived_discards_the_decision(lab, provider):
    """The response completes, but the investigation was stopped while it was being produced: the decide activity
    records its (certain) usage and proposes nothing."""
    slow = provider(delay=1.5)
    pid, inv, acts, base = _setup(lab, slow.url)
    acts.runtime_overrides["abort_poll_seconds"] = 30  # the watcher will not notice; the lab still refuses
    thread, out = _decide_in_thread(acts, base)
    assert slow.arrived.wait(20)
    lab.cmd(lab.admin, "stop_investigation", {"investigation_id": inv}, project=pid)
    thread.join(20)
    assert slow.answered == 1
    assert out["result"]["accepted"] is False and out["result"]["reason"] == "investigation-stopped", out
    decisions = _decisions(lab, pid, base["turn_id"])
    assert decisions and not any(d["accepted"] for d in decisions)
    [usage] = _usage_events(lab, pid, inv)
    assert usage["calls"] == 1 and not usage.get("uncertain")  # a completed response's usage is known
    budget, entries = _model_tokens(lab, pid, inv)  # certain usage (100 in + 20 out), nothing uncertain, rest released
    assert [(e["kind"], float(e["amount"])) for e in entries] == [("reserve", RESERVED), ("usage", 120),
                                                                  ("release", RESERVED - 120)]
    assert budget == {"limit_amount": 1e6, "reserved": 0, "used": 120, "uncertain": 0}


def _turn_with_attempts(lab, deadline_seconds: int = 600):
    from test_turn_recovery import setup
    pid, inv, acts = setup(lab)
    if deadline_seconds != 600:
        inv = lab.cmd(lab.admin, "start_investigation", {
            "question": "q", "charter_id": f"charter:s{pid}", "limits": {"model_tokens": 1e6},
            "investigator": {"runtime": "fixture", "turn_deadline_seconds": deadline_seconds}},
            project=pid)["result"]["investigation_id"]
    b = ActivityEnvironment().run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    return pid, inv, b["turn_id"], lab.principal(f"wf-{pid}", ["workflow"], [pid])


NOTE = {"rationale": "r", "calls": [{"tool": "record_note", "args": {"kind": "observation", "text": "x"}}],
             "control": {}}


def test_a_superseded_attempt_never_gets_its_decision_applied(lab):
    pid, inv, tid, wf = _turn_with_attempts(lab)
    for attempt in (1, 2):
        lab.cmd(wf, "reserve_model_budget", {"turn_id": tid, "slot": 0, "attempt": attempt,
                                             "estimate": {"model_tokens": 1000}}, project=pid)
    # Attempt 1 timed out (Temporal retried it as attempt 2) and its request finished late.
    late = lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "attempt": 1, "proposal": NOTE},
                   project=pid)["result"]
    assert late == {"accepted": False, "decision_id": late["decision_id"], "reason": "superseded-attempt"}
    ok = lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "attempt": 2, "proposal": NOTE},
                 project=pid)["result"]
    assert ok["accepted"]
    ev = lab.q(lab.admin, "list_events", {"investigation_id": inv, "types": ["decision.unapplied"]}, project=pid)
    assert [e["body"]["reason"] for e in ev["events"]] == ["superseded-attempt"]


def test_an_attempt_past_its_deadline_never_gets_its_decision_applied(lab):
    pid, inv, tid, wf = _turn_with_attempts(lab, deadline_seconds=60)
    lab.cmd(wf, "reserve_model_budget", {"turn_id": tid, "slot": 0, "attempt": 1, "estimate": {}}, project=pid)
    with lab.ledger.transaction() as tx:  # the attempt began 61 s ago (lab clock)
        tx.project_id = pid
        rec = tx.get_record("decision_attempt", f"{tid}:0")
        tx.put_record("decision_attempt", rec.id, pid, {**rec.body, "started_at": time.time() - 61},
                      created_by="test", status="started")
    r = lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "attempt": 1, "proposal": NOTE},
                project=pid)["result"]
    assert not r["accepted"] and r["reason"] == "decision-deadline-passed"
    # A caller that does not report its attempt is judged as before (no attempt checks).
    r2 = lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": NOTE}, project=pid)["result"]
    assert r2["accepted"]


def test_no_decision_is_applied_once_the_investigation_is_stopped(lab):
    pid, inv, tid, wf = _turn_with_attempts(lab)
    lab.cmd(lab.admin, "stop_investigation", {"investigation_id": inv}, project=pid)
    r = lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": NOTE}, project=pid)["result"]
    assert not r["accepted"] and r["reason"] == "investigation-stopped"
    assert lab.q(lab.admin, "list_jobs", {"investigation_id": inv}, project=pid) == []
