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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_incumbent_challengers import Scene
from test_turn_recovery import ShimClient, setup

from eidos.application.handlers import investigations
from eidos.investigator.activities import InvestigatorActivities
from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import (
    ALL_WORKFLOWS,
    MAX_FAILED_TURN_RETRIES,
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
                    outer.requests.append({"path": self.path, "body": json.loads(raw or b"{}"), "received_at": time.time()})
                    status, body = outer.script.pop(0) if outer.script else outer.default
                delay = body.get("_fixture_delay_seconds", 0)
                if delay:
                    time.sleep(delay)
                data = json.dumps({k: v for k, v in body.items() if k != "_fixture_delay_seconds"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
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
async def running(env, lab, prov: FakeProvider, *, investigator_options=None, investigation_limits=None,
                  before_complete=None, start_in_finalization=False, scene=None, seed_investigation=None, **workflow_args):
    """Start the real investigation coordinator against ``prov`` and terminate it at the end."""
    pid = scene.pid if scene is not None else setup(lab)[0]  # project, environment, component and charter
    inv = lab.cmd(lab.admin, "start_investigation", {
        "question": "q", "charter_id": f"charter:s{pid}",
        "limits": {"model_tokens": 1e6, "trials": 100, **(investigation_limits or {})},
        "investigator": {"runtime": "pydantic-ai", "model": "openai-compatible:fake", "finalization": "operator",
                         "settings": {"max_tokens_per_decision": 120_000},
                         **(investigator_options or {})}}, project=pid)["result"]["investigation_id"]
    if seed_investigation is not None:
        seed_investigation(inv)
    if start_in_finalization:
        admitted = lab.cmd(lab.admin, "enter_finalization", {
            "investigation_id": inv, "trigger": "fixture-recovery",
        }, project=pid)["result"]
        assert admitted["enabled"]
    acts = InvestigatorActivities(
        ShimClient(lab, lab.principal(f"inv-{pid}", ["investigator"], [pid])),
        ShimClient(lab, lab.principal(f"wf-{pid}", ["workflow"], [pid])),
        runtime_overrides={"base_url": prov.url, "api_key_env": "EIDOS_TEST_NO_SUCH_KEY", "output_mode": "json",
                           "request_timeout": 30, "abort_poll_seconds": 0.2, "max_output_tokens": MAX_OUTPUT_TOKENS})
    @activity.defn(name="complete_turn")
    def delayed_complete(a):
        before_complete(a)
        return acts.complete_turn(a)

    # The activities the real investigator worker registers (eidos.apps.roots.run_investigator_worker).
    complete = delayed_complete if before_complete else acts.complete_turn
    registered = [acts.begin_turn, acts.build_context, acts.decide, acts.execute_decision, complete,
                  acts.check_news, acts.mark_needs_input, acts.record_turn_retry, acts.enter_finalization, acts.reconcile_finalization]
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
        assert 1.0 <= retry["body"]["delay_seconds"] <= 1.2  # ADR 0023: up to 20 % deterministic jitter
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
        assert view["needs_input"]["code"] == "runtime-bounded"
        assert view["needs_input"]["details"]["runtime_outcome"] == "reasoning-runaway"
        assert [t["stop_reason"] for t in view["turns"]] == ["runtime-local-bound"] * 3
        assert run.events("investigation.turn_retry_scheduled") == []
        [marked] = run.events("investigation.needs_input")
        assert marked["body"]["code"] == "runtime-bounded"
        bounded = run.events("decision.runtime_bounded")
        assert [row["body"]["slot"] for row in bounded] == [0, 1] * 3
        assert all(row["body"]["runtime_outcome"] == "reasoning-runaway" and row["body"]["response_artifact_id"]
                   for row in bounded)
        # The existing two-slot refusal limit and two fresh-turn retries bound the total.
        usages = [e["body"]["usage"] for e in run.events("model.usage")]
        assert len(usages) == 6 and sum(u["calls"] for u in usages) == prov.count == 12
        for usage in usages:
            assert usage["calls"] == 2
            assert len(usage["structured_output"]["runaway"]) == 2
            assert usage["structured_output"]["runaway_reasks"] == 1
            assert "reasoning runaway" in usage["limit_reached"]
            assert "failed_request" not in usage
        for ended in view["turns"]:
            turn = run.lab.q(run.lab.admin, "get_turn", {"turn_id": ended["id"]}, project=run.pid)
            assert turn["decisions"] == []


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
        assert [r["attempt"] for r in retries] == [1, 2]  # base 1 s, doubling, up to 20 % jitter (ADR 0023)
        assert 1.0 <= retries[0]["delay_seconds"] <= 1.2 and 2.0 <= retries[1]["delay_seconds"] <= 2.4
        assert [r["failed_turn_id"] for r in retries] == [t["id"] for t in view["turns"][:-1]]
        # Every failed turn says why on its turn.completed event; three Temporal attempts of decide per turn.
        completed = run.events("turn.completed")
        assert len(completed) == 3 and all(e["body"]["failure"]["retryable"] is True for e in completed)
        assert prov.count == 3 * 3
        assert run.model_tokens()["reserved"] == 0
        # And then it waits for the operator: no further turn, no further request.
        await asyncio.sleep(3)
        assert run.stop_reasons() == ["error"] * 3 and prov.count == 9


async def test_slow_provider_still_records_a_conclusion_before_export_margin(env, lab, provider):
    export_margin = 5.0  # arm-4 adapter's WATCHDOG_MARGIN_SECONDS, tested at its registration boundary
    status, delayed = decision_reply(ASK, "delayed-exploration")
    conclude = decision_reply({
        "rationale": "no eligible locked candidate",
        "calls": [],
        "control": {"action": "conclude", "summary": "Abstain because no candidate was locked.",
                    "claim_json": json.dumps({"disposition": "abstain"})},
    }, "conclusion")
    fake = provider([(status, {**delayed, "_fixture_delay_seconds": 12})], conclude)
    watchdog_deadline = time.time() + 24.0
    options = {"finalization": "autonomous", "wall_seconds": 24.0, "conclude_threshold_seconds": 9.0,
               "wall_deadline_unix": watchdog_deadline - export_margin}
    async with running(env, lab, fake, investigator_options=options) as run:
        wall = run.view()["body"]["wall"]
        assert wall["deadline_unix"] == watchdog_deadline - export_margin
        result = await asyncio.wait_for(run.handle.result(), 25)
        assert result["status"] == "finished" and result["reason"] == "concluded"
        shown = run.view()
        assert shown["status"] == "concluded" and shown["body"]["conclusion"]["claim_state"]["state"] == "abstained"
        (closure,) = [event for event in run.events("turn.completed") if event["body"]["stop_reason"] == "concluded"]
        written_at = datetime.fromisoformat(closure["created_at"]).timestamp()
        assert watchdog_deadline - written_at > export_margin
        assert run.view()["needs_input"] is None
        assert run.stop_reasons()[-1] == "concluded" and fake.count == 2


async def test_failed_default_recovery_finishes_with_an_attributed_controller_abstention(env, lab, provider):
    invalid = decision_reply({
        "rationale": "an empty wait is not admissible",
        "calls": [],
        "control": {"action": "wait_for_jobs", "summary": "scripted refused wait"},
    })
    fake = provider([], invalid)
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous"}) as run:
        result = await asyncio.wait_for(run.handle.result(), 20)
        assert result["status"] == "abstained" and result["reason"] == "controller-abstained"
        shown = run.view()
        assert shown["status"] == "stopped" and shown["needs_input"] is None
        assert "conclusion" not in shown["body"]
        abstention = shown["body"]["controller_abstention"]
        assert abstention["author"] == "controller" and abstention["investigator_concluded"] is False
        assert shown["finalization_export_receipt"]["controller_abstention"] == abstention
        assert len(run.events("investigation.controller_abstained")) == 1
        assert not run.events("investigation.needs_input")


@pytest.mark.parametrize(("reply", "code", "kind"), [
    ((429, {"error": {"message": "provider quota exhausted, usage limit exceeded", "code": "2056"}}),
     "provider-unavailable", "quota-exhausted"),
    (error_reply(401), "turn-failed", "permanent"),
])
async def test_default_terminal_non_retryable_provider_before_any_lock_abstains(env, lab, provider, reply, code, kind):
    fake = provider([reply], reply)
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous"}) as run:
        result = await asyncio.wait_for(run.handle.result(), 20)
        assert result["status"] == "abstained" and result["reason"] == "controller-abstained"
        shown = run.view()
        assert shown["status"] == "stopped" and shown["needs_input"] is None
        abstention = shown["body"]["controller_abstention"]
        assert abstention["incumbent"] is None and abstention["code"] == code
        assert abstention["failure"]["provider"]["kind"] == kind
        assert abstention["investigator_concluded"] is False
        assert fake.count == 1 and not run.events("investigation.turn_retry_scheduled")


async def test_default_no_lock_abstains_after_retryable_failure_exhausts_its_retries(env, lab, provider):
    unavailable = error_reply(503)
    fake = provider([], unavailable)
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous"}) as run:
        result = await asyncio.wait_for(run.handle.result(), 45)
        assert result["status"] == "abstained" and result["reason"] == "controller-abstained"
        shown = run.view()
        assert shown["status"] == "stopped" and shown["needs_input"] is None
        abstention = shown["body"]["controller_abstention"]
        assert abstention["incumbent"] is None and abstention["code"] == "turn-failed"
        assert abstention["failure"]["retryable"] is True
        assert abstention["failure"]["provider"]["status"] == 503 and abstention["investigator_concluded"] is False
        assert len(run.events("investigation.turn_retry_scheduled")) == MAX_FAILED_TURN_RETRIES
        assert run.stop_reasons() == ["error"] * (MAX_FAILED_TURN_RETRIES + 1)
        assert fake.count == 3 * (MAX_FAILED_TURN_RETRIES + 1)


@pytest.mark.parametrize(("options", "limits", "cause"), [
    ({"max_turns": 0}, {}, "max-turns-reached"),
    ({}, {"turns": 0}, "turn-budget-exhausted"),
    ({}, {"model_tokens": 0}, "budget-exhausted"),
    ({"wall_seconds": 0.00001}, {}, "wall-deadline-reached"),
])
async def test_default_hard_limit_before_first_turn_abstains_with_exact_cause(env, lab, provider, options, limits, cause):
    fake = provider([], decision_reply(ASK))
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous", **options},
                       investigation_limits=limits) as run:
        result = await asyncio.wait_for(run.handle.result(), 20)
        assert result["status"] == "abstained" and result["reason"] == "controller-abstained"
        shown = run.view()
        assert shown["status"] == "stopped" and shown["needs_input"] is None
        abstention = shown["body"]["controller_abstention"]
        assert abstention["incumbent"] is None and abstention["code"] == "coordinator-stopped"
        assert abstention["failure"]["error_type"] == cause and abstention["investigator_concluded"] is False
        assert not shown["turns"] and fake.count == 0


@pytest.mark.parametrize("policy", ["autonomous", "operator"])
async def test_queued_completion_cannot_commit_a_late_conclusion(env, lab, provider, monkeypatch, policy):
    fake = provider([], decision_reply({
        "rationale": "timely response", "calls": [],
        "control": {"action": "conclude", "summary": "No candidate was locked.",
                    "claim_json": json.dumps({"disposition": "abstain"})},
    }))
    queued, release = threading.Event(), threading.Event()

    def hold_completion(a):
        assert a["stop_reason"] == "concluded"
        queued.set()
        assert release.wait(12), "test did not release the completion activity"

    deadline = time.time() + 30
    async with running(env, lab, fake, before_complete=hold_completion,
                       investigator_options={"finalization": policy, "wall_seconds": 40,
                                             "wall_deadline_unix": deadline, "conclude_threshold_seconds": 0}) as run:
        try:
            assert await asyncio.to_thread(queued.wait, 10)
            (decision,) = run.events("decision.accepted")
            assert datetime.fromisoformat(decision["created_at"]).timestamp() < deadline
            child = env.client.get_workflow_handle(f"{run.inv}-turn-1")
            history = await child.fetch_history()
            (completion,) = [event for event in history.events
                             if event.activity_task_scheduled_event_attributes.activity_type.name == "complete_turn"]
            assert completion.event_time.ToDatetime().timestamp() < deadline
            before = run.view()
            assert "conclusion" not in before["body"]
            actual_lab_deadline = before["body"]["wall"]["deadline_unix"]
            monkeypatch.setattr(investigations, "time", SimpleNamespace(time=lambda: actual_lab_deadline + 1))
        finally:
            release.set()
        result = await asyncio.wait_for(run.handle.result(), 15)
        shown = run.view()
        assert "conclusion" not in shown["body"] and run.stop_reasons() == ["deadline"]
        (closed,) = run.events("turn.completed")
        assert closed["body"]["control_refused"] == "wall-deadline-reached"
        if policy == "autonomous":
            assert result["status"] == "abstained" and shown["status"] == "stopped"
            assert shown["needs_input"] is None
            abstention = shown["body"]["controller_abstention"]
            assert abstention["failure"]["error_type"] == "wall-deadline-reached"
            assert abstention["investigator_concluded"] is False
        else:
            assert result["reason"] == "wall-deadline-reached" and shown["status"] == "active"
            assert shown["needs_input"]["details"]["failure"]["error_type"] == "wall-deadline-reached"


@pytest.mark.parametrize("control, calls", [
    ({"action": "request_input"}, []),
    ({"action": "wait_for_jobs"}, [{"tool": "run_component", "args": {"component_ref": "test.echo@1.0.0"}}]),
])
async def test_first_autonomous_reply_has_one_effect_free_authored_correction(env, lab, provider, control, calls):
    first = decision_reply({"rationale": "Synthetic invalid finalization.", "calls": calls, "control": control},
                           "invalid-finalization")
    second = decision_reply({"rationale": "Synthetic correction.", "calls": [],
                             "control": {"action": "stop", "summary": "Synthetic limited conclusion."}},
                            "authored-correction")
    fake = provider([first, second], second)
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous"},
                       start_in_finalization=True) as run:
        assert (await asyncio.wait_for(run.handle.result(), 25))["reason"] == "concluded"
        assert fake.count == 2
        [accepted] = run.events("decision.accepted")
        assert accepted["body"]["slot"] == 0 and accepted["body"]["calls"] == []
        assert not run.events("investigation.controller_abstained")
        assert not run.events("job.created")
        first_schema, second_schema = [request["body"]["response_format"]["json_schema"]["schema"]
                                       for request in fake.requests]
        assert first_schema["properties"]["control"]["properties"]["action"]["enum"] == ["conclude", "stop"]
        assert first_schema["properties"]["calls"]["maxItems"] == 0
        assert "read" in first_schema["properties"] and "read" not in second_schema["properties"]
        assert "Your reply could not be used" in str(fake.requests[1]["body"]["messages"])
        assert len(run.view()["turns"]) == 1


async def test_early_local_envelope_bound_before_lock_keeps_exploration_open(env, lab, provider):
    status, first = decision_reply({"rationale": "Inspect a job before deciding.",
                                    "read": [{"tool": "get_job", "args": {"job_id": "missing-fixture"}}]},
                                   "read-before-envelope-refusal")
    first["usage"].update(prompt_tokens=117_000, total_tokens=117_020)
    second = decision_reply(ASK, "normal-exploration-continuation")
    fake = provider([(status, first), second], second)
    async with running(env, lab, fake, investigator_options={"wall_seconds": 600, "finalization": "autonomous"}) as run:
        await until("first normal post-bound decision", lambda: run.events("decision.accepted"))
        [bound] = run.events("decision.runtime_bounded")
        assert bound["body"]["slot"] == 0 and bound["body"]["runtime_outcome"] == "request-budget-exhausted"
        assert run.events("decision.accepted")[0]["body"]["slot"] == 1 and fake.count == 2
        assert not run.events("investigation.finalization_started")
        assert not run.events("investigation.finalization_phase_started")
        assert run.view()["body"]["wall"]["deadline_unix"] - time.time() > 300


async def test_transient_provider_failure_retries_same_recovery_without_double_usage(env, lab, provider):
    second = decision_reply({"rationale": "Synthetic recovery after transient error.", "calls": [],
                             "control": {"action": "stop", "summary": "Synthetic limited conclusion."}},
                            "recovery-after-provider-retry")
    fake = provider([error_reply(503), second], second)
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous"},
                       start_in_finalization=True) as run:
        assert (await asyncio.wait_for(run.handle.result(), 25))["reason"] == "concluded"
        assert fake.count == 2 and len(run.view()["turns"]) == 1
        assert len(run.events("investigation.finalization_started")) == 1
        [failed, successful] = [event["body"]["usage"] for event in run.events("model.usage")]
        assert failed["failure"]["status"] == 503 and failed["calls"] == 0
        assert successful["calls"] == 1 and successful["request_ids"] == ["recovery-after-provider-retry"]
        assert run.model_tokens()["reserved"] == 0


