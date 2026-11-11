"""Dependent batches against a real lab (ADR 0022): acceptance through ``accept_decision`` and execution through the
investigator activity and the real attempt runner on PostgreSQL. Temporal is not involved: the activity runs under
``ActivityEnvironment``, and each job is run by the test between ``execute_decision`` slices.
"""

from __future__ import annotations

import pytest
from test_cancel_through_acceptance import EXECUTION, _begin, _env, _jobs, _turn, _wf
from test_command_path import _publish
from test_turn_recovery import ShimClient

from eidos.runtime.runner import AttemptRunner

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]

FIT = {"id": "test.fit", "release": "1.0.0", "implements": "fit/1", "kind": "artifact-op", "entry": "fit:run",
       "scientific_effects": ["observe"], "optional_inputs": {"source": "source-bundle/1"},
       "output": {"model": "fit-model/1"}}
FIT_CODE = """
def run(inputs, config, *, reader=None, writer=None, context=None):
    if config.get("fail"):
        raise RuntimeError("planned failure")
    writer.stage_json("model", {"source": reader.json("source")}, schema="fit-model/1")
    return {"fitted": True}
"""
SCORE = {"id": "test.score", "release": "1.0.0", "implements": "score/1", "kind": "artifact-op", "entry": "score:run",
         "scientific_effects": ["observe"], "input": {"model": "fit-model/1"}, "output": {"report": "score-report/1"}}
SCORE_CODE = """
def run(inputs, config, *, reader=None, writer=None, context=None):
    writer.stage_json("report", {"saw": sorted(reader.json("model"))}, schema="score-report/1")
    return {"scored": True}
"""
FIT_REF, SCORE_REF = "test.fit@1.0.0", "test.score@1.0.0"
AUTHOR = {"tool": "author_source", "args": {"files": {"a.py": "x = 1\n"}}}
RUN = {"environment": "lab", "isolation_profile": "dev:same-user"}


def _fit(fail: bool = False) -> dict:
    return {"tool": "run_component", "args": {"component_ref": FIT_REF, **RUN, "config": {"fail": fail},
                                              "inputs": {"source": {"$ref": "call[0].artifact_id"}}}}


def _score(ref: str = "call[1].outputs.model") -> dict:
    return {"tool": "run_component", "args": {"component_ref": SCORE_REF, **RUN, "inputs": {"model": {"$ref": ref}}}}


def _lab(lab, tmp_path):
    pid, inv, acts, base = _turn(lab)
    _publish(lab, pid, FIT, {"fit.py": FIT_CODE})
    _publish(lab, pid, SCORE, {"score.py": SCORE_CODE})
    acts.batch_wait_slice, acts.batch_poll = 0.01, 0.001
    launcher = lab.principal(f"svc-launcher-{pid}", ["launcher"], [pid])
    root = tmp_path / "labroot"
    runner = AttemptRunner(ShimClient(lab, launcher), lab.store, lab_root=root, launcher_id=launcher.id, api_port=1,
                           credential_path=str(root / "config" / "secrets" / "launcher.token"))
    return pid, inv, acts, base, runner


def _accept(lab, pid, base, calls, **extra) -> dict:
    wf = _wf(lab, pid)
    epoch = _begin(lab, wf, pid, base["turn_id"], 1)["epoch"]
    proposal = {"rationale": "batch", "calls": calls, "control": {"action": "wait_for_jobs"}, **extra}
    return lab.cmd(wf, "accept_decision", {"turn_id": base["turn_id"], "slot": 0, "proposal": proposal,
                                           "usage_recorded": True, "attempt": 1, "epoch": epoch,
                                           "execution": EXECUTION}, project=pid)["result"]


def _execute(acts, base, decision_id: str, continuation: int = 0) -> dict:
    return _env(1, activity_id="exec").run(acts.execute_decision, {**base, "decision_id": decision_id,
                                                                   "continuation": continuation})


def _receipts(lab, pid, decision_id: str) -> list[dict]:
    return lab.q(lab.admin, "get_record", {"kind": "decision_receipts", "id": decision_id}, project=pid)["body"]["receipts"]


def _admitted(lab, pid, inv) -> list[dict]:
    return [j for j in _jobs(lab, pid, inv) if j["status"] in ("admitted", "queued")]


