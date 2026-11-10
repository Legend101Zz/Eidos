"""MCP adapter argument normalization (ADR 0017) against the *fake* lab client (fixture-only: no ledger).

What is checked here is what the adapter sends to the lab: normalized arguments, the ``normalized_arguments`` record
that travels with the command, and, just as important, the cases where nothing is normalized and nothing extra is
sent. The lab-side refusal of the original shapes is covered by tests/integration/test_mcp_normalized_receipts.py.
"""

from __future__ import annotations

import copy

import httpx
import pytest
from fake_client import FakeLab
from test_mcp_server import body, sessioned
from test_mcp_wire import stdio_params

from eidos.client import LabClient, LabError
from eidos.mcp.gateway import ToolFailure, call_tool
from eidos.mcp.tools import BY_NAME
from mcp import Client

REF = "demo.command@1.0.0"
CONFIG_SCHEMA = {"type": "object", "properties": {
    "argv": {"type": "array", "items": {"type": "string"}},
    "seed": {"type": "integer"},
    "timeout_seconds": {"type": "number"}}}
ARGV = ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", "{seed}"]
# MiniMax via Claude Code calling the run_component tool: the exact config the lab refused.
MANGLED = {"argv": {"item": ARGV}, "seed": "11", "timeout_seconds": "180"}
NORMALIZED = {"argv": ARGV, "seed": 11, "timeout_seconds": 180}
MANGLED_RECORD = [
    {"path": "config.argv", "rule": "item-wrapper", "expected": "array", "original": {"item": "<array of 9>"}},
    {"path": "config.seed", "rule": "numeric-string", "expected": "integer", "original": "11", "normalized": 11},
    {"path": "config.timeout_seconds", "rule": "numeric-string", "expected": "number", "original": "180",
     "normalized": 180},
]
SEED_RECORD = {"path": "seed", "rule": "numeric-string", "expected": "integer", "original": "7", "normalized": 7}


@pytest.fixture
def lab():
    return FakeLab()


def release(schema=CONFIG_SCHEMA, **manifest):
    """A describe_component answer: the record view the lab returns (manifest under body)."""
    m = {"id": "demo.command", "release": "1.0.0", **manifest}
    if schema is not None:
        m["config_schema"] = schema
    return {"id": REF, "kind": "component_release", "revision": 1, "body": {"manifest": m}}


def run_args(**over):
    return {"project": "p", "command_key": "k-1", "component_ref": REF, "environment": "lab", **over}


def commands(lab):
    return [c for c in lab.calls if c[0] == "command"]


def describes(lab):
    return [c for c in lab.calls if c[:2] == ("query", "describe_component")]


def call(lab, name, args):
    return call_tool(lab.client("good-token"), name, args)


# -- the real example, end to end over an MCP session -------------------------------------------------------------

@sessioned
async def test_mangled_run_component_config_reaches_the_lab_normalized_and_recorded(client, lab):
    lab.queries["describe_component"] = release()
    args = run_args(config=copy.deepcopy(MANGLED))
    r = await client.call_tool("run_component", args)
    assert not r.is_error
    [(_, name, payload, project, key, token)] = commands(lab)
    assert (name, project, key, token) == ("run_component", "p", "k-1", "good-token")
    assert payload == {"component_ref": REF, "environment": "lab", "config": NORMALIZED}
    assert type(payload["config"]["seed"]) is int
    assert lab.command_extras == [{"normalized_arguments": MANGLED_RECORD}]
    assert body(r)["normalized_arguments"] == MANGLED_RECORD  # the receipt carries the record back to the caller
    [(_, _, params, d_project, _, d_token)] = describes(lab)  # the schema is read with the caller's own token
    assert params == {"component_ref": REF} and d_project == "p" and d_token == "good-token"


@sessioned
async def test_top_level_numeric_string_and_query_record_over_a_session(client, lab):
    r = await client.call_tool("list_events", {"project": "p", "limit": "5", "horizon": 9})
    assert not r.is_error
    assert body(r)["params"] == {"limit": 5, "horizon": 9}
    assert body(r)["normalized_arguments"] == [
        {"path": "limit", "rule": "numeric-string", "expected": "integer", "original": "5", "normalized": 5}]


