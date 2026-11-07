"""MCP adapter tests: a real SDK client session in-process against a *fake* lab client
(fixture-only; no ledger involved)."""

from __future__ import annotations

import functools
import inspect
import json

import pytest
from fake_client import FakeLab
from mcp.shared.exceptions import MCPError

from eidos.mcp.server import Credentials, build_server
from eidos.mcp.tools import TOOLS
from mcp import Client

EXPECTED = {
    "list_components", "describe_component", "validate_component", "publish_component", "bind_component",
    "retire_component", "withdraw_component", "start_investigation", "steer_investigation",
    "pause_investigation", "resume_investigation", "commit_study_charter", "amend_study_charter",
    "get_study_charter", "plan_trial", "launch_trial", "run_component", "file_claim", "draft_evidence_plan",
    "freeze_evidence_plan", "launch_evidence_plan", "get_claim", "get_evidence_plan", "get_assessment",
    "plan_branch", "launch_branch", "qualify_protocol", "promote_protocol", "get_investigation", "get_run",
    "list_events", "compare_trials", "cancel_run", "get_artifact_access",
}


@pytest.fixture
def lab():
    return FakeLab()


def sessioned(fn):
    """Open a real SDK client session in-process for the test (no async fixture: anyio cancel scopes
    must be entered and exited in the same task)."""
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


def body(result):
    return result.structured_content


