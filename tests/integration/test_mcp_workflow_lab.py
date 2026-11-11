"""The compact MCP workflow (ADR 0025) against a real lab: the real LabClient over the real FastAPI app (so the source
upload takes the HTTP artifact route), the real ledger on PostgreSQL and the real attempt runner. Upload, a dependent
batch, a bounded status wait and submission for confirmation, in a handful of MCP tool calls, with the same records the
investigator's own path makes: job locks name the uploaded source and the fit's output, the claim's state is derived."""

from __future__ import annotations

import json

import pytest
from test_dependent_batch_lab import FIT_REF, SCORE_REF, _jobs, _lab
from test_mcp_normalized_receipts import lab_client_over_http

from eidos.mcp.gateway import ToolFailure, call_tool

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]
# MCP's run_component names environment; other documented fields go in payload, as for a single MCP call
RUN = {"environment": "lab", "payload": {"isolation_profile": "dev:same-user"}}


def _fit(fail: bool = False) -> dict:
    return {"tool": "run_component", "args": {"component_ref": FIT_REF, **RUN, "config": {"fail": fail},
                                              "inputs": {"source": {"$ref": "call[0].artifact_id"}}}}


def _score() -> dict:
    return {"tool": "run_component", "args": {"component_ref": SCORE_REF, **RUN,
                                              "inputs": {"model": {"$ref": "call[1].outputs.model"}}}}


def test_upload_batch_wait_submit_against_the_real_lab(lab, tmp_path):
    pid, inv, _acts, _base, runner = _lab(lab, tmp_path)
    client = lab_client_over_http(lab)
    calls = 0

    def tool(name: str, **args):
        nonlocal calls
        calls += 1
        return call_tool(client, name, {"project": pid, **args})

    # 1. upload_source: the real HTTP upload route, no manual metadata
    up = tool("upload_source", command_key="up-1", files={"a.py": "x = 1\n"})
    art = lab.q(lab.admin, "get_artifact", {"artifact_id": up["artifact_id"]}, project=pid)
    assert art["schema"] == "source-bundle/1" and art["role"] == "source"

    # 2. run_batch: source (literal) -> fit -> score; the scorer waits for the fit, which the runner has not run yet
    batch = [{"tool": "upload_source", "args": {"files": {"a.py": "x = 1\n"}}}, _fit(), _score()]
    first = tool("run_batch", command_key="b-1", calls=batch, investigation_id=inv, wait_seconds=0)
    assert first["status"] == "pending" and first["next_call"] == 2
    fit_job = first["jobs"][0]["job_id"]
    runner.run_job(fit_job, pid, activity_attempt=1)

    # 3. the same key and calls continue the batch: committed calls replay, the scorer is admitted
    second = tool("run_batch", command_key="b-1", calls=batch, investigation_id=inv, wait_seconds=0)
    assert second["status"] == "done", second
    assert [c["ok"] for c in second["calls"]] == [True, True, True]
    assert second["calls"][0].get("replayed") and second["calls"][1].get("replayed")
    score_job = second["calls"][2]["result"]["job_id"]
    runner.run_job(score_job, pid, activity_attempt=1)
    assert len(_jobs(lab, pid, inv)) == 2  # replays admitted nothing new

    # lineage is the lab's own: the job locks name the uploaded source and the fit's published model
    fit = lab.q(lab.admin, "get_job", {"job_id": fit_job}, project=pid)
    score = lab.q(lab.admin, "get_job", {"job_id": score_job}, project=pid)
    model = next(o["artifact_id"] for o in fit["outputs"] if o["slot"] == "model")
    assert fit["lock"]["inputs"]["source"]["artifact_id"] == second["calls"][0]["result"]["artifact_id"]
    assert score["lock"]["inputs"]["model"]["artifact_id"] == model
    assert second["calls"][2]["resolved"] == [{"path": "inputs.model", "value": model}]
    # the batch was recorded before its effects (its first command), references included
    with lab.ledger.transaction() as tx:
        row = tx._one("SELECT receipt FROM commands WHERE project_id=:p AND command_key='b-1:batch'", p=pid)
    note = lab.q(lab.admin, "get_record", {"kind": "note", "id": row["receipt"]["result"]["note_id"]}, project=pid)
    recorded = json.loads(note["body"]["text"])
    assert note["body"]["kind"] == "mcp-batch" and [c["depends_on"] for c in recorded["calls"]] == [[], [0], [1]]

    # 4. wait_status: compact, with output handles
    status = tool("wait_status", job_ids=[fit_job, score_job], wait_seconds=0)
    assert status["done"] is True
    assert status["jobs"][0]["outputs"]["model"] == model

    # 5. submit_candidate: lock + improves claim; the lab derives candidate-submitted
    sub = tool("submit_candidate", command_key="sub-1", investigation_id=inv, job_id=fit_job,
               statement="the fit improves on the reference", scope={"task": "demo", "population": "visible validation"})
    assert sub["claim"]["state"] == "candidate-submitted" and sub["claim"]["confirmed"] is False
    claim = lab.q(lab.admin, "get_claim", {"claim_id": sub["claim_id"]}, project=pid)
    assert (claim.get("body") or claim)["kind"] == "improves"
    note = lab.q(lab.admin, "get_record", {"kind": "note", "id": sub["lock_note_id"]}, project=pid)
    assert json.loads(note["body"]["text"]) == {"job_id": fit_job} and note["body"]["kind"] == "candidate_lock"
    assert calls <= 6

    # a retry of the submission replays every step: no second note or claim
    again = tool("submit_candidate", command_key="sub-1", investigation_id=inv, job_id=fit_job,
                 statement="the fit improves on the reference", scope={"task": "demo", "population": "visible validation"})
    assert again["claim_id"] == sub["claim_id"] and again["lock_note_id"] == sub["lock_note_id"]


