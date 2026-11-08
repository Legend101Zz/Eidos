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


@fixture_policy("test-wait-on-nothing")
def _wait_on_nothing(req):
    rejected = [x for x in req.turn_so_far if x.get("rejected")]
    if not rejected:
        return {"rationale": "idle", "calls": [], "control": {"action": "wait_for_jobs"}}
    return {"rationale": "told there is nothing to wait for", "calls": [],
            "control": {"action": "conclude", "summary": str(rejected[-1]["error"]["message"])[:200]}}


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
                  "investigator": {"runtime": "fixture", "settings": {"policy": "test-run-echo-then-wait",
                                                                      "max_tokens_per_decision": 120_000}}},
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


def test_in_turn_model_budget_is_reserved_before_each_decision_and_released(lab):
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    # Leave room for one reservation (default envelope 120k tokens) but not two.
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        tx.reserve(f"inv:{inv}", "model_tokens", 1e6 - 150_000, "test:fill")
    d0 = env.run(acts.decide, {**base, "slot": 0})
    assert d0["accepted"]
    with lab.ledger.transaction() as tx:  # simulate a reservation leaked by a dead activity
        tx.project_id = pid
        tx.reserve(f"inv:{inv}", "model_tokens", 100_000, f"model:{b['turn_id']}:9:1")
    d1 = env.run(acts.decide, {**base, "slot": 1})
    assert not d1["accepted"] and d1["reason"] == "budget-exhausted"  # no model call without budget
    env.run(acts.complete_turn, {**base, "stop_reason": "budget-exhausted"})
    turn = lab.q(lab.admin, "get_turn", {"turn_id": b["turn_id"]}, project=pid)
    assert turn["result"]["released_model_reservations"]["model_tokens"] == 100_000
    b2 = lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]
    assert b2["reserved"] == 1e6 - 150_000 and b2["used"] > 0  # only the test fill remains reserved


def test_context_build_retry_after_new_evidence_is_idempotent(lab):
    """Retrying build_context after the ledger moved on must neither conflict nor change the snapshot."""
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    first = env.run(acts.build_context, base)
    # New evidence and a budget change after the horizon, then a forced rebuild (as after a crash).
    lab.cmd(lab.admin, "record_note", {"investigation_id": inv, "text": "late"}, project=pid)
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        tx.record_usage(f"inv:{inv}", "model_tokens", 5, "late-usage")
    orig = acts.wf.query
    calls = {"n": 0}

    def first_read_hides_context(name, params=None, **k):
        out = orig(name, params, **k)
        if name == "get_turn":
            calls["n"] += 1
            if calls["n"] == 1:  # force the rebuild path once
                out = {**out, "context": None}
        return out
    acts.wf.query = first_read_hides_context
    try:
        second = env.run(acts.build_context, base)
    finally:
        acts.wf.query = orig
    assert second["context_artifact_id"] == first["context_artifact_id"]


def test_retried_decide_does_not_collide_with_its_own_dead_reservation(lab):
    """Review 2 finding 2: attempt 1 reserved and died; the retry must not see BUDGET_EXHAUSTED from its own
    leaked reservation, and the dead reservation is released exactly once."""
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    with lab.ledger.transaction() as tx:      # leave 150k of 1M: room for ONE 120k reservation
        tx.project_id = pid
        tx.reserve(f"inv:{inv}", "model_tokens", 1e6 - 150_000, "test:fill")
    wf = lab.principal(f"wf-{pid}", ["workflow"], [pid])
    lab.cmd(wf, "reserve_model_budget", {"turn_id": b["turn_id"], "slot": 0, "attempt": 1,
                                         "estimate": {"model_tokens": 120_000}}, project=pid)
    env2 = ActivityEnvironment()
    env2.info = env2.info.__class__(**{**env2.info.__dict__, "attempt": 2})
    d = env2.run(acts.decide, {**base, "slot": 0})
    assert d.get("reason") != "budget-exhausted", d
    # a late settlement from the dead attempt releases nothing more
    lab.cmd(wf, "record_model_usage", {"turn_id": b["turn_id"], "usage": {"input_tokens": 0, "output_tokens": 0},
                                       "reserved": {"model_tokens": 120_000},
                                       "reservation_ref": f"model:{b['turn_id']}:0:1"}, project=pid)
    bud = lab.q(lab.admin, "budgets", {"investigation_id": inv}, project=pid)["budgets"]["model_tokens"]
    assert bud["reserved"] >= 1e6 - 150_000 - 1, bud   # the test fill is still held, not released twice


