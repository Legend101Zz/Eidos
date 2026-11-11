"""The compact research workflow (ADR 0025): upload_source, run_batch, wait_status and submit_candidate, through real
in-process MCP SDK sessions against a fixture-only fake lab (``workflow_lab``; no lab, no ledger, no network)."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import time

import pytest
from workflow_lab import WorkflowLab

from eidos import batches
from eidos.client import LabError
from eidos.mcp import workflow
from eidos.mcp.server import INSTRUCTIONS, Credentials, _tool_defs, build_server
from eidos.mcp.tools import BY_NAME, TOOLS, WORKFLOW_TOOLS
from mcp import Client

P = "p"
TRAIN = "trainer@1.0.0"
SCORE = "scorer@1.0.0"
INV = "inv_1"
SOURCE_MEDIA_TYPE = "application/vnd.eidos.source-bundle+json"


@pytest.fixture
def lab():
    lab = WorkflowLab()
    lab.outputs[TRAIN] = [{"slot": "model", "artifact_id": "art_model"}]
    lab.outputs[SCORE] = [{"slot": "predictions", "artifact_id": "art_pred"}]
    lab.job_receipts[SCORE] = {"metrics": {"accuracy": 0.91}}
    return lab


def sessioned(fn):
    """Open a real SDK client session in-process for the test (no async fixture: anyio cancel scopes must be entered
    and exited in the same task)."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(lab):
        server = build_server(lab.client, Credentials("good-token"))
        async with Client(server) as c:
            kw = {"client": c}
            if "lab" in sig.parameters:
                kw["lab"] = lab
            await fn(**kw)
    wrapper.__signature__ = inspect.Signature([inspect.Parameter("lab", inspect.Parameter.POSITIONAL_OR_KEYWORD)])
    return wrapper


def ok(result) -> dict:
    assert not result.is_error, result.structured_content
    return result.structured_content


def err(result) -> dict:
    assert result.is_error, result.structured_content
    return result.structured_content["error"]


class Tools:
    """The MCP tool calls of one scenario, counted."""

    def __init__(self, client):
        self.client, self.calls = client, []

    async def __call__(self, name: str, args: dict):
        self.calls.append(name)
        return await self.client.call_tool(name, args)


def run(component: str, **args) -> dict:
    return {"tool": "run_component", "args": {"component_ref": component, **args}}


def ref(text: str) -> dict:
    return {"$ref": text}


def batch(key: str, calls: list, **extra) -> dict:
    return {"project": P, "command_key": key, "calls": calls, **extra}


class FakeTime:
    """A clock the test owns: ``sleep`` advances it (and may flip a job), so waits are instant and exact."""

    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = 1000.0, [], on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))


@pytest.fixture
def clock(monkeypatch):
    def install(on_sleep=None) -> FakeTime:
        fake = FakeTime(on_sleep)
        monkeypatch.setattr(workflow, "time", fake)
        monkeypatch.setattr(batches, "time", fake)
        return fake
    return install


# -- the whole loop in a handful of calls --------------------------------------------------------------------------------

@sessioned
async def test_full_loop_in_four_calls(client, lab):
    tools = Tools(client)
    up = ok(await tools("upload_source", {"project": P, "command_key": "up-1", "investigation_id": INV,
                                          "files": {"train.py": "print('train')\n", "score.py": "print('score')\n"}}))
    source = up["artifact_id"]
    assert source == lab.uploads[0]["artifact_id"] and up["files"] == 2 and "replayed" not in up

    calls = [run(TRAIN, inputs={"source": source}, seed=1),
             run(SCORE, inputs={"model": ref("call[0].outputs.model"), "source": source}),
             run(TRAIN, inputs={"source": source}, seed=2)]
    done = ok(await tools("run_batch", batch("b1", calls, investigation_id=INV, wait_seconds=0)))
    assert done["status"] == "done" and done["batch"] == "b1" and "next_call" not in done
    assert [(c["i"], c["tool"], c["ok"]) for c in done["calls"]] == [(0, "run_component", True),
                                                                       (1, "run_component", True),
                                                                       (2, "run_component", True)]
    train_job, score_job, baseline_job = (c["result"]["job_id"] for c in done["calls"])
    # the resolved reference is in the batch receipt as provenance, and it is what reached the lab
    assert done["calls"][1]["resolved"] == [{"path": "inputs.model", "value": "art_model"}]
    assert "resolved" not in done["calls"][0]
    scorer = lab.commands("run_component")[1].payload
    assert scorer["inputs"] == {"model": "art_model", "source": source} and scorer["investigation_id"] == INV
    assert [j["job_id"] for j in done["jobs"]] == [train_job, score_job, baseline_job]
    assert done["jobs"][0]["outputs"] == {"model": "art_model"}

    waited = ok(await tools("wait_status", {"project": P, "job_ids": [train_job, score_job, baseline_job],
                                            "wait_seconds": 0}))
    assert waited["done"] is True and "next" not in waited
    assert {j["job_id"]: j["status"] for j in waited["jobs"]} == {train_job: "succeeded", score_job: "succeeded",
                                                                  baseline_job: "succeeded"}
    assert waited["jobs"][1]["receipt"] == {"metrics": {"accuracy": 0.91}}

    submitted = ok(await tools("submit_candidate", {
        "project": P, "command_key": "sub-1", "investigation_id": INV, "job_id": train_job,
        "statement": "trainer beats the baseline", "scope": {"model": "trainer", "task": "toy"},
        "assessment": {"success_rule_id": "r1", "baseline": {"job_id": baseline_job}, "seeds": [11, 12],
                       "data_provenance": {"split": "selection"}}}))
    assert len(tools.calls) == 4 <= 5
    assert submitted["candidate_job_id"] == train_job
    assert submitted["claim"]["state"] == "candidate-submitted" and submitted["claim"]["confirmed"] is False
    assert submitted["assessment"]["status"] == "launched" and submitted["assessment"]["plan_id"]
    assert "wait_status" in submitted["next"]

    # what reached the lab: only ordinary commands and uploads, with keys derived from the caller's
    assert [(c.kind, c.key) for c in lab.effects()] == [
        ("upload", "up-1"), ("command", "b1:batch"), ("command", "b1:0"), ("command", "b1:1"), ("command", "b1:2"),
        ("command", "sub-1:submit"), ("command", "sub-1:lock"), ("command", "sub-1:claim"), ("command", "sub-1:assess")]
    assert [c.name for c in lab.commands()] == ["record_note", "run_component", "run_component", "run_component",
                                                "record_note", "record_note", "file_claim", "assess_candidate"]
    assert all(c.token == "good-token" and c.project == P for c in lab.effects())
    batch_note, submit_note, lock_note = lab.notes
    assert batch_note["kind"] == "mcp-batch" and batch_note["investigation_id"] == INV
    plan = json.loads(batch_note["text"])
    assert plan["command_key"] == "b1" and [c["tool"] for c in plan["calls"]] == ["run_component"] * 3
    assert [c["depends_on"] for c in plan["calls"]] == [[], [0], []]  # recorded with its references, before any effect
    assert lab.effects()[1].name == "record_note"
    assert submit_note["kind"] == "mcp-submission" and submit_note["investigation_id"] == INV
    assert json.loads(submit_note["text"])["assessment"] is True
    assert lock_note["kind"] == "candidate_lock" and lock_note["investigation_id"] == INV
    assert json.loads(lock_note["text"]) == {"job_id": train_job}
    claim = lab.claims[submitted["claim_id"]]["payload"]
    assert claim["kind"] == "improves" and claim["statement"] == "trainer beats the baseline"
    assert claim["scope"] == {"model": "trainer", "task": "toy"} and claim["investigation_id"] == INV
    assess = lab.commands("assess_candidate")[0].payload
    assert assess["claim_id"] == submitted["claim_id"] and assess["candidate"] == {"job_id": train_job}
    assert assess["baseline"] == {"job_id": baseline_job} and assess["success_rule_id"] == "r1"


@sessioned
async def test_a_batch_can_upload_the_source_and_use_it(client, lab):
    calls = [{"tool": "upload_source", "args": {"files": {"train.py": "x = 1\n"}, "name": "mine"}},
             run(TRAIN, inputs={"source": ref("call[0].artifact_id")}),
             run(SCORE, inputs={"model": ref("call[1].outputs.model"), "source": ref("call[0].artifact_id")})]
    r = ok(await client.call_tool("run_batch", batch("ub", calls, investigation_id=INV)))
    assert r["status"] == "done"
    artifact = lab.uploads[0]["artifact_id"]
    assert r["calls"][0]["result"]["artifact_id"] == artifact and r["calls"][0]["tool"] == "upload_source"
    assert lab.uploads[0]["key"] == "ub:0" and lab.uploads[0]["name"] == "mine" and lab.uploads[0]["investigation"] == INV
    assert r["calls"][1]["resolved"] == [{"path": "inputs.source", "value": artifact}]
    assert {x["path"] for x in r["calls"][2]["resolved"]} == {"inputs.model", "inputs.source"}
    assert [c.payload["inputs"] for c in lab.commands("run_component")] == [
        {"source": artifact}, {"model": "art_model", "source": artifact}]
    assert lab.keys("run_component") == ["ub:1", "ub:2"]
    assert [j["job_id"] for j in r["jobs"]] == ["job_1", "job_2"]  # the upload is not a job


