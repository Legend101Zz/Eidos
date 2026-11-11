"""ADR 0025 batch rules that mirror single MCP calls: ADR 0017 normalization per call (recorded and sent with the
command), the derived-key length bound, and literal arguments inside ``payload``. Fake lab, no ledger."""

from __future__ import annotations

import pytest
from fake_client import FakeLab

from eidos.mcp.gateway import ToolFailure, call_tool
from eidos.mcp.tools import BY_NAME

SCHEMA = {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}},
                                           "seed": {"type": "integer"}}}


@pytest.fixture
def lab():
    lab = FakeLab()
    lab.queries["describe_component"] = {"manifest": {"config_schema": SCHEMA}}
    return lab


def _batch(lab, calls, key="b1"):
    return call_tool(lab.client("good-token"), "run_batch", {"project": "p", "command_key": key, "calls": calls,
                                                             "wait_seconds": 0})


def test_a_batched_call_is_normalized_like_a_single_call_and_the_record_is_sent(lab):
    out = _batch(lab, [{"tool": "plan_trial", "args": {"component_ref": "x@1", "seed": "11",
                                                      "config": {"argv": {"item": ["a", "b"]}, "seed": "3"}}}])
    sent = [c for c in lab.calls if c[0] == "command" and c[1] == "plan_trial"]
    assert sent and sent[0][2]["seed"] == 11 and sent[0][2]["config"] == {"argv": ["a", "b"], "seed": 3}
    record = lab.command_extras[[c[1] for c in lab.calls if c[0] == "command"].index("plan_trial")]
    paths = sorted(r["path"] for r in record["normalized_arguments"])
    assert paths == ["config.argv", "config.seed", "seed"]
    assert sorted(r["path"] for r in out["calls"][0]["normalized_arguments"]) == paths


def test_the_batch_note_is_recorded_before_the_first_effect(lab):
    _batch(lab, [{"tool": "plan_trial", "args": {"component_ref": "x@1"}}])
    commands = [(c[1], c[4]) for c in lab.calls if c[0] == "command"]
    assert commands == [("record_note", "b1:batch"), ("plan_trial", "b1:0")]


def test_composite_keys_leave_room_for_derived_suffixes(lab):
    assert BY_NAME["run_batch"].input_schema()["properties"]["command_key"]["maxLength"] == 180
    assert BY_NAME["run_component"].input_schema()["properties"]["command_key"]["maxLength"] == 200
    with pytest.raises(ToolFailure) as exc:
        _batch(lab, [{"tool": "plan_trial", "args": {"component_ref": "x@1"}}], key="k" * 181)
    assert exc.value.code == "INVALID_ARGUMENTS"
    assert not [c for c in lab.calls if c[0] == "command"]


@pytest.mark.parametrize("payload", [{"environment": {"$ref": "call[0].job_id"}},
                                     {"purpose": {"$ref": "call[0].job_id"}},
                                     {"environment": {"$ref": "call[0].job_id", "x": 1}}])
def test_literal_arguments_stay_literal_inside_payload(lab, payload):
    calls = [{"tool": "run_component", "args": {"component_ref": "x@1"}},
             {"tool": "run_component", "args": {"component_ref": "x@1", "payload": payload}}]
    with pytest.raises(ToolFailure) as exc:
        _batch(lab, calls)
    assert exc.value.code == "INVALID_REF"
    assert not [c for c in lab.calls if c[0] == "command"]
