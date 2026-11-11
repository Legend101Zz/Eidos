"""ADR 0019 end to end: a failed investigator turn never leaves the investigation active with nothing scheduled.

The real lab (PostgreSQL), the real ``InvestigatorActivities`` with the real pydantic-ai runtime, and the real
``InvestigationWorkflow`` / ``TurnWorkflow`` run on a Temporal development server. The model is a local fake
OpenAI-compatible HTTP server that answers ``/v1/chat/completions`` from a script (an HTTP status and a JSON body per
request) and records every request body. No live model is called.

What is asserted is what an operator or an MCP client sees: the ``get_investigation`` query (``needs_input``, the
turns and their stop reasons) and the ledger events (``investigation.needs_input``, ``investigation.turn_retry_scheduled``,
``turn.completed``, ``model.usage``).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_turn_recovery import ShimClient, setup

from eidos.investigator.activities import InvestigatorActivities
from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import (
    ALL_WORKFLOWS,
    MAX_FAILED_TURN_RETRIES,
    MAX_INVALID_RETRY_TURNS,
    InvestigationWorkflow,
)

pytestmark = [pytest.mark.pg, pytest.mark.temporal]
TEMPORAL_CLI = os.environ.get("EIDOS_TEMPORAL_CLI", "/opt/temporal/temporal")

MAX_OUTPUT_TOKENS = 512  # the output cap the fake reasoning model "spends" in a runaway reply
ASK = {"rationale": "need a measurement the catalog does not offer", "calls": [],
       "control": {"action": "request_input", "summary": "need data"}}


# -- the fake provider ---------------------------------------------------------------------------------------


def decision_reply(decision: dict, rid: str = "r1") -> tuple[int, dict]:
    return 200, {"choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": json.dumps(decision)}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                           "completion_tokens_details": {"reasoning_tokens": 5}},
                 "model": "fake", "id": rid, "object": "chat.completion", "created": 0}


def runaway_reply(cap: int = MAX_OUTPUT_TOKENS, rid: str = "run") -> tuple[int, dict]:
    """A reasoning model that spent its whole output cap on reasoning: finish_reason=length and no answer."""
    return 200, {"choices": [{"index": 0, "finish_reason": "length",
                              "message": {"role": "assistant", "content": "",
                                          "reasoning_content": "Let me think about every possible measurement..."}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": cap, "total_tokens": 100 + cap,
                           "completion_tokens_details": {"reasoning_tokens": cap}},
                 "model": "fake", "id": rid, "object": "chat.completion", "created": 0}


def error_reply(status: int) -> tuple[int, dict]:
    return status, {"error": {"message": f"fake provider says {status}", "type": "fake_error", "code": str(status)}}


def _output_cap(body: dict) -> int | None:
    """The output cap a request carries (the field name depends on the provider profile)."""
    return body.get("max_completion_tokens", body.get("max_tokens"))


class FakeProvider:
    """``POST /v1/chat/completions`` answering from a script, then from ``default``; every request body is recorded."""

    def __init__(self, script: list[tuple[int, dict]], default: tuple[int, dict]):
        self.script, self.default = list(script), default
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with outer.lock:
                    outer.requests.append({"path": self.path, "body": json.loads(raw or b"{}")})
                    status, body = outer.script.pop(0) if outer.script else outer.default
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def answer_from_now_on(self, reply: tuple[int, dict]) -> None:
        with self.lock:
            self.script, self.default = [], reply

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def provider():
    servers: list[FakeProvider] = []

    def make(script: list[tuple[int, dict]], default: tuple[int, dict]) -> FakeProvider:
        servers.append(FakeProvider(script, default))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


# -- the real lab, activities and workflows -----------------------------------------------------------------


@pytest.fixture(scope="module")
async def env():
    if not Path(TEMPORAL_CLI).exists():
        pytest.skip("temporal CLI not available")
    e = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    yield e
    await e.shutdown()


class Run:
    """One running investigation: the lab views an operator sees, and the way to act on it as an operator."""

    def __init__(self, lab, pid: str, inv: str, handle):
        self.lab, self.pid, self.inv, self.handle = lab, pid, inv, handle

    def view(self) -> dict:
        return self.lab.q(self.lab.admin, "get_investigation", {"investigation_id": self.inv}, project=self.pid)

    def events(self, *types: str) -> list[dict]:
        return self.lab.q(self.lab.admin, "list_events", {"investigation_id": self.inv, "types": list(types),
                                                          "limit": 200}, project=self.pid)["events"]

    def stop_reasons(self) -> list[str | None]:
        return [t["stop_reason"] for t in self.view()["turns"]]

    def model_tokens(self) -> dict:
        return self.lab.q(self.lab.admin, "budgets", {"investigation_id": self.inv},
                          project=self.pid)["budgets"]["model_tokens"]

    def steer(self, instruction: str = "use another approach") -> dict:
        return self.lab.cmd(self.lab.admin, "steer_investigation",
                            {"investigation_id": self.inv, "instruction": instruction}, project=self.pid)


async def until(what: str, probe, timeout: float = 55.0, every: float = 0.3):
    """Poll ``probe`` (a synchronous callable, run off the event loop) until it returns a truthy value."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await asyncio.to_thread(probe)
        if last:
            return last
        await asyncio.sleep(every)
    raise AssertionError(f"timed out after {timeout:g}s waiting for {what}; last probe: {last!r}")