@sessioned
async def test_references_resolve_inside_nested_values_and_name_their_path(client, lab):
    calls = [run(TRAIN), run(SCORE, config={"parents": [ref("call[0].job_id"), "x"]},
                             inputs={"model": ref("call[0].outputs.model")}, hypothesis="h")]
    r = ok(await client.call_tool("run_batch", batch("nest", calls)))
    assert r["status"] == "done"
    assert lab.commands("run_component")[1].payload["config"] == {"parents": ["job_1", "x"]}
    assert {(u["path"], u["value"]) for u in r["calls"][1]["resolved"]} == {("config.parents[0]", "job_1"),
                                                                          ("inputs.model", "art_model")}


@sessioned
async def test_results_are_compact_json_text_with_structured_content(client, lab):
    r = await client.call_tool("run_batch", batch("compact", [run(TRAIN)]))
    assert r.content[0].text == json.dumps(r.structured_content, separators=(",", ":"))
    assert ": " not in r.content[0].text and ", " not in r.content[0].text


# -- pending and resume ---------------------------------------------------------------------------------------------------

@sessioned
async def test_a_running_job_makes_the_batch_pending_and_a_retry_resumes_it(client, lab):
    lab.scripts[TRAIN] = "running"
    args = batch("bp", [run(TRAIN), run(SCORE, inputs={"model": ref("call[0].outputs.model")})], wait_seconds=0)
    first = ok(await client.call_tool("run_batch", args))
    assert first["status"] == "pending" and first["next_call"] == 1
    assert [c["i"] for c in first["calls"]] == [0]
    assert "same command_key" in first["next"]
    assert first["jobs"][0]["status"] == "running"
    job = first["jobs"][0]["job_id"]
    assert len(lab.jobs) == 1 and lab.count("run_component") == 1

    lab.finish(job)
    second = ok(await client.call_tool("run_batch", args))
    assert second["status"] == "done" and "next_call" not in second
    assert second["calls"][0]["replayed"] is True and second["calls"][0]["result"]["job_id"] == job
    assert "replayed" not in second["calls"][1]
    assert len(lab.jobs) == 2 and lab.count("run_component") == 2 and lab.count("record_note") == 1
    assert second["calls"][1]["resolved"] == [{"path": "inputs.model", "value": "art_model"}]

    # once done, calling again changes nothing and replays every call
    again = ok(await client.call_tool("run_batch", args))
    assert again["status"] == "done" and all(c["replayed"] for c in again["calls"])
    assert len(lab.jobs) == 2 and lab.count("run_component") == 2


@pytest.mark.parametrize("ended", ["failed", "cancelled", "pruned"])
async def test_a_job_that_ends_badly_while_the_batch_is_pending_skips_its_dependents_on_resume(lab, ended):
    lab.scripts[TRAIN] = "running"
    args = batch("pf", [run(TRAIN), run(SCORE, inputs={"model": ref("call[0].outputs.model")}), run("other@1.0.0")],
                 wait_seconds=0)
    async with Client(build_server(lab.client, Credentials("good-token"))) as client:
        first = ok(await client.call_tool("run_batch", args))
        assert first["status"] == "pending" and first["next_call"] == 1
        lab.set_status(first["jobs"][0]["job_id"], ended)
        second = ok(await client.call_tool("run_batch", args))
    assert second["status"] == "done" and second["calls"][0]["replayed"] is True
    e = second["calls"][1]["error"]
    assert e["code"] == "DEPENDENCY_FAILED" and e["details"]["job_status"] == ended and e["details"]["depends_on"] == 0
    assert second["calls"][2]["ok"] is True
    assert [k for n, k in lab.committed if n == "run_component"] == ["pf:0", "pf:2"]  # pf:1 never reached the lab


@sessioned
async def test_a_batch_whose_last_job_still_runs_is_done_and_points_to_wait_status(client, lab):
    lab.scripts[SCORE] = "running"
    r = ok(await client.call_tool("run_batch", batch("tail", [run(TRAIN), run(SCORE)], wait_seconds=0)))
    assert r["status"] == "done" and [j["status"] for j in r["jobs"]] == ["succeeded", "running"]
    assert "wait_status" in r["next"] and "job_ids" in r["next"]


def test_a_dependency_is_awaited_within_the_wait_and_then_the_batch_goes_on(lab, clock):
    lab.scripts[TRAIN] = "running"
    fake = clock(on_sleep=lambda n: lab.finish("job_1") if n == 3 else None)
    out = workflow.run_batch(lab.client("good-token"), P, "w", {"wait_seconds": 30, "calls": [
        run(TRAIN), run(SCORE, inputs={"model": ref("call[0].outputs.model")})]})
    assert out["status"] == "done" and fake.now - 1000.0 == 3.0
    assert [c["ok"] for c in out["calls"]] == [True, True]
    assert out["calls"][1]["resolved"] == [{"path": "inputs.model", "value": "art_model"}]


def test_the_wait_is_bounded_and_a_batch_past_it_is_pending(lab, clock):
    lab.scripts[TRAIN] = "running"
    fake = clock()
    c = lab.client("good-token")
    calls = [run(TRAIN), run(SCORE, inputs={"model": ref("call[0].outputs.model")})]
    out = workflow.run_batch(c, P, "cap", {"wait_seconds": 500, "calls": calls})
    assert out["status"] == "pending" and out["next_call"] == 1
    assert fake.now - 1000.0 == workflow.MAX_WAIT_SECONDS == 50.0 and set(fake.sleeps) == {workflow.POLL_SECONDS}
    fake.now = 1000.0
    out = workflow.run_batch(c, P, "cap", {"calls": calls})  # no wait_seconds: the default
    assert out["status"] == "pending" and fake.now - 1000.0 == workflow.DEFAULT_WAIT_SECONDS == 20.0


# -- failures inside a batch --------------------------------------------------------------------------------------------------

@sessioned
async def test_a_failed_dependency_skips_its_dependents_but_an_independent_call_still_runs(client, lab):
    lab.scripts[TRAIN] = "fail"
    lab.job_errors[TRAIN] = {"category": "component_error", "message": "x" * 600}
    calls = [run(TRAIN),
             run(SCORE, inputs={"model": ref("call[0].outputs.model")}),
             run(SCORE, inputs={"other": ref("call[1].job_id")}),
             run("other@1.0.0")]
    r = ok(await client.call_tool("run_batch", batch("df", calls, wait_seconds=0)))
    assert r["status"] == "done"
    by = {c["i"]: c for c in r["calls"]}
    assert by[0]["ok"] is True  # the job was admitted; it ended failed
    for i in (1, 2):
        assert by[i]["ok"] is False and by[i]["error"]["code"] == "DEPENDENCY_FAILED"
    assert by[1]["error"]["details"]["job_status"] == "failed" and by[1]["error"]["details"]["depends_on"] == 0
    assert by[1]["error"]["details"]["error"]["category"] == "component_error"
    assert by[2]["error"]["details"]["depends_on"] == 1
    assert by[3]["ok"] is True and by[3]["result"]["component"] == "other@1.0.0"
    assert lab.keys("run_component") == ["df:0", "df:3"]  # the skipped calls never reached the lab
    train_job = r["jobs"][0]
    assert train_job["status"] == "failed" and train_job["error"]["category"] == "component_error"
    assert len(train_job["error"]["message"]) == 300


@sessioned
async def test_a_refused_call_is_a_receipt_and_its_dependents_are_skipped(client, lab):
    lab.fail_command("run_component", LabError(422, {"code": "INPUT_REJECTED", "message": "no such artifact",
                                                     "next_action": "Upload the source first.",
                                                     "details": {"port": "source"}}))
    calls = [run(TRAIN), run(SCORE, inputs={"model": ref("call[0].outputs.model")}), run("other@1.0.0")]
    r = ok(await client.call_tool("run_batch", batch("rf", calls)))
    assert r["status"] == "done"
    first = r["calls"][0]
    assert first["ok"] is False and first["error"] == {"code": "INPUT_REJECTED", "message": "no such artifact",
                                                       "next_action": "Upload the source first.",
                                                       "details": {"port": "source"}}
    assert r["calls"][1]["error"]["code"] == "DEPENDENCY_FAILED"
    assert r["calls"][2]["ok"] is True and "jobs" in r and len(r["jobs"]) == 1


@sessioned
async def test_a_missing_output_slot_is_ref_unresolved_not_a_crash(client, lab):
    calls = [run(TRAIN), run(SCORE, inputs={"model": ref("call[0].outputs.weights")}), run("other@1.0.0")]
    r = ok(await client.call_tool("run_batch", batch("ru", calls)))
    assert r["status"] == "done"
    e = r["calls"][1]["error"]
    assert r["calls"][1]["ok"] is False and e["code"] == "REF_UNRESOLVED" and "weights" in e["message"]
    assert e["details"]["published"] == ["model"] and e["details"]["depends_on"] == 0
    assert r["calls"][2]["ok"] is True  # an independent call is unaffected
    assert lab.keys("run_component") == ["ru:0", "ru:2"]