async def test_first_recovery_runaway_requires_evidence_limited_authored_decision(env, lab, provider):
    second = decision_reply({"rationale": "No extra evidence was read.", "calls": [],
                             "control": {"action": "stop", "summary": "Synthetic limited conclusion."}},
                            "limited-after-runaway")
    fake = provider([runaway_reply(), second], second)
    async with running(env, lab, fake, investigator_options={"finalization": "autonomous"},
                       start_in_finalization=True) as run:
        assert (await asyncio.wait_for(run.handle.result(), 25))["reason"] == "concluded"
        assert fake.count == 2
        [usage] = [event["body"]["usage"] for event in run.events("model.usage")]
        assert usage["calls"] == 2 and usage["structured_output"]["evidence_limited"] is True
        assert "No new evidence was gathered" in str(fake.requests[1]["body"]["messages"])
        schema = fake.requests[1]["body"]["response_format"]["json_schema"]["schema"]
        assert "read" not in schema["properties"]


async def test_inflight_exploration_deadline_preserves_ordinary_reproduction_and_conclusion(env, lab, provider):
    scene = Scene(lab)

    def seed(inv):
        scene.rule(inv)
        scene.lock(inv, scene.accuracy_job(inv, .91))

    status, delayed = decision_reply(ASK, "exploration-inflight")
    reproduction = decision_reply({
        "rationale": "Synthetic ordinary reproduction of the locked fixture recipe.",
        "purpose": "reproduction",
        "calls": [{"tool": "run_component", "args": {"component_ref": scene.trial, "environment": "lab", "purpose": "reproduction",
                  "config": {"split": "validation"}, "wall_seconds": 4}}],
    }, "ordinary-reproduction")
    conclusion = decision_reply({
        "rationale": "Synthetic conclusion that reproduction was admitted, with no verification asserted.",
        "calls": [], "control": {"action": "stop", "summary": "Synthetic limited conclusion."},
    }, "ordinary-conclusion")
    fake = provider([(status, {**delayed, "_fixture_delay_seconds": 17}), reproduction, conclusion], conclusion)
    options = {"finalization": "autonomous", "wall_seconds": 39, "conclude_threshold_seconds": 9,
               "exploration": {"min_holdback_seconds": 14, "max_holdback_seconds": 14}}
    async with running(env, lab, fake, investigator_options=options, scene=scene, seed_investigation=seed) as run:
        assert (await asyncio.wait_for(run.handle.result(), 35))["reason"] == "concluded"
        assert fake.count == 3 and not run.events("investigation.finalization_started")
        [entered] = run.events("investigation.finalization_phase_started")
        assert entered["body"]["trigger"] == "exploration-boundary"
        assert run.stop_reasons() == ["deadline", "concluded"]
        early_turn = scene.q("get_turn", {"turn_id": run.view()["turns"][0]["id"]})
        boundary = early_turn["spec"]["completion"]["exploration_deadline_unix"]
        assert 11 <= boundary - fake.requests[0]["received_at"] <= 17
        assert fake.requests[1]["received_at"] >= boundary
        [bounded] = run.events("decision.runtime_bounded")
        assert bounded["body"]["phase_deadline"] is True and bounded["body"]["usage"]["uncertain"]
        accepted = run.events("decision.accepted")
        attempted = scene.q("get_record", {"kind": "decision_receipts", "id": accepted[0]["subject"]})["body"]
        admitted = [event for event in run.events("job.admitted") if event["body"]["purpose"] == "reproduction"]
        assert len(admitted) == 1, attempted
        [reproduced] = admitted
        assert reproduced["body"]["component"] == scene.trial