@sessioned
async def test_ambiguous_shape_is_a_lab_matter_not_an_adapter_rewrite_over_a_session(client, lab):
    lab.queries["describe_component"] = release()
    cfg = {"argv": {"item": "python"}, "seed": " 11"}
    r = await client.call_tool("run_component", run_args(config=copy.deepcopy(cfg)))
    assert not r.is_error
    assert commands(lab)[0][2]["config"] == cfg
    assert lab.command_extras == [{}] and "normalized_arguments" not in body(r)


# -- run_component, plan_trial, plan_branch: every tool that takes a component config --------------------------------

@pytest.mark.parametrize("tool, extra", [("run_component", {}), ("plan_trial", {}),
                                         ("plan_branch", {"parent_checkpoint_id": "ckpt-1"})])
def test_every_config_tool_normalizes_against_the_component_schema(lab, tool, extra):
    lab.queries["describe_component"] = release()
    call(lab, tool, run_args(config=copy.deepcopy(MANGLED), **extra))
    [(_, name, payload, *_rest)] = commands(lab)
    assert name == tool and payload["config"] == NORMALIZED
    assert lab.command_extras == [{"normalized_arguments": MANGLED_RECORD}]
    assert len(describes(lab)) == 1


def test_nested_shapes_in_a_config_are_found_through_the_schema(lab):
    schema = {"type": "object", "properties": {
        "stages": {"type": "array", "items": {"type": "object", "properties": {
            "lr": {"type": "number"}, "steps": {"type": "integer"}, "tags": {"type": "array"}}}}}}
    lab.queries["describe_component"] = release(schema)
    cfg = {"stages": [{"lr": "0.5", "steps": "10", "tags": {"item": ["a"]}}, {"lr": 0.1, "steps": 3}]}
    call(lab, "run_component", run_args(config=cfg))
    assert commands(lab)[0][2]["config"] == {"stages": [{"lr": 0.5, "steps": 10, "tags": ["a"]},
                                                        {"lr": 0.1, "steps": 3}]}
    paths = [c["path"] for c in lab.command_extras[0]["normalized_arguments"]]
    assert paths == ["config.stages[0].lr", "config.stages[0].steps", "config.stages[0].tags"]


# -- nothing to normalize: nothing extra is sent ------------------------------------------------------------------

def test_a_correct_call_sends_no_record_and_reads_no_schema(lab):
    cfg = {"argv": ["python", "project.py"], "seed": 11, "timeout_seconds": 180, "note": "x"}
    r = call(lab, "run_component", run_args(config=copy.deepcopy(cfg), seed=7, wall_seconds=300))
    assert lab.command_extras == [{}]  # the normalized_arguments keyword is not passed at all
    assert not describes(lab)
    assert commands(lab)[0][2] == {"component_ref": REF, "environment": "lab", "config": cfg, "seed": 7,
                                   "wall_seconds": 300}
    assert "normalized_arguments" not in r


def test_a_call_without_a_config_reads_no_schema(lab):
    call(lab, "run_component", run_args(seed="7"))
    assert not describes(lab)
    assert lab.command_extras == [{"normalized_arguments": [SEED_RECORD]}]


def test_a_correct_config_with_leading_dash_strings_changes_nothing(lab):
    """``--seed`` makes the adapter look the schema up (it might be a negative number); nothing is then changed."""
    lab.queries["describe_component"] = release()
    cfg = {"argv": ["python", "project.py", "--seed", "-3", "--out", "{out}"], "seed": 11}
    r = call(lab, "run_component", run_args(config=copy.deepcopy(cfg)))
    assert commands(lab)[0][2]["config"] == cfg
    assert lab.command_extras == [{}] and "normalized_arguments" not in r


def test_a_query_without_changes_has_no_normalized_arguments_key(lab):
    r = call(lab, "list_events", {"project": "p", "limit": 5})
    assert r == {"query": "list_events", "params": {"limit": 5}}


# -- ambiguous shapes pass through untouched (the lab refuses them) -------------------------------------------------

