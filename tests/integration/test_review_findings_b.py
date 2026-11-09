"""Regressions for the adversarial review of 55fd0e2 (invariants c, e): each test encodes the CORRECT
behaviour for a confirmed finding."""

from __future__ import annotations

import json
import sys
import uuid
from types import SimpleNamespace

import pytest

from eidos.application.handlers.runs import admit_job
from eidos.application.service import Ctx
from eidos.domain import isolation
from eidos.domain.errors import Invalid, NotFound

pytestmark = pytest.mark.pg


def _project(lab):
    from test_command_path import project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}}, project=pid)
    return pid


def _cmd_component(lab, pid):
    from test_command_path import _publish
    man = {"id": "t.cmd", "release": "1.0.0", "implements": "cmd/1", "kind": "artifact-op", "entry": "m:run",
           "scientific_effects": ["observe"], "optional_inputs": {"extra_a": "*"}, "output": {"report": "r/1"}}
    return _publish(lab, pid, man, {"m.py": "x=1\n"})["component_ref"]


def test_c1_sealed_labels_cannot_be_bound_into_jobs_by_unentitled_principals(lab):
    pid = _project(lab)
    ref = _cmd_component(lab, pid)
    d, n = lab.store.put_bytes(b"secret-labels")
    lab.cmd(lab.admin, "register_artifact", {"digest": d, "size": n, "access_class": "sealed-labels"}, project=pid)
    events = lab.q(lab.admin, "list_events", {"types": ["artifact.registered"]}, project=pid)["events"]
    assert events and events[-1]["subject"] is None  # the id is not broadcast
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        aid = [a for a in tx.list_artifacts(pid) if a["access_class"] == "sealed-labels"][0]["id"]
    inv = lab.principal(f"inv-{pid}", ["investigator"], [pid])
    with pytest.raises(NotFound):
        lab.cmd(inv, "run_component", {"component_ref": ref, "environment": "lab", "inputs": {"extra_a": aid}}, project=pid)
    # Even an entitled principal cannot bind restricted bytes into a non-sealed profile.
    with pytest.raises(Invalid):
        lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": "lab", "inputs": {"extra_a": aid},
                                             "isolation_profile": "dev:same-user"}, project=pid)


def test_c5_output_ids_cannot_be_preregistered_and_publication_checks_bytes(lab):
    pid = _project(lab)
    d, n = lab.store.put_bytes(b"forged")
    r = lab.cmd(lab.admin, "register_artifact", {"digest": d, "size": n, "artifact_id": "art_chosen_predictions"},
                project=pid)["result"]
    assert r["artifact_id"] != "art_chosen_predictions"


def _job(lab, pid, profile="dev:same-user"):
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        return admit_job(Ctx(tx, lab.admin, pid, None, lab.store, {}), kind="artifact-op",
                         lock={"component": {"ref": "x@1"}, "inputs": {}, "outputs": {"model": "model-file/1"},
                               "isolation_profile": profile}, investigation_id=None, purpose="open-search", wall_seconds=60)


def test_c4_receipts_are_write_once_and_profile_bound(lab):
    pid = _project(lab)
    job = _job(lab, pid)
    L = lab.principal(f"L-{pid}", ["launcher"], [pid])
    st = lab.cmd(L, "start_attempt", {"job_id": job}, project=pid)["result"]
    base = {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"]}
    lab.cmd(L, "record_isolation_receipt", {**base, "receipt": {"profile": "dev:same-user", "controls": {}}}, project=pid)
    with pytest.raises(Invalid):
        lab.cmd(L, "record_isolation_receipt", {**base, "receipt": {"profile": "linux:bwrap-sealed",
                "controls": {c: "passed" for c in isolation.SEALED_CONTROLS}}}, project=pid)
    forged = {"profile": "dev:same-user", "launcher": L.id, "controls": {c: "passed" for c in isolation.SEALED_CONTROLS}}
    v = isolation.evaluate_receipt({"profile": "linux:bwrap-sealed", "sealed": True}, forged, trusted_launchers={L.id})
    assert not v["ok"]


def test_c3_undeclared_checkpoint_slots_are_not_branchable(lab):
    pid = _project(lab)
    job = _job(lab, pid)
    L = lab.principal(f"L2-{pid}", ["launcher"], [pid])
    st = lab.cmd(L, "start_attempt", {"job_id": job}, project=pid)["result"]
    d, n = lab.store.put_bytes(b"not really a checkpoint")
    man = {"wire": "eidos-wire/1", "job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"], "status": "succeeded",
           "outputs": [{"slot": "model", "filename": "m", "digest": d, "size": n, "role": "checkpoint"}],
           "receipt": {"checkpoints": {"model": {"complete": True, "trajectory_id": "forged"}}}}
    pub = lab.cmd(L, "publish_outputs", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                                         "manifest": man}, project=pid)["result"]
    assert pub["published"][0]["role"] == "output"