async def test_conclusion_reserve_uses_only_recorded_metrics_when_ordinary_time_is_gone(env, lab, provider):
    scene = Scene(lab)
    locked = {}

    stop = decision_reply({
        "rationale": "Synthetic scoped stop, citing only the fixture's recorded visible metric.",
        "calls": [], "control": {"action": "stop", "summary": "Visible validation accuracy 0.913. No confirmation."},
    }, "recorded-metric-only")
    fake = provider([], stop)

    def seed(inv):
        scene.rule(inv)
        locked["job_id"] = scene.job(None, metrics={"accuracy": .913})
        scene.lock(inv, locked["job_id"])
        fake.script = [decision_reply({"rationale": "Read the recorded locked job.",
                                      "read": [{"tool": "get_job", "args": {"job_id": locked["job_id"]}}]})]

    options = {"finalization": "autonomous", "wall_seconds": 20, "conclude_threshold_seconds": 30}
    async with running(env, lab, fake, investigator_options=options, scene=scene, seed_investigation=seed) as run:
        assert (await asyncio.wait_for(run.handle.result(), 15))["reason"] == "concluded"
        [entered] = run.events("investigation.finalization_started")
        assert entered["body"]["trigger"] == "ordinary-finalization-time-exhausted"
        [turn] = run.view()["turns"]
        turn = scene.q("get_turn", {"turn_id": turn["id"]})
        assert turn["spec"]["completion"]["conclude_now"] is True
        assert turn["spec"]["finalization"]["locked_candidate"]["id"] == locked["job_id"]
        assert turn["spec"]["finalization"]["metrics"]
        assert fake.count == 2
        assert locked["job_id"] in str(fake.requests[0]["body"]["messages"])
        assert ".913" in str(fake.requests[1]["body"]["messages"])


