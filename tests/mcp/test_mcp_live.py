"""Live test against a running lab over the real stdio adapter. Skipped unless EIDOS_HOME points at a
lab. Uses its own project (h1-mcp-live) and a fresh study id per run; never touches other projects."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from eidos.client import LabClient
from eidos.settings import LabConfig

pytestmark = pytest.mark.live
SRC = str(Path(__file__).resolve().parents[2] / "src")
PROJECT = "h1-mcp-live"


def params(home: str) -> StdioServerParameters:
    env = dict(os.environ, EIDOS_HOME=home, PYTHONPATH=SRC)
    env.pop("EIDOS_TOKEN", None)  # exercise the admin-token-file path
    return StdioServerParameters(command=sys.executable, args=["-c", "from eidos.mcp.server import serve; serve('stdio')"],
                                 env=env)


async def test_start_detach_reconnect_read_back(live_lab):
    cfg = LabConfig.load(live_lab)
    admin = LabClient(cfg.api_url, cfg.secret_path("admin.token").read_text().strip(), client_name="mcp-live-test")
    try:
        admin.command("create_project", {"project_id": PROJECT, "name": "MCP live test"}, key="h1-mcp-live:create")
    finally:
        admin.close()
    study = "s-" + uuid.uuid4().hex[:8]
    inv_key = "live-inv-" + study
    charter = {"study_id": study, "question": "Does the adapter survive a detach?", "exploration_only": True,
               "known_exposures": ["none: live adapter test"]}

    async with Client(params(live_lab)) as c1:
        r = await c1.call_tool("commit_study_charter", {"project": PROJECT, "command_key": "live-ch-" + study,
                                                        "charter": charter})
        assert not r.is_error, r.structured_content
        charter_id = r.structured_content["result"]["charter_id"]
        args = {"project": PROJECT, "command_key": inv_key, "question": "Does the adapter survive a detach?",
                "charter_id": charter_id, "limits": {"turns": 2}}
        first = await c1.call_tool("start_investigation", args)
        assert not first.is_error, first.structured_content
        again = await c1.call_tool("start_investigation", args)  # lost-reply retry: same receipt
        assert again.structured_content["result"] == first.structured_content["result"]
        assert again.structured_content["replayed"] is True
        inv_id = first.structured_content["result"]["investigation_id"]
        bad = await c1.call_tool("start_investigation", {**args, "question": "different"})
        assert bad.is_error and bad.structured_content["error"]["code"] == "COMMAND_KEY_CONFLICT"
    # client closed: only detached. New process, new session.
    async with Client(params(live_lab)) as c2:
        got = await c2.call_tool("get_investigation", {"project": PROJECT, "investigation_id": inv_id})
        assert not got.is_error
        assert got.structured_content["id"] == inv_id
        assert got.structured_content["body"]["charter_ref"]["id"] == charter_id
        res = await c2.read_resource(f"lab://projects/{PROJECT}/investigations/{inv_id}")
        assert json.loads(res.contents[0].text)["id"] == inv_id
        ev = await c2.call_tool("list_events", {"project": PROJECT, "investigation_id": inv_id})
        assert not ev.is_error and "next_cursor" in ev.structured_content
        ch = await c2.call_tool("get_study_charter", {"project": PROJECT, "charter_id": charter_id})
        assert ch.structured_content["body"]["study_id"] == study
        missing = await c2.call_tool("get_run", {"project": PROJECT, "job_id": "job_nope"})
        assert missing.is_error and missing.structured_content["error"]["code"] == "NOT_FOUND"
        cat = await c2.call_tool("list_components", {"project": "demo"})
        assert not cat.is_error