@pytest.mark.parametrize("cfg", [
    {"argv": {"item": "python"}},
    {"argv": {"item": ARGV, "other": 1}},
    {"argv": {"items": ARGV}},
    {"seed": " 11"}, {"seed": "011"}, {"seed": "+1"}, {"seed": "1.0"}, {"seed": "1e2"},
    {"timeout_seconds": "NaN"}, {"timeout_seconds": "Infinity"}, {"timeout_seconds": "1e400"},
    {"timeout_seconds": "0x10"}, {"timeout_seconds": "1_000"}, {"seed": ""}, {"seed": "١٢"},
    {"seed": True}, {"seed": None},
])
def test_ambiguous_config_is_sent_to_the_lab_exactly_as_received(lab, cfg):
    lab.queries["describe_component"] = release()
    r = call(lab, "plan_trial", run_args(config=copy.deepcopy(cfg)))
    assert commands(lab)[0][2]["config"] == cfg
    assert lab.command_extras == [{}] and "normalized_arguments" not in r


def test_only_the_unambiguous_fields_are_normalized_and_recorded(lab):
    lab.queries["describe_component"] = release()
    call(lab, "run_component", run_args(config={"argv": {"item": "python"}, "seed": "11", "timeout_seconds": "NaN"}))
    assert commands(lab)[0][2]["config"] == {"argv": {"item": "python"}, "seed": 11, "timeout_seconds": "NaN"}
    assert [c["path"] for c in lab.command_extras[0]["normalized_arguments"]] == ["config.seed"]


def test_values_are_not_rewritten_where_the_component_schema_does_not_say_so(lab):
    schema = {"type": "object", "properties": {"label": {"type": "string"}, "free": {"type": "object"},
                                               "either": {"type": ["array", "null"]}}}
    lab.queries["describe_component"] = release(schema)
    cfg = {"label": "11", "free": {"item": ["a"]}, "either": {"item": ["a"]}, "unlisted": "5"}
    call(lab, "run_component", run_args(config=copy.deepcopy(cfg)))
    assert commands(lab)[0][2]["config"] == cfg
    assert lab.command_extras == [{}]


# -- top-level arguments -------------------------------------------------------------------------------------------

def test_top_level_numeric_strings_are_normalized_and_recorded(lab):
    r = call(lab, "run_component", run_args(seed="7", wall_seconds="300"))
    payload = commands(lab)[0][2]
    assert payload["seed"] == 7 and payload["wall_seconds"] == 300
    assert [c["path"] for c in lab.command_extras[0]["normalized_arguments"]] == ["seed", "wall_seconds"]
    assert lab.command_extras[0]["normalized_arguments"][0] == SEED_RECORD
    assert r["normalized_arguments"] == lab.command_extras[0]["normalized_arguments"]


def test_expected_revision_as_a_string_is_normalized_before_the_revision_check(lab):
    r = call(lab, "pause_investigation", {"project": "p", "command_key": "k", "investigation_id": "i",
                                          "expected_revision": "3"})  # the fake lab's revision is 3
    assert r["status"] == "committed"
    assert r["normalized_arguments"][0]["path"] == "expected_revision"


def test_a_stale_string_revision_is_still_refused_by_the_lab(lab):
    with pytest.raises(ToolFailure) as e:
        call(lab, "pause_investigation", {"project": "p", "command_key": "k", "investigation_id": "i",
                                          "expected_revision": "2"})
    assert e.value.code == "STALE_REVISION"


def test_top_level_array_wrapper_is_unwrapped_for_a_declared_array_parameter(lab):
    r = call(lab, "compare_trials", {"project": "p", "job_ids": {"item": ["j1", "j2"]}})
    assert r["params"] == {"job_ids": ["j1", "j2"]}
    assert r["normalized_arguments"] == [
        {"path": "job_ids", "rule": "item-wrapper", "expected": "array", "original": {"item": "<array of 2>"}}]