@sessioned
async def test_a_dependent_of_a_refused_file_claim_is_skipped(client, lab):
    lab.fail_command("file_claim", LabError(409, {"code": "CLAIM_REFUSED", "message": "no"}))
    calls = [{"tool": "file_claim", "args": {"statement": "s", "kind": "improves"}},
             {"tool": "draft_evidence_plan", "args": {"claim_id": ref("call[0].claim_id"), "charter_id": "c",
                                                      "template_ref": "t@1"}}]
    r = ok(await client.call_tool("run_batch", batch("nf", calls)))
    assert [c["ok"] for c in r["calls"]] == [False, False]
    assert r["calls"][0]["error"]["code"] == "CLAIM_REFUSED" and r["calls"][1]["error"]["code"] == "DEPENDENCY_FAILED"
    assert lab.commands("draft_evidence_plan") == []


@sessioned
async def test_a_result_field_the_call_did_not_return_is_ref_unresolved(client, lab):
    execute = lab.execute
    lab.execute = lambda name, payload: {} if name == "plan_trial" else execute(name, payload)  # a receipt without it
    calls = [{"tool": "plan_trial", "args": {"component_ref": TRAIN}},
             {"tool": "launch_trial", "args": {"trial_id": ref("call[0].trial_id")}}]
    r = ok(await client.call_tool("run_batch", batch("nt", calls)))
    e = r["calls"][1]["error"]
    assert e["code"] == "REF_UNRESOLVED" and "trial_id" in e["message"] and e["details"]["depends_on"] == 0
    assert lab.commands("launch_trial") == []


@sessioned
async def test_a_plan_then_launch_chain_passes_the_trial_id_and_waits_for_its_job(client, lab):
    lab.scripts[TRAIN] = "running"
    calls = [{"tool": "plan_trial", "args": {"component_ref": TRAIN}},
             {"tool": "launch_trial", "args": {"trial_id": ref("call[0].trial_id")}},
             run(SCORE, inputs={"model": ref("call[1].outputs.model")})]
    args = batch("chain", calls, wait_seconds=0)
    first = ok(await client.call_tool("run_batch", args))
    trial = first["calls"][0]["result"]["trial_id"]
    assert first["calls"][1]["resolved"] == [{"path": "trial_id", "value": trial}]
    assert first["status"] == "pending" and first["next_call"] == 2 and lab.count("run_component") == 0
    lab.finish(first["jobs"][0]["job_id"])
    second = ok(await client.call_tool("run_batch", args))
    assert second["status"] == "done" and second["calls"][2]["resolved"] == [{"path": "inputs.model",
                                                                              "value": "art_model"}]
    # every effect was committed exactly once across the two requests (the second one replayed what the first did)
    fresh = [(n, k) for n, k in lab.committed]
    assert fresh == [("record_note", "chain:batch"), ("plan_trial", "chain:0"), ("launch_trial", "chain:1"),
                     ("run_component", "chain:2")]


@sessioned
async def test_validation_then_publication_waits_for_the_validation_job(client, lab):
    lab.scripts["validate_component"] = "running"
    calls = [{"tool": "upload_source", "args": {"files": {"component.py": "x"}}},
             {"tool": "validate_component", "args": {"bundle_artifact_id": ref("call[0].artifact_id")}},
             {"tool": "publish_component", "args": {"bundle_artifact_id": ref("call[0].artifact_id"),
                                                    "validation_job_id": ref("call[1].job_id")}}]
    args = batch("pub", calls, wait_seconds=0)
    first = ok(await client.call_tool("run_batch", args))
    assert first["status"] == "pending" and first["next_call"] == 2 and lab.commands("publish_component") == []
    validation = first["jobs"][0]["job_id"]
    lab.finish(validation)
    second = ok(await client.call_tool("run_batch", args))
    assert second["status"] == "done" and [c.get("replayed") for c in second["calls"]] == [True, True, None]
    published = lab.commands("publish_component")[0].payload
    assert published == {"bundle_artifact_id": lab.uploads[0]["artifact_id"], "validation_job_id": validation}
    assert len(lab.uploads) == 1 and second["calls"][2]["result"]["component_ref"].startswith("published@")


@sessioned
async def test_rerunning_a_finished_batch_with_an_upload_replays_the_upload(client, lab):
    calls = [{"tool": "upload_source", "args": {"files": {"a.py": "x"}}},
             run(TRAIN, inputs={"source": ref("call[0].artifact_id")})]
    first = ok(await client.call_tool("run_batch", batch("again", calls)))
    second = ok(await client.call_tool("run_batch", batch("again", calls)))
    assert "replayed" not in first["calls"][0] and second["calls"][0]["replayed"] is True
    assert second["calls"][0]["result"] == first["calls"][0]["result"] and len(lab.uploads) == 1
    assert second["calls"][1]["replayed"] is True and len(lab.jobs) == 1


@sessioned
async def test_a_bad_source_inside_a_batch_is_refused_before_any_effect(client, lab):
    for files in ({"/etc/passwd": "x"}, {"../up.py": "x"}, {"a.py": 3}, {}):
        calls = [run(TRAIN), {"tool": "upload_source", "args": {"files": files}},
                 run(TRAIN, inputs={"source": ref("call[1].artifact_id")})]
        r = await client.call_tool("run_batch", batch("bs", calls))
        assert r.is_error and r.structured_content["error"]["code"] == "INVALID_BATCH", files
    assert lab.uploads == [] and lab.count("run_component") == 0 and lab.count("record_note") == 0


@sessioned
async def test_twelve_calls_are_allowed(client, lab):
    r = ok(await client.call_tool("run_batch", batch("twelve", [run(TRAIN, seed=i) for i in range(12)])))
    assert r["status"] == "done" and len(r["calls"]) == 12 and len(lab.jobs) == 12
    assert lab.keys("run_component") == [f"twelve:{i}" for i in range(12)]


# -- the batch's compute allowance and evidence plans ------------------------------------------------------------------------

@sessioned
async def test_a_launched_evidence_plan_holds_the_allowance_until_its_jobs_end(client, lab):
    plan_job = lab.add_plan_job("plan_7", "running")
    calls = [{"tool": "launch_evidence_plan", "args": {"plan_id": "plan_7"}}, run(TRAIN)]
    args = batch("ep", calls, wait_seconds=0)
    first = ok(await client.call_tool("run_batch", args))
    assert first["status"] == "pending" and first["next_call"] == 1 and first["plans"] == ["plan_7"]
    assert lab.commands("launch_evidence_plan")[0].payload == {"plan_id": "plan_7", "max_concurrent_jobs": 1}
    assert lab.count("run_component") == 0

    lab.finish(plan_job)
    second = ok(await client.call_tool("run_batch", args))
    assert second["status"] == "done" and second["calls"][0]["replayed"] is True
    assert second["plans"] == ["plan_7"] and lab.count("run_component") == 1 and lab.count("launch_evidence_plan") == 1


@sessioned
async def test_a_refused_assessment_in_a_batch_admits_nothing(client, lab):
    lab.refuse_assessment(("SEEDS_NOT_FRESH", "seeds were already run"))
    call = {"tool": "assess_candidate", "args": {"success_rule_id": "r1", "claim_id": "c1",
                                                  "baseline": {"job_id": "j0"}, "candidate": {"job_id": "j1"}}}
    r = ok(await client.call_tool("run_batch", batch("as", [call, run(TRAIN)], wait_seconds=0)))
    assert r["status"] == "done" and r["calls"][0]["ok"] is True and r["calls"][0]["result"]["status"] == "refused"
    assert "plans" not in r and r["calls"][1]["ok"] is True  # a refusal holds no allowance
    assert lab.commands("assess_candidate")[0].payload["max_concurrent_jobs"] == 1


# -- invalid batches are rejected before any effect -----------------------------------------------------------------------------

def rc(**kw):
    return {"tool": "run_component", "args": {"component_ref": TRAIN, **kw}}


