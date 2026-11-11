"""Evidence-plan arms as multi-step pipelines (per-seed train → post → evaluate DAGs)."""

from __future__ import annotations

import sys
import uuid

import pytest

from eidos.domain.errors import Invalid

pytestmark = pytest.mark.pg

CHARTER = {
    "study_id": "pipe", "question": "q", "population": "pairs", "comparator": "baseline",
    "metrics": {"both_correct_rate": {"direction": "increase", "denominator": "pairs"}},
    "success_rules": [{"id": "r1", "metric": "both_correct_rate", "min_effect": 0.1,
                       "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                       "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95}}],
}


def _setup(lab, *, with_investigation=False):
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
    inv = (lab.cmd(lab.admin, "start_investigation", {"question": "q"}, project=pid)["result"]["investigation_id"]
           if with_investigation else None)
    plan = lab.cmd(lab.admin, "draft_evidence_plan", {"claim_id": claim["claim_id"], "charter_id": "charter:pipe",
                   "template_ref": "p.tmpl@1.0.0", "draft": draft, **({"investigation_id": inv} if inv else {})},
                   project=pid)["result"]
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


def _launch(lab, pid, plan_id):
    return lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)["body"]["launch"]


def _active(lab, pid, launch):
    return [k for k, j in launch["steps"].items()
            if lab.q(lab.admin, "get_job", {"job_id": j}, project=pid)["status"] not in ("succeeded", "failed", "cancelled")]


def test_pipeline_admits_steps_in_order_resolves_refs_and_blocks_on_failure(lab):
    pid, plan_id = _setup(lab)
    r = lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)["result"]
    # ADR 0022 addendum R11: one job at a time under the default policy, the rest admitted as earlier ones end
    assert r["jobs"] == {"baseline": [r["jobs"]["baseline"][0]]} and r["pending_steps"] == 7 and r["max_concurrent_jobs"] == 1
    launcher = lab.principal(f"l-{pid}", ["launcher"], [pid])
    steps = _launch(lab, pid, plan_id)["steps"]
    train_job = steps["baseline/baseline.s1.train"]
    _finish(lab, pid, launcher, train_job, slot="checkpoint")
    steps = _launch(lab, pid, plan_id)["steps"]
    ev = steps["baseline/baseline.s1.evaluate"]
    job = lab.q(lab.admin, "get_job", {"job_id": ev}, project=pid)
    train_out = lab.q(lab.admin, "get_job", {"job_id": train_job}, project=pid)["outputs"][0]
    assert job["lock"]["inputs"]["checkpoint"]["artifact_id"] == train_out["artifact_id"]
    assert job["lock"]["step"] == "baseline.s1.evaluate" and job["lock"]["cluster"] == "1"
    assert _active(lab, pid, _launch(lab, pid, plan_id)) == ["baseline/baseline.s1.evaluate"]
    # run the rest in order, failing one training step: its evaluation is blocked, the others still run, one at a time
    while True:
        launch = _launch(lab, pid, plan_id)
        active = _active(lab, pid, launch)
        assert len(active) <= 1, active
        if not active:
            break
        key = active[0]
        if key == "candidate/candidate.s2.train":
            _finish(lab, pid, launcher, launch["steps"][key], status="failed")
        else:
            _finish(lab, pid, launcher, launch["steps"][key], slot="checkpoint" if key.endswith("train") else "predictions")
    plan = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)
    assert plan["body"]["launch"]["blocked"]["candidate/candidate.s2.evaluate"].startswith("predecessor")
    assert plan["status"] == "awaiting-assessment"
    inputs = lab.q(lab.admin, "assessment_inputs", {"plan_id": plan_id}, project=pid)
    assert inputs["arms"]["baseline"]["status"] == "succeeded" and inputs["arms"]["candidate"]["status"] == "incomplete"


