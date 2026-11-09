"""G3 (v1.0.0): a lab whose strongest profile runs candidate code as the same OS user (macOS
Seatbelt, or no sealed profile) refuses plans that require sealed (hostile-code) execution, with a
clear message, at freeze and again at launch; a same-user Seatbelt receipt never satisfies them."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg


def _draft_sealed(lab, pid, required):
    import test_plan_pipelines as tp
    claim = lab.cmd(lab.admin, "file_claim", {"statement": "s2", "kind": "improves", "scope": {"m": 2}}, project=pid)["result"]
    draft = {"success_rule_id": "r1", "assessor": {"ref": "p.assess@1.0.0"}, "analysis_unit": "pair", "cluster_by": "seed",
             "required_isolation": required, "seed_policy": {"seeds": [1]},
             "arms": [{"id": "baseline", "role": "baseline", "runs": [{"seed": 1, "cluster": "1", "jobs": [
                 {"id": "b.train", "component": "p.train@1.0.0", "inputs": {}, "config": {}, "environment": "lab"}]}]},
                      {"id": "candidate", "role": "candidate", "runs": [{"seed": 1, "cluster": "1", "jobs": [
                 {"id": "c.train", "component": "p.train@1.0.0", "inputs": {}, "config": {}, "environment": "lab"}]}]}]}
    assert tp.CHARTER
    return lab.cmd(lab.admin, "draft_evidence_plan", {"claim_id": claim["claim_id"], "charter_id": "charter:pipe",
                   "template_ref": "p.tmpl@1.0.0", "draft": draft}, project=pid)["result"]["plan_id"]


@pytest.mark.parametrize("required", [{"sealed": True}, {"profile": "linux:bwrap-sealed"}])
def test_seatbelt_lab_refuses_sealed_plan_at_freeze_and_launch(lab, required):
    from test_plan_pipelines import _setup

    from eidos.domain.errors import Invalid
    pid, _ = _setup(lab)
    plan_id = _draft_sealed(lab, pid, required)
    lab.svc.settings = {"sealed_available": True, "sealed_profile": "macos:seatbelt-sealed"}
    with pytest.raises(Invalid) as e:
        lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan_id}, project=pid)
    assert "same OS user" in str(e.value) and "linux:bwrap-sealed" in str(e.value)
    assert e.value.details["same_os_user"] is True and e.value.details["hostile_code_isolation"] is False
    # Frozen where sealed execution exists (a Linux lab), then launched on the Seatbelt lab: refused too.
    lab.svc.settings = {"sealed_available": True, "sealed_profile": "linux:bwrap-sealed"}
    lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan_id}, project=pid)
    lab.svc.settings = {"sealed_available": True, "sealed_profile": "macos:seatbelt-sealed"}
    with pytest.raises(Invalid) as e2:
        lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)
    assert "hostile-code" in str(e2.value)
    assert lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)["status"] == "frozen"


def test_no_sealed_profile_lab_refuses_sealed_plan(lab):
    from test_plan_pipelines import _setup

    from eidos.domain.errors import Invalid
    pid, _ = _setup(lab)
    plan_id = _draft_sealed(lab, pid, {"sealed": True})
    lab.svc.settings = {}
    with pytest.raises(Invalid, match="dev:same-user"):
        lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan_id}, project=pid)


def test_seatbelt_lab_still_freezes_control_level_plans(lab):
    from test_plan_pipelines import _setup
    pid, _ = _setup(lab)
    lab.svc.settings = {"sealed_available": True, "sealed_profile": "macos:seatbelt-sealed"}
    plan_id = _draft_sealed(lab, pid, {"profile": "macos:seatbelt-sealed", "controls": ["no-label-read"]})
    assert lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan_id}, project=pid)["result"]["plan_digest"]


def test_recorded_seatbelt_receipt_is_marked_same_user_by_the_service(lab):
    from test_plan_pipelines import _setup
    pid, plan_id = _setup(lab)
    r = lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)["result"]
    job = next(j for js in r["jobs"].values() for j in js)
    launcher = lab.principal(f"lr-{pid}", ["launcher"], [pid])
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    lab.cmd(launcher, "record_isolation_receipt", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
            "receipt": {"profile": "macos:seatbelt-sealed", "same_os_user": False, "hostile_code_isolation": True,
                        "controls": {}}}, project=pid)
    rec = lab.q(lab.admin, "get_record", {"kind": "isolation_receipt", "id": st["attempt_id"]}, project=pid)
    assert rec["body"]["same_os_user"] is True and rec["body"]["hostile_code_isolation"] is False  # not the payload's claim