INVALID_BATCHES = [
    # references (INVALID_REF)
    ("forward-ref", [rc(inputs={"x": ref("call[1].job_id")}), rc()], "INVALID_REF"),
    ("self-ref", [rc(), rc(inputs={"x": ref("call[1].job_id")})], "INVALID_REF"),
    ("index-beyond-batch", [rc(), rc(inputs={"x": ref("call[5].job_id")})], "INVALID_REF"),
    ("non-numeric-index", [rc(), rc(inputs={"x": ref("call[x].job_id")})], "INVALID_REF"),
    ("no-field", [rc(), rc(inputs={"x": ref("call[0]")})], "INVALID_REF"),
    ("empty-field", [rc(), rc(inputs={"x": ref("call[0].")})], "INVALID_REF"),
    ("leading-zero-index", [rc(), rc(inputs={"x": ref("call[00].job_id")})], "INVALID_REF"),
    ("negative-index", [rc(), rc(inputs={"x": ref("call[-1].job_id")})], "INVALID_REF"),
    ("trailing-newline", [rc(), rc(inputs={"x": ref("call[0].job_id\n")})], "INVALID_REF"),
    ("uppercase-field", [rc(), rc(inputs={"x": ref("call[0].JOB_ID")})], "INVALID_REF"),
    ("not-a-reference-text", [rc(), rc(inputs={"x": ref("job_id")})], "INVALID_REF"),
    ("extra-key-beside-ref", [rc(), rc(inputs={"x": {"$ref": "call[0].job_id", "also": 1}})], "INVALID_REF"),
    ("non-string-ref", [rc(), rc(inputs={"x": {"$ref": 5}})], "INVALID_REF"),
    ("field-the-tool-does-not-produce", [rc(), rc(inputs={"x": ref("call[0].plan_id")})], "INVALID_REF"),
    ("claim-id-of-run-component", [rc(), rc(inputs={"x": ref("call[0].claim_id")})], "INVALID_REF"),
    ("job-id-of-plan-trial", [{"tool": "plan_trial", "args": {"component_ref": TRAIN}},
                              {"tool": "launch_trial", "args": {"trial_id": ref("call[0].job_id")}}], "INVALID_REF"),
    ("outputs-of-upload", [{"tool": "upload_source", "args": {"files": {"a.py": "x"}}},
                           rc(inputs={"x": ref("call[0].outputs.source")})], "INVALID_REF"),
    ("outputs-of-plan-trial", [{"tool": "plan_trial", "args": {"component_ref": TRAIN}},
                               rc(inputs={"x": ref("call[0].outputs.model")})], "INVALID_REF"),
    ("ref-in-component-ref", [rc(), {"tool": "run_component", "args": {"component_ref": ref("call[0].component")}}],
     "INVALID_REF"),
    ("ref-in-investigation-id", [rc(), rc(investigation_id=ref("call[0].job_id"))], "INVALID_REF"),
    ("ref-in-environment", [rc(), rc(environment=ref("call[0].job_id"))], "INVALID_REF"),
    ("ref-in-purpose", [rc(), rc(purpose=ref("call[0].job_id"))], "INVALID_REF"),
    # the batch itself (INVALID_BATCH)
    ("empty", [], "INVALID_BATCH"),
    ("thirteen-calls", [rc(seed=i) for i in range(13)], "INVALID_BATCH"),
    ("unbatchable-promote-protocol", [{"tool": "promote_protocol", "args": {"release_ref": "t@1", "scope": ["improves"]}}],
     "INVALID_BATCH"),
    ("unbatchable-read-tool", [{"tool": "get_run", "args": {"job_id": "j"}}], "INVALID_BATCH"),
    ("unbatchable-bind", [{"tool": "bind_component", "args": {"component_ref": TRAIN}}], "INVALID_BATCH"),
    ("nested-run-batch", [{"tool": "run_batch", "args": {"calls": [rc()]}}], "INVALID_BATCH"),
    ("nested-wait-status", [{"tool": "wait_status", "args": {"job_ids": ["j"]}}], "INVALID_BATCH"),
    ("nested-submit-candidate", [{"tool": "submit_candidate", "args": {}}], "INVALID_BATCH"),
    ("unknown-tool", [{"tool": "nope", "args": {}}], "INVALID_BATCH"),
    ("non-string-tool", [{"tool": 5, "args": {}}], "INVALID_BATCH"),
    ("missing-tool", [{"args": {"component_ref": TRAIN}}], "INVALID_BATCH"),
    ("call-is-not-an-object", ["run_component"], "INVALID_BATCH"),
    ("extra-key-in-call", [{"tool": "run_component", "args": {"component_ref": TRAIN}, "key": "x"}], "INVALID_BATCH"),
    ("args-not-an-object", [{"tool": "run_component", "args": "x"}], "INVALID_BATCH"),
    ("command-key-in-args", [rc(command_key="mine")], "INVALID_BATCH"),
    ("project-in-args", [rc(project="other")], "INVALID_BATCH"),
    ("expected-revision-in-args", [rc(expected_revision=3)], "INVALID_BATCH"),
    ("wrong-type-string-for-integer", [rc(seed="x")], "INVALID_BATCH"),
    ("numeric-string-that-is-not-a-number", [rc(seed="11x")], "INVALID_BATCH"),
    ("bool-for-integer", [rc(seed=True)], "INVALID_BATCH"),
    ("float-for-integer", [rc(wall_seconds=1.5)], "INVALID_BATCH"),
    ("wrong-type-inputs", [rc(inputs="art_1")], "INVALID_BATCH"),
    ("unknown-argument", [rc(bogus=1)], "INVALID_BATCH"),
    ("missing-required-argument", [{"tool": "run_component", "args": {}}], "INVALID_BATCH"),
    ("ref-in-literal-payload-argument", [rc(), rc(payload={"environment": ref("call[0].job_id")})], "INVALID_REF"),
]


@pytest.mark.parametrize("calls,code", [c[1:] for c in INVALID_BATCHES], ids=[c[0] for c in INVALID_BATCHES])
async def test_an_invalid_batch_is_refused_before_any_effect(lab, calls, code):
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        e = err(await client.call_tool("run_batch", batch("bad", calls, investigation_id=INV)))
    assert e["code"] == code and e["message"]
    assert lab.effects() == [] and lab.jobs == {} and lab.notes == []  # not even the batch note


# -- one investigation per batch (R15 finding 1) -------------------------------------------------------------------------------

def plan(**kw):
    return {"tool": "plan_trial", "args": {"component_ref": TRAIN, **kw}}


def launch(trial_id, **kw):
    return {"tool": "launch_trial", "args": {"trial_id": trial_id, **kw}}


SCORE_A_MODEL = {"model": ref("call[0].outputs.model")}
CROSS_INVESTIGATION_BATCHES = [
    # (id, batch investigation_id, calls, the refused call, whether it references across)
    ("named-ref-across", None, [rc(investigation_id="inv_a"), rc(inputs=SCORE_A_MODEL, investigation_id="inv_b")], 1, True),
    ("payload-ref-across", None, [rc(investigation_id="inv_a"),
                                  rc(inputs=SCORE_A_MODEL, payload={"investigation_id": "inv_b"})], 1, True),
    ("payload-overrides-batch-default", INV, [rc(), rc(inputs=SCORE_A_MODEL, payload={"investigation_id": "inv_b"})],
     1, False),
    ("named-overrides-batch-default", INV, [rc(), rc(inputs=SCORE_A_MODEL, investigation_id="inv_b")], 1, True),
    ("first-call-differs-from-batch", INV, [rc(investigation_id="inv_b"), rc(inputs=SCORE_A_MODEL)], 0, True),
    ("ref-from-no-investigation", None, [rc(investigation_id="inv_a"), rc(inputs=SCORE_A_MODEL)], 1, True),
    ("unreferenced-calls-in-two-investigations", None, [rc(investigation_id="inv_a"), rc(investigation_id="inv_b")],
     1, False),
    ("launch-of-a-trial-planned-elsewhere", None, [plan(investigation_id="inv_a"),
                                                   launch(ref("call[0].trial_id"), payload={"investigation_id": "inv_b"})],
     1, False),
    ("launch-inherits-and-is-used-across", None, [plan(investigation_id="inv_a"), launch(ref("call[0].trial_id")),
                                                  rc(inputs={"m": ref("call[1].outputs.model")},
                                                     investigation_id="inv_b")], 2, True),
    ("literal-trial-of-another-investigation", INV, [launch("trial_b"), rc(inputs={"m": ref("call[0].outputs.model")})],
     0, True),
]


@pytest.mark.parametrize("inv,calls,refused,crossing", [c[1:] for c in CROSS_INVESTIGATION_BATCHES],
                         ids=[c[0] for c in CROSS_INVESTIGATION_BATCHES])
async def test_a_batch_has_one_investigation_and_no_ref_crosses_one(lab, inv, calls, refused, crossing):
    lab.records[("trial", "trial_b")] = {"investigation_id": "inv_b"}
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        e = err(await client.call_tool("run_batch", batch("x", calls, **({"investigation_id": inv} if inv else {}))))
    assert e["code"] == "INVESTIGATION_MISMATCH" and e["details"]["call"] == refused
    assert ("references" in e["details"]) is crossing and ("referenced across" in e["message"]) is crossing
    assert lab.effects() == [] and lab.jobs == {} and lab.notes == []  # refused before the batch note


@sessioned
async def test_a_ref_across_investigations_names_both_calls(client, lab):
    e = err(await client.call_tool("run_batch", batch("x", [rc(investigation_id="inv_a"),
                                                            rc(inputs=SCORE_A_MODEL, investigation_id="inv_b")])))
    assert e["details"] == {"call": 1, "investigation_id": "inv_b", "batch_investigation_id": "inv_a",
                            "references": [0]}
    assert "inv_a" in e["message"] and "inv_b" in e["message"] and lab.effects() == []


@sessioned
async def test_one_investigation_named_anywhere_runs_and_launches_inherit_it(client, lab):
    lab.records[("trial", "trial_a")] = {"investigation_id": "inv_a"}
    calls = [plan(payload={"investigation_id": "inv_a"}), launch(ref("call[0].trial_id")), launch("trial_a"),
             rc(inputs={"m": ref("call[1].outputs.model")}, investigation_id="inv_a")]
    r = ok(await client.call_tool("run_batch", batch("one", calls, wait_seconds=0)))
    assert r["status"] == "done" and all(c["ok"] for c in r["calls"])
    assert lab.notes[0]["kind"] == "mcp-batch" and lab.notes[0]["investigation_id"] == "inv_a"
    assert lab.commands("run_component")[0].payload["investigation_id"] == "inv_a"


