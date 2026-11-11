"""Session reuse in the study's loopback MCP transport (``study/eidos_arms/mcp_http.py``, ADR 0025) against the real
``http_app`` served by uvicorn on a free loopback port. Fixture-only: the lab behind it is ``workflow_lab``; no network
beyond loopback, no LLM."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from workflow_lab import WorkflowLab

from eidos.mcp.server import http_app

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("mcp_http_under_test", ROOT / "study" / "eidos_arms" / "mcp_http.py")
mcp_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_http)
Session = mcp_http.Session

TOKEN = "good-token"
GET = {"project": "p", "investigation_id": "i1"}


class RecordingApp:
    """ASGI wrapper that records what the server saw: (session id the request carried, response status). ``initialize``
    is the only request a client sends without a session id, so those are the server-side initializations."""

    def __init__(self, app):
        self.app, self.log = app, []

    @property
    def initializes(self) -> int:
        return sum(1 for sid, _ in self.log if sid is None)

    def sessions(self) -> set[str]:
        return {sid for sid, _ in self.log if sid}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        sid = {k.decode().lower(): v.decode() for k, v in scope["headers"]}.get("mcp-session-id")

        async def recording_send(message):
            if message["type"] == "http.response.start":
                self.log.append((sid, message["status"]))
            await send(message)
        return await self.app(scope, receive, recording_send)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def _server():
    """One uvicorn server for the module (starting one per test costs more than the tests); ``srv`` gives each test a
    fresh lab and an empty request log behind it."""
    box = SimpleNamespace(lab=WorkflowLab())
    port = free_port()
    app = RecordingApp(http_app(lambda token: box.lab.client(token), port=port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    yield SimpleNamespace(box=box, app=app, url=f"http://127.0.0.1:{port}/mcp")
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def srv(_server, monkeypatch):
    for name in ("no_proxy", "NO_PROXY"):  # loopback must never go through a proxy
        monkeypatch.setenv(name, "127.0.0.1,localhost,::1")
    lab = _server.box.lab = WorkflowLab()
    lab.tokens["other-token"] = "bob"
    _server.app.log.clear()
    return SimpleNamespace(lab=lab, app=_server.app, url=_server.url)


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path / ".mcp_session.json"


# -- one session, many calls -------------------------------------------------------------------------------------------------------

def test_one_session_initializes_once_for_many_calls(srv, state):
    s = Session(srv.url, TOKEN, state)
    assert s.session_id is None and s.initializations == 0
    for i in range(3):
        r = s.call("get_investigation", {**GET, "investigation_id": f"i{i}"})
        assert r["structuredContent"]["id"] == f"i{i}" and r["isError"] is False
    assert s.initializations == 1 and srv.app.initializes == 1  # the client's count and the server's agree
    assert s.session_id and len(srv.app.sessions()) == 1 and srv.app.sessions() == {s.session_id}
    assert all(status < 300 for _, status in srv.app.log)
    assert all(c.token == TOKEN for c in srv.lab.queries("get_investigation"))


def test_without_a_state_file_a_session_still_reuses_itself_in_process(srv):
    s = Session(srv.url, TOKEN)
    s.call("get_investigation", GET)
    s.call("get_investigation", GET)
    assert s.initializations == 1 and s.state_path is None


def test_a_second_session_with_the_same_state_file_reuses_the_first_ones_id(srv, state):
    a = Session(srv.url, TOKEN, state)
    a.call("get_investigation", GET)
    b = Session(srv.url, TOKEN, state)
    assert b.session_id == a.session_id  # loaded, before any call
    assert b.call("get_investigation", GET)["structuredContent"]["id"] == "i1"
    assert b.initializations == 0 and b.session_id == a.session_id and srv.app.initializes == 1
    # and a third process after that
    c = Session(srv.url, TOKEN, state)
    c.call("get_investigation", GET)
    assert c.initializations == 0 and srv.app.initializes == 1 and len(srv.app.sessions()) == 1


def test_a_different_token_or_server_does_not_reuse_the_session(srv, state):
    a = Session(srv.url, TOKEN, state)
    a.call("get_investigation", GET)
    other = Session(srv.url, "other-token", state)
    assert other.session_id is None  # bound to url + token by digest
    assert Session(srv.url.replace("127.0.0.1", "localhost"), TOKEN, state).session_id is None
    other.call("get_investigation", GET)
    assert other.initializations == 1 and other.session_id != a.session_id and srv.app.initializes == 2
    assert srv.lab.queries("get_investigation")[-1].token == "other-token"  # it acted with its own credential
    # the file now belongs to the other credential: the first one starts a new session rather than reuse it
    assert Session(srv.url, TOKEN, state).session_id is None
    assert Session(srv.url, "other-token", state).session_id == other.session_id


def test_a_wrong_token_is_refused_and_leaves_no_state(srv, state):
    s = Session(srv.url, "wrong-token", state)
    with pytest.raises(urllib.error.HTTPError) as info:
        s.call("get_investigation", GET)
    assert info.value.code == 401 and s.initializations == 0 and not state.exists()


def test_the_one_shot_helper_uses_the_state_file_too(srv, state):
    assert mcp_http.call(srv.url, TOKEN, "get_investigation", GET, state)["structuredContent"]["id"] == "i1"
    assert mcp_http.call(srv.url, TOKEN, "get_investigation", GET, state)["isError"] is False
    assert srv.app.initializes == 1


def test_a_jsonrpc_error_is_raised_with_its_payload(srv, state):
    s = Session(srv.url, TOKEN, state)
    with pytest.raises(RuntimeError, match="Method not found"):
        s.request("nosuch/method", {})


# -- a session the server no longer knows ---------------------------------------------------------------------------------------------

def test_a_bogus_session_id_is_reinitialized_exactly_once_and_the_call_succeeds(srv, state):
    s = Session(srv.url, TOKEN, state)
    s.call("get_investigation", GET)
    first_id = s.session_id
    s.session_id = "bogus"
    r = s.call("get_investigation", {**GET, "investigation_id": "after"})
    assert r["structuredContent"]["id"] == "after"
    assert s.initializations == 2 and srv.app.initializes == 2 and s.session_id not in ("bogus", first_id)
    assert [status for sid, status in srv.app.log if sid == "bogus"] == [404]  # sent once, not retried with it
    assert json.loads(state.read_text())["session_id"] == s.session_id
    s.call("get_investigation", GET)  # and the new session is reused
    assert s.initializations == 2 and srv.app.initializes == 2


def test_an_expired_id_in_the_state_file_is_replaced_and_the_file_updated(srv, state):
    probe = Session(srv.url, TOKEN, state)
    state.write_text(json.dumps({"owner": probe._owner, "session_id": "expired-session-id"}))
    s = Session(srv.url, TOKEN, state)
    assert s.session_id == "expired-session-id"
    assert s.call("get_investigation", GET)["isError"] is False
    assert s.initializations == 1 and srv.app.initializes == 1
    assert json.loads(state.read_text())["session_id"] == s.session_id != "expired-session-id"
    assert Session(srv.url, TOKEN, state).session_id == s.session_id


@pytest.mark.parametrize("content", ["", "{not json", '{"owner": "someone-else", "session_id": "x"}', "{}"],
                         ids=["empty", "truncated", "other-owner", "no-session"])
def test_an_unusable_state_file_means_a_new_session(srv, state, content):
    state.write_text(content)
    s = Session(srv.url, TOKEN, state)
    assert s.session_id is None
    s.call("get_investigation", GET)
    assert s.initializations == 1 and json.loads(state.read_text())["session_id"] == s.session_id


@pytest.mark.parametrize("content", ["[]", "null", '"text"', "3"])
def test_a_state_file_with_valid_json_that_is_not_an_object_means_a_new_session(srv, state, content):
    state.write_text(content)
    assert Session(srv.url, TOKEN, state).session_id is None


class _Scripted(Session):
    """A Session whose wire is scripted: records what it was asked to send; ``fail`` is the status tools/call gets."""

    def __init__(self, *a, fail: int, **kw):
        super().__init__(*a, **kw)
        self.sent, self.fail = [], fail

    def _send(self, body):
        self.sent.append(body.get("method"))
        if body.get("method") == "initialize":
            self.session_id = f"sid-{self.sent.count('initialize')}"
        if body.get("method") == "tools/call":
            raise urllib.error.HTTPError(self.url, self.fail, "scripted", {}, io.BytesIO(b""))
        return None


def test_a_session_that_stays_unknown_is_reinitialized_once_then_the_error_is_raised(state):
    s = _Scripted("http://127.0.0.1:9/mcp", TOKEN, state, fail=404)
    s.session_id = "stale"
    with pytest.raises(urllib.error.HTTPError) as info:
        s.call("t", {})
    assert info.value.code == 404 and s.initializations == 1
    assert s.sent == ["tools/call", "initialize", "notifications/initialized", "tools/call"]  # no third attempt


def test_other_http_errors_are_not_retried_and_do_not_reinitialize(state):
    for code in (400, 401, 403, 500, 503):
        s = _Scripted("http://127.0.0.1:9/mcp", TOKEN, state, fail=code)
        s.session_id = "live"
        with pytest.raises(urllib.error.HTTPError) as info:
            s.call("t", {})
        assert info.value.code == code and s.initializations == 0 and s.sent == ["tools/call"]


def test_a_404_on_the_second_attempt_after_a_fresh_initialize_is_not_hidden(state):
    s = _Scripted("http://127.0.0.1:9/mcp", TOKEN, state, fail=404)
    with pytest.raises(urllib.error.HTTPError):
        s.call("t", {})  # no session at all: initialize, then the 404 of attempt 0 retries once
    assert s.initializations == 2 and s.sent.count("tools/call") == 2


# -- the state file ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mask", [0o022, 0o077, 0o000])
def test_the_state_file_is_private_and_never_holds_the_token(srv, state, mask):
    old = os.umask(mask)
    try:
        s = Session(srv.url, TOKEN, state)
        s.call("get_investigation", GET)
        s.session_id = "bogus"  # a rewrite replaces the file; the mode must not depend on the previous one
        s.call("get_investigation", GET)
    finally:
        os.umask(old)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    text = state.read_text()
    assert TOKEN not in text and "Bearer" not in text and "token" not in text.lower()
    saved = json.loads(text)
    assert set(saved) == {"owner", "session_id"} and saved["session_id"] == s.session_id
    assert saved["owner"] == s._owner and TOKEN not in saved["owner"]
    assert [p.name for p in state.parent.iterdir()] == [state.name]  # no temporary file left behind


@pytest.mark.parametrize("url", [
    "http://example.com/mcp", "http://10.0.0.5:8766/mcp", "http://0.0.0.0:8766/mcp", "https://127.0.0.1:8766/mcp",
    "https://localhost/mcp", "http://127.0.0.1.evil.example/mcp", "http://127.0.0.1@evil.example/mcp",
    "http://localhost.evil.example:8766/mcp", "ftp://localhost/mcp", "file:///etc/passwd", "127.0.0.1:8766/mcp", "",
])
def test_a_non_loopback_url_is_refused(url, state):
    with pytest.raises(ValueError, match="loopback"):
        Session(url, TOKEN, state)
    assert not state.exists()


@pytest.mark.parametrize("url", ["http://127.0.0.1:8766/mcp", "http://localhost:8766/mcp", "http://[::1]:8766/mcp",
                                 "http://LOCALHOST:1/mcp"])
def test_loopback_urls_are_accepted(url):
    assert Session(url, TOKEN).url == url


# -- the command-line transport -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli(srv, tmp_path, monkeypatch, capsys):
    """Run ``mcp_http.main()`` as ``python mcp_http.py ...`` would, in a directory with a ``lab_context.json``."""
    (tmp_path / "lab_context.json").write_text(json.dumps({"mcp_url": srv.url, "project": "p"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EIDOS_TOKEN", TOKEN)
    inits = []
    real = Session.initialize

    def counting(self):
        inits.append(1)
        real(self)
    monkeypatch.setattr(Session, "initialize", counting)

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["mcp_http.py", *argv])
        return mcp_http.main()
    run.inits, run.dir, run.srv, run.out = inits, tmp_path, srv, capsys
    return run


def test_the_cli_writes_the_payload_once_without_the_duplicated_content(cli):
    code = cli("get_investigation", json.dumps(GET), "--out", "response.json")
    text = (cli.dir / "response.json").read_text()
    out = json.loads(text)
    assert code == 0 and "content" not in out and out["structuredContent"]["id"] == "i1" and out["isError"] is False
    assert text == json.dumps(out, separators=(",", ":")) + "\n"  # compact, one line
    assert cli.out.readouterr().out == "" and TOKEN not in text


def test_the_cli_prints_the_same_without_out(cli):
    assert cli("get_investigation", json.dumps(GET)) == 0
    printed = cli.out.readouterr().out
    out = json.loads(printed)
    assert printed.endswith("\n") and printed.count("\n") == 1
    assert "content" not in out and out["structuredContent"]["id"] == "i1" and TOKEN not in printed


def test_the_cli_exits_nonzero_for_a_tool_error_and_still_drops_content(cli):
    code = cli("get_run", json.dumps({"project": "p", "job_id": "job_404"}), "--out", "response.json")
    out = json.loads((cli.dir / "response.json").read_text())
    assert code == 1 and out["isError"] is True and "content" not in out
    assert out["structuredContent"]["error"]["code"] == "NOT_FOUND"


def test_the_cli_reads_arguments_from_a_file_and_drives_the_workflow_tools(cli):
    (cli.dir / "args.json").write_text(json.dumps({"project": "p", "command_key": "up-1", "files": {"a.py": "x = 1\n"}}))
    assert cli("upload_source", "@args.json", "--out", "up.json") == 0
    up = json.loads((cli.dir / "up.json").read_text())["structuredContent"]
    assert up["artifact_id"] == cli.srv.lab.uploads[0]["artifact_id"] and up["files"] == 1
    assert cli.srv.lab.uploads[0]["key"] == "up-1" and cli.srv.lab.uploads[0]["role"] == "source"


def test_the_cli_keeps_content_when_there_is_no_structured_content(cli, monkeypatch):
    monkeypatch.setattr(Session, "call", lambda self, tool, arguments: {"content": [{"type": "text", "text": "hi"}]})
    assert cli("anything", "{}", "--out", "response.json") == 0
    assert json.loads((cli.dir / "response.json").read_text()) == {"content": [{"type": "text", "text": "hi"}]}


def test_two_cli_invocations_share_one_session_and_new_session_starts_another(cli):
    assert cli("get_investigation", json.dumps(GET), "--out", "a.json") == 0
    state = cli.dir / ".mcp_session.json"
    first = json.loads(state.read_text())["session_id"]
    assert cli("get_investigation", json.dumps({**GET, "investigation_id": "i2"}), "--out", "b.json") == 0
    assert json.loads((cli.dir / "b.json").read_text())["structuredContent"]["id"] == "i2"
    assert len(cli.inits) == 1 and cli.srv.app.initializes == 1  # the second process did not initialize
    assert json.loads(state.read_text())["session_id"] == first and stat.S_IMODE(state.stat().st_mode) == 0o600
    assert cli("get_investigation", json.dumps(GET), "--new-session", "--out", "c.json") == 0
    assert len(cli.inits) == 2 and cli.srv.app.initializes == 2
    assert json.loads(state.read_text())["session_id"] != first
    assert cli("get_investigation", json.dumps(GET), "--out", "d.json") == 0  # and that one is reused in turn
    assert len(cli.inits) == 2 and TOKEN not in state.read_text()
