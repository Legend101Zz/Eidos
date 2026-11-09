"""eidos_worker.llm (ADR 0013): standard-library client for the lab's LLM broker over a Unix socket.
A stub broker stands in for the lab; the client's wire format, error mapping and injection are checked."""

import json
import os
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from eidos_worker.llm import LlmClient, LlmRefused
from eidos_worker.run import execute
from eidos_worker.testing import make_invocation


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _serve(reply):
    seen = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self):
            return "unix"

        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), body))
            status, data = reply(body)
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    d = tempfile.mkdtemp(prefix="ew-", dir="/tmp")
    path = os.path.join(d, "b.sock")
    srv = _Server(path, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, path, seen


def _grant(path):
    return {"grant_id": "g1", "token": "eidos_llmg_p.tok", "socket": path, "models": ["fake:echo"],
            "max_requests": 2, "max_tokens": 100, "max_usd": None, "expires_at": "2099-01-01T00:00:00+00:00"}


def test_complete_sends_bearer_grant_and_returns_text_and_usage():
    srv, path, seen = _serve(lambda b: (200, {"call_id": "c1", "model": "fake:echo", "text": "hi",
                                              "usage": {"input_tokens": 3, "output_tokens": 1}, "usd": 0.0}))
    try:
        out = LlmClient(_grant(path)).complete("hello", max_tokens=5, request_key="r")
        assert (out.text, out.call_id, out.usage["output_tokens"]) == ("hi", "c1", 1)
        route, auth, body = seen[0]
        assert route == "/v1/llm/complete" and auth == "Bearer eidos_llmg_p.tok"
        assert body == {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 5, "model": None,
                        "request_key": "r"}
        assert "tok" not in repr(LlmClient(_grant(path)))
    finally:
        srv.shutdown()


def test_refusal_maps_to_llmrefused_with_the_broker_code():
    srv, path, _ = _serve(lambda b: (403, {"error": {"code": "GRANT_EXHAUSTED", "message": "no requests left"}}))
    try:
        with pytest.raises(LlmRefused) as e:
            LlmClient(_grant(path)).complete([{"role": "user", "content": "x"}], max_tokens=5)
        assert e.value.code == "GRANT_EXHAUSTED" and e.value.status == 403
    finally:
        srv.shutdown()


def test_unreachable_broker_is_a_clear_refusal():
    with pytest.raises(LlmRefused) as e:
        LlmClient(_grant("/tmp/no-such-eidos-broker.sock")).complete("x", max_tokens=1)
    assert e.value.code == "BROKER_UNREACHABLE"


def test_components_get_the_client_injected_only_with_a_grant(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text("def run(inputs, config, *, llm=None, context=None):\n"
                              "    return {'has_llm': llm is not None, 'same': llm is context.llm}\n")
    out = execute(make_invocation(tmp_path / "a", "m:run", source_dir=src))
    assert out.receipt["has_llm"] is False
    out = execute(make_invocation(tmp_path / "b", "m:run", source_dir=src, llm_grant=_grant("/tmp/x.sock")))
    assert out.receipt["has_llm"] is True and out.receipt["same"] is True