@sessioned
async def test_a_batch_without_any_investigation_still_runs(client, lab):
    r = ok(await client.call_tool("run_batch", batch("none", [rc(), rc(inputs=SCORE_A_MODEL)], wait_seconds=0)))
    assert r["status"] == "done" and lab.notes[0]["investigation_id"] is None


@pytest.mark.parametrize("args,code", [
    ({"calls": "x"}, "INVALID_ARGUMENTS"), ({"calls": {"tool": "run_component"}}, "INVALID_ARGUMENTS"),
    ({}, "INVALID_ARGUMENTS"), ({"calls": [rc()], "wait_seconds": "x"}, "INVALID_ARGUMENTS"),
    ({"calls": [rc()], "bogus": 1}, "INVALID_ARGUMENTS"), ({"calls": [rc()], "command_key": ""}, "INVALID_ARGUMENTS"),
], ids=["calls-string", "calls-object", "no-calls", "wait-not-integer", "unknown-argument", "empty-key"])
async def test_a_malformed_run_batch_request_is_refused_before_any_effect(lab, args, code):
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        request = {"project": P, "command_key": "k", **args}
        if "command_key" in args:
            request["command_key"] = args["command_key"]
        e = err(await client.call_tool("run_batch", request))
    assert e["code"] == code and lab.effects() == []


@sessioned
async def test_invalid_refs_name_the_call_and_the_path(client, lab):
    e = err(await client.call_tool("run_batch", batch("bad", [rc(), rc(inputs={"x": ref("call[0].plan_id")})])))
    assert e["code"] == "INVALID_REF" and e["details"]["call"] == 1 and e["details"]["path"] == "inputs.x"
    assert "job_id" in e["details"]["allowed"] and "outputs.<slot>" in e["details"]["allowed"]
    assert e["next_action"]
    e = err(await client.call_tool("run_batch", batch("bad", [rc(seed="x")])))
    assert e["code"] == "INVALID_BATCH" and e["details"] == {"call": 0} and "seed" in e["message"]


@sessioned
async def test_a_batched_call_gets_the_same_normalization_as_a_single_call(client, lab):
    r = ok(await client.call_tool("run_batch", batch("norm", [run(TRAIN, seed="11")])))
    assert r["status"] == "done" and lab.commands("run_component")[0].payload["seed"] == 11
    assert r["calls"][0]["normalized_arguments"] == [{"path": "seed", "rule": "numeric-string", "expected": "integer",
                                                      "original": "11", "normalized": 11}]


@pytest.mark.parametrize("tool,extra", [("upload_source", {"files": {"a.py": "x"}}), ("run_batch", {"calls": [rc()]}),
                                        ("submit_candidate", None)])
async def test_a_composite_key_leaves_room_for_the_keys_derived_from_it(lab, tool, extra):
    job = lab.start_job(TRAIN)
    extra = extra or {"investigation_id": INV, "job_id": job, "statement": "s", "scope": {"a": 1}}
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        e = err(await client.call_tool(tool, {"project": P, "command_key": "k" * 181, **extra}))
        assert e["code"] == "INVALID_ARGUMENTS" and "180" in e["message"] and lab.effects() == []
        assert not ok(await client.call_tool(tool, {"project": P, "command_key": "k" * 180, **extra})).get("error")
    assert max(len(c.key) for c in lab.effects()) <= 200  # the lab's own bound holds for every derived key


@sessioned
async def test_the_same_batch_key_with_other_calls_is_a_conflict_with_no_further_effect(client, lab):
    first = ok(await client.call_tool("run_batch", batch("bk", [run(TRAIN, seed=1)])))
    assert first["status"] == "done"
    before = (lab.executed, len(lab.jobs), len(lab.notes))
    e = err(await client.call_tool("run_batch", batch("bk", [run(TRAIN, seed=9)])))
    assert e["code"] == "COMMAND_KEY_CONFLICT" and e["next_action"]
    assert (lab.executed, len(lab.jobs), len(lab.notes)) == before
    last = lab.effects()[-1]  # the only thing that reached the lab was the refused note
    assert (last.name, last.key, last.outcome) == ("record_note", "bk:batch", "conflict")
    assert lab.effects()[:-1] == lab.effects()[:2]
    # ... and with the same calls it still just replays
    again = ok(await client.call_tool("run_batch", batch("bk", [run(TRAIN, seed=1)])))
    assert again["calls"][0]["replayed"] is True and lab.count("run_component") == 1


@sessioned
async def test_a_pending_batch_key_cannot_be_reused_for_other_calls(client, lab):
    lab.scripts[TRAIN] = "running"
    ok(await client.call_tool("run_batch", batch("bk2", [run(TRAIN), run(SCORE, inputs={"m": ref("call[0].job_id")})],
                                                 wait_seconds=0)))
    e = err(await client.call_tool("run_batch", batch("bk2", [run(TRAIN, seed=2)], wait_seconds=0)))
    assert e["code"] == "COMMAND_KEY_CONFLICT" and len(lab.jobs) == 1 and lab.count("run_component") == 1


# -- submit_candidate -------------------------------------------------------------------------------------------------------------

def submit(job_id: str, key: str = "s1", **extra) -> dict:
    return {"project": P, "command_key": key, "investigation_id": INV, "job_id": job_id, "statement": "better",
            "scope": {"model": "m", "task": "t"}, **extra}


ASSESSMENT = {"success_rule_id": "r1", "baseline": {"job_id": "job_0"}, "seeds": [1, 2],
              "data_provenance": {"split": "selection"}}


@pytest.mark.parametrize("status", ["running", "queued", "failed", "cancelled"])
async def test_submitting_a_job_that_has_not_succeeded_is_candidate_not_ready(lab, status):
    job = lab.start_job(TRAIN, status)
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        e = err(await client.call_tool("submit_candidate", submit(job, assessment=ASSESSMENT)))
    assert e["code"] == "CANDIDATE_NOT_READY" and status in e["message"] and "wait_status" in e["next_action"]
    assert e["details"] == {"job_id": job, "status": status}
    assert lab.effects() == [] and lab.notes == [] and lab.claims == {}


@sessioned
async def test_submitting_an_unknown_job_is_not_found_and_records_nothing(client, lab):
    e = err(await client.call_tool("submit_candidate", submit("job_404")))
    assert e["code"] == "NOT_FOUND" and lab.effects() == []


@sessioned
async def test_a_refused_assessment_is_reported_and_the_claim_stays_candidate_submitted(client, lab):
    job = lab.start_job(TRAIN)
    lab.refuse_assessment(("SEEDS_NOT_FRESH", "seeds were already run"), ("NO_BASELINE", "m" * 500))
    r = ok(await client.call_tool("submit_candidate", submit(job, assessment=ASSESSMENT)))
    a = r["assessment"]
    assert a["status"] == "refused" and "plan_id" not in a
    assert [x["code"] for x in a["reasons"]] == ["SEEDS_NOT_FRESH", "NO_BASELINE"]
    assert a["reasons"][1]["message"] == "m" * 500  # R15: reasons are kept whole
    assert r["claim"]["state"] == "candidate-submitted" and r["claim"]["confirmed"] is False
    assert "next" not in r  # nothing is running to wait for
    assert lab.keys() == ["s1:submit", "s1:lock", "s1:claim", "s1:assess"]


@sessioned
async def test_a_launched_assessment_returns_its_plan_and_a_next_hint(client, lab):
    job = lab.start_job(TRAIN)
    r = ok(await client.call_tool("submit_candidate", submit(job, assessment=ASSESSMENT)))
    a = r["assessment"]
    assert a["status"] == "launched" and a["plan_id"].startswith("plan_") and a["request_id"].startswith("req_")
    assert "wait_status" in r["next"] and "plan_id" in r["next"] and "claim_id" in r["next"]
    assert r["claim"] == {"claim_id": r["claim_id"], "revision": 1, "state": "candidate-submitted",
                          "label": "Candidate submitted", "confirmed": False, "assessments": []}
    assert r["lock_note_id"] == "note_2" and r["candidate_job_id"] == job


@sessioned
async def test_submitting_without_an_assessment_files_the_claim_only(client, lab):
    job = lab.start_job(TRAIN)
    r = ok(await client.call_tool("submit_candidate", submit(job)))
    assert "assessment" not in r and "next" not in r and r["claim"]["state"] == "candidate-submitted"
    assert lab.keys() == ["s1:submit", "s1:lock", "s1:claim"] and lab.commands("assess_candidate") == []
    assert lab.commands("record_note")[1].payload == {
        "investigation_id": INV, "kind": "candidate_lock", "text": json.dumps({"job_id": job}), "evidence_refs": [job]}