@contextlib.asynccontextmanager
async def running(env, lab, prov: FakeProvider, **workflow_args):
    """Start the real investigation coordinator against ``prov`` and terminate it at the end."""
    pid, _, _ = setup(lab)  # project, environment, component and charter of the turn-recovery tests
    inv = lab.cmd(lab.admin, "start_investigation", {
        "question": "q", "charter_id": f"charter:s{pid}", "limits": {"model_tokens": 1e6, "trials": 100},
        "investigator": {"runtime": "pydantic-ai", "model": "openai-compatible:fake",
                         "settings": {"max_tokens_per_decision": 120_000}}}, project=pid)["result"]["investigation_id"]
    acts = InvestigatorActivities(
        ShimClient(lab, lab.principal(f"inv-{pid}", ["investigator"], [pid])),
        ShimClient(lab, lab.principal(f"wf-{pid}", ["workflow"], [pid])),
        runtime_overrides={"base_url": prov.url, "api_key_env": "EIDOS_TEST_NO_SUCH_KEY", "output_mode": "json",
                           "request_timeout": 30, "abort_poll_seconds": 0.2, "max_output_tokens": MAX_OUTPUT_TOKENS})
    # The activities the real investigator worker registers (eidos.apps.roots.run_investigator_worker).
    registered = [acts.begin_turn, acts.build_context, acts.decide, acts.execute_decision, acts.complete_turn,
                  acts.check_news, acts.mark_needs_input, acts.record_turn_retry]
    args = {"investigation_id": inv, "project_id": pid, "idle_poll_seconds": 1, "failed_turn_backoff_seconds": 1,
            **workflow_args}
    pool = ThreadPoolExecutor(max_workers=16)  # the real activities are synchronous
    try:
        async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS), \
                Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=registered, activity_executor=pool):
            handle = await env.client.start_workflow(InvestigationWorkflow.run, args, id=f"fte-{uuid.uuid4().hex[:8]}",
                                                     task_queue=WORKFLOW_QUEUE)
            try:
                yield Run(lab, pid, inv, handle)
            finally:
                with contextlib.suppress(Exception):
                    await handle.terminate("test over")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_the_real_investigator_worker_registers_every_activity_the_coordinator_calls():
    """The activity list of these end-to-end runs mirrors ``run_investigator_worker``: no drift."""
    from eidos.apps import roots
    defined = {m.__temporal_activity_definition.name for _, m in inspect.getmembers(InvestigatorActivities)
               if hasattr(m, "__temporal_activity_definition")}
    registered = set(re.findall(r"acts\.(\w+)", inspect.getsource(roots.run_investigator_worker)))
    assert defined == registered, (defined ^ registered)
    assert {"mark_needs_input", "record_turn_retry"} <= registered


# -- A: a non-retryable failure ------------------------------------------------------------------------------


