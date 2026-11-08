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