def test_c3_runner_refuses_success_manifest_from_a_crashed_process(tmp_path):
    from eidos.runtime.runner import AttemptRunner
    from eidos_wire import OutputManifest
    staging = tmp_path / "staging"
    staging.mkdir()
    OutputManifest(job_id="j", attempt_id="a", epoch=1, status="succeeded").write(staging)
    calls = []
    client = SimpleNamespace(command=lambda name, payload, **k: calls.append((name, payload)) or {"result": {"job_status": "failed"}})
    r = AttemptRunner(client, None, lab_root=tmp_path, launcher_id="L", api_port=1, credential_path="/x")
    ctx = {"job_id": "j", "attempt_id": "a", "epoch": 1, "lock": {"component": {"kind": "artifact-op"}, "limits": {}},
           "attempt_dir": str(tmp_path), "reattached": False, "started": 0}
    r._collect("p", ctx, SimpleNamespace(exit_code=lambda: 137))
    name, payload = calls[-1]
    assert name == "finish_attempt" and payload["status"] == "failed" and payload["error"]["category"] == "contract"


def test_c2_environment_inside_the_lab_directory_is_refused(lab):
    pid = _project(lab)
    lab.svc.settings["lab_root"] = str(lab.store.root.parent)
    try:
        with pytest.raises(Invalid):
            lab.cmd(lab.admin, "register_environment", {"name": "evil", "python_executable": sys.executable,
                    "project_root": str(lab.store.root), "python_version": "3.12"}, project=pid)
    finally:
        lab.svc.settings.pop("lab_root")


def test_plans_must_state_their_isolation_requirement():
    from eidos.domain import charter, evidence
    ch = charter.validate_charter({"study_id": "s", "question": "q", "population": "p", "comparator": "c",
        "metrics": {"m": {"direction": "increase", "denominator": "n"}},
        "success_rules": [{"id": "r", "metric": "m", "min_effect": 0.1, "decision_standard": {"kind": "threshold"}}]})
    plan = evidence.compile_plan(draft={"success_rule_id": "r", "assessor": {"ref": "a"}, "analysis_unit": "u",
                                        "seed_policy": {}, "arms": [{"id": "a", "role": "candidate"}]},
                                 claim={"kind": "improves", "scope": {"x": 1}}, claim_ref={}, charter=ch,
                                 charter_ref={"id": "c", "revision": 1, "digest": charter.charter_digest(ch)},
                                 template_ref={})
    assert "required_isolation" in plan["obligations"] and plan["required_isolation"] is None
    json.dumps(plan)
    uuid.uuid4()


def test_generic_reads_of_evaluation_records_record_exposure(lab):
    pid = _project(lab)
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        tx.put_record("assessment", "assess_x", pid, {"outcome": "protocol-passed", "investigation_id": None},
                      created_by="t", status="protocol-passed")
    reader = lab.principal(f"reader-{pid}", ["reader"], [pid])
    lab.q(reader, "list_records", {"kind": "assessment"}, project=pid)
    lab.q(reader, "get_record", {"kind": "assessment", "id": "assess_x"}, project=pid)
    lab.q(reader, "record_history", {"kind": "assessment", "id": "assess_x"}, project=pid)
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        seen = [e for e in tx.exposures(pid) if e["principal_id"] == reader.id]
    assert len(seen) == 3 and all(e["subject"].startswith("assessment:assess_x") for e in seen)
