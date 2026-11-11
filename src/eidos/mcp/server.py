"""MCP adapter: an operator interface over the lab HTTP command API.

* No state lives here. Every tool is one lab command/query, authorized by the lab under the
  *caller's* token. Closing a client only detaches; investigations keep running in the lab.
* stdio: protocol on stdout, logs on stderr only; credential from ``EIDOS_TOKEN`` or the lab's
  admin token file.
* streamable HTTP: bound to 127.0.0.1, bearer token required and validated by the lab's
  ``/v1/whoami``; the adapter then acts with that same token (never a stronger one). Browser
  ``Origin`` headers must be loopback (or listed in ``EIDOS_MCP_ALLOWED_ORIGINS``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any
from urllib.parse import urlparse

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from eidos.mcp.gateway import (
    RESOURCE_TEMPLATES,
    BadUri,
    ClientFactory,
    ToolFailure,
    call_tool,
    default_factory,
    failure_from,
    read_resource,
)
from eidos.mcp.tools import TOOLS
from eidos.settings import LabConfig

log = logging.getLogger("eidos.mcp")
SERVER_NAME = "eidos-lab"
RESOURCE_NOT_FOUND = -32002

INSTRUCTIONS = (
    "Eidos lab operator interface. Installed components are data: use list_components / describe_component. "
    "Every mutating tool requires a caller-chosen command_key (<=200 chars): if a reply is lost, retry with the same key "
    "and the same arguments to get the original receipt; the same key with other arguments is COMMAND_KEY_CONFLICT; use a "
    "fresh key for a new operation. expected_revision, where offered, guards against concurrent change. Errors are tool "
    "results with isError=true, error.code and error.next_action. Long work returns a job/investigation id and keeps "
    "running if you disconnect. Compact workflow: upload_source, then run_batch (chain calls with $ref), wait_status, and "
    "submit_candidate; get_run / get_artifact_access expand any id they return."
)


def _tool_defs() -> list[types.Tool]:
    out = []
    for t in TOOLS:
        out.append(types.Tool(
            name=t.name, description=t.description(), input_schema=t.input_schema(),
            annotations=types.ToolAnnotations(
                read_only_hint=not t.mutating,
                destructive_hint=False if not t.mutating else t.name in {"withdraw_component", "retire_component",
                                                                         "cancel_run"},
                idempotent_hint=True,  # reads trivially; commands are idempotent by command_key
                open_world_hint=False)))
    return out


def _result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":"),
                                                                                   default=str))],
                                structured_content=payload, is_error=is_error)


class Credentials:
    """Where a request's lab token comes from. HTTP requests must carry their own bearer token and
    never fall back to the adapter's own credential."""

    def __init__(self, stdio_token: str | None):
        self.stdio_token = stdio_token

    def for_request(self, request: Any) -> str:
        if request is None:
            if not self.stdio_token:
                raise ToolFailure("UNAUTHENTICATED", "no lab credential configured for this adapter",
                                  "Set EIDOS_TOKEN or run on a host with the lab's operator token file.")
            return self.stdio_token
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            raise ToolFailure("UNAUTHENTICATED", "missing bearer token", "Send Authorization: Bearer <lab token>.")
        return auth[7:].strip()


def build_server(factory: ClientFactory, creds: Credentials) -> Server:
    async def with_client(ctx, fn):
        token = creds.for_request(ctx.request)
        client = factory(token)

        def run():
            try:
                return fn(client)
            finally:
                close = getattr(client, "close", None)
                if close:
                    close()
        return await anyio.to_thread.run_sync(run)

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=_tool_defs(), ttl_ms=0, cache_scope="private")

    async def on_call_tool(ctx, params):
        try:
            payload = await with_client(ctx, lambda c: call_tool(c, params.name, dict(params.arguments or {})))
            return _result(payload)
        except Exception as exc:  # noqa: BLE001
            f = failure_from(exc)
            if f.code in ("INTERNAL",):
                log.exception("tool %s crashed", params.name)
            return _result(f.to_dict(), is_error=True)

    async def on_list_resources(ctx, params):
        return types.ListResourcesResult(resources=[], ttl_ms=0, cache_scope="private")

    async def on_list_templates(ctx, params):
        return types.ListResourceTemplatesResult(resource_templates=[
            types.ResourceTemplate(name=n, uri_template=u, description=d, mime_type="application/json")
            for n, u, d in RESOURCE_TEMPLATES], ttl_ms=0, cache_scope="private")

    async def on_read_resource(ctx, params):
        uri = str(params.uri)
        try:
            data = await with_client(ctx, lambda c: read_resource(c, uri))
        except BadUri as exc:
            raise MCPError(types.INVALID_PARAMS, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            f = failure_from(exc)
            code = RESOURCE_NOT_FOUND if f.code == "NOT_FOUND" else types.INTERNAL_ERROR
            raise MCPError(code, f"{f.code}: {f.message}", f.to_dict()["error"]) from exc
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type="application/json",
                                                 text=json.dumps(data, indent=2, default=str))],
            ttl_ms=0, cache_scope="private")

    return Server(SERVER_NAME, version="0.1.0", instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool,
                  on_list_resources=on_list_resources, on_list_resource_templates=on_list_templates,
                  on_read_resource=on_read_resource)


