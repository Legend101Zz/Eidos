"""The single authoritative write path against real PostgreSQL: grants, idempotency, charters,
revisions and fencing."""

from __future__ import annotations

import copy
import threading
import uuid

import pytest
import sqlalchemy

from eidos.domain.errors import (
    CharterRequired,
    CommandConflict,
    Fenced,
    Forbidden,
    NotFound,
    StaleRevision,
    ThresholdOverride,
)

pytestmark = pytest.mark.pg

CHARTER = {
    "study_id": "s1", "question": "does the edit help?", "population": "pairs", "comparator": "parent",
    "metrics": {"both_correct": {"direction": "increase", "denominator": "pairs"}},
    "success_rules": [{"id": "r1", "metric": "both_correct", "min_effect": 0.1,
                       "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                       "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95}}],
}


def project(lab, name=None):
    pid = name or f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid, "budgets": {"compute_seconds": 10_000, "trials": 50}})
    return pid


def test_service_roles_cannot_mutate_ledger_directly(pg_roles, lab):
    pid = project(lab)
    reader = sqlalchemy.create_engine(pg_roles.url("reader"))
    with reader.connect() as c, pytest.raises(sqlalchemy.exc.ProgrammingError):
        c.exec_driver_sql(f"INSERT INTO ledger.events(project_id, type) VALUES ('{pid}', 'forged')")
    with reader.connect() as c, pytest.raises(sqlalchemy.exc.ProgrammingError):
        c.exec_driver_sql("SELECT token_hash FROM ledger.principals")
    writer = sqlalchemy.create_engine(pg_roles.url("writer"))
    with writer.connect() as c, pytest.raises(sqlalchemy.exc.DBAPIError):
        c.exec_driver_sql("UPDATE ledger.records SET body='{}'::jsonb")
    with writer.connect() as c, pytest.raises(sqlalchemy.exc.DBAPIError):
        c.exec_driver_sql("DELETE FROM ledger.events")
    reader.dispose()
    writer.dispose()


def test_lost_reply_returns_same_receipt_and_changed_payload_conflicts(lab):
    pid = project(lab)
    r1 = lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER}, project=pid, key="k-1")
    r2 = lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER}, project=pid, key="k-1")
    assert r2["replayed"] and r2["result"] == r1["result"]
    other = copy.deepcopy(CHARTER)
    other["question"] = "different"
    with pytest.raises(CommandConflict):
        lab.cmd(lab.admin, "commit_study_charter", {"charter": other}, project=pid, key="k-1")


def test_concurrent_same_key_admits_once(lab):
    pid = project(lab)
    results, errors = [], []

    def go():
        try:
            results.append(lab.cmd(lab.admin, "start_investigation", {"question": "q", "investigation_id": "inv-c"},
                                   project=pid, key="same-key"))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sum(1 for r in results if not r["replayed"]) == 1
    with lab.ledger.transaction() as tx:
        assert len([e for e in tx.events_after(pid, 0) if e["type"] == "investigation.started"]) == 1


def test_scope_and_project_isolation(lab):
    pid, other = project(lab), project(lab)
    inv = lab.principal("investigator-x", ["investigator"], [pid])
    with pytest.raises(Forbidden):
        lab.cmd(inv, "commit_study_charter", {"charter": CHARTER}, project=pid)
    with pytest.raises(NotFound):
        lab.q(inv, "project_summary", project=other)


def test_exploration_requires_charter_and_steering_cannot_bypass_rules(lab):
    pid = project(lab)
    r = lab.cmd(lab.admin, "start_investigation", {"question": "q"}, project=pid)
    inv = r["result"]["investigation_id"]
    from eidos.application.handlers.investigations import check_exploration_allowed
    from eidos.application.service import Ctx
    with lab.ledger.transaction() as tx, pytest.raises(CharterRequired):
        tx.project_id = pid
        check_exploration_allowed(Ctx(tx, lab.admin, pid, None, lab.store, {}), inv)
    with pytest.raises(ThresholdOverride):
        lab.cmd(lab.admin, "steer_investigation", {"investigation_id": inv, "instruction": "lower the bar",
                                                   "structured": {"min_effect": 0.01}}, project=pid)
    lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER, "investigation_id": inv}, project=pid)
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        assert check_exploration_allowed(Ctx(tx, lab.admin, pid, None, lab.store, {}), inv)["id"] == "charter:s1"


def test_amendment_after_exposure_preserves_history_across_new_study_id(lab):
    pid = project(lab)
    lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER}, project=pid)
    lab.cmd(lab.admin, "record_exposure", {"study_id": "s1", "subject": "dev-score", "disclosed": {"both": 0.3}},
            project=pid)
    new = copy.deepcopy(CHARTER)
    new["study_id"] = "s1-renamed"
    new["success_rules"][0]["min_effect"] = 0.05
    with pytest.raises(StaleRevision):
        lab.cmd(lab.admin, "amend_study_charter", {"charter_id": "charter:s1", "charter": new, "rationale": "r"},
                project=pid, rev=7)
    r = lab.cmd(lab.admin, "amend_study_charter", {"charter_id": "charter:s1", "charter": new, "rationale": "pilot"},
                project=pid, rev=1)["result"]
    assert r["charter_id"] == "charter:s1-renamed" and r["exposure_informed"] and r["exposure_informed_lineage"]
    old = lab.q(lab.admin, "get_record", {"kind": "charter", "id": "charter:s1"}, project=pid)
    assert old["status"] == "superseded" and old["body"]["success_rules"][0]["min_effect"] == 0.1
    amended = lab.q(lab.admin, "get_record", {"kind": "charter", "id": "charter:s1-renamed"}, project=pid)
    assert amended["body"]["amendment"]["changes"]["success_rules"]["before"][0]["min_effect"] == 0.1