def test_wait_for_jobs_with_nothing_outstanding_is_refused_within_the_turn(lab):
    """A decision that waits while no jobs or evidence plans are outstanding would park the investigation until
    the max-idle timer. It is refused as an invalid proposal, fed back to the next slot of the same turn."""
    pid, _, acts = setup(lab)
    wf = lab.principal(f"wf-{pid}", ["workflow"], [pid])
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "charter_id": f"charter:s{pid}",
                  "limits": {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100},
                  "investigator": {"runtime": "fixture", "settings": {"policy": "test-wait-on-nothing"}}},
                  project=pid)["result"]["investigation_id"]
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    d0 = env.run(acts.decide, {**base, "slot": 0})
    assert not d0["accepted"] and d0.get("reason") == "invalid", d0
    turn = lab.q(lab.admin, "get_turn", {"turn_id": b["turn_id"]}, project=pid)
    refused = [d for d in turn["decisions"] if not d["accepted"]]
    assert "nothing to wait for" in refused[-1]["body"]["error"]["message"]
    d1 = env.run(acts.decide, {**base, "slot": 1})
    assert d1["accepted"] and d1["control"]["action"] == "conclude", d1
    # with a job outstanding, waiting is legitimate
    ok = lab.cmd(wf, "accept_decision", {"turn_id": b["turn_id"], "slot": 6, "proposal": {
        "rationale": "launch", "calls": [{"tool": "run_component", "args": {"component_ref": "test.echo@1.0.0",
                                                                            "environment": "lab", "config": {}}}],
        "control": {"action": "wait_for_jobs"}}}, project=pid)["result"]
    assert ok["accepted"], ok


def test_wait_refused_when_launched_jobs_finished_before_the_decision_and_next_view_sees_them(lab):
    """Timing variant: the job launched in slot 0 finishes before slot 1 proposes to wait for it. The wait is
    refused (nothing outstanding) and the next turn's view reports the job as finished, so a policy that
    decides from the view can proceed."""
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    d0 = env.run(acts.decide, {**base, "slot": 0})
    env.run(acts.execute_decision, {**base, "decision_id": d0["decision_id"]})
    jobs = lab.q(lab.admin, "list_jobs", {"investigation_id": inv}, project=pid)
    with lab.ledger.transaction() as tx:  # the job finishes immediately (fast runner)
        tx.project_id = pid
        for j in jobs:
            tx.update_job(j["id"], status="succeeded", terminal={"receipt": {}})
            tx.append_event(pid, "job.succeeded", j["id"], {}, investigation_id=inv)
    d1 = env.run(acts.decide, {**base, "slot": 1})
    assert not d1["accepted"] and d1["reason"] == "invalid", d1
    env.run(acts.complete_turn, {**base, "stop_reason": "invalid-proposals"})
    h = lab.q(lab.admin, "project_summary", {}, project=pid)["horizon"]
    view = lab.q(lab.admin, "investigation_view", {"investigation_id": inv, "horizon": h}, project=pid)
    assert {j["status"] for j in view["jobs"]} == {"succeeded"}, view["jobs"]


@fixture_policy("test-failing-call-then-wait")
def _failing_then_wait(req):
    return {"rationale": "launch a trial that does not exist", "calls": [
        {"tool": "launch_trial", "args": {"trial_id": "trial_missing", "purpose": "open-search"}}],
        "control": {"action": "wait_for_jobs"}}


def test_wait_after_calls_that_all_failed_reports_nothing_outstanding(lab):
    """Mac request 8: the only call fails at execution and the decision waits; the turn must learn that there is
    nothing to wait for (and which call failed) instead of parking until max-idle."""
    pid, _, acts = setup(lab)
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "charter_id": f"charter:s{pid}",
                  "investigator": {"runtime": "fixture", "settings": {"policy": "test-failing-call-then-wait"}}},
                  project=pid)["result"]["investigation_id"]
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    base = {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv}
    env.run(acts.build_context, base)
    d = env.run(acts.decide, {**base, "slot": 0})
    assert d["accepted"], d
    r = env.run(acts.execute_decision, {**base, "decision_id": d["decision_id"]})
    assert r.get("nothing_outstanding") is True and r["failed_calls"][0]["tool"] == "launch_trial", r


def test_environment_must_be_a_registered_name_and_author_source_returns_a_source_artifact(lab):
    """Mac requests 9-10: a non-string or unknown environment is refused at acceptance, listing the names; the
    whole-trainer path has its own tool (author_source) with the same meaning in the lab and the lab-free driver."""
    pid, inv, acts = setup(lab)
    env = ActivityEnvironment()
    b = env.run(acts.begin_turn, {"investigation_id": inv, "project_id": pid, "index": 1})
    wf = lab.principal(f"wf-{pid}", ["workflow"], [pid])
    bad = lab.cmd(wf, "accept_decision", {"turn_id": b["turn_id"], "slot": 0, "proposal": {
        "rationale": "x", "calls": [{"tool": "author_component", "args": {"files": {"a.py": "x"},
                                                                         "environment": {"base": "project-root"}}}],
        "control": {}}}, project=pid)["result"]
    assert not bad["accepted"] and "lab" in bad["error"]["message"] and "environment" in bad["error"]["message"]
    ok = lab.cmd(wf, "accept_decision", {"turn_id": b["turn_id"], "slot": 1, "proposal": {
        "rationale": "x", "calls": [{"tool": "author_source", "args": {"files": {"project.py": "print(1)"}}}],
        "control": {}}}, project=pid)["result"]
    assert ok["accepted"], ok
    r = env.run(acts.execute_decision, {"turn_id": b["turn_id"], "project_id": pid, "investigation_id": inv,
                                        "decision_id": ok["decision_id"]})
    assert r["summary"][0] == {"tool": "author_source", "ok": True, "replayed": None, "code": None} or r["summary"][0]["ok"]