def test_a_batch_authors_then_fits_then_scores_on_the_real_outputs(lab, tmp_path):
    pid, inv, acts, base, runner = _lab(lab, tmp_path)
    d = _accept(lab, pid, base, [AUTHOR, _fit(), _score()])
    assert d["accepted"], d
    persisted = next(x for x in lab.q(lab.admin, "get_turn", {"turn_id": base["turn_id"]}, project=pid)["decisions"]
                     if x["id"] == d["decision_id"])["body"]["calls"]
    assert persisted[1]["args"]["inputs"]["source"] == {"$ref": "call[0].artifact_id"}  # prospective: unresolved
    assert [c.get("depends_on") for c in persisted] == [None, [0], [1]]

    first = _execute(acts, base, d["decision_id"])
    assert first["pending"] and first["next_call"] == 2  # the scorer waits: only the fit job has been admitted
    fit_job = _admitted(lab, pid, inv)
    assert len(fit_job) == 1 and fit_job[0]["component"] == FIT_REF
    assert lab.q(lab.admin, "get_job", {"job_id": fit_job[0]["id"]}, project=pid)["status"] != "succeeded"
    runner.run_job(fit_job[0]["id"], pid, activity_attempt=1)

    # nothing depends on the scorer, so this slice admits it and the batch is finished
    done = _execute(acts, base, d["decision_id"], 1)
    assert [s["ok"] for s in done["summary"]] == [True, True, True]
    scorer = _admitted(lab, pid, inv)
    assert len(scorer) == 1 and scorer[0]["component"] == SCORE_REF
    runner.run_job(scorer[0]["id"], pid, activity_attempt=1)
    assert "nothing_outstanding" not in done  # the batch's jobs are its news
    receipts = _receipts(lab, pid, d["decision_id"])
    fit = lab.q(lab.admin, "get_job", {"job_id": receipts[1]["result"]["job_id"]}, project=pid)
    score = lab.q(lab.admin, "get_job", {"job_id": receipts[2]["result"]["job_id"]}, project=pid)
    model = next(o for o in fit["outputs"] if o["slot"] == "model")
    assert score["lock"]["inputs"]["model"]["artifact_id"] == model["artifact_id"]
    assert fit["lock"]["inputs"]["source"]["artifact_id"] == receipts[0]["result"]["artifact_id"]
    assert receipts[2]["resolved"] == [{"path": "inputs.model", "ref": "call[1].outputs.model",
                                        "value": model["artifact_id"]}]
    assert receipts[1]["command_key"] != receipts[2]["command_key"]
    assert len(_jobs(lab, pid, inv)) == 2  # replays admitted nothing new


def test_a_failed_job_stops_its_dependents_and_is_recorded(lab, tmp_path):
    pid, inv, acts, base, runner = _lab(lab, tmp_path)
    d = _accept(lab, pid, base, [AUTHOR, _fit(fail=True), _score()])
    assert _execute(acts, base, d["decision_id"])["pending"]
    fit_job = _admitted(lab, pid, inv)[0]
    runner.run_job(fit_job["id"], pid, activity_attempt=1)
    done = _execute(acts, base, d["decision_id"], 1)
    assert [(s["ok"], s["code"]) for s in done["summary"]] == [(True, None), (True, None), (False, "DEPENDENCY_FAILED")]
    receipt = _receipts(lab, pid, d["decision_id"])[2]
    assert receipt["skipped"] is True and receipt["error"]["details"]["job_status"] == "failed"
    assert len(_jobs(lab, pid, inv)) == 1  # the scorer was never admitted


def test_a_reference_to_an_undeclared_output_is_refused_at_acceptance(lab, tmp_path):
    pid, inv, acts, base, _ = _lab(lab, tmp_path)
    d = _accept(lab, pid, base, [AUTHOR, _fit(), _score("call[1].outputs.predictions")])
    assert not d["accepted"] and d["reason"] == "invalid"
    assert d["error"]["details"]["declared"] == ["model"]
    assert not _jobs(lab, pid, inv)


def test_concurrency_needs_the_projects_resource_policy(lab, tmp_path):
    pid, inv, acts, base, _ = _lab(lab, tmp_path)
    two = {"resource_policy": {"max_concurrent_jobs": 2}}
    d = _accept(lab, pid, base, [AUTHOR, _fit()], **two)
    assert not d["accepted"] and d["error"]["details"] == {"requested": 2, "allowed": 1}
    lab.cmd(lab.admin, "update_profile_policy", {"policy": {"resources": {"max_concurrent_jobs": 2}}}, project=pid)
    d = _accept(lab, pid, base, [AUTHOR, _fit()], **two)
    assert d["accepted"], d