@pytest.mark.parametrize("variant", ["omitted", "explicit", "mixed", "planned", "same-batch-plan", "unresolved"])
async def test_ordinary_finalization_refuses_challenger_effect_hidden_by_reproduction_headline(
        env, lab, provider, variant):
    scene = Scene(lab)
    stop = decision_reply({"rationale": "Synthetic stop after durable refusal.", "calls": [],
                           "control": {"action": "stop", "summary": "No verification."}}, "ordinary-stop")
    fake = provider([], stop)
    seeded_jobs = []

    def seed(inv):
        args = {"component_ref": scene.trial, "environment": "lab", "config": {"split": "validation"}}
        scene.rule(inv)
        if variant == "planned":
            plan = scene.cmd("plan_trial", {**args, "investigation_id": inv, "purpose": "open-search"})
            calls = [{"tool": "launch_trial", "args": {"trial_id": plan["trial_id"], "purpose": "reproduction"}}]
        elif variant == "unresolved":
            calls = [{"tool": "launch_trial", "args": {"trial_id": "trial_missing", "purpose": "reproduction"}}]
        elif variant == "same-batch-plan":
            calls = [{"tool": "plan_trial", "args": {**args, "purpose": "open-search"}},
                     {"tool": "launch_trial", "args": {"trial_id": {"$ref": "call[0].trial_id"}}}]
        else:
            declared = {} if variant == "omitted" else {"purpose": "open-search"}
            calls = [{"tool": "run_component", "args": {**args, **declared}}]
            if variant == "mixed":
                calls.insert(0, {"tool": "run_component", "args": {**args, "purpose": "reproduction"}})
        scene.lock(inv, scene.accuracy_job(inv, .91))
        seeded_jobs.extend(event["subject"] for event in scene.events(inv, "job.admitted"))
        fake.script = [decision_reply({"rationale": "Synthetic conflicting purpose.", "purpose": "reproduction",
                                      "calls": calls}, "conflicting-purpose")]

    options = {"finalization": "autonomous", "exploration": {"max_rounds": 0}}
    async with running(env, lab, fake, investigator_options=options, scene=scene, seed_investigation=seed) as run:
        assert (await asyncio.wait_for(run.handle.result(), 20))["reason"] == "concluded"
        [turn] = run.view()["turns"]
        rejected, accepted = scene.q("get_turn", {"turn_id": turn["id"]})["decisions"]
        assert not rejected["accepted"] and accepted["accepted"]
        details = rejected["body"]["error"]["details"]
        assert details["purpose"] == "reproduction"
        source = {"omitted": "run_component default", "planned": "stored trial plan",
                  "same-batch-plan": "same-decision trial plan", "unresolved": "unresolved trial plan"}
        assert details["effect_purposes"] == [{"call": 1 if variant in ("mixed", "same-batch-plan") else 0,
                                               "purpose": None if variant == "unresolved" else "open-search",
                                               "source": source.get(variant, "run_component args.purpose")}]
        assert "args.purpose to reproduction" in rejected["body"]["error"]["next_action"]
        assert [event["subject"] for event in run.events("job.admitted")] == seeded_jobs