@sessioned
async def test_the_state_is_what_the_lab_derives_never_what_the_tool_asserts(client, lab):
    job = lab.start_job(TRAIN)
    lab.claim_state = {"state": "protocol-passed", "label": "Protocol passed", "confirmed": True}
    lab.claim_assessments = [{"id": "asm_1", "outcome": "supported", "conformance": "conforming", "health": "healthy",
                              "ignored": 1}]
    r = ok(await client.call_tool("submit_candidate", submit(job)))
    assert r["claim"]["state"] == "protocol-passed" and r["claim"]["confirmed"] is True
    assert r["claim"]["assessments"] == [{"id": "asm_1", "outcome": "supported", "conformance": "conforming",
                                          "health": "healthy"}]


@sessioned
async def test_an_assessment_cannot_override_the_claim_or_the_candidate(client, lab):
    job = lab.start_job(TRAIN)
    sneaky = {**ASSESSMENT, "claim_id": "claim_evil", "candidate": {"job_id": "job_evil"}, "investigation_id": "inv_evil"}
    r = ok(await client.call_tool("submit_candidate", submit(job, assessment=sneaky)))
    p = lab.commands("assess_candidate")[0].payload
    assert p["claim_id"] == r["claim_id"] != "claim_evil" and p["candidate"] == {"job_id": job}
    assert p["investigation_id"] == INV


@sessioned
async def test_a_retry_with_the_same_key_replays_every_step(client, lab):
    job = lab.start_job(TRAIN)
    args = submit(job, assessment=ASSESSMENT)
    first = ok(await client.call_tool("submit_candidate", args))
    executed = lab.executed
    second = ok(await client.call_tool("submit_candidate", args))
    assert second == first  # same note, claim, plan: nothing new
    assert lab.executed == executed and len(lab.notes) == 2 and len(lab.claims) == 1
    retries = lab.commands()[4:]
    assert [(c.key, c.outcome) for c in retries] == [("s1:submit", "replayed"), ("s1:lock", "replayed"),
                                                     ("s1:claim", "replayed"), ("s1:assess", "replayed")]


@sessioned
async def test_a_failed_step_names_itself_and_the_retry_resumes_after_the_committed_ones(client, lab):
    job = lab.start_job(TRAIN)
    lab.fail_command("file_claim", LabError(422, {"code": "INVALID_SCOPE", "message": "scope is not specific enough",
                                                  "details": {"field": "scope"}}))
    e = err(await client.call_tool("submit_candidate", submit(job, assessment=ASSESSMENT)))
    assert e["code"] == "INVALID_SCOPE" and "file_claim" in e["message"] and "scope" in e["message"]
    assert e["details"] == {"step": "claim", "field": "scope"} and "same command_key" in e["next_action"]
    assert len(lab.notes) == 2 and lab.claims == {} and lab.commands("assess_candidate") == []  # the lock committed
    r = ok(await client.call_tool("submit_candidate", submit(job, assessment=ASSESSMENT)))
    assert r["claim"]["state"] == "candidate-submitted" and r["assessment"]["status"] == "launched"
    assert [(c.key, c.outcome) for c in lab.commands()] == [
        ("s1:submit", "committed"), ("s1:lock", "committed"), ("s1:claim", "error"), ("s1:submit", "replayed"),
        ("s1:lock", "replayed"), ("s1:claim", "committed"), ("s1:assess", "committed")]


@sessioned
async def test_the_same_submit_key_for_another_candidate_is_a_conflict_before_any_step(client, lab):
    first, other = lab.start_job(TRAIN), lab.start_job(TRAIN)
    ok(await client.call_tool("submit_candidate", submit(first)))
    committed, calls = lab.executed, len(lab.commands())
    e = err(await client.call_tool("submit_candidate", submit(other)))
    assert e["code"] == "COMMAND_KEY_CONFLICT" and e["details"]["step"] == "submit"
    assert lab.executed == committed and len(lab.claims) == 1
    e = err(await client.call_tool("submit_candidate", submit(first, statement="a different statement")))
    assert e["code"] == "COMMAND_KEY_CONFLICT" and e["details"]["step"] == "submit" and lab.executed == committed
    assert [c.key for c in lab.commands()[calls:]] == ["s1:submit", "s1:submit"]  # no lock or claim replayed


@pytest.mark.parametrize("first,retry", [
    ({}, {"assessment": ASSESSMENT}),  # adding an assessment to a committed submission does not start one
    ({"assessment": ASSESSMENT}, {}),  # dropping it does not silently omit its result
    ({"assessment": ASSESSMENT}, {"assessment": {**ASSESSMENT, "seeds": [3, 4]}}),
    ({"assessment": ASSESSMENT}, {"assessment": ASSESSMENT, "scope": {"model": "m", "task": "other"}}),
], ids=["assessment-added", "assessment-dropped", "assessment-changed", "scope-changed"])
async def test_the_submit_key_binds_the_whole_request_before_any_step(lab, first, retry):
    """R15 finding 2: a changed request under the same key is COMMAND_KEY_CONFLICT, with nothing replayed."""
    job = lab.start_job(TRAIN)
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        ok(await client.call_tool("submit_candidate", submit(job, **first)))
        committed, calls, assessments = lab.executed, len(lab.commands()), len(lab.commands("assess_candidate"))
        e = err(await client.call_tool("submit_candidate", submit(job, **retry)))  # the whole retried request
    assert e["code"] == "COMMAND_KEY_CONFLICT" and e["details"]["step"] == "submit"
    assert lab.executed == committed and len(lab.commands("assess_candidate")) == assessments
    assert [(c.key, c.outcome) for c in lab.commands()[calls:]] == [("s1:submit", "conflict")]  # no partial replay


@sessioned
async def test_an_empty_assessment_is_the_same_request_as_none(client, lab):
    job = lab.start_job(TRAIN)
    first = ok(await client.call_tool("submit_candidate", submit(job)))
    again = ok(await client.call_tool("submit_candidate", submit(job, assessment={})))
    assert again == first and lab.commands("assess_candidate") == []


@pytest.mark.parametrize("missing", ["investigation_id", "job_id", "statement", "scope", "command_key"])
async def test_submit_candidate_requires_its_arguments_before_anything_runs(lab, missing):
    job = lab.start_job(TRAIN)
    args = submit(job)
    del args[missing]
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        e = err(await client.call_tool("submit_candidate", args))
    assert e["code"] == "INVALID_ARGUMENTS" and missing in e["message"] and lab.effects() == []


@sessioned
async def test_an_assessment_that_is_not_an_object_is_refused_before_anything_runs(client, lab):
    job = lab.start_job(TRAIN)
    e = err(await client.call_tool("submit_candidate", submit(job, assessment="all of it")))
    assert e["code"] == "INVALID_ARGUMENTS" and lab.effects() == []


# -- wait_status -----------------------------------------------------------------------------------------------------------------

@sessioned
async def test_wait_status_needs_something_to_wait_for(client, lab):
    e = err(await client.call_tool("wait_status", {"project": P}))
    assert e["code"] == "INVALID_ARGUMENTS" and "job_ids" in e["message"] and lab.calls == []
    e = err(await client.call_tool("wait_status", {"project": P, "job_ids": [], "wait_seconds": 0}))
    assert e["code"] == "INVALID_ARGUMENTS" and lab.calls == []


@sessioned
async def test_wait_status_takes_at_most_twenty_jobs_of_strings(client, lab):
    ids = [lab.start_job(TRAIN) for _ in range(21)]
    e = err(await client.call_tool("wait_status", {"project": P, "job_ids": ids}))
    assert e["code"] == "INVALID_ARGUMENTS" and "20" in e["message"] and lab.queries() == []
    assert len(ok(await client.call_tool("wait_status", {"project": P, "job_ids": ids[:20],
                                                         "wait_seconds": 0}))["jobs"]) == 20
    for bad in ([""], [5], [None], ["a", ""]):
        e = err(await client.call_tool("wait_status", {"project": P, "job_ids": bad}))
        assert e["code"] == "INVALID_ARGUMENTS", bad


@sessioned
async def test_wait_status_with_zero_seconds_polls_once_and_returns_promptly(client, lab):
    job = lab.start_job(TRAIN, "running")
    started = time.perf_counter()
    r = ok(await client.call_tool("wait_status", {"project": P, "job_ids": [job], "wait_seconds": 0}))
    assert time.perf_counter() - started < 2
    assert r["done"] is False and r["jobs"] == [{"job_id": job, "status": "running", "component": TRAIN}]
    assert r["waited_seconds"] == 0 and "wait_status again" in r["next"]
    assert len(lab.queries("get_job")) == 1
    lab.finish(job)
    r = ok(await client.call_tool("wait_status", {"project": P, "job_ids": [job], "wait_seconds": 0}))
    assert r["done"] is True and "next" not in r and r["jobs"][0]["outputs"] == {"model": "art_model"}


@sessioned
async def test_wait_status_reports_a_plan_with_active_jobs(client, lab):
    running = lab.add_plan_job("plan_1", "running")
    lab.add_plan_job("plan_1", "succeeded")
    lab.add_plan_job("plan_2", "running")  # another plan's job is not this plan's
    args = {"project": P, "plan_id": "plan_1", "wait_seconds": 0}
    r = ok(await client.call_tool("wait_status", args))
    assert r["done"] is False and r["plan"] == {"plan_id": "plan_1", "jobs": {"running": 1, "succeeded": 1}, "active": 1}
    assert "jobs" not in r and "next" in r
    lab.finish(running)
    r = ok(await client.call_tool("wait_status", args))
    assert r["done"] is True and r["plan"] == {"plan_id": "plan_1", "jobs": {"succeeded": 2}, "active": 0}


