"""Archived pilot-3 arm-4 replies through the decision runtime and real coordinator."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ThinkingPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from test_f5d_conclude_race import _complete
from test_failed_turn_policy import running, until
from test_incumbent_challengers import Scene
from test_provider_resilience_workflow import Provider
from test_r4_finalization_workflows import Recovery
from test_workflows import env  # noqa: F401 - Temporal fixture

from eidos.investigator import pydantic_ai_runtime
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import DecisionAbort, DecisionRequest
from eidos.investigator.structured_output import READ_TOOLS
from eidos.orchestration.names import INVESTIGATOR_QUEUE, WORKFLOW_QUEUE
from eidos.orchestration.workflows import ALL_WORKFLOWS, InvestigationWorkflow

FIXTURES = Path(__file__).parents[1] / "fixtures" / "deadline" / "pilot-3"
TIME_SCALE = 0.1
SYNTHETIC_FOLLOW_UP = {
    "rationale": "The historical runtime could not send this request.",
    "calls": [],
    "control": {"action": "request_input", "summary": "Synthetic replay divergence. No historical reply exists."},
}


def replay_slot(slot, *, surrogate_thinking=None):
    recorded = slot["replies"]
    original = [ModelMessagesTypeAdapter.validate_python([item["response"]])[0] for item in recorded]
    assert all(isinstance(reply, ModelResponse) for reply in original)
    usage = slot["usage_receipt"]["body"]["usage"]
    missing = slot.get("missing_runaway_reply_ids", [])
    assert bool(missing) == bool(surrogate_thinking)
    assert usage["request_ids"] == [item["timing"]["provider_response_id"] for item in recorded] + missing
    assert usage["calls"] == len(original) + len(missing)
    known_usage = usage["per_request"][:len(original)]
    assert sum(item["input_tokens"] for item in known_usage) == sum(reply.usage.input_tokens for reply in original)
    assert sum(item["output_tokens"] for item in known_usage) == sum(reply.usage.output_tokens for reply in original)
    if missing:
        runaway, = usage["structured_output"]["runaway"]
        assert missing == [runaway["request_id"]] and runaway["finish_reason"] == "length"
        assert usage["per_request"][-1]["finish_reason"] == "length"
        assert not slot["decision"]["accepted"]
        assert slot["decision"]["body"]["proposal"]["control"]["bounded_outcome"] == "decision-deadline"
    else:
        assert usage["input_tokens"] == sum(reply.usage.input_tokens for reply in original)
        assert usage["output_tokens"] == sum(reply.usage.output_tokens for reply in original)
    deadline = recorded[0]["timing"]["decision_remaining_s"] * TIME_SCALE
    now = [0.0]
    requests = []
    successor_history = []
    expected_reads = [read for read in usage["structured_output"]["reads"] if read.get("executed")]
    actual_reads = []

    def provider(messages, info):
        index = len(requests)
        timeout = info.model_settings["timeout"]
        assert 0 < timeout <= min(300 * TIME_SCALE, deadline - now[0])
        requests.append(timeout)
        if missing and index == len(original):
            # The receipt measures the whole decision activity. Its elapsed time
            # is a proxy for the missing request end, not a provider timestamp.
            now[0] = usage["elapsed_seconds"] * TIME_SCALE
            assert now[0] < deadline
            billed = usage["per_request"][-1]
            response = ModelResponse(
                parts=[ThinkingPart(content=surrogate_thinking)], finish_reason=runaway["finish_reason"],
                model_name=billed["model"], provider_response_id=missing[0],
                usage=RequestUsage(input_tokens=billed["input_tokens"], output_tokens=billed["output_tokens"],
                                   details={"reasoning_tokens": billed["reasoning_tokens"]}),
            )
            assert pydantic_ai_runtime.is_runaway(response)
            return response
        if index == len(original) + len(missing):
            if missing:
                raw = ModelMessagesTypeAdapter.dump_json(messages).decode()
                assert surrogate_thinking not in raw and missing[0] not in raw
                successor_history.extend(
                    [(part["part_kind"], part.get("content")) for part in message["parts"]]
                    for message in json.loads(raw)
                )
            return ModelResponse(parts=[TextPart(json.dumps(SYNTHETIC_FOLLOW_UP))], usage=RequestUsage())
        timing = recorded[index]["timing"]
        next_start = (recorded[index + 1]["timing"]["started_unix"]
                      if index + 1 < len(recorded) else timing["ended_unix"])
        now[0] += (next_start - timing["started_unix"]) * TIME_SCALE
        return original[index]

    def read(tool):
        def execute(*args):
            index = len(actual_reads)
            actual_reads.append((tool, args))
            receipt = expected_reads[index]
            # Replay the recorded read receipt. The recorded assistant replies
            # are fixed independently of this stub response payload.
            return {"replay_receipt": receipt}

        return execute

    runtime = PydanticAIRuntime({
        "_model_object": FunctionModel(provider),
        "model": "openai-compatible:fake",
        "output_mode": "json",
        "request_timeout": 300 * TIME_SCALE,
        "max_requests_per_decision": 8,
        "max_tokens_per_decision": 1_000_000,
    })
    request = DecisionRequest(
        f"historical-{slot['turn']}", slot["slot"], "Recorded pilot failure tail", [],
        {"deadline_seconds": deadline}, abort=DecisionAbort(deadline, clock=lambda: now[0]),
        read_tools={tool: read(tool) for tool in READ_TOOLS},
    )
    result = runtime.decide(request)
    expected = [(receipt["tool"], tuple(receipt["args"][arg] for arg, _, _ in READ_TOOLS[receipt["tool"]]))
                for receipt in expected_reads]
    assert actual_reads == expected
    if missing:
        assert len(requests) == len(original) + len(missing) + 1
        assert result.runtime_outcome is None and result.proposal["control"]["action"] == "request_input"
        assert result.usage["structured_output"]["runaway"] == usage["structured_output"]["runaway"]
        assert result.usage["request_ids"][:usage["calls"]] == usage["request_ids"]
        for key in ("input_tokens", "output_tokens", "reasoning_tokens"):
            assert [item[key] for item in result.usage["per_request"][:usage["calls"]]] == [
                item[key] for item in usage["per_request"]
            ]
        raw = json.dumps(result.raw_response)
        assert surrogate_thinking not in raw and missing[0] not in raw
        # The coordinator continues with the recorded refusal for this slot.
        # Compare both synthetic variants' effects at the actual runtime boundary.
        return len(original), None, (requests, successor_history, result.proposal, result.usage)
    elif slot["decision"]["accepted"]:
        assert len(requests) == len(original) and result.runtime_outcome is None
        assert result.proposal["calls"]
    else:
        historical_control = slot["decision"]["body"]["proposal"]["control"]
        assert historical_control["action"] == "wait_for_jobs"
        if historical_control.get("bounded_outcome") == "decision-deadline":
            if deadline - now[0] <= pydantic_ai_runtime.DURABLE_WRITE_MARGIN_SECONDS:
                assert len(requests) == len(original)
                assert result.runtime_outcome == "deadline-exhausted" and result.proposal == {}
            else:
                assert len(requests) == len(original) + 1
                assert result.runtime_outcome is None and result.proposal["control"]["action"] == "request_input"
        else:
            # This historical reply proposed the wait itself. There is no missing
            # follow-up to synthesize, and the coordinator preserves its refusal.
            assert len(requests) == len(original)
            assert result.runtime_outcome is None and result.proposal["control"]["action"] == "wait_for_jobs"
    return len(original), result.runtime_outcome, None


@pytest.mark.pg
@pytest.mark.temporal
@pytest.mark.fixture_llm
@pytest.mark.parametrize(("fixture_name", "block"), [
    ("N01-r1", "N01-r1"),
    ("N01-r2", "N01-r2"),
    ("N01-r2-aborted-addendum1", "N01-r2"),
    ("N01-r3", "N01-r3"),
    ("N02-r2", "N02-r2"),
    ("N02-r3", "N02-r3"),
    ("N03-r1", "N03-r1"),
    ("N03-r3", "N03-r3"),
    ("N04-r2", "N04-r2"),
    ("N04-r3", "N04-r3"),
])
async def test_recorded_failure_tail_reaches_controller_abstention(env, lab, monkeypatch, fixture_name, block):  # noqa: F811
    fixture = json.loads((FIXTURES / f"{fixture_name}.json").read_text())
    assert fixture["schema_version"] == "eidos-deadline-tail/1"
    assert fixture["source"]["block_id"] == block and fixture["source"]["arm"] == 4
    assert fixture["recorded_outcome"] == "crash" and fixture["recorded_needs_input"]["code"] == "refused-proposals"
    monkeypatch.setattr(pydantic_ai_runtime, "DURABLE_WRITE_MARGIN_SECONDS",
                        pydantic_ai_runtime.DURABLE_WRITE_MARGIN_SECONDS * TIME_SCALE)
    scene = Scene(lab)
    inv = scene.investigation()
    scene.rule(inv)
    candidate = scene.job(inv, metrics={"accuracy": .91})
    scene.lock(inv, candidate)

    class Historical(Recovery):
        def __init__(self):
            super().__init__("invalid")
            self.replies = 0
            self.index = 0
            self.recovery_turn = None

        @activity.defn(name="begin_turn")
        async def begin_turn(self, a):
            result = await super().begin_turn(a)
            if self.final:
                self.recovery_turn = await asyncio.to_thread(scene.begin, inv)
            else:
                historical_turn = fixture["slots"][0]["turn"] + a["index"] - 1
                result["max_model_calls"] = sum(item["turn"] == historical_turn for item in fixture["slots"])
            return result

        @activity.defn(name="decide")
        async def decide(self, a):
            if self.final:
                # The archived run never reached recovery. End the synthetic
                # successor without a model conclusion or confirmation.
                return {"accepted": False, "runtime_outcome": "no-recorded-recovery-reply"}
            item = fixture["slots"][self.index]
            assert a["turn_id"] == f"t{item['turn'] - fixture['slots'][0]['turn'] + 1}" and a["slot"] == item["slot"]
            if item.get("missing_runaway_reply_ids"):
                replay = await asyncio.to_thread(replay_slot, item, surrogate_thinking="Synthetic reasoning variant A.")
                other = await asyncio.to_thread(replay_slot, item, surrogate_thinking="Different synthetic reasoning B.")
                assert replay == other
            else:
                replay = await asyncio.to_thread(replay_slot, item)
            replies, runtime_outcome, _ = replay
            self.replies += replies
            self.index += 1
            if runtime_outcome:
                return {"accepted": False, "runtime_outcome": runtime_outcome, "reason": "runtime-bounded"}
            if item["decision"]["accepted"]:
                # This historical slot preceded the six refusals. Effects are
                # already represented by the recorded receipt and stay offline.
                decision = item["decision"]
                return {"accepted": True, "decision_id": decision["id"], "n_calls": len(decision["body"]["calls"])}
            return {"accepted": False, "reason": item["decision"]["body"]["reason"]}

        @activity.defn(name="execute_decision")
        async def execute_decision(self, a):
            item = fixture["slots"][self.index - 1]
            (receipt,) = item["recorded_effect_receipts"]
            assert receipt["decision_id"] == a["decision_id"]
            return {"summary": receipt["summary"]}

        @activity.defn(name="enter_finalization")
        async def enter_finalization(self, a):
            await super().enter_finalization(a)
            return await asyncio.to_thread(scene.cmd, "enter_finalization", {
                "investigation_id": inv, "trigger": a["trigger"],
            })

        @activity.defn(name="complete_turn")
        async def complete_turn(self, a):
            if not self.final:
                return await super().complete_turn(a)
            result = await asyncio.to_thread(
                _complete, lab, scene.pid, self.recovery_turn["turn_id"], a["stop_reason"], {},
            )
            return result["result"]

    fake = Historical()
    async with Worker(env.client, task_queue=WORKFLOW_QUEUE, workflows=ALL_WORKFLOWS),             Worker(env.client, task_queue=INVESTIGATOR_QUEUE, activities=fake.acts()):
        result = await asyncio.wait_for(env.client.execute_workflow(
            InvestigationWorkflow.run, {"investigation_id": inv, "project_id": scene.pid},
            id=f"pilot-replay-{fixture_name}-{uuid.uuid4().hex}", task_queue=WORKFLOW_QUEUE,
        ), timeout=30)
    assert result["status"] == "abstained" and result["reason"] == "controller-abstained"
    assert fake.index == len(fixture["slots"])
    assert fake.replies == sum(len(item["replies"]) for item in fixture["slots"])
    assert len(fake.transitions) == 1 and not fake.marks
    transition = fake.transitions[0][1]
    if fixture_name == "N01-r3":
        assert transition["trigger"] == "runtime-bounded"
        assert transition["runtime_outcome"] == "deadline-exhausted"
    else:
        assert transition["trigger"] == "refused-proposals"
    shown = scene.q("get_investigation", {"investigation_id": inv})
    assert shown["status"] == "stopped" and shown["needs_input"] is None and "conclusion" not in shown["body"]
    receipt = shown["finalization_export_receipt"]
    assert receipt["incumbent"]["job_id"] == candidate and receipt["investigator_concluded"] is False
    assert receipt["controller_abstention"]["author"] == "controller"


@pytest.mark.temporal
async def test_recorded_terminal_quota_remains_a_provider_failure(env):  # noqa: F811
    fixture = json.loads((FIXTURES / "N02-r2-terminal-quota.json").read_text())
    assert fixture["source"]["recorded_outcome"] == "incident"
    usage = fixture["receipt"]["body"]["usage"]
    assert usage["calls"] == 0 and usage["failed_request"]["status_code"] == 429

    class Quota(Provider):
        @activity.defn(name="decide")
        async def decide(self, a):
            self.decide_attempts[1] = self.decide_attempts.get(1, 0) + 1
            failure = usage["failure"]
            raise ApplicationError(failure["error"], {k: failure[k] for k in ("kind", "status", "retry_after_seconds")},
                                   type="PROVIDER_QUOTA_EXHAUSTED", non_retryable=True)

    fake = Quota({})
    async with running(env, fake):
        await until(lambda: fake.marks, 15, "terminal provider failure")
        (mark,) = fake.marks
        assert mark["code"] == "provider-unavailable"
        assert mark["details"]["failure"]["provider"]["kind"] == "quota-exhausted"
        assert fake.decide_attempts == {1: 1}
        assert not fake.retries