async def test_a_non_retryable_provider_error_surfaces_needs_input_at_once(env, lab, provider):
    prov = provider([], error_reply(401))
    async with running(env, lab, prov) as run:
        view = await until("needs_input after the failed turn", lambda: run.view()["needs_input"] and run.view())
        # Visible to operators and MCP clients: still active, needs input, machine-readable why.
        assert view["status"] == "active"
        ni = view["needs_input"]
        assert ni["code"] == "turn-failed"
        assert ni["details"]["retryable"] is False
        assert ni["details"]["failure"]["error_type"] == "MODEL_CALL_FAILED"
        assert ni["details"]["failure"]["retryable"] is False
        assert ni["details"]["automatic_retries"] == 0
        assert "turn failed" in ni["reason"]
        # One failed turn; no second turn was started and nothing was retried on its own.
        assert [t["stop_reason"] for t in view["turns"]] == ["error"]
        assert ni["details"]["turn_id"] == view["turns"][0]["id"]
        [done] = run.events("turn.completed")
        assert done["body"]["stop_reason"] == "error" and done["body"]["status"] == "failed"
        assert done["body"]["failure"]["error_type"] == "MODEL_CALL_FAILED"
        assert done["body"]["failure"]["retryable"] is False
        turn = run.lab.q(run.lab.admin, "get_turn", {"turn_id": view["turns"][0]["id"]}, project=run.pid)
        assert turn["result"]["failure"] == done["body"]["failure"]
        [marked] = run.events("investigation.needs_input")
        assert marked["body"]["code"] == "turn-failed"
        assert run.events("investigation.turn_retry_scheduled") == []
        # The provider was asked exactly once: Temporal does not retry a non-retryable failure.
        assert prov.count == 1
        assert run.model_tokens()["reserved"] == 0  # the failed decision's reservation was settled
        # The coordinator keeps waiting for the operator; it does not start a turn on its own.
        await asyncio.sleep(3)
        assert run.stop_reasons() == ["error"] and prov.count == 1
        assert (await run.handle.query("state"))["status"] == "needs operator input (turn failed)"

        # An operator steers: needs_input is cleared and the next turn runs (the provider works again).
        prov.answer_from_now_on(decision_reply(ASK))
        await asyncio.to_thread(run.steer)
        [received] = run.events("investigation.input_received")
        assert received["body"]["action"] == "steered" and received["body"]["was"]["code"] == "turn-failed"
        view = await until("a second turn after the operator acted",
                           lambda: (v := run.view())["turns"][1:] and v["turns"][1]["stop_reason"] and v)
        assert [t["stop_reason"] for t in view["turns"]] == ["error", "input-requested"]
        assert view["needs_input"] is None and view["status"] == "active"
        assert prov.count == 2


# -- B: a retryable failure that recovers -------------------------------------------------------------------


async def test_a_retryable_provider_error_is_retried_automatically_and_recovers(env, lab, provider):
    # Three 503s: the three attempts Temporal makes at the decide activity inside turn 1; then the model answers.
    prov = provider([error_reply(503)] * 3, decision_reply(ASK))
    async with running(env, lab, prov) as run:
        view = await until("a second, successful turn",
                           lambda: (v := run.view())["turns"][1:] and v["turns"][1]["stop_reason"] and v)
        assert [t["stop_reason"] for t in view["turns"]] == ["error", "input-requested"]
        assert view["needs_input"] is None and view["status"] == "active"
        # The retry was recorded on the ledger before its backoff, with the reason.
        [retry] = run.events("investigation.turn_retry_scheduled")
        assert retry["body"]["attempt"] == 1 and retry["body"]["max_attempts"] == 2
        assert retry["body"]["delay_seconds"] == 1.0
        assert retry["body"]["failed_turn_id"] == view["turns"][0]["id"]
        assert retry["body"]["failure"]["retryable"] is True
        assert retry["body"]["failure"]["error_type"] == "MODEL_CALL_FAILED"
        # The failed turn carries its failure; the retried turn is an ordinary completed turn.
        first, second = run.events("turn.completed")
        assert first["body"]["stop_reason"] == "error" and first["body"]["failure"]["retryable"] is True
        assert second["body"] == {"stop_reason": "input-requested", "status": "completed"}
        assert run.events("investigation.needs_input") == []
        assert prov.count == 4  # three attempts of turn 1, one decision of turn 2


# -- C: runaway reasoning recovered inside the decision -----------------------------------------------------


async def test_runaway_reasoning_is_recovered_by_one_concise_reask_within_the_decision(env, lab, provider):
    prov = provider([runaway_reply()], decision_reply(ASK))
    async with running(env, lab, prov) as run:
        view = await until("the first turn to end", lambda: (v := run.view())["turns"] and v["turns"][0]["stop_reason"] and v)
        assert [t["stop_reason"] for t in view["turns"]] == ["input-requested"]  # not "error"
        assert view["needs_input"] is None
        assert run.events("investigation.turn_retry_scheduled") == [] and run.events("investigation.needs_input") == []
        [usage_event] = run.events("model.usage")
        usage = usage_event["body"]["usage"]
        assert usage["calls"] == 2  # the runaway request and its re-ask are both counted
        runaway = usage["structured_output"]["runaway"]
        assert len(runaway) == 1 and runaway[0]["finish_reason"] == "length" and runaway[0]["reask"] is False
        assert runaway[0]["output_tokens"] == MAX_OUTPUT_TOKENS and runaway[0]["reasoning_tokens"] == MAX_OUTPUT_TOKENS
        assert usage["structured_output"]["runaway_reasks"] == 1
        # Both requests reached the provider; the second one is the concise, decision-only re-ask.
        assert prov.count == 2
        first, second = (r["body"] for r in prov.requests)
        assert "used its whole output limit" not in json.dumps(first)
        assert "used its whole output limit" in json.dumps(second["messages"])
        assert [_output_cap(b) for b in (first, second)] == [MAX_OUTPUT_TOKENS] * 2  # the same cap, not a smaller one
        first_schema = first["response_format"]["json_schema"]["schema"]
        second_schema = second["response_format"]["json_schema"]["schema"]
        assert "read" in first_schema["properties"]  # the first request may ask to read
        assert "read" not in second_schema["properties"]  # the re-ask is decision-only
        assert second_schema["properties"].keys() <= first_schema["properties"].keys()
        # The decision it produced was accepted and its control honoured.
        turn = run.lab.q(run.lab.admin, "get_turn", {"turn_id": view["turns"][0]["id"]}, project=run.pid)
        assert [d["accepted"] for d in turn["decisions"]] == [True]
        assert turn["decisions"][0]["body"]["control"]["action"] == "request_input"


