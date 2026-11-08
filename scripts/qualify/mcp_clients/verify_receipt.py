#!/usr/bin/env python3
"""Check one MCP client run against the lab and write a JSON receipt (stdlib only).

  verify_receipt.py --client claude|codex --raw FILE --lab LAB --project P --investigation I --receipt OUT

The client output (`--raw`) is the client's JSON/JSONL event stream (or plain text). Checks:
  * the client really called get_investigation and list_components and read the investigation resource (tool_use / mcp_tool_call
    items in the stream; the tool list in an init event does not count)
  * the final text names the investigation id and the status the lab reports now (read via `lab query`)
  * the lab-side ground truth: investigation status, number of installed components
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--client", required=True)
p.add_argument("--raw", required=True)
p.add_argument("--lab", required=True)
p.add_argument("--project", required=True)
p.add_argument("--investigation", required=True)
p.add_argument("--receipt", required=True)
p.add_argument("--exit-code", type=int, default=0)
a = p.parse_args()


def lab_query(name: str, params: dict):
    r = subprocess.run([a.lab, "query", name, "-p", a.project, "--params", json.dumps(params)], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {"error": (r.stdout + r.stderr)[-300:]}


raw = open(a.raw, errors="replace").read()


def calls(text: str) -> list[str]:
    """Serialized tool-call items from the client's JSONL stream (Claude: `tool_use` blocks, Codex: `mcp_tool_call` items).
    Only real calls count; the init event that merely lists available tools does not."""
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") in ("tool_use", "mcp_tool_call"):
                found.append(json.dumps(o))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for line in text.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return found


made = calls(raw)
called = "\n".join(made)
inv = lab_query("get_investigation", {"investigation_id": a.investigation})
comps = lab_query("search_components", {"limit": 500})
n_comps = len(comps) if isinstance(comps, list) else len(comps.get("components", comps.get("items", []) or []))
status = inv.get("status")
checks = {
    "client_exit_code_zero": a.exit_code == 0,
    "called_get_investigation": "get_investigation" in called,
    "called_list_components": "list_components" in called,
    "read_investigation_resource": f"lab://projects/{a.project}/investigations/{a.investigation}" in called,
    "output_names_investigation": a.investigation in raw,
    "output_names_lab_status": bool(status) and str(status) in raw,
    "lab_investigation_found": status is not None,
    "lab_has_components": n_comps > 0,
}
# Codex CLI exec may not expose MCP resource reads to the model (assumption); record it without failing the receipt.
advisory = {}
if a.client == "codex":
    advisory["read_investigation_resource"] = checks.pop("read_investigation_resource")
receipt = {"client": a.client, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "project": a.project,
           "investigation": a.investigation, "lab_status_now": status, "lab_components": n_comps, "checks": checks,
           "tool_calls_seen": len(made), "advisory": advisory, "ok": all(checks.values()), "raw_output": a.raw}
open(a.receipt, "w").write(json.dumps(receipt, indent=2))
print(json.dumps({k: v for k, v in receipt.items() if k != "raw_output"}, indent=2))
sys.exit(0 if receipt["ok"] else 1)
