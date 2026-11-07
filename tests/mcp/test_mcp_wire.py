"""Real transports (stdio subprocess and streamable HTTP on loopback) against the fake lab client.
Fixture-only: no lab. Also records which protocol revisions the SDK negotiates."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from eidos.mcp.server import http_app

from fake_client import FakeLab

HERE = Path(__file__).parent
SRC = str(HERE.parents[1] / "src")


def stdio_params():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([SRC, str(HERE)]))
    return StdioServerParameters(command=sys.executable, args=[str(HERE / "fake_stdio_server.py")], env=env)


@pytest.mark.parametrize("mode,expected", [("auto", "2026-07-28"), ("legacy", "2025-11-25"), ("2026-07-28", "2026-07-28")])
async def test_stdio_negotiates_both_revisions(mode, expected):
    async with Client(stdio_params(), mode=mode) as c:
        assert c.protocol_version == expected
        names = {t.name for t in (await c.list_tools()).tools}
        assert "start_investigation" in names
        key = {"project": "p", "command_key": f"wire-{mode}", "question": "q"}
        a = await c.call_tool("start_investigation", key)
        b = await c.call_tool("start_investigation", key)
        assert a.structured_content["result"] == b.structured_content["result"] and not a.is_error


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server():
    lab = FakeLab()
    port = free_port()
    cfg = uvicorn.Config(http_app(lab.client, port=port), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield lab, f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    th.join(5)


def test_http_rejects_missing_or_bad_token_and_foreign_origin(http_server):
    _, url = http_server
    r = httpx.post(url, json={})
    assert r.status_code == 401 and "www-authenticate" in r.headers
    r = httpx.post(url, json={}, headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = httpx.post(url, json={}, headers={"authorization": "Bearer good-token", "origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_http_acts_with_callers_token(http_server, mode):
    lab, url = http_server
    lab.tokens["other-token"] = "bob"
    async with httpx2.AsyncClient(headers={"authorization": "Bearer other-token"}) as h:
        async with Client(streamable_http_client(url, http_client=h), mode=mode) as c:
            assert c.protocol_version == ("2026-07-28" if mode == "auto" else "2025-11-25")
            r = await c.call_tool("get_investigation", {"project": "p", "investigation_id": "i1"})
            assert not r.is_error
    assert lab.calls and all(call[5] == "other-token" for call in lab.calls)
