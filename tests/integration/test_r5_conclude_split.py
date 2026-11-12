"""D02's phase transition, abandoned terminal request and fallback through real coordinator activities and ledger."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from test_cancel_through_acceptance import _env
from test_incumbent_challengers import Scene
from test_r4_finalization import _controller_abstention, _locked
from test_turn_recovery import ShimClient

from eidos.domain.turns import turn_boundary
from eidos.investigator import activities as activities_module
from eidos.investigator import pydantic_ai_runtime as runtime_module
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.pydantic_ai_runtime import FINALIZATION_FALLBACK_NOTE
from eidos.investigator.runtime import DecisionAbort

pytestmark = [pytest.mark.pg, pytest.mark.fixture_llm]


@pytest.fixture
def scene(lab):
    return Scene(lab)


@pytest.mark.parametrize("mode", ["json", "tool"])
@pytest.mark.parametrize("fallback_concludes", [True, False])
def test_d02_ordinary_deadline_then_two_read_rounds_and_timeout_fallback(scene, monkeypatch, mode, fallback_concludes):
    inv, job = _locked(scene, wall_seconds=1800, decision_deadline_seconds=600)
    wall = scene.q("get_investigation", {"investigation_id": inv})["body"]["wall"]
    assert wall["conclude_threshold_seconds"] == 270
    wall_now, mono = [wall["deadline_unix"] - 400], [0.0]
    clock = SimpleNamespace(time=lambda: wall_now[0], monotonic=lambda: mono[0])
    monkeypatch.setattr("eidos.application.handlers.investigations.time", clock)
    monkeypatch.setattr(activities_module, "time", clock)
    monkeypatch.setattr(runtime_module, "time", clock)
    monkeypatch.setattr(activities_module, "DecisionAbort", lambda deadline: DecisionAbort(deadline, clock=clock.monotonic))
    real_wait_for = asyncio.wait_for

    async def accelerated(awaitable, timeout):
        if timeout is None or timeout < 1:
            return await real_wait_for(awaitable, timeout)
        task = asyncio.ensure_future(awaitable)
        try:
            return await real_wait_for(task, 0.02)
        except TimeoutError:
            if task.cancelled():
                mono[0] += timeout
                wall_now[0] += timeout
            raise

    monkeypatch.setattr(asyncio, "wait_for", accelerated)
    ordinary_seen, recovery_seen, read_jobs = [], [], []

    async def ordinary_provider(messages, info):
        ordinary_seen.append(deepcopy(info))
        if len(ordinary_seen) > 1:
            await asyncio.Future()
        # Ordinary finalization begins with one quick read, then a request hangs at the reserve boundary.
        if mode == "tool":
            parts = [ToolCallPart("get_job", {"job_id": job})]
        else:
            parts = [TextPart(json.dumps({"rationale": "Check incumbent.", "read": [
                {"tool": "get_job", "args": {"job_id": job}}]}))]
        return ModelResponse(parts=parts, usage=RequestUsage(input_tokens=200, output_tokens=30))

    async def conclude_provider(messages, info):
        recovery_seen.append((deepcopy(messages), deepcopy(info)))
        index = len(recovery_seen)
        if index <= 2:
            elapsed = 5.7 if index == 1 else 5.0
            mono[0] += elapsed
            wall_now[0] += elapsed
            if mode == "tool":
                parts = [ToolCallPart("get_job", {"job_id": job}) for _ in range(4)]
            else:
                parts = [TextPart(json.dumps({"rationale": "Inspect visible receipts.", "read": [
                    {"tool": "get_job", "args": {"job_id": job}} for _ in range(4)]}))]
        elif index == 3 or not fallback_concludes:
            await asyncio.Future()
        else:
            assert FINALIZATION_FALLBACK_NOTE in str(messages)
            assert not info.function_tools
            value = {"rationale": "Report the fixed incumbent with its limits.", "calls": [],
                "control": {"action": "conclude", "summary": f"Locked incumbent {job}; validation only, unverified."}}
            parts = [ToolCallPart(info.output_tools[0].name, value)] if mode == "tool" else [TextPart(json.dumps(value))]
        return ModelResponse(parts=parts, usage=RequestUsage(input_tokens=200, output_tokens=30))

    investigator = scene.lab.principal(f"investigator-{inv}", ["investigator"], [scene.pid])
    acts = InvestigatorActivities(ShimClient(scene.lab, investigator), ShimClient(scene.lab, scene.wf),
        runtime_overrides={"runtime": "pydantic-ai", "output_mode": mode, "model": "openai-compatible:fixture",
            "_model_object": FunctionModel(ordinary_provider), "request_timeout": 300, "max_output_tokens": 16_384,
            "max_tokens_per_decision": 262_144})
    scene.cmd("enter_finalization", {"investigation_id": inv, "trigger": "exploration-boundary"})
    env = _env(1)
    ordinary = env.run(acts.begin_turn, {"project_id": scene.pid, "investigation_id": inv, "index": 1})
    spec = scene.q("get_turn", {"turn_id": ordinary["turn_id"]})["spec"]
    assert spec["finalization_phase"]["mode"] == "ordinary" and not spec.get("finalization")
    assert turn_boundary(spec["completion"]) == wall["deadline_unix"] - 270
    base = {"project_id": scene.pid, "investigation_id": inv, "turn_id": ordinary["turn_id"]}
    env.run(acts.build_context, base)
    bounded = env.run(acts.decide, {**base, "slot": 0})
    assert bounded["runtime_outcome"] == "deadline-exhausted" and not bounded["accepted"]
    usage = scene.events(inv, "model.usage")[-1]["body"]["usage"]
    assert usage["aborted"]["in_flight"] and usage["in_flight_estimate"]["output_tokens"] == 16_384
    env.run(acts.complete_turn, {**base, "stop_reason": "runtime-bounded", "control": {}})

    # Move to conclude-only admission after the ordinary boundary, including the activity's 15 s holdback.
    wall_now[0] = wall["deadline_unix"] - 269
    acts.runtime_overrides["_model_object"] = FunctionModel(conclude_provider)
    final = env.run(acts.begin_turn, {"project_id": scene.pid, "investigation_id": inv, "index": 2})
    final_spec = scene.q("get_turn", {"turn_id": final["turn_id"]})["spec"]
    assert final_spec["finalization"]["enabled"] is True and final_spec["limits"]["max_model_calls"] == 1
    base = {**base, "turn_id": final["turn_id"]}
    env.run(acts.build_context, base)
    # Record executions while retaining the real, horizon-authorized read implementation.
    real_read = runtime_module._read

    def record_read(tools, name, *args):
        read_jobs.append((name, args))
        return real_read(tools, name, *args)

    monkeypatch.setattr(runtime_module, "_read", record_read)
    decision = env.run(acts.decide, {**base, "slot": 0})
    usage = scene.events(inv, "model.usage")[-1]["body"]["usage"]
    report = usage["structured_output"]
    assert len(ordinary_seen) == 2 and len(recovery_seen) == 4 and len(read_jobs) == 8
    assert report["time_split"]["window_s"] == pytest.approx(253, abs=1)
    assert report["time_split"]["read_slot_s"] == 30 and report["time_split"]["armed"] is True
    assert report["time_split"]["fallback_reserve_s"] == pytest.approx(75.9, abs=0.3)
    assert report["time_split"]["final_timeout_s"] == pytest.approx(164.4, abs=1)
    assert report["fallback_used"] and report["final_attempt_timed_out"] and usage["uncertain"]
    assert usage["in_flight_estimate"]["input_tokens"] > 0
    assert usage["in_flight_estimate"]["output_tokens"] == 16_384 * (1 if fallback_concludes else 2)
    assert all(info.model_settings["max_tokens"] == 16_384 for _, info in recovery_seen)
    if fallback_concludes:
        assert decision["accepted"] and decision["control"]["action"] == "conclude"
        env.run(acts.complete_turn, {**base, "stop_reason": "concluded", "control": decision["control"]})
        shown = scene.q("get_investigation", {"investigation_id": inv})
        assert shown["status"] == "concluded" and "controller_abstention" not in shown["body"]
        # Investigator authorship is represented by the accepted provider decision and export flag.
        assert shown["body"]["conclusion"]["summary"] == decision["control"]["summary"]
        persisted = scene.q("get_turn", {"turn_id": final["turn_id"]})["decisions"]
        assert len(persisted) == 1 and persisted[0]["accepted"]
        assert persisted[0]["body"]["rationale"] == "Report the fixed incumbent with its limits."
        assert shown["finalization_export_receipt"]["investigator_concluded"] is True
        assert report["fallback_outcome"] == "valid"
    else:
        assert decision["runtime_outcome"] == "deadline-exhausted" and not decision["accepted"]
        env.run(acts.complete_turn, {**base, "stop_reason": "runtime-bounded", "control": {}})
        shown, _ = _controller_abstention(scene, inv, job)
        assert shown["finalization_export_receipt"]["failure"]["reason"] == "runtime-bounded"
        assert report["fallback_outcome"] == "deadline-exhausted"
        assert shown["body"]["controller_abstention"]["code"] == "finalization-failed"
    with scene.lab.ledger.transaction() as tx:
        tx.project_id = scene.pid
        entries = [entry for entry in tx.budget_entries(f"inv:{inv}")
                   if entry["dimension"] == "model_tokens" and entry["ref"].startswith(f"model:{final['turn_id']}:")]
    possible = [entry for entry in entries if entry["kind"] == "possible-usage"]
    assert len(possible) == 1
    estimate = usage["in_flight_estimate"]
    assert float(possible[0]["amount"]) == estimate["input_tokens"] + estimate["output_tokens"]
    known = [entry for entry in entries if entry["kind"] == "usage"]
    assert len(known) == 1
    assert float(known[0]["amount"]) == usage["input_tokens"] + usage["output_tokens"]
    assert known[0]["detail"]["structured_output"]["time_split"] == report["time_split"]
    assert possible[0]["detail"]["structured_output"]["time_split"] == report["time_split"]
    assert possible[0]["detail"]["structured_output"]["fallback_outcome"] == report["fallback_outcome"]