# -- D: runaway twice, nothing outstanding -------------------------------------------------------------------


async def test_a_repeated_runaway_does_not_stall_the_investigation(env, lab, provider):
    prov = provider([], runaway_reply())  # every request runs away
    async with running(env, lab, prov) as run:
        view = await until("needs_input after the bounded runaway outcomes",
                           lambda: run.view()["needs_input"] and run.view())
        assert view["status"] == "active"
        assert view["needs_input"]["code"] == "refused-proposals"  # the existing bounded invalid-turn path
        assert view["needs_input"]["refused_turns"] == MAX_INVALID_RETRY_TURNS + 1
        # Every turn ended on refused proposals (the bounded no-effect outcome is a wait on nothing, which the lab
        # refuses); none of them ended in an error, so no provider failure is reported.
        assert [t["stop_reason"] for t in view["turns"]] == ["invalid-proposals"] * (MAX_INVALID_RETRY_TURNS + 1)
        assert run.events("investigation.turn_retry_scheduled") == []
        [marked] = run.events("investigation.needs_input")
        assert marked["body"]["code"] == "refused-proposals"
        # Each decision made the first request and exactly one re-ask, both counted and recorded.
        usages = [e["body"]["usage"] for e in run.events("model.usage")]
        assert usages and all(u["calls"] == 2 for u in usages)
        for u in usages:
            assert len(u["structured_output"]["runaway"]) == 2 and u["structured_output"]["runaway_reasks"] == 1
            assert "reasoning runaway" in u["limit_reached"]
        assert prov.count == 2 * len(usages)
        # Nothing was applied from any of those decisions.
        for t in view["turns"]:
            turn = run.lab.q(run.lab.admin, "get_turn", {"turn_id": t["id"]}, project=run.pid)
            assert not any(d["accepted"] for d in turn["decisions"])


# -- E: a retryable failure that persists ---------------------------------------------------------------------


async def test_a_persistent_retryable_failure_stops_after_the_bounded_automatic_retries(env, lab, provider):
    prov = provider([], error_reply(503))  # the provider stays down
    async with running(env, lab, prov) as run:
        view = await until("needs_input after the automatic retries ran out", lambda: run.view()["needs_input"] and run.view(),
                           timeout=58)
        assert view["status"] == "active"
        ni = view["needs_input"]
        assert ni["code"] == "turn-failed"
        # The failure is retryable, but the retries are bounded: 1 turn + MAX_FAILED_TURN_RETRIES retry turns.
        assert ni["details"]["retryable"] is True and ni["details"]["failure"]["retryable"] is True
        assert ni["details"]["automatic_retries"] == MAX_FAILED_TURN_RETRIES
        assert ni["details"]["consecutive_failed_turns"] == MAX_FAILED_TURN_RETRIES + 1
        assert ni["details"]["max_automatic_retries"] == MAX_FAILED_TURN_RETRIES
        assert "still failing after" in ni["reason"]
        assert [t["stop_reason"] for t in view["turns"]] == ["error"] * (MAX_FAILED_TURN_RETRIES + 1)
        assert ni["details"]["turn_id"] == view["turns"][-1]["id"]
        # Each automatic retry was on the ledger before its backoff, which doubles (base 1 s here).
        retries = [e["body"] for e in run.events("investigation.turn_retry_scheduled")]
        assert [(r["attempt"], r["delay_seconds"]) for r in retries] == [(1, 1.0), (2, 2.0)]
        assert [r["failed_turn_id"] for r in retries] == [t["id"] for t in view["turns"][:-1]]
        # Every failed turn says why on its turn.completed event; three Temporal attempts of decide per turn.
        completed = run.events("turn.completed")
        assert len(completed) == 3 and all(e["body"]["failure"]["retryable"] is True for e in completed)
        assert prov.count == 3 * 3
        assert run.model_tokens()["reserved"] == 0
        # And then it waits for the operator: no further turn, no further request.
        await asyncio.sleep(3)
        assert run.stop_reasons() == ["error"] * 3 and prov.count == 9
