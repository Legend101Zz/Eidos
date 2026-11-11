"""Review 2 finding 3: every read path that reveals evaluation outcomes records an exposure, attributed to
the study (via the investigation or the record's charter_ref)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.pg


def _exposures(lab, pid, who):
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        return [e for e in tx.exposures(pid) if e["principal_id"] == who]


def test_outcome_revealing_reads_record_exposures(lab):
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid})
    inv = "inv_x"
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        tx.put_record("investigation", inv, pid, {"charter_ref": None}, created_by="t", status="active")
        tx.put_record("claim", "claim_x", pid, {"statement": "s", "investigation_id": inv}, created_by="t",
                      status="assessed", labels={"latest_assessment": "assess_x", "outcome": "protocol-passed"})
        tx.put_record("assessment", "assess_y", pid, {"outcome": "protocol-passed", "conformance": "full",
                      "charter_ref": {"id": "charter:study-z", "revision": 1}}, created_by="t", status="protocol-passed")
        tx.insert_job({"id": "job_ev", "project_id": pid, "investigation_id": inv, "kind": "evidence-arm",
                       "trial_id": None, "purpose": "evidence", "status": "succeeded", "lock": {},
                       "lock_digest": "d", "created_by": "t"})
        tx.update_job("job_ev", terminal={"receipt": {}})
        tx.append_event(pid, "assessment.committed", "assess_x", {"plan": "p", "outcome": "protocol-passed"},
                        investigation_id=inv)
        h = tx.horizon()
    r = lab.principal(f"reader-{pid}", ["reader"], [pid])
    reads = {
        "list_events": lambda: lab.q(r, "list_events", {"investigation_id": inv, "types": ["assessment.committed"]}, project=pid),
        "list_records(claim)": lambda: lab.q(r, "list_records", {"kind": "claim"}, project=pid),
        "get_record(claim)": lambda: lab.q(r, "get_record", {"kind": "claim", "id": "claim_x"}, project=pid),
        "get_investigation": lambda: lab.q(r, "get_investigation", {"investigation_id": inv}, project=pid),
        "investigation_view": lambda: lab.q(r, "investigation_view", {"investigation_id": inv, "horizon": h}, project=pid),
    }
    for name, fn in reads.items():
        before = len(_exposures(lab, pid, r.id))
        fn()
        assert len(_exposures(lab, pid, r.id)) > before, f"{name} recorded no exposure"
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        tx.insert_artifact({"id": "art_ev", "project_id": pid, "digest": "0" * 64, "size": 1,
                            "media_type": "application/json", "origin": "job", "producer_job": "job_ev"})
    before = len(_exposures(lab, pid, r.id))
    lab.q(r, "get_artifact", {"artifact_id": "art_ev"}, project=pid)   # also the download path's authorization
    assert len(_exposures(lab, pid, r.id)) > before, "evidence-arm artifact read recorded no exposure"
    lab.q(r, "get_record", {"kind": "assessment", "id": "assess_y"}, project=pid)
    last = _exposures(lab, pid, r.id)[-1]
    assert last["study_id"] == "study-z", last


def test_transient_deadlock_is_retried_once_and_commits_once(lab, monkeypatch):
    """Review 2 finding 5: lock-order deadlocks (40P01) between commands are retried, not surfaced."""
    import dataclasses

    from eidos.application import service

    class Deadlock(Exception):
        sqlstate = "40P01"

    spec = service.COMMANDS["create_project"]
    calls = {"n": 0}

    def flaky(ctx, p):
        calls["n"] += 1
        out = spec.fn(ctx, p)
        if calls["n"] == 1:
            raise Deadlock("deadlock detected")  # after the handler's writes: the transaction must roll back
        return out

    monkeypatch.setitem(service.COMMANDS, "create_project", dataclasses.replace(spec, fn=flaky))
    pid = f"p{uuid.uuid4().hex[:8]}"
    r = lab.cmd(lab.admin, "create_project", {"project_id": pid}, key="k-" + pid)
    assert calls["n"] == 2 and not r.get("replayed")
    again = lab.cmd(lab.admin, "create_project", {"project_id": pid}, key="k-" + pid)
    assert again.get("replayed") is True


def test_needs_input_is_durable_visible_and_cleared_by_an_operator_action(lab):
    """Review 3 open item: surfacing for input is a ledger state (label + event), visible in the investigation
    and project summary, and cleared (with an event) by steering."""
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid})
    lab.cmd(lab.admin, "commit_study_charter", {"charter": {"study_id": f"s{pid}", "question": "q",
                                                            "exploration_only": True}}, project=pid)
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "charter_id": f"charter:s{pid}"},
                  project=pid)["result"]["investigation_id"]
    wf = lab.principal(f"wf-{pid}", ["workflow"], [pid])
    r = lab.cmd(wf, "mark_needs_input", {"investigation_id": inv, "reason": "refused", "refused_turns": 3}, project=pid)
    assert not r["result"]["already"]
    again = lab.cmd(wf, "mark_needs_input", {"investigation_id": inv, "reason": "refused"}, project=pid)
    assert again["result"]["already"]
    shown = lab.q(lab.admin, "get_investigation", {"investigation_id": inv}, project=pid)
    assert shown["needs_input"]["refused_turns"] == 3 and shown["status"] == "active"
    assert lab.q(lab.admin, "project_summary", {}, project=pid)["investigations_needing_input"] == [inv]
    lab.cmd(lab.admin, "steer_investigation", {"investigation_id": inv, "instruction": "try a different approach"},
            project=pid)
    shown = lab.q(lab.admin, "get_investigation", {"investigation_id": inv}, project=pid)
    assert not shown["needs_input"]
    types = [e["type"] for e in lab.q(lab.admin, "list_events", {"investigation_id": inv, "limit": 500},
                                      project=pid)["events"]]
    assert "investigation.needs_input" in types and "investigation.input_received" in types


def test_turn_failure_retry_and_needs_input_code_are_on_the_ledger(lab):
    """ADR 0019: a failed turn records why (turn result and turn.completed event), a scheduled automatic retry is an
    event, and the needs-input mark carries a machine-readable code and bounded details."""
    from eidos.domain.errors import Invalid
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid})
    lab.cmd(lab.admin, "commit_study_charter", {"charter": {"study_id": f"s{pid}", "question": "q",
                                                            "exploration_only": True}}, project=pid)
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "charter_id": f"charter:s{pid}"},
                  project=pid)["result"]["investigation_id"]
    wf = lab.principal(f"wf-{pid}", ["workflow"], [pid])
    tid = lab.cmd(wf, "begin_turn", {"investigation_id": inv}, project=pid)["result"]["turn_id"]
    failure = {"activity": "decide", "error_type": "MODEL_CALL_FAILED", "retryable": True,
               "retry_state": "MAXIMUM_ATTEMPTS_REACHED", "message": "UnexpectedModelBehavior: token limit"}
    lab.cmd(wf, "complete_turn", {"turn_id": tid, "stop_reason": "error", "failure": failure}, project=pid)
    turn = lab.q(lab.admin, "get_turn", {"turn_id": tid}, project=pid)
    assert turn["status"] == "failed" and turn["result"]["failure"] == failure

    retry = lab.cmd(wf, "record_turn_retry", {"investigation_id": inv, "failed_turn_id": tid, "attempt": 1,
                                              "max_attempts": 2, "delay_seconds": 60, "failure": failure},
                    project=pid)["result"]
    assert retry["attempt"] == 1 and retry["failure"] == failure

    with pytest.raises(Invalid, match="code must be one of"):
        lab.cmd(wf, "mark_needs_input", {"investigation_id": inv, "code": "no-such-code"}, project=pid)
    details = {"turn_id": tid, "retryable": False, "failure": {**failure, "message": "x" * 5000}}
    mark = lab.cmd(wf, "mark_needs_input", {"investigation_id": inv, "code": "turn-failed", "reason": "turn failed",
                                            "details": details}, project=pid)["result"]["needs_input"]
    assert mark["code"] == "turn-failed" and mark["details"]["truncated"] is True
    assert len(mark["details"]["preview"]) <= 2000
    shown = lab.q(lab.admin, "get_investigation", {"investigation_id": inv}, project=pid)
    assert shown["needs_input"]["code"] == "turn-failed" and shown["turns"][0]["stop_reason"] == "error"
    events = lab.q(lab.admin, "list_events", {"investigation_id": inv, "limit": 500}, project=pid)["events"]
    by_type = {e["type"]: e["body"] for e in events}
    assert by_type["turn.completed"]["failure"] == failure
    assert by_type["investigation.turn_retry_scheduled"]["delay_seconds"] == 60
    assert by_type["investigation.needs_input"]["code"] == "turn-failed"

    # Older callers (no code) keep their meaning: refused proposals, or a coordinator limit.
    lab.cmd(lab.admin, "steer_investigation", {"investigation_id": inv, "instruction": "go on"}, project=pid)
    m = lab.cmd(wf, "mark_needs_input", {"investigation_id": inv, "reason": "r", "refused_turns": 3}, project=pid)
    assert m["result"]["needs_input"]["code"] == "refused-proposals"