def test_plan_concurrency_needs_the_projects_resource_policy(lab):
    pid, plan_id = _setup(lab)
    with pytest.raises(Invalid, match="resource policy allows 1"):
        lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id, "max_concurrent_jobs": 3}, project=pid)
    lab.cmd(lab.admin, "update_profile_policy", {"policy": {"resources": {"max_concurrent_jobs": 3}}}, project=pid)
    r = lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id, "max_concurrent_jobs": 3}, project=pid)["result"]
    assert sum(len(v) for v in r["jobs"].values()) == 3 and r["max_concurrent_jobs"] == 3
    launcher = lab.principal(f"l-{pid}", ["launcher"], [pid])
    launch = _launch(lab, pid, plan_id)
    # the fourth root waits for a slot: ending one job admits exactly one more
    assert len(_active(lab, pid, launch)) == 3
    first = next(k for k in launch["steps"] if k.endswith("train"))
    _finish(lab, pid, launcher, launch["steps"][first], slot="checkpoint")
    assert len(_active(lab, pid, _launch(lab, pid, plan_id))) == 3


def _investigation_plan(lab):
    """The pipeline plan of ``_setup``, launched under an investigation (so operator controls reach it)."""
    pid, plan_id = _setup(lab, with_investigation=True)
    inv = lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)["body"]["investigation_id"]
    lab.cmd(lab.admin, "launch_evidence_plan", {"plan_id": plan_id}, project=pid)
    return pid, plan_id, inv, lab.principal(f"lc-{pid}", ["launcher"], [pid])


def _status(lab, pid, plan_id):
    return lab.q(lab.admin, "get_record", {"kind": "evidence_plan", "id": plan_id}, project=pid)["status"]


def test_stopping_an_investigation_blocks_the_plans_steps_not_yet_admitted(lab):
    pid, plan_id, inv, launcher = _investigation_plan(lab)
    first = _launch(lab, pid, plan_id)["steps"]["baseline/baseline.s1.train"]
    lab.cmd(lab.admin, "stop_investigation", {"investigation_id": inv, "running_job_policy": "drain"}, project=pid)
    launch = _launch(lab, pid, plan_id)
    assert len(launch["blocked"]) == 7 and set(launch["blocked"].values()) == {"the investigation was stopped"}
    assert list(launch["steps"]) == ["baseline/baseline.s1.train"]  # the drained job runs on
    _finish(lab, pid, launcher, first, slot="checkpoint")
    assert list(_launch(lab, pid, plan_id)["steps"]) == ["baseline/baseline.s1.train"]  # nothing is admitted after a stop
    assert _status(lab, pid, plan_id) == "awaiting-assessment"


def test_a_paused_investigations_plan_admits_nothing_until_it_resumes(lab):
    pid, plan_id, inv, launcher = _investigation_plan(lab)
    first = _launch(lab, pid, plan_id)["steps"]["baseline/baseline.s1.train"]
    lab.cmd(lab.admin, "pause_investigation", {"investigation_id": inv, "running_job_policy": "drain"}, project=pid)
    _finish(lab, pid, launcher, first, slot="checkpoint")
    launch = _launch(lab, pid, plan_id)
    assert list(launch["steps"]) == ["baseline/baseline.s1.train"] and launch["blocked"] == {}
    assert _status(lab, pid, plan_id) == "executing"
    lab.cmd(lab.admin, "resume_investigation", {"investigation_id": inv}, project=pid)
    assert _active(lab, pid, _launch(lab, pid, plan_id)) == ["baseline/baseline.s1.evaluate"]


def test_a_question_change_that_cancels_work_blocks_the_rest_of_the_plan(lab):
    pid, plan_id, inv, launcher = _investigation_plan(lab)
    lab.cmd(lab.admin, "revise_question", {"investigation_id": inv, "question": "q2", "reason": "changed",
                                           "running_job_policy": "cancel"}, project=pid)
    launch = _launch(lab, pid, plan_id)
    assert len(launch["blocked"]) == 7 and all("question" in v for v in launch["blocked"].values())
    assert list(launch["steps"]) == ["baseline/baseline.s1.train"]

