"""Regressions for the adversarial review of 55fd0e2 (invariants a, b, d). Each test encodes the
CORRECT behaviour for a finding that was confirmed by an executed reproduction."""

from __future__ import annotations

import uuid

import pytest

from eidos.application.handlers.runs import admit_job
from eidos.application.service import Ctx
from eidos.domain.errors import Fenced, Invalid, NotFound

pytestmark = pytest.mark.pg


def project(lab):
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid, "budgets": {"compute_seconds": 10_000, "trials": 50}})
    return pid


def mkjob(lab, pid, wall=100):
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        ctx = Ctx(tx, lab.admin, pid, None, lab.store, {})
        return admit_job(ctx, kind="artifact-op", lock={"component": {"ref": "x@1"}, "inputs": {}, "outputs": {}},
                         investigation_id=None, purpose="open-search", wall_seconds=wall)


def test_F1_other_project_cannot_touch_an_investigation_budget(lab):
    a, b = project(lab), project(lab)
    inv = f"inv-{uuid.uuid4().hex[:6]}"
    lab.cmd(lab.admin, "start_investigation", {"question": "q", "investigation_id": inv, "limits": {"model_tokens": 1000}},
            project=a)
    pb = lab.principal(f"res-b-{b}", ["researcher"], [b])
    with pytest.raises(Invalid):
        lab.cmd(pb, "start_investigation", {"question": "q", "investigation_id": inv, "limits": {"model_tokens": 10**9}},
                project=b)
    assert lab.q(lab.admin, "budgets", {"scope": f"inv:{inv}"}, project=a)["budgets"]["model_tokens"]["limit_amount"] == 1000


def test_F2_budgets_query_is_project_scoped(lab):
    a, b = project(lab), project(lab)
    rb = lab.principal(f"reader-b-{b}", ["reader"], [b])
    with pytest.raises(NotFound):
        lab.q(rb, "budgets", {"scope": f"project:{a}", "entries": True}, project=b)


def test_F3_late_finish_cannot_rewrite_a_terminal_job_or_double_count(lab):
    pid = project(lab)
    job = mkjob(lab, pid)
    L = lab.principal(f"L-{pid}", ["launcher"], [pid])
    st = lab.cmd(L, "start_attempt", {"job_id": job}, project=pid)["result"]
    base = dict(job_id=job, attempt_id=st["attempt_id"], epoch=st["epoch"])
    lab.cmd(L, "finish_attempt", {**base, "status": "failed", "final": True, "error": {"category": "infrastructure"},
                                  "usage": {"compute_seconds": 1}}, project=pid, key="finalfail:x")
    late = lab.cmd(L, "finish_attempt", {**base, "status": "succeeded", "usage": {"compute_seconds": 1}},
                   project=pid, key="finish:x")["result"]
    assert late["fenced"]
    assert lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)["status"] == "failed"
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["trials"]["used"] == 1.0 and b["compute_seconds"]["used"] == 2.0  # late usage still recorded
    d, n = lab.store.put_bytes(b"x")
    with pytest.raises(Fenced):
        lab.cmd(L, "publish_outputs", {**base, "manifest": {"wire": "eidos-wire/1", **base, "status": "succeeded",
                "outputs": [{"slot": "o", "filename": "o", "digest": d, "size": n}]}}, project=pid)
    r = lab.cmd(L, "mark_job_outcome_unknown", {"job_id": job, "reason": "late"}, project=pid)["result"]
    assert r["job_status"] == "failed"


def test_F5_new_workflow_run_start_keys_do_not_replay_stale_attempts(lab):
    """Keys now embed the workflow run id; a fresh run gets a fresh epoch."""
    pid = project(lab)
    job = mkjob(lab, pid)
    L = lab.principal(f"L3-{pid}", ["launcher"], [pid])
    s1 = lab.cmd(L, "start_attempt", {"job_id": job}, project=pid, key=f"start:{job}:runA:1")["result"]
    s2 = lab.cmd(L, "start_attempt", {"job_id": job}, project=pid, key=f"start:{job}:runB:1")["result"]
    assert s2["epoch"] == s1["epoch"] + 1 and s2["run"]


def test_F6_turn_commands_are_project_scoped(lab):
    a, b = project(lab), project(lab)
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "q", "limits": {"model_tokens": 1000}},
                  project=a)["result"]["investigation_id"]
    wf = lab.principal(f"wf-a-{a}", ["workflow"], [a])
    tid = lab.cmd(wf, "begin_turn", {"investigation_id": inv}, project=a)["result"]["turn_id"]
    lab.cmd(wf, "accept_decision", {"turn_id": tid, "slot": 0, "proposal": {"rationale": "r", "calls": [],
            "control": {"action": "wait_for_jobs"}}, "usage": {}}, project=a)
    wb = lab.principal(f"wf-b-{b}", ["workflow"], [b])
    for name, payload in (("accept_decision", {"turn_id": tid, "slot": 0, "proposal": {}, "usage": {"input_tokens": 999}}),
                          ("complete_turn", {"turn_id": tid, "stop_reason": "error"}),
                          ("record_model_usage", {"turn_id": tid, "usage": {"input_tokens": 5}})):
        with pytest.raises(Invalid):
            lab.cmd(wb, name, payload, project=b)
    assert lab.q(lab.admin, "budgets", {"scope": f"inv:{inv}"}, project=a)["budgets"]["model_tokens"]["used"] == 0
    assert lab.q(lab.admin, "get_turn", {"turn_id": tid}, project=a)["status"] == "open"


def test_never_started_job_is_released_when_retries_exhaust(lab):
    pid = project(lab)
    job = mkjob(lab, pid, wall=300)
    L = lab.principal(f"L4-{pid}", ["launcher"], [pid])
    assert lab.cmd(L, "abandon_unstarted_job", {"job_id": job, "reason": "no runner"}, project=pid)["result"]["job_status"] == "failed"
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["compute_seconds"]["reserved"] == 0 and b["trials"]["reserved"] == 0


def test_usage_cost_is_computed_on_every_path():
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
    from eidos.investigator.runtime import DecisionRequest

    def fn(messages, info):
        return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": "x"})])

    tools = {"get_job": lambda j: {}, "inspect_evidence": lambda a, m=1: {}, "search_components": lambda t="", c=None: {},
             "describe_component": lambda r: {}}
    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "function:test", "max_requests_per_decision": 2,
                            "price_per_mtok": {"input": 3.0, "output": 15.0}})
    res = rt.decide(DecisionRequest(turn_id="t", slot=0, context="c", turn_so_far=[], limits={}, read_tools=tools))
    assert res.usage.get("limit_reached") and res.usage["usd"] > 0  # bounded outcome still carries cost