@sessioned
async def test_wait_status_is_not_done_until_every_job_and_the_plan_have_ended(client, lab):
    j1, j2 = lab.start_job(TRAIN), lab.start_job(SCORE, "running")
    plan_job = lab.add_plan_job("plan_1", "queued")
    args = {"project": P, "job_ids": [j1, j2], "plan_id": "plan_1", "wait_seconds": 0}
    assert ok(await client.call_tool("wait_status", args))["done"] is False
    lab.finish(j2)
    assert ok(await client.call_tool("wait_status", args))["done"] is False  # the plan still has an active job
    lab.set_status(plan_job, "failed")
    r = ok(await client.call_tool("wait_status", args))
    assert r["done"] is True and r["plan"]["jobs"] == {"failed": 1}


@sessioned
async def test_wait_status_summarizes_the_claim_and_the_investigation(client, lab):
    claim = lab.add_claim("claim_9")
    lab.claim_assessments = [{"id": "asm_1", "outcome": "supported", "conformance": "ok", "health": "healthy", "x": 1}]
    lab.claim_state = {"state": "protocol-passed", "label": "Protocol passed", "confirmed": True}
    lab.investigations[INV] = {"status": "paused", "needs_input": {"code": "turn-failed", "reason": "r", "details": {}},
                               "turns": ["a lot of turns"]}
    r = ok(await client.call_tool("wait_status", {"project": P, "claim_id": claim, "investigation_id": INV,
                                                  "wait_seconds": 0}))
    assert r["done"] is True and "jobs" not in r and "plan" not in r
    assert r["claim"] == {"claim_id": claim, "revision": 1, "state": "protocol-passed", "label": "Protocol passed",
                          "confirmed": True, "assessments": [{"id": "asm_1", "outcome": "supported",
                                                             "conformance": "ok", "health": "healthy"}]}
    assert r["investigation"] == {"id": INV, "status": "paused", "revision": 3,
                                  "needs_input": {"code": "turn-failed", "reason": "r", "details": {}}}


@sessioned
async def test_wait_status_on_an_unknown_job_is_a_not_found_error(client, lab):
    e = err(await client.call_tool("wait_status", {"project": P, "job_ids": ["job_404"], "wait_seconds": 0}))
    assert e["code"] == "NOT_FOUND"


@sessioned
async def test_wait_status_keeps_receipts_small_and_errors_short(client, lab):
    big, small = lab.start_job(TRAIN), lab.start_job(SCORE)
    lab.job_receipts[TRAIN] = {f"metric_{i:02d}": "v" * 20 for i in range(40)}
    failed = lab.start_job("bad@1.0.0", "failed")
    lab.job_errors["bad@1.0.0"] = {"category": "oom", "message": "y" * 1000}
    r = ok(await client.call_tool("wait_status", {"project": P, "job_ids": [big, small, failed], "wait_seconds": 0}))
    by = {j["job_id"]: j for j in r["jobs"]}
    assert by[big]["receipt"] == {"keys": sorted(f"metric_{i:02d}" for i in range(20)), "expand": "get_run"}
    assert by[small]["receipt"] == {"metrics": {"accuracy": 0.91}}
    assert by[failed]["error"] == {"category": "oom", "message": "y" * 300} and "receipt" not in by[failed]
    assert len(json.dumps(r)) < 1500


@sessioned
async def test_a_numeric_string_wait_is_normalized_and_recorded(client, lab):
    job = lab.start_job(TRAIN, "running")
    r = ok(await client.call_tool("wait_status", {"project": P, "job_ids": [job], "wait_seconds": "0"}))
    assert r["done"] is False and r["normalized_arguments"][0]["path"] == "wait_seconds"
    assert r["normalized_arguments"][0]["normalized"] == 0


def test_wait_seconds_is_capped_and_a_wait_for_a_running_job_ends_there(lab, clock):
    job = lab.start_job(TRAIN, "running")
    fake = clock()
    c = lab.client("good-token")
    r = workflow.wait_status(c, P, {"job_ids": [job], "wait_seconds": 10_000})
    assert r["done"] is False and r["waited_seconds"] == 50.0 and fake.now - 1000.0 == 50.0
    assert len(fake.sleeps) == 50 and set(fake.sleeps) == {workflow.POLL_SECONDS}
    fake.now, fake.sleeps = 1000.0, []
    r = workflow.wait_status(c, P, {"job_ids": [job]})
    assert r["waited_seconds"] == workflow.DEFAULT_WAIT_SECONDS == 20.0  # the default


def test_a_wait_ends_as_soon_as_the_job_does(lab, clock):
    job = lab.start_job(TRAIN, "running")
    fake = clock(on_sleep=lambda n: lab.finish(job) if n == 3 else None)
    r = workflow.wait_status(lab.client("good-token"), P, {"job_ids": [job], "wait_seconds": 50})
    assert r["done"] is True and r["waited_seconds"] == 3.0 and len(fake.sleeps) == 3


@pytest.mark.parametrize("given,seconds", [(None, 20.0), (0, 0.0), (1, 1.0), (50, 50.0), (51, 50.0), (10_000, 50.0),
                                           (-5, 0.0), ("7", 7.0)])
def test_bounded_wait(given, seconds):
    assert workflow._bounded_wait({} if given is None else {"wait_seconds": given}) == seconds


# -- upload_source ---------------------------------------------------------------------------------------------------------------

@sessioned
async def test_upload_source_writes_the_canonical_source_bundle(client, lab):
    files = {"b.py": "print(2)\n", "pkg/a.py": "x = '☃'\n", "data.csv": "1,2\n3,4\n"}
    r = ok(await client.call_tool("upload_source", {"project": P, "command_key": "up-1", "files": files,
                                                    "investigation_id": INV}))
    expected = json.dumps({"files": files}, sort_keys=True).encode()
    up = lab.uploads[0]
    assert up["data"] == expected
    assert (up["media_type"], up["schema"], up["role"], up["key"], up["project"]) == (
        SOURCE_MEDIA_TYPE, "source-bundle/1", "source", "up-1", P)
    assert up["name"] == "authored-source" and up["investigation"] == INV and up["access_class"] == "project"
    assert r == {"artifact_id": up["artifact_id"], "sha256": hashlib.sha256(expected).hexdigest(), "files": 3,
                 "use_as": "run_component inputs.source"}


@sessioned
async def test_upload_source_names_the_bundle_and_is_independent_of_key_order(client, lab):
    a = ok(await client.call_tool("upload_source", {"project": P, "command_key": "u1", "name": "mine",
                                                    "files": {"a.py": "1", "b.py": "2"}}))
    b = ok(await client.call_tool("upload_source", {"project": P, "command_key": "u2",
                                                    "files": {"b.py": "2", "a.py": "1"}}))
    assert a["sha256"] == b["sha256"] and lab.uploads[0]["data"] == lab.uploads[1]["data"]
    assert (lab.uploads[0]["name"], lab.uploads[1]["name"], lab.uploads[0]["investigation"]) == (
        "mine", "authored-source", None)


@sessioned
async def test_upload_source_retry_replays_and_another_bundle_under_the_key_conflicts(client, lab):
    args = {"project": P, "command_key": "up-1", "files": {"a.py": "x"}}
    first = ok(await client.call_tool("upload_source", args))
    second = ok(await client.call_tool("upload_source", args))
    assert second == {**first, "replayed": True} and "replayed" not in first
    assert len(lab.uploads) == 1 and lab.executed == 1
    e = err(await client.call_tool("upload_source", {**args, "files": {"a.py": "y"}}))
    assert e["code"] == "COMMAND_KEY_CONFLICT" and len(lab.uploads) == 1


@pytest.mark.parametrize("files", [
    {"/etc/passwd": "x"}, {"/abs/train.py": "x", "ok.py": "y"}, {"../escape.py": "x"}, {"a/../../escape.py": "x"},
    {"a/..": "x"}, {"..": "x"}, {"ok.py": 5}, {"ok.py": None}, {"ok.py": ["x"]}, {"ok.py": {"text": "x"}},
    {"": "x"}, {}, {"ok.py": "x", "bad.py": 7},
], ids=["absolute", "absolute-with-valid", "parent", "nested-parent", "trailing-parent", "bare-parent",
        "int-content", "null-content", "list-content", "object-content", "empty-name", "no-files", "second-bad"])
async def test_upload_source_rejects_bad_files_before_any_upload(lab, files):
    server = build_server(lab.client, Credentials("good-token"))
    async with Client(server) as client:
        e = err(await client.call_tool("upload_source", {"project": P, "command_key": "bad", "files": files}))
    assert e["code"] == "INVALID_ARGUMENTS" and e["next_action"] and lab.effects() == [] and lab.uploads == []