def test_a_failed_fit_skips_its_scorer_and_a_bad_reference_runs_nothing(lab, tmp_path):
    pid, inv, _acts, _base, runner = _lab(lab, tmp_path)
    client = lab_client_over_http(lab)
    batch = [{"tool": "upload_source", "args": {"files": {"a.py": "x = 1\n"}}}, _fit(fail=True), _score()]
    first = call_tool(client, "run_batch", {"project": pid, "command_key": "b-f", "calls": batch, "investigation_id": inv,
                                            "wait_seconds": 0})
    runner.run_job(first["jobs"][0]["job_id"], pid, activity_attempt=1)
    done = call_tool(client, "run_batch", {"project": pid, "command_key": "b-f", "calls": batch, "investigation_id": inv,
                                           "wait_seconds": 0})
    assert [(c["ok"], (c.get("error") or {}).get("code")) for c in done["calls"]] == \
        [(True, None), (True, None), (False, "DEPENDENCY_FAILED")]
    assert len(_jobs(lab, pid, inv)) == 1

    before = len(lab.q(lab.admin, "list_jobs", {}, project=pid))
    with pytest.raises(ToolFailure) as exc:
        call_tool(client, "run_batch", {"project": pid, "command_key": "b-bad", "investigation_id": inv,
                                        "calls": [_fit(), {"tool": "run_component", "args": {
                                            "component_ref": SCORE_REF, **RUN, "inputs": {"model": {"$ref": "call[2].outputs.model"}}}}]})
    assert exc.value.code == "INVALID_REF"
    assert len(lab.q(lab.admin, "list_jobs", {}, project=pid)) == before


def test_submission_without_a_succeeded_job_records_nothing(lab, tmp_path):
    pid, inv, _acts, _base, _runner = _lab(lab, tmp_path)
    client = lab_client_over_http(lab)
    first = call_tool(client, "run_batch", {"project": pid, "command_key": "b-s", "investigation_id": inv,
                                            "calls": [{"tool": "upload_source", "args": {"files": {"a.py": "x\n"}}}, _fit()],
                                            "wait_seconds": 0})
    job = first["jobs"][0]["job_id"]
    with pytest.raises(ToolFailure) as exc:
        call_tool(client, "submit_candidate", {"project": pid, "command_key": "s-x", "investigation_id": inv,
                                               "job_id": job, "statement": "s", "scope": {"task": "demo"}})
    assert exc.value.code == "CANDIDATE_NOT_READY"
    with lab.ledger.transaction() as tx:
        row = tx._one("SELECT count(*) AS n FROM commands WHERE project_id=:p AND command_key LIKE 's-x:%%'", p=pid)
    assert row["n"] == 0