def test_query_results_carry_the_record_including_the_special_query_paths(lab):
    r = call(lab, "get_artifact_access", {"project": "p", "artifact_id": "a1", "representation": "preview",
                                          "max_bytes": "2048"})
    assert r["normalized_arguments"][0]["path"] == "max_bytes"
    assert lab.calls[-1][1:3] == ("artifact_preview", {"artifact_id": "a1", "max_bytes": 2048})
    r = call(lab, "get_study_charter", {"project": "p", "charter_id": "charter:s", "history": True, "revision": "2"})
    assert r["normalized_arguments"][0]["path"] == "revision"
    assert lab.calls[-1][1] == "record_history"


def test_the_callers_arguments_are_never_mutated(lab):
    lab.queries["describe_component"] = release()
    args = run_args(config=copy.deepcopy(MANGLED), seed="7")
    snapshot = copy.deepcopy(args)
    call(lab, "run_component", args)
    assert args == snapshot


# -- the adapter's own validation is not relaxed -----------------------------------------------------------------

@pytest.mark.parametrize("tool, args, fragment", [
    ("compare_trials", {"project": "p", "job_ids": {"item": "x"}}, "'job_ids' must be array"),
    ("compare_trials", {"project": "p", "job_ids": {"item": ["a"], "x": 1}}, "'job_ids' must be array"),
    ("list_events", {"project": "p", "limit": "1.5"}, "'limit' must be integer"),
    ("list_events", {"project": "p", "limit": " 5"}, "'limit' must be integer"),
    ("list_events", {"project": "p", "limit": "NaN"}, "'limit' must be integer"),
    ("list_events", {"project": "p", "limit": True}, "'limit' must be integer"),
    ("list_components", {"project": "p", "text": 5}, "'text' must be string"),
])
def test_invalid_top_level_arguments_are_still_refused_before_the_lab(lab, tool, args, fragment):
    with pytest.raises(ToolFailure) as e:
        call(lab, tool, args)
    assert e.value.code == "INVALID_ARGUMENTS" and fragment in e.value.message
    assert not lab.calls


@sessioned
async def test_invalid_arguments_are_tool_errors_over_a_session(client, lab):
    r = await client.call_tool("compare_trials", {"project": "p", "job_ids": {"item": "x"}})
    assert r.is_error and body(r)["error"]["code"] == "INVALID_ARGUMENTS"
    assert not lab.calls


@sessioned
async def test_an_absurdly_long_digit_string_is_an_invalid_argument_not_an_internal_error(client, lab):
    r = await client.call_tool("list_events", {"project": "p", "limit": "9" * 5000})
    assert r.is_error and body(r)["error"]["code"] == "INVALID_ARGUMENTS"
    assert not lab.calls


# -- the schema lookup can fail or say nothing: no normalization, the call is still sent ----------------------------

@pytest.mark.parametrize("status, code", [(404, "NOT_FOUND"), (403, "FORBIDDEN"), (500, "INTERNAL")])
def test_a_failing_schema_lookup_means_no_config_normalization_and_the_call_is_still_sent(lab, status, code):
    def fail(_params):
        raise LabError(status, {"code": code, "message": "no", "next_action": ""})
    lab.queries["describe_component"] = fail
    r = call(lab, "run_component", run_args(config=copy.deepcopy(MANGLED)))
    assert r["status"] == "committed"
    assert commands(lab)[0][2]["config"] == MANGLED  # the lab will refuse it and say why
    assert lab.command_extras == [{}]
    assert len(describes(lab)) == 1


def test_a_non_lab_error_in_the_lookup_is_swallowed_the_same_way(lab):
    def boom(_params):
        raise RuntimeError("transport hiccup")
    lab.queries["describe_component"] = boom
    call(lab, "run_component", run_args(config=copy.deepcopy(MANGLED)))
    assert commands(lab)[0][2]["config"] == MANGLED and lab.command_extras == [{}]


def test_top_level_normalization_survives_a_failed_schema_lookup(lab):
    def fail(_params):
        raise LabError(404, {"code": "NOT_FOUND", "message": "no", "next_action": ""})
    lab.queries["describe_component"] = fail
    call(lab, "run_component", run_args(config=copy.deepcopy(MANGLED), seed="7"))
    [(_, _, payload, *_rest)] = commands(lab)
    assert payload["seed"] == 7 and payload["config"] == MANGLED
    assert lab.command_extras == [{"normalized_arguments": [SEED_RECORD]}]