@sessioned
async def test_upload_source_accepts_odd_but_relative_names(client, lab):
    files = {"..hidden": "x", "a..b/c.py": "y", "./rel.py": "z", "dir/": "", "empty.py": ""}
    r = ok(await client.call_tool("upload_source", {"project": P, "command_key": "odd", "files": files}))
    assert r["files"] == 5 and json.loads(lab.uploads[0]["data"]) == {"files": files}


@sessioned
async def test_upload_source_needs_files_and_a_key(client, lab):
    e = err(await client.call_tool("upload_source", {"project": P, "command_key": "k"}))
    assert e["code"] == "INVALID_ARGUMENTS" and "files" in e["message"]
    e = err(await client.call_tool("upload_source", {"project": P, "files": {"a.py": "x"}}))
    assert e["code"] == "INVALID_ARGUMENTS" and "command_key" in e["message"] and lab.effects() == []


def test_source_bundle_is_deterministic_text():
    assert workflow.source_bundle({"b": "2", "a": "1"}) == b'{"files": {"a": "1", "b": "2"}}'
    with pytest.raises(workflow.WorkflowFailure) as info:
        workflow.source_bundle({"/x": "1"})
    assert info.value.code == "INVALID_ARGUMENTS"


# -- compact views (pure functions) ----------------------------------------------------------------------------------------------

def test_job_summary_is_a_handle_not_a_dump():
    job = {"id": "job_1", "status": "succeeded", "lock": {"component": {"ref": "c@1"}},
           "outputs": [{"slot": "model", "artifact_id": "a1", "size": 99}, {"slot": "log"}],
           "terminal": {"receipt": {"k": 1}}, "attempts": [1, 2, 3]}
    assert workflow.job_summary(job) == {"job_id": "job_1", "status": "succeeded", "component": "c@1",
                                         "outputs": {"model": "a1"}, "receipt": {"k": 1}}
    assert workflow.job_summary({"id": "j", "status": "running", "component": "c@2"}) == {
        "job_id": "j", "status": "running", "component": "c@2"}
    assert workflow.job_summary({"id": "j", "status": "failed", "terminal": {"error": "plain text"}}) == {
        "job_id": "j", "status": "failed"}


def test_compact_receipts_replace_large_results_with_what_the_call_produced():
    big = {"job_id": "job_1", "trial_id": "t", "lock_digest": "d", "status": "ok", "blob": "z" * 2000}
    r = workflow._compact_receipt({"index": 0, "tool": "run_component", "ok": True, "result": big})
    assert r == {"i": 0, "tool": "run_component", "ok": True,
                 "result": {"job_id": "job_1", "trial_id": "t", "lock_digest": "d", "status": "ok", "truncated": True}}
    r = workflow._compact_receipt({"index": 2, "tool": "x", "ok": False, "code": "E", "replayed": True, "skipped": True,
                                   "error": {"message": "m" * 500, "next_action": "n" * 500,
                                             "details": {"d": "z" * 2000}}})
    assert r["replayed"] is True and r["error"]["code"] == "E" and len(r["error"]["message"]) == 300
    assert len(r["error"]["next_action"]) == 200 and "details" not in r["error"]


def test_a_large_refusal_keeps_its_reasons_in_the_compact_receipt():
    """R15 finding 3: a refusal is a successful command whose reasons are the answer; compaction never drops them."""
    reasons = [{"code": f"R{i}", "message": f"reason {i}: " + "m" * 300, "details": {"field": f"f{i}"}} for i in range(6)]
    reasons.append({"code": "BIG", "message": "x", "details": {"seeds": list(range(400))}})
    refusal = {"request_id": "req_9", "status": "refused", "exploratory": False, "reasons": reasons}
    assert len(json.dumps(refusal)) > workflow.SMALL_JSON
    r = workflow._compact_receipt({"index": 3, "tool": "assess_candidate", "ok": True, "result": refusal})
    out = r["result"]
    assert out["status"] == "refused" and out["request_id"] == "req_9" and out["exploratory"] is False
    assert [x["code"] for x in out["reasons"]] == [*(f"R{i}" for i in range(6)), "BIG"]
    assert out["reasons"][0] == reasons[0]  # code, whole message and small details
    assert out["reasons"][-1]["details"] == {"keys": ["seeds"]}  # only an oversized details object is summarized


@sessioned
async def test_a_refused_assessment_in_a_batch_returns_every_reason(client, lab):
    job = lab.start_job(TRAIN)
    lab.add_claim("claim_x")
    lab.refuse_assessment(*[(f"RULE_{i}", f"rule {i} failed: " + "why " * 60) for i in range(5)])
    call = {"tool": "assess_candidate", "args": {**ASSESSMENT, "claim_id": "claim_x", "candidate": {"job_id": job}}}
    r = ok(await client.call_tool("run_batch", batch("ra", [call], investigation_id=INV, wait_seconds=0)))
    result = r["calls"][0]["result"]
    assert r["calls"][0]["ok"] is True and result["status"] == "refused" and result["truncated"] is True
    assert [x["code"] for x in result["reasons"]] == [f"RULE_{i}" for i in range(5)]
    assert all(x["message"].startswith(f"rule {i} failed") and len(x["message"]) > 200
               for i, x in enumerate(result["reasons"]))


# -- schema size regressions (ADR 0025: tools/list is paid for on every session) ---------------------------------------------------------

def _listing() -> tuple[list[dict], str]:
    tools = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in _tool_defs()]
    return tools, json.dumps({"tools": tools}, separators=(",", ":"))


def test_tools_list_stays_small():
    tools, listing = _listing()
    assert len(tools) == len(TOOLS) == 39
    assert len(listing.encode()) <= 41_000  # 46,387 bytes for 35 tools before ADR 0025; 39,726 for 39 now
    assert sum(len(t.get("description", "")) for t in tools) <= 7_500


def test_the_command_key_reminder_is_short_and_the_semantics_are_stated_once():
    tools, _ = _listing()
    mutating = [t for t in tools if "command_key" in t["inputSchema"]["properties"]]
    assert len(mutating) == sum(1 for t in TOOLS if t.mutating) > 20
    for t in mutating:
        doc = t["inputSchema"]["properties"]["command_key"]["description"]
        assert len(doc) <= 100, t["name"]
        assert "COMMAND_KEY_CONFLICT" not in json.dumps(t), t["name"]
    assert INSTRUCTIONS.count("COMMAND_KEY_CONFLICT") == 1
    assert INSTRUCTIONS.count("retry with the same key") == 1
    assert "caller-chosen command_key" in INSTRUCTIONS and "original receipt" in INSTRUCTIONS
    for name in ("upload_source", "run_batch", "wait_status", "submit_candidate", "$ref"):
        assert name in INSTRUCTIONS


def test_composite_tools_have_no_payload_escape_hatch():
    tools, _ = _listing()
    by = {t["name"]: t for t in tools}
    assert {t.name for t in WORKFLOW_TOOLS} == {"upload_source", "run_batch", "wait_status", "submit_candidate"}
    for t in WORKFLOW_TOOLS:
        assert t.composite and "payload" not in by[t.name]["inputSchema"]["properties"], t.name
        assert by[t.name]["inputSchema"]["additionalProperties"] is False
    for name, tool in BY_NAME.items():
        if not tool.composite:
            assert "payload" in by[name]["inputSchema"]["properties"], name


def test_workflow_tool_schemas():
    tools, _ = _listing()
    by = {t["name"]: t for t in tools}
    assert by["upload_source"]["inputSchema"]["required"] == ["project", "command_key", "files"]
    assert by["run_batch"]["inputSchema"]["required"] == ["project", "command_key", "calls"]
    assert by["submit_candidate"]["inputSchema"]["required"] == [
        "project", "command_key", "investigation_id", "job_id", "statement", "scope"]
    assert by["wait_status"]["inputSchema"]["required"] == ["project"]
    assert "command_key" not in by["wait_status"]["inputSchema"]["properties"]
    assert by["wait_status"]["annotations"]["readOnlyHint"] is True
    for name in ("upload_source", "run_batch", "submit_candidate"):
        assert by[name]["annotations"]["readOnlyHint"] is False and by[name]["annotations"]["destructiveHint"] is False
    for name in ("run_batch", "wait_status"):  # the wait is bounded and the schema says so
        assert "50" in by[name]["inputSchema"]["properties"]["wait_seconds"]["description"] + by[name]["description"]


def test_every_batchable_tool_is_a_real_non_nested_tool():
    assert set(workflow.BATCH_TOOLS) <= set(BY_NAME)
    assert not {"run_batch", "wait_status", "submit_candidate"} & set(workflow.BATCH_TOOLS)
    assert [n for n in workflow.BATCH_TOOLS if BY_NAME[n].composite] == ["upload_source"]
    assert set(workflow.BATCH_JOB_TOOLS) <= set(workflow.BATCH_TOOLS)
    assert set(workflow.BATCH_RESULT_FIELDS) <= set(workflow.BATCH_TOOLS)
    for name in workflow.BATCH_TOOLS:
        assert BY_NAME[name].mutating, name
    for tool in workflow.BATCH_JOB_TOOLS:  # a job is referenced by its job_id and its output slots
        assert "job_id" in workflow.BATCH_RESULT_FIELDS[tool]
