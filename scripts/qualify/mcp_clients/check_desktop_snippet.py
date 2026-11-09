#!/usr/bin/env python3
"""Check a Claude Desktop `mcpServers` snippet without Claude Desktop (and without touching its config).

  <lab venv>/bin/python check_desktop_snippet.py SNIPPET.json [--project P --investigation I] [--out RECEIPT.json]

Static checks: valid JSON, one `eidos` server, absolute existing executable, `args == ["mcp"]`, the only environment
variable is EIDOS_HOME (no token or key values in the file). Live check: launch exactly that command/args/env the way a
desktop app does (no shell, minimal PATH, no inherited variables), then MCP `initialize`, `tools/list`,
`resources/templates/list` and, with --project/--investigation, one read-only `get_investigation` call.
What this does not show: Claude Desktop's own config loading, UI, or log format (check_desktop_log.sh, owner item).
Needs the `mcp` package (the lab venv has it)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _g(o, *names):  # mcp SDK 1.x camelCase / 2.x snake_case
    for n in names:
        if hasattr(o, n):
            return getattr(o, n)
    return None


async def live(server: dict, project: str | None, inv: str | None) -> dict:
    from mcp.client.stdio import stdio_client

    from mcp import ClientSession, StdioServerParameters
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/"), **server.get("env", {})}
    params = StdioServerParameters(command=server["command"], args=server.get("args", []), env=env)
    out: dict = {}
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        info = _g(init, "server_info", "serverInfo")
        out["server_info"] = {"name": info.name, "version": info.version}
        out["protocol_version"] = _g(init, "protocol_version", "protocolVersion")
        tools = (await s.list_tools()).tools
        out["tools"] = len(tools)
        out["has_start_investigation"] = any(t.name == "start_investigation" for t in tools)
        rt = await s.list_resource_templates()
        out["resource_templates"] = [_g(t, "uri_template", "uriTemplate") for t in _g(rt, "resource_templates", "resourceTemplates")]
        if project and inv:
            res = await s.call_tool("get_investigation", {"project": project, "investigation_id": inv})
            text = "".join(getattr(c, "text", "") for c in res.content)
            out["get_investigation"] = {"is_error": bool(_g(res, "is_error", "isError")), "names_investigation": inv in text}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snippet")
    ap.add_argument("--project")
    ap.add_argument("--investigation")
    ap.add_argument("--out")
    a = ap.parse_args()
    raw = Path(a.snippet).read_text()
    cfg = json.loads(raw)
    servers = cfg.get("mcpServers", {})
    srv = servers.get("eidos", {})
    cmd = srv.get("command", "")
    checks = {
        "single_eidos_server": list(servers) == ["eidos"],
        "command_absolute_executable": os.path.isabs(cmd) and os.access(cmd, os.X_OK),
        "args_mcp": srv.get("args") == ["mcp"],
        "env_only_eidos_home": set(srv.get("env", {})) == {"EIDOS_HOME"} and os.path.isabs(srv["env"]["EIDOS_HOME"]),
        "no_secret_values": not any(m in raw for m in ("sk-ant-", "TOKEN", "API_KEY", "PASSWORD")),
    }
    result: dict = {"snippet": a.snippet, "checks": checks}
    if all(checks.values()):
        result["live"] = asyncio.run(live(srv, a.project, a.investigation))
        lv = result["live"]
        checks["live_initialize"] = bool(lv.get("server_info", {}).get("name"))
        checks["live_tools_list_has_start_investigation"] = lv.get("has_start_investigation", False)
        if a.project and a.investigation:
            gi = lv.get("get_investigation", {})
            checks["live_get_investigation"] = not gi.get("is_error", True) and gi.get("names_investigation", False)
    result["ok"] = all(checks.values())
    text = json.dumps(result, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
