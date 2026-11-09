"""Evidence-plan arms as multi-step pipelines (per-seed train → post → evaluate DAGs)."""

from __future__ import annotations

import sys
import uuid

import pytest

pytestmark = pytest.mark.pg

CHARTER = {
    "study_id": "pipe", "question": "q", "population": "pairs", "comparator": "baseline",
    "metrics": {"both_correct_rate": {"direction": "increase", "denominator": "pairs"}},
    "success_rules": [{"id": "r1", "metric": "both_correct_rate", "min_effect": 0.1,
                       "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                       "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95}}],
}


def _setup(lab):
    from test_command_path import _publish, project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}}, project=pid)
    _publish(lab, pid, {"id": "p.train", "release": "1.0.0", "implements": "train/1", "kind": "artifact-op", "entry": "m:run",
                        "scientific_effects": ["change-weights"], "output": {"checkpoint": "ckpt/1"}}, {"m.py": "x=1\n"})
    _publish(lab, pid, {"id": "p.eval", "release": "1.0.0", "implements": "eval/1", "kind": "artifact-op", "entry": "m:run",
                        "scientific_effects": ["observe"], "input": {"checkpoint": "ckpt/1"},
                        "output": {"predictions": "pairs/1"}}, {"m.py": "x=1\n"})
    for cid, kind, eff, impl in (("p.tmpl", "planning", "plan", "evidence-plan/1"),
                                 ("p.assess", "assessment", "assess", "protocol-assessment/1")):
        _publish(lab, pid, {"id": cid, "release": "1.0.0", "implements": impl, "kind": kind, "entry": "m:run",
                            "scientific_effects": [eff]}, {"m.py": "x=1\n"})
        suite = {"target": {"kind": "assessor"}, "scope": ["improves"], "controls": [
            {"id": "p", "type": "planted", "input": "f1"}, {"id": "n", "type": "null", "input": "f2"}]}
        s = lab.cmd(lab.admin, "register_control_suite", {"suite": suite, "suite_id": f"suite:{cid}"}, project=pid)["result"]
        rel = lab.q(lab.admin, "describe_component", {"component_ref": f"{cid}@1.0.0"}, project=pid)
        lab.cmd(lab.admin, "submit_qualification", {"suite_id": s["suite_id"], "suite_digest": s["suite_digest"],
                "release_ref": f"{cid}@1.0.0", "release_digest": rel["body"]["digest"],
                "results": {"p": [{"detected": True}], "n": [{"detected": False}]}}, project=pid)
        lab.cmd(lab.admin, "promote_protocol", {"release_ref": f"{cid}@1.0.0", "scope": ["improves"]}, project=pid)
    lab.cmd(lab.admin, "commit_study_charter", {"charter": CHARTER}, project=pid)
    claim = lab.cmd(lab.admin, "file_claim", {"statement": "s", "kind": "improves", "scope": {"m": 1}}, project=pid)["result"]
    runs = lambda arm: [{"seed": s, "cluster": str(s), "jobs": [  # noqa: E731
        {"id": f"{arm}.s{s}.train", "component": "p.train@1.0.0", "inputs": {}, "config": {"seed": s}, "environment": "lab"},
        {"id": f"{arm}.s{s}.evaluate", "component": "p.eval@1.0.0", "inputs": {"checkpoint": f"{arm}.s{s}.train.checkpoint"},
         "config": {}, "environment": "lab"}]} for s in (1, 2)]
    draft = {"success_rule_id": "r1", "assessor": {"ref": "p.assess@1.0.0"}, "analysis_unit": "pair", "cluster_by": "seed",
             "required_isolation": {"profile": "dev:same-user", "controls": [], "note": "explicit exploratory choice"},
             "seed_policy": {"seeds": [1, 2]},
             "arms": [{"id": "baseline", "role": "baseline", "runs": runs("baseline")},
                      {"id": "candidate", "role": "candidate", "runs": runs("candidate")}]}
    plan = lab.cmd(lab.admin, "draft_evidence_plan", {"claim_id": claim["claim_id"], "charter_id": "charter:pipe",
                   "template_ref": "p.tmpl@1.0.0", "draft": draft}, project=pid)["result"]
    lab.cmd(lab.admin, "freeze_evidence_plan", {"plan_id": plan["plan_id"]}, project=pid)
    return pid, plan["plan_id"]


SCHEMAS = {"checkpoint": "ckpt/1", "predictions": "pairs/1"}


def _finish(lab, pid, launcher, job, status="succeeded", slot=None):
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    if status == "succeeded" and slot:
        d, n = lab.store.put_bytes(uuid.uuid4().hex.encode())
        lab.cmd(launcher, "publish_outputs", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                "manifest": {"wire": "eidos-wire/1", "job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                             "status": "succeeded", "outputs": [{"slot": slot, "filename": f"{slot}.bin", "digest": d,
                                                                 "size": n, "schema": SCHEMAS[slot]}]}}, project=pid)
    lab.cmd(launcher, "finish_attempt", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
            "status": status, "final": True, "error": None if status == "succeeded" else {"category": "user-code"}},
            project=pid)


def test_pipeline_admits_steps_in_order_resolves_refs_and_blocks_on_failure(lab):
    pid, plan_id = _setup(lab)
    r = lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)["result"]
    assert sum(len(v) for v in r["jobs"].values()) == 4 and r["pending_steps"] == 4  # only train steps are roots
    launcher = lab.principal(f"l-{pid}", ["launcher"], [pid])
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)["body"]
    steps = plan["launch"]["steps"]
    _finish(lab, pid, launcher, steps["baseline/baseline.s1.train"], slot="checkpoint")
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    ev = plan["body"]["launch"]["steps"]["baseline/baseline.s1.evaluate"]
    job = lab.q(lab.admin, "get_job", {"job_id": ev}, project=pid)
    train_out = lab.q(lab.admin, "get_job", {"job_id": steps["baseline/baseline.s1.train"]}, project=pid)["outputs"][0]
    assert job["lock"]["inputs"]["checkpoint"]["artifact_id"] == train_out["artifact_id"]
    assert job["lock"]["step"] == "baseline.s1.evaluate" and job["lock"]["cluster"] == "1"
    _finish(lab, pid, launcher, steps["candidate/candidate.s2.train"], status="failed")
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    assert "candidate/candidate.s2.evaluate" in plan["body"]["launch"]["blocked"]
    # finish everything else; the plan then requests assessment even though one arm is incomplete
    for key in ("baseline/baseline.s2.train", "candidate/candidate.s1.train"):
        _finish(lab, pid, launcher, steps[key], slot="checkpoint")
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    for key, jid in plan["body"]["launch"]["steps"].items():
        if key.endswith("evaluate") and lab.q(lab.admin, "get_job", {"job_id": jid}, project=pid)["status"] == "admitted":
            _finish(lab, pid, launcher, jid, slot="predictions")
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    assert plan["status"] == "awaiting-assessment"
    inputs = lab.q(lab.admin, "assessment_inputs", {"plan_id": plan_id}, project=pid)
    assert inputs["arms"]["baseline"]["status"] == "succeeded" and inputs["arms"]["candidate"]["status"] == "incomplete"