@pytest.mark.parametrize("unrelated_sibling", [False, True])
async def test_ordinary_finalization_pending_exception_is_bound_to_the_effect(env, lab, provider, unrelated_sibling):
    scene = Scene(lab)
    stop = decision_reply({"rationale": "Synthetic limited stop.", "calls": [],
                           "control": {"action": "stop", "summary": "Pending scoring was admitted."}})
    fake = provider([], stop)
    pending = {}

    def seed(inv):
        scene.rule(inv, scorer="proj.scorer")
        incumbent = scene.job(inv, config={"variant": "incumbent"})
        scene.scored(inv, incumbent, {"accuracy": .70})
        scene.lock(inv, incumbent)
        pending["artifact"] = scene.output(scene.job(inv, config={"variant": "challenger"}))
        calls = [{"tool": "run_component", "args": {"component_ref": scene.scorer, "environment": "lab",
                  "purpose": "measurement-development", "inputs": {"predictions": pending["artifact"]},
                  "config": {"split": "validation"}}}]
        if unrelated_sibling:
            calls.append({"tool": "run_component", "args": {"component_ref": scene.trial, "environment": "lab",
                          "purpose": "measurement-development", "config": {"split": "validation"}}})
        fake.script = [decision_reply({"rationale": "Score the already-admitted challenger.",
                                      "purpose": "measurement-development", "calls": calls})]

    async with running(env, lab, fake, investigator_options={"exploration": {"max_rounds": 1}},
                       scene=scene, seed_investigation=seed) as run:
        assert (await asyncio.wait_for(run.handle.result(), 20))["reason"] == "concluded"
        [phase] = run.events("investigation.finalization_phase_started")
        assert phase["body"]["trigger"] == "search-limit"
        admitted = [event for event in run.events("job.admitted")
                    if event["body"]["purpose"] == "measurement-development"]
        if unrelated_sibling:
            assert not admitted
            [turn] = run.view()["turns"]
            rejected, _ = scene.q("get_turn", {"turn_id": turn["id"]})["decisions"]
            assert rejected["body"]["error"]["details"]["calls"] == [1]
        else:
            [scorer] = admitted
            assert scorer["body"]["component"] == scene.scorer
            job = scene.q("get_job", {"job_id": scorer["subject"]})
            assert job["lock"]["inputs"]["predictions"]["artifact_id"] == pending["artifact"]
