"""Interruption guarantees of the bounded turn, using the real investigator activities against the
real command service (in-process client shim) and PostgreSQL. Fixture investigator policy."""

from __future__ import annotations

import json
import shutil
import sys
import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from eidos.client import LabError
from eidos.domain.errors import DomainError
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.runtime import fixture_policy

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]


@fixture_policy("test-run-echo-then-wait")
def _policy(req):
    if req.turn_so_far:
        return {"rationale": "wait for the echo job", "calls": [], "control": {"action": "wait_for_jobs"}}
    return {"rationale": "measure", "purpose": "measurement-development",
            "calls": [{"tool": "run_component", "args": {"component_ref": "test.echo@1.0.0", "environment": "lab",
                                                         "config": {"value": 1}}},
                      {"tool": "record_note", "args": {"kind": "hypothesis", "text": "echo returns its config"}}]}


class ShimClient:
    """LabClient-compatible adapter that calls the command service in-process."""

    def __init__(self, lab, principal):
        self.lab, self.pr = lab, principal

    def command(self, name, payload=None, *, project=None, key=None, expected_revision=None):
        try:
            return self.lab.svc.execute(self.pr, name, project_id=project, command_key=key or uuid.uuid4().hex,
                                        payload=payload or {}, expected_revision=expected_revision)
        except DomainError as exc:
            raise LabError(exc.http_status, exc.to_dict()) from exc

    def query(self, name, params=None, *, project=None):
        try:
            return json.loads(json.dumps(self.lab.svc.query(self.pr, name, project_id=project, params=params or {}),
                                         default=str))
        except DomainError as exc:
            raise LabError(exc.http_status, exc.to_dict()) from exc

    def upload(self, project, path=None, *, data=None, media_type="application/octet-stream", schema=None,
               role="input", access_class="project", name=None, key=None, investigation=None):
        digest, size = self.lab.store.put_bytes(data)
        return self.command("register_artifact", {"digest": digest, "size": size, "media_type": media_type,
                                                  "schema": schema, "role": role, "name": name}, project=project,
                            key=key or f"up:{digest}")

    def download(self, project, artifact_id, dest):
        art = self.query("get_artifact", {"artifact_id": artifact_id}, project=project)
        shutil.copyfile(self.lab.store.path_for(art["digest"]), dest)
        return dest


def setup(lab):
    from test_command_path import _publish, project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}}, project=pid)
    man = {"id": "test.echo", "release": "1.0.0", "implements": "echo/1", "kind": "artifact-op", "entry": "echo:run",
           "scientific_effects": ["observe"], "output": {"report": "echo-report/1"}}
    _publish(lab, pid, man, {"echo.py": "def run(i, c):\n    return {}\n"})
    lab.cmd(lab.admin, "commit_study_charter", {"charter": {"study_id": f"s{pid}", "question": "q", "exploration_only": True}},
            project=pid)
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "charter_id": f"charter:s{pid}",
                  "limits": {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100},
                  "investigator": {"runtime": "fixture", "settings": {"policy": "test-run-echo-then-wait"}}},
                  project=pid)["result"]["investigation_id"]
    acts = InvestigatorActivities(ShimClient(lab, lab.principal(f"inv-{pid}", ["investigator"], [pid])),
                                  ShimClient(lab, lab.principal(f"wf-{pid}", ["workflow"], [pid])))
    return pid, inv, acts


def test_retry_after_committed_tool_effect_reuses_keys_and_admits_once(lab):
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    a = {"investigation_id": inv, "project_id": pid, "index": 1}
    b = env.run(acts.begin_turn, a)
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    d = env.run(acts.decide, {**base, "slot": 0})
    assert d["accepted"] and d["n_calls"] == 2
    first = env.run(acts.execute_decision, {**base, "decision_id": d["decision_id"]})
    # The worker "dies" after the effects committed; Temporal retries the activity.
    again = env.run(acts.execute_decision, {**base, "decision_id": d["decision_id"]})
    assert [r["ok"] for r in first["summary"]] == [True, True]
    assert all(r["replayed"] for r in again["summary"])
    jobs = lab.q(lab.admin, "list_jobs", {"investigation_id": inv}, project=pid)
    assert len(jobs) == 1  # one logical admission
    # A retried decide after acceptance never regenerates the decision.
    env2 = ActivityEnvironment()
    env2.info = env2.info.__class__(**{**env2.info.__dict__, "attempt": 2})
    d2 = env2.run(acts.decide, {**base, "slot": 0})
    assert d2["decision_id"] == d["decision_id"] and d2["reason"] == "recovered"
    # The next slot reconstructs from evidence (this turn's receipts) and decides to wait.
    d3 = env.run(acts.decide, {**base, "slot": 1})
    assert d3["control"]["action"] == "wait_for_jobs" and d3["n_calls"] == 0
    env.run(acts.complete_turn, {**base, "stop_reason": "waiting", "control": d3["control"]})
    turn = lab.q(lab.admin, "get_turn", {"turn_id": b["turn_id"]}, project=pid)
    assert turn["status"] == "completed" and turn["context"]["policy"] == "context-policy/1"
    usage = lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]
    assert usage["used"] > 0  # fixture usage is still accounted


def test_late_decision_after_steering_is_saved_unapplied(lab):
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    lab.cmd(lab.admin, "steer_investigation", {"investigation_id": inv, "instruction": "focus on stratum 'adjacent'"},
            project=pid)
    d = env.run(acts.decide, {**base, "slot": 0})
    assert not d["accepted"] and d["reason"] == "stale-revision"
    assert lab.q(lab.admin, "list_jobs", {"investigation_id": inv}, project=pid) == []


def test_offline_replay_uses_recorded_horizon_and_never_executes(lab):
    from eidos.investigator.replay import evaluate_turn, load_recorded_turn
    from eidos.investigator.runtime import FIXTURE_POLICIES, FixtureRuntime
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    d = env.run(acts.decide, {**base, "slot": 0})
    env.run(acts.execute_decision, {**base, "decision_id": d["decision_id"]})
    env.run(acts.complete_turn, {**base, "stop_reason": "calls-exhausted"})
    # Later evidence must not leak into an evaluation of turn 1.
    lab.cmd(lab.admin, "record_note", {"investigation_id": inv, "text": "FUTURE-ONLY NOTE"}, project=pid)
    reader = ShimClient(lab, lab.principal(f"eval-{pid}", ["reader"], [pid]))
    rec = load_recorded_turn(reader, pid, b["turn_id"])
    assert "FUTURE-ONLY" not in json.dumps(rec["source"]) and rec["source"]["jobs"] == []
    out = evaluate_turn(reader, pid, b["turn_id"], FixtureRuntime(FIXTURE_POLICIES["conclude-immediately"],
                        policy_name="conclude-immediately"), context_policy="context-policy/minimal-1")
    assert out["executed"] is False and not out["same_calls_as_recorded"] and out["outcome"].startswith("unknown")
    with pytest.raises(LabError):  # the evaluation principal cannot resubmit production commands
        reader.command("run_component", {"component_ref": "test.echo@1.0.0"}, project=pid)
    assert len(lab.q(lab.admin, "list_jobs", {"investigation_id": inv}, project=pid)) == 1