@pytest.mark.parametrize("answer", [
    release(schema=None),                                   # a component without a config_schema
    {"id": REF, "body": {}},                                # no manifest
    {"id": REF, "body": {"manifest": {"config_schema": "not a schema"}}},
    {"id": REF},                                            # no body
    [],                                                     # not an object
])
def test_a_missing_or_unusable_schema_means_no_config_normalization(lab, answer):
    lab.queries["describe_component"] = answer
    call(lab, "run_component", run_args(config=copy.deepcopy(MANGLED)))
    assert commands(lab)[0][2]["config"] == MANGLED and lab.command_extras == [{}]


def test_the_lookup_is_not_made_for_a_tool_without_a_config_parameter(lab):
    lab.queries["describe_component"] = release()
    call(lab, "launch_trial", {"project": "p", "command_key": "k", "trial_id": "t1",
                               "payload": {"config": copy.deepcopy(MANGLED), "component_ref": REF}})
    assert not describes(lab)
    assert commands(lab)[0][2]["config"] == MANGLED and lab.command_extras == [{}]


# -- retries and replays -------------------------------------------------------------------------------------------

def test_retry_with_the_same_key_replays_the_same_receipt_with_its_record(lab):
    lab.queries["describe_component"] = release()
    args = run_args(config=copy.deepcopy(MANGLED))
    a = call(lab, "run_component", copy.deepcopy(args))
    b = call(lab, "run_component", copy.deepcopy(args))
    assert a["replayed"] is False and b["replayed"] is True
    assert lab.executed == 1  # the same mangled arguments normalize to the same payload, so the key replays
    assert a["normalized_arguments"] == b["normalized_arguments"] == MANGLED_RECORD


def test_the_record_is_not_part_of_the_idempotency_payload(lab):
    """A retry whose arguments arrive already correct still replays the original admission."""
    lab.queries["describe_component"] = release()
    a = call(lab, "run_component", run_args(config=copy.deepcopy(MANGLED)))
    b = call(lab, "run_component", run_args(config=copy.deepcopy(NORMALIZED)))
    assert b["replayed"] is True and lab.executed == 1
    assert b["result"] == a["result"]
    assert lab.command_extras[1] == {}  # the second call had nothing to record


# -- tool descriptions and the HTTP client -------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["run_component", "plan_trial", "plan_branch"])
def test_the_config_parameter_asks_for_exact_json_types(tool):
    doc = BY_NAME[tool].input_schema()["properties"]["config"]["description"]
    assert "config_schema" in doc and "JSON arrays" in doc and "JSON numbers" in doc


def lab_client_with(handler) -> LabClient:
    c = LabClient("http://lab.invalid", "tok")
    c.http = httpx.Client(base_url="http://lab.invalid", transport=httpx.MockTransport(handler))
    return c


def test_lab_client_sends_normalized_arguments_only_when_there_are_some():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "committed"})

    c = lab_client_with(handler)
    c.command("run_component", {"a": 1}, project="p", key="k1")
    c.command("run_component", {"a": 1}, project="p", key="k2", normalized_arguments=[])
    c.command("run_component", {"a": 1}, project="p", key="k3", normalized_arguments=MANGLED_RECORD)
    assert "normalized_arguments" not in seen[0] and "normalized_arguments" not in seen[1]
    assert seen[2]["normalized_arguments"] == MANGLED_RECORD
    assert seen[2]["payload"] == {"a": 1} and seen[2]["command_key"] == "k3"


# -- real transport --------------------------------------------------------------------------------------------------

async def test_stdio_transport_carries_the_record_back_in_the_receipt():
    async with Client(stdio_params()) as c:
        r = await c.call_tool("pause_investigation", {"project": "p", "command_key": "wire-norm-1",
                                                      "investigation_id": "i", "expected_revision": "3"})
        assert not r.is_error
        assert r.structured_content["normalized_arguments"] == [
            {"path": "expected_revision", "rule": "numeric-string", "expected": "integer", "original": "3",
             "normalized": 3}]