def _register_lab_env(lab, pid):
    import sys
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}},
            project=pid)


def _publish(lab, pid, manifest: dict, files: dict, *, bundle_bytes: bytes | None = None):
    """Publish via the real path but with a synthetic passing validation job (unit-level)."""
    import json as _json
    if bundle_bytes is None:
        bundle = {"files": {"eidos-component.json": _json.dumps(manifest), **files}}
        bundle_bytes = _json.dumps(bundle).encode()
    digest, size = lab.store.put_bytes(bundle_bytes)
    art = lab.cmd(lab.admin, "register_artifact", {"digest": digest, "size": size, "schema": "source-bundle/1",
                                                   "media_type": "application/vnd.eidos.source-bundle+json"},
                  project=pid)["result"]["artifact_id"]
    job = lab.cmd(lab.admin, "validate_component", {"bundle_artifact_id": art, "environment": "lab"},
                  project=pid)["result"]["job_id"]
    launcher = lab.principal(f"launcher-{uuid.uuid4().hex[:4]}", ["launcher"], [pid])
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    rd, rs = lab.store.put_bytes(b'{"passed": true}')
    lab.cmd(launcher, "publish_outputs", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
            "manifest": {"wire": "eidos-wire/1", "job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                         "status": "succeeded", "outputs": [{"slot": "validation_report", "filename": "r.json",
                                                             "digest": rd, "size": rs}]}}, project=pid)
    lab.cmd(launcher, "finish_attempt", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                                         "status": "succeeded", "receipt": {"passed": True}}, project=pid)
    return lab.cmd(lab.admin, "publish_component", {"bundle_artifact_id": art, "validation_job_id": job},
                   project=pid)["result"]


def test_fencing_rejects_stale_epoch_publication_but_records_usage(lab):
    pid = project(lab)
    _register_lab_env(lab, pid)
    man = {"id": "demo.op", "release": "1.0.0", "implements": "demo/1", "kind": "artifact-op", "entry": "m:run",
           "scientific_effects": ["observe"], "output": {"report": "demo-report/1"}}
    rel = _publish(lab, pid, man, {"m.py": "def run(i, c):\n    return {}\n"})
    job = lab.cmd(lab.admin, "run_component", {"component_ref": rel["component_ref"], "environment": "lab"},
                  project=pid)["result"]["job_id"]
    launcher = lab.principal("launcher-f", ["launcher"], [pid])
    a1 = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    a2 = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    assert a2["epoch"] == a1["epoch"] + 1
    digest, size = lab.store.put_bytes(b'{"x": 1}')
    manifest = {"wire": "eidos-wire/1", "job_id": job, "attempt_id": a1["attempt_id"], "epoch": a1["epoch"],
                "status": "succeeded", "outputs": [{"slot": "report", "filename": "outputs/report.json",
                                                    "digest": digest, "size": size, "media_type": "application/json"}]}
    with pytest.raises(Fenced):
        lab.cmd(launcher, "publish_outputs", {"job_id": job, "attempt_id": a1["attempt_id"], "epoch": a1["epoch"],
                                              "manifest": manifest}, project=pid)
    stale = lab.cmd(launcher, "finish_attempt", {"job_id": job, "attempt_id": a1["attempt_id"], "epoch": a1["epoch"],
                                                 "status": "succeeded", "usage": {"compute_seconds": 3}}, project=pid)
    assert stale["result"]["fenced"]
    m2 = {**manifest, "attempt_id": a2["attempt_id"], "epoch": a2["epoch"]}
    pub = lab.cmd(launcher, "publish_outputs", {"job_id": job, "attempt_id": a2["attempt_id"], "epoch": a2["epoch"],
                                                "manifest": m2}, project=pid)["result"]
    assert pub["published"][0]["slot"] == "report"
    dup = lab.cmd(launcher, "publish_outputs", {"job_id": job, "attempt_id": a2["attempt_id"], "epoch": a2["epoch"],
                                                "manifest": m2}, project=pid)["result"]
    assert dup["duplicates"] == ["report"]
    done = lab.cmd(launcher, "finish_attempt", {"job_id": job, "attempt_id": a2["attempt_id"], "epoch": a2["epoch"],
                                                "status": "succeeded", "usage": {"compute_seconds": 5}}, project=pid)
    assert done["result"]["job_status"] == "succeeded"
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["compute_seconds"]["used"] == 8 and b["compute_seconds"]["reserved"] == 0