# -- credentials, config --------------------------------------------------------------------------

def _config() -> LabConfig:
    return LabConfig.load()


def stdio_token(cfg: LabConfig | None = None) -> str | None:
    tok = os.environ.get("EIDOS_TOKEN")
    if tok:
        return tok.strip()
    try:
        cfg = cfg or _config()
        return cfg.secret_path("admin.token").read_text().strip()
    except (OSError, ValueError, KeyError):
        return None


# -- streamable HTTP --------------------------------------------------------------------------------

def allowed_origin(origin: str, extra: list[str]) -> bool:
    if origin in extra:
        return True
    host = urlparse(origin).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


class GuardMiddleware:
    """Pure-ASGI gate in front of the MCP endpoint: Origin policy, then bearer token validated by
    the lab (``/v1/whoami``). The validated token is what tool calls later use."""

    def __init__(self, app, factory: ClientFactory, extra_origins: list[str] | None = None):
        self.app, self.factory, self.extra = app, factory, extra_origins or []

    async def _deny(self, send, status: int, code: str, message: str, headers: list | None = None):
        body = json.dumps({"error": {"code": code, "message": message}}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *(headers or [])]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        origin = headers.get("origin")
        if origin is not None and not allowed_origin(origin, self.extra):
            return await self._deny(send, 403, "FORBIDDEN_ORIGIN", "Origin not allowed")
        auth = headers.get("authorization", "")
        challenge = [(b"www-authenticate", b'Bearer realm="eidos-lab"')]
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            return await self._deny(send, 401, "UNAUTHENTICATED", "bearer token required", challenge)
        token = auth[7:].strip()

        def check():
            c = self.factory(token)
            try:
                return c.whoami()
            finally:
                getattr(c, "close", lambda: None)()
        try:
            await anyio.to_thread.run_sync(check)
        except Exception as exc:  # noqa: BLE001
            f = failure_from(exc)
            if f.status in (401, 403) or f.code == "UNAUTHENTICATED":
                return await self._deny(send, 401, "UNAUTHENTICATED", "token rejected by the lab", challenge)
            return await self._deny(send, 503, f.code, "lab API unavailable")
        return await self.app(scope, receive, send)


def http_app(factory: ClientFactory, *, port: int = 8766, extra_origins: list[str] | None = None):
    from mcp.server.transport_security import TransportSecuritySettings
    server = build_server(factory, Credentials(None))
    inner = server.streamable_http_app(
        host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", *(extra_origins or [])]))
    return GuardMiddleware(inner, factory, extra_origins)


# -- entry point ---------------------------------------------------------------------------------------

def serve(transport: str = "stdio", port: int = 8766) -> None:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("EIDOS_MCP_LOG", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = _config()
    factory = default_factory(cfg.api_url)
    if transport == "stdio":
        anyio.run(_serve_stdio, factory, stdio_token(cfg))
    elif transport in ("http", "streamable-http"):
        import uvicorn
        extra = [o for o in os.environ.get("EIDOS_MCP_ALLOWED_ORIGINS", "").split(",") if o]
        uvicorn.run(http_app(factory, port=port, extra_origins=extra), host="127.0.0.1", port=port,
                    log_level="warning")
    else:
        raise SystemExit(f"unknown transport {transport!r}: use stdio or http")


async def _serve_stdio(factory: ClientFactory, token: str | None) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(factory, Credentials(token))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
