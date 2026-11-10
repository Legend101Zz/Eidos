"""The LLM credential broker (ADR 0013): a loopback endpoint served by the investigator worker over a Unix socket.

For each request: authenticate the grant (by token hash) and reserve against the grant and the investigation's ledger
(``broker_reserve_call``), then call the provider with the lab's key, then settle actual usage
(``broker_settle_call``). A refusal happens before any provider call. A timeout or transport failure after sending
settles as uncertain (possible spend = the reserved maximum). If the broker dies between reserve and settle, the open
reservation is settled as uncertain when the attempt finishes.

Components never see a provider key: keys live only in this process's provider objects, and every error message
leaving the broker is redacted against them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socketserver
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Protocol

from eidos.client import LabError

log = logging.getLogger("eidos.llm_broker")
TOKEN_PREFIX = "eidos_llmg_"
MAX_BODY = 1 << 20


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    request_id: str | None = None
    reasoning_tokens: int | None = None  # as reported (OpenAI-compatible completion_tokens_details); None if absent


def openai_usage(u: dict) -> tuple[int, int, int | None]:
    """(input, charged output, reasoning) from an OpenAI-compatible ``usage`` object. Reasoning tokens are part of
    ``completion_tokens``; a provider reporting more reasoning than completion tokens counts them separately, and
    they are then added to the charged output (ADR 0016). Never dropped: the reported value is returned."""
    inp, out = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    r = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    reasoning = int(r) if isinstance(r, int) and not isinstance(r, bool) else None
    if reasoning is not None and reasoning > out:
        out += reasoning
    return inp, out, reasoning


class ProviderRejected(Exception):
    """The provider refused the request before doing work (4xx): nothing is charged."""


class ProviderUncertain(Exception):
    """Timeout or transport failure after the request may have been accepted: possible spend."""


class Provider(Protocol):
    def complete(self, model: str, messages: list[dict], max_tokens: int) -> ProviderResult: ...


class AnthropicProvider:
    """Anthropic Messages API with the lab's key (held only by this process)."""

    def __init__(self, api_key: str, *, timeout: float = 120.0, base_url: str = "https://api.anthropic.com"):
        self._key, self.timeout, self.base_url = api_key, timeout, base_url.rstrip("/")

    def complete(self, model: str, messages: list[dict], max_tokens: int) -> ProviderResult:
        import httpx
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                                "messages": [m for m in messages if m["role"] != "system"]}
        if system:
            body["system"] = system
        try:
            r = httpx.post(f"{self.base_url}/v1/messages", json=body, timeout=self.timeout,
                           headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"})
        except httpx.TimeoutException as exc:
            raise ProviderUncertain(f"timeout: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise ProviderUncertain(f"transport: {type(exc).__name__}") from None
        rid = r.headers.get("request-id")
        if r.status_code >= 500:
            raise ProviderUncertain(f"provider status {r.status_code} (request {rid})")
        if r.status_code != 200:
            raise ProviderRejected(f"provider status {r.status_code} (request {rid})")
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage") or {}
        return ProviderResult(text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), rid)

    def __repr__(self) -> str:
        return "AnthropicProvider()"


class OpenAICompatibleProvider:
    """OpenAI-compatible chat completions (hosted or a local server such as Ollama)."""

    def __init__(self, base_url: str, api_key: str | None = None, *, timeout: float = 300.0):
        self.base_url, self._key, self.timeout = base_url.rstrip("/"), api_key, timeout

    def complete(self, model: str, messages: list[dict], max_tokens: int) -> ProviderResult:
        import httpx
        headers = {"authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", headers=headers, timeout=self.timeout,
                           json={"model": model, "messages": messages, "max_tokens": max_tokens})
        except httpx.TimeoutException as exc:
            raise ProviderUncertain(f"timeout: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            raise ProviderUncertain(f"transport: {type(exc).__name__}") from None
        if r.status_code >= 500:
            raise ProviderUncertain(f"provider status {r.status_code}")
        if r.status_code != 200:
            raise ProviderRejected(f"provider status {r.status_code}")
        data = r.json()
        inp, out, reasoning = openai_usage(data.get("usage") or {})
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return ProviderResult(text, inp, out, data.get("id"), reasoning)

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider({self.base_url!r})"


def providers_from_env(environ: dict[str, str], endpoints: dict[str, str] | None = None) -> dict[str, Provider]:
    """Provider adapters for the keys this process holds. Model ids are ``<provider>:<model>``."""
    out: dict[str, Provider] = {}
    if environ.get("ANTHROPIC_API_KEY"):
        out["anthropic"] = AnthropicProvider(environ["ANTHROPIC_API_KEY"])
    eps = endpoints or {}
    if eps.get("openai-compatible"):
        out["openai-compatible"] = OpenAICompatibleProvider(eps["openai-compatible"], environ.get("OPENAI_API_KEY"))
    if environ.get("OPENAI_API_KEY"):
        out["openai"] = OpenAICompatibleProvider(eps.get("openai", "https://api.openai.com/v1"), environ["OPENAI_API_KEY"])
    return out


def _project_of(token: str) -> str | None:
    """Tokens are ``eidos_llmg_<project>.<random>``: the project routes the command; the hash authenticates."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    project, sep, rest = token[len(TOKEN_PREFIX):].rpartition(".")
    return project if sep and project and rest else None


class LlmBroker:
    """Request handling, independent of the transport (tests call ``handle`` directly or over the socket)."""

    def __init__(self, client: Any, providers: dict[str, Provider], *, secrets: tuple[str, ...] = ()):
        self.client = client
        self.providers = providers
        self._secrets = tuple(s for s in secrets if s)

    def _redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "[redacted]")
        return text

    def _err(self, status: int, code: str, message: str, **extra: Any) -> tuple[int, dict]:
        return status, {"error": {"code": code, "message": self._redact(str(message))[:500], **extra}}

    def handle(self, token: str, body: dict) -> tuple[int, dict]:
        project = _project_of(token or "")
        if not project:
            return self._err(401, "GRANT_INVALID", "missing or malformed grant token")
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or not all(
                isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
                and isinstance(m.get("content"), str) for m in msgs):
            return self._err(400, "INVALID", "messages must be a non-empty list of {role, content} objects")
        try:
            max_tokens = int(body.get("max_tokens") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        if max_tokens <= 0:
            return self._err(400, "INVALID", "max_tokens must be a positive integer")
        th = hashlib.sha256(token.encode()).hexdigest()
        rk = body.get("request_key")
        payload = {"token_sha256": th, "model": body.get("model"), "max_tokens": max_tokens,
                   "input_bytes": sum(len(m["content"].encode()) for m in msgs), "request_key": rk}
        try:
            res = self.client.command("broker_reserve_call", payload, project=project,
                                      # a fresh key per request: a retried request_key is recognised by the
                                      # handler, which returns the existing call instead of reserving again
                                      key=f"llm-reserve:{th[:16]}:{uuid.uuid4().hex}")["result"]
        except LabError as exc:
            status = exc.status if 400 <= exc.status < 500 else 502
            return self._err(status, exc.code or "REFUSED", (exc.error or {}).get("message", ""))
        if res.get("replayed"):
            # The broker keeps no response bodies: a repeated request_key is refused (not re-sent, not re-charged),
            # with the earlier call's identity and usage, so a client can never mistake it for new text.
            return self._err(409, "REQUEST_REPLAYED", f"request_key {rk!r} was already used by call {res['call_id']} "
                             f"({res.get('status')}); its response is not stored", call_id=res["call_id"],
                             previous=res.get("result"))
        cid, model = res["call_id"], res["model"]
        provider = self.providers.get(model.split(":", 1)[0])
        settle = {"call_id": cid}
        if provider is None:
            self._settle(project, {**settle, "status": "error", "error": {"category": "no-provider"}})
            return self._err(502, "PROVIDER_UNAVAILABLE", f"no provider configured for {model}", call_id=cid)
        try:
            out = provider.complete(model.split(":", 1)[1], msgs, max_tokens)
        except ProviderRejected as exc:
            self._settle(project, {**settle, "status": "error", "error": {"category": "provider-rejected",
                                                                           "message": self._redact(str(exc))[:300]}})
            return self._err(502, "PROVIDER_ERROR", str(exc), call_id=cid)
        except Exception as exc:  # noqa: BLE001 - timeout/transport/anything after sending: possible spend
            self._settle(project, {**settle, "status": "uncertain", "error": {"category": "uncertain",
                                                                               "message": self._redact(str(exc))[:300]}})
            return self._err(504, "OUTCOME_UNKNOWN", f"the provider did not confirm the request ({exc}); its "
                             "possible cost was charged as uncertain", call_id=cid)
        digest = "sha256:" + hashlib.sha256(out.text.encode()).hexdigest()
        usage = {"input_tokens": out.input_tokens, "output_tokens": out.output_tokens,
                 **({"reasoning_tokens": out.reasoning_tokens} if out.reasoning_tokens is not None else {})}
        st = self._settle(project, {**settle, "status": "ok", "usage": usage, "provider_request_id": out.request_id,
                                    "response_digest": digest})
        return 200, {"call_id": cid, "model": model, "text": out.text, "usage": usage,
                     "usd": (st or {}).get("charged_usd")}

    def _settle(self, project: str, payload: dict) -> dict | None:
        try:
            return self.client.command("broker_settle_call", payload, project=project,
                                       key=f"llm-settle:{payload['call_id']}")["result"]
        except Exception as exc:  # noqa: BLE001 - the reservation stays open and is reconciled as uncertain
            log.warning("llm call settlement failed; left for reconciliation",
                        extra={"call_id": payload["call_id"], "error": self._redact(str(exc))[:200]})
            return None


MAX_CONNECTIONS = 32
CONNECTION_TIMEOUT = 30.0


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    broker: LlmBroker
    _slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Bounded concurrency: a local client cannot exhaust threads or descriptors of the worker holding the keys.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _Handler(BaseHTTPRequestHandler):
    server: _UnixHTTPServer
    protocol_version = "HTTP/1.1"
    timeout = CONNECTION_TIMEOUT  # an idle or slow client is dropped

    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - no request lines (tokens) in logs
        return

    def _send(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/llm/complete":
            return self._send(404, {"error": {"code": "NOT_FOUND", "message": "unknown path"}})
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            return self._send(413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "request body too large"}})
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return self._send(400, {"error": {"code": "INVALID", "message": "body must be JSON"}})
        status, data = self.server.broker.handle(token, body if isinstance(body, dict) else {})
        self._send(status, data)


def serve_unix(broker: LlmBroker, path: str | os.PathLike) -> tuple[_UnixHTTPServer, Callable[[], None]]:
    """Serve the broker on a Unix socket in a daemon thread. The socket is connectable by every local identity
    (mode 0666, e.g. the candidate user): the grant token, not the file mode, authenticates a request."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o711)
    if p.exists() or p.is_symlink():
        p.unlink()
    srv = _UnixHTTPServer(str(p), _Handler)
    srv.broker = broker
    os.chmod(p, 0o666)
    t = threading.Thread(target=srv.serve_forever, name="llm-broker", daemon=True)
    t.start()

    def stop() -> None:
        srv.shutdown()
        srv.server_close()
        p.unlink(missing_ok=True)
    return srv, stop