@sessioned
async def test_tool_list_is_the_stable_set(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED
    assert len(tools) == len(TOOLS) == 34
    by = {t.name: t for t in tools}
    for t in tools:
        schema = t.input_schema
        if t.annotations.read_only_hint:
            assert "command_key" not in schema["properties"]
        else:
            assert "command_key" in schema["required"], t.name
            assert "expected_revision" in schema["properties"]
            assert "idempotency" in schema["properties"]["command_key"]["description"].lower() or \
                "retry" in schema["properties"]["command_key"]["description"].lower()
    assert "test.echo" not in json.dumps([t.name for t in tools])  # components are data, not tools
    assert by["get_investigation"].annotations.read_only_hint is True


@sessioned
async def test_read_tool(client, lab):
    r = await client.call_tool("get_investigation", {"project": "p", "investigation_id": "inv_9"})
    assert not r.is_error
    assert body(r)["id"] == "inv_9"
    r = await client.call_tool("list_components", {"project": "p", "text": "echo"})
    assert body(r)["components"][0]["ref"] == "test.echo@1.0.0"
    assert lab.calls[-1][:2] == ("query", "search_components")


@sessioned
async def test_mutating_tool_same_key_returns_same_receipt(client, lab):
    args = {"project": "p", "command_key": "k-1", "question": "why?"}
    a = await client.call_tool("start_investigation", args)
    b = await client.call_tool("start_investigation", args)
    assert not a.is_error and not b.is_error
    assert body(a)["result"] == body(b)["result"]
    assert body(a)["replayed"] is False and body(b)["replayed"] is True
    assert lab.executed == 1  # the retry did not execute a second time
    assert [c for c in lab.calls if c[0] == "command"][0][4] == "k-1"


@sessioned
async def test_same_key_different_payload_is_conflict(client):
    await client.call_tool("start_investigation", {"project": "p", "command_key": "k-2", "question": "a"})
    r = await client.call_tool("start_investigation", {"project": "p", "command_key": "k-2", "question": "b"})
    assert r.is_error
    assert body(r)["error"]["code"] == "COMMAND_KEY_CONFLICT"
    assert body(r)["error"]["next_action"]


@sessioned
async def test_domain_errors_are_tool_results_with_codes(client):
    r = await client.call_tool("launch_trial", {"project": "p", "command_key": "k", "trial_id": "t-broke"})
    assert r.is_error
    e = body(r)["error"]
    assert e["code"] == "BUDGET_EXHAUSTED" and e["next_action"] and e["details"]["dimension"] == "compute_seconds"
    r = await client.call_tool("pause_investigation", {"project": "p", "command_key": "k", "investigation_id": "i",
                                                       "expected_revision": 1})
    assert body(r)["error"]["code"] == "STALE_REVISION"
    assert body(r)["error"]["details"]["observed_revision"] == 3
    # the error text is also visible to clients that ignore structured content
    assert "STALE_REVISION" in r.content[0].text


@sessioned
async def test_missing_command_key_is_a_tool_error_not_a_protocol_error(client, lab):
    r = await client.call_tool("pause_investigation", {"project": "p", "investigation_id": "i"})
    assert r.is_error
    assert body(r)["error"]["code"] == "INVALID_ARGUMENTS"
    assert "command_key" in body(r)["error"]["message"]
    assert not [c for c in lab.calls if c[0] == "command"]


@sessioned
async def test_unknown_argument_and_not_found(client):
    r = await client.call_tool("get_run", {"project": "p", "job_id": "missing"})
    assert r.is_error and body(r)["error"]["code"] == "NOT_FOUND"
    r = await client.call_tool("get_run", {"project": "p", "job_id": "j", "bogus": 1})
    assert r.is_error and body(r)["error"]["code"] == "INVALID_ARGUMENTS"


@sessioned
async def test_payload_passthrough_and_named_precedence(client, lab):
    await client.call_tool("start_investigation", {
        "project": "p", "command_key": "k-3", "question": "named", "payload": {"question": "x", "notes": "n"}})
    pl = [c for c in lab.calls if c[0] == "command"][-1][2]
    assert pl == {"question": "named", "notes": "n"}


@sessioned
async def test_charter_history_and_artifact_access(client, lab):
    await client.call_tool("get_study_charter", {"project": "p", "charter_id": "charter:s", "history": True})
    assert lab.calls[-1][1] == "record_history"
    r = await client.call_tool("get_artifact_access", {"project": "p", "artifact_id": "a1",
                                                       "representation": "content"})
    assert body(r)["content_access"]["path"] == "/v1/projects/p/artifacts/a1/content"


@sessioned
async def test_resources(client, lab):
    templates = (await client.list_resource_templates()).resource_templates
    assert {t.uri_template for t in templates} >= {
        "lab://projects/{project}/investigations/{investigation}", "lab://projects/{project}/runs/{job}",
        "lab://projects/{project}/artifacts/{artifact}/manifest", "lab://projects/{project}/claims/{claim}",
        "lab://projects/{project}/evidence-plans/{plan}", "lab://projects/{project}/assessments/{assessment}"}
    r = await client.read_resource("lab://projects/p7/investigations/i42")
    assert json.loads(r.contents[0].text)["id"] == "i42"
    r = await client.read_resource("lab://projects/p7/evidence-plans/ep3")
    assert json.loads(r.contents[0].text)["kind"] == "evidence_plan"
    await client.read_resource("lab://projects/p7/artifacts/a91/manifest")
    assert lab.calls[-1][1] == "get_artifact"
    with pytest.raises(MCPError):
        await client.read_resource("lab://projects/p7/runs/missing")
    with pytest.raises(MCPError):
        await client.read_resource("lab://projects/p7/bogus/x")


async def test_no_credential_is_a_tool_error(lab):
    server = build_server(lab.client, Credentials(None))
    async with Client(server) as c:
        r = await c.call_tool("get_investigation", {"project": "p", "investigation_id": "i"})
        assert r.is_error and body(r)["error"]["code"] == "UNAUTHENTICATED"


async def test_bad_token_is_a_domain_error(lab):
    server = build_server(lab.client, Credentials("nope"))
    async with Client(server) as c:
        r = await c.call_tool("get_investigation", {"project": "p", "investigation_id": "i"})
        assert r.is_error and body(r)["error"]["code"] == "UNAUTHENTICATED"
