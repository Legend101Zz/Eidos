"""Scoped LLM calls through the lab's credential broker (ADR 0013). Standard library only, Python >= 3.9.

A component that declares ``llm`` use in its manifest gets a grant in its invocation when the project enables the
broker. The grant holds a short-lived token, never a provider key. Use the injected ``llm`` keyword (or
``context.llm``)::

    def run(inputs, config, *, writer=None, llm=None):
        out = llm.complete([{"role": "user", "content": "Write one sentence."}], max_tokens=64)
        writer.stage_json("text", {"text": out.text, "call": out.call_id})

``complete`` raises :class:`LlmRefused` when the broker refuses (grant exhausted or expired, model not allowed,
investigation budget exhausted, provider error); a refused request was never sent to the provider.
"""

from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

Messages = Union[str, Sequence[Dict[str, str]]]


class LlmRefused(Exception):
    """The broker refused or failed the request. ``code`` is the broker's stable error code."""

    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    call_id: str
    usage: Dict[str, Any] = field(default_factory=dict)
    usd: Optional[float] = None


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("broker.local", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class LlmClient:
    """Client for one attempt's grant. Holds the grant token (scoped to this attempt), never a provider key."""

    def __init__(self, grant: Dict[str, Any], timeout: float = 300.0):
        self._grant = dict(grant)
        self.timeout = timeout

    @classmethod
    def from_grant(cls, grant: Optional[Dict[str, Any]]) -> Optional[LlmClient]:
        return cls(grant) if grant else None

    @property
    def models(self) -> List[str]:
        return list(self._grant.get("models") or [])

    @property
    def limits(self) -> Dict[str, Any]:
        return {k: self._grant.get(k) for k in ("max_requests", "max_tokens", "max_usd", "expires_at")}

    def complete(self, messages: Messages, max_tokens: int, *, model: Optional[str] = None,
                 request_key: Optional[str] = None) -> LlmResult:
        """One bounded completion. ``messages`` is a prompt string or ``[{"role", "content"}, ...]``.
        ``request_key`` makes a retried request return the same call instead of a second one."""
        if isinstance(messages, str):
            msgs = [{"role": "user", "content": messages}]
        else:
            msgs = [{"role": str(m["role"]), "content": str(m["content"])} for m in messages]
        body = json.dumps({"messages": msgs, "max_tokens": int(max_tokens), "model": model,
                           "request_key": request_key}).encode()
        conn = _UnixConnection(self._grant["socket"], self.timeout)
        try:
            conn.request("POST", "/v1/llm/complete", body=body,
                         headers={"Authorization": "Bearer " + self._grant["token"],
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise LlmRefused("BROKER_UNREACHABLE", f"the LLM broker is not reachable ({type(exc).__name__})") from None
        finally:
            conn.close()
        try:
            data = json.loads(raw.decode() or "{}")
        except ValueError:
            raise LlmRefused("BROKER_PROTOCOL", "the broker returned a malformed response", resp.status) from None
        if resp.status != 200:
            err = data.get("error") or {}
            raise LlmRefused(str(err.get("code", "BROKER_ERROR")), str(err.get("message", "")), resp.status)
        return LlmResult(text=str(data.get("text", "")), model=str(data.get("model")), call_id=str(data.get("call_id")),
                         usage=dict(data.get("usage") or {}), usd=data.get("usd"))

    def __repr__(self) -> str:  # never show the token
        return f"LlmClient(grant={self._grant.get('grant_id')!r}, models={self.models!r})"
