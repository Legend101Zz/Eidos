#!/usr/bin/env python3
"""Write MCP client configuration for the Eidos stdio server without touching any global client config.

  gen_configs.py --home <EIDOS_HOME> --lab <path to the `lab` executable> [--out DIR]

Writes into DIR (default ./mcp-client-configs):
  claude-code-mcp.json      pass to `claude --mcp-config FILE --strict-mcp-config` (server key `eidos`)
  codex-overrides.json      the `-c key=value` strings for `codex exec` (TOML values; server key `eidos`)
  codex-overrides.txt       the same, one `-c` argument per line, shell-quoted
  claude-desktop-snippet.json  for MANUAL, later use only: merge into claude_desktop_config.json yourself

The server is `lab mcp` (stdio). Its only environment is EIDOS_HOME; the credential comes from the lab's admin token file
(EIDOS_TOKEN is deliberately not set). The server's self-reported name is `eidos-lab`; the client-side key is `eidos`, so
Claude Code tool names are `mcp__eidos__<tool>`.
"""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path


def toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string for these ASCII-safe paths


def build(home: str, lab: str) -> dict:
    server = {"command": lab, "args": ["mcp"], "env": {"EIDOS_HOME": home}}
    claude = {"mcpServers": {"eidos": {"type": "stdio", **server}}}
    codex = [
        f"mcp_servers.eidos.command={toml_str(lab)}",
        'mcp_servers.eidos.args=["mcp"]',
        f"mcp_servers.eidos.env={{EIDOS_HOME={toml_str(home)}}}",
    ]
    desktop = {"mcpServers": {"eidos": server}}
    return {"claude": claude, "codex": codex, "desktop": desktop}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", required=True, help="EIDOS_HOME of the lab")
    ap.add_argument("--lab", required=True, help="absolute path of the `lab` executable (from the Eidos venv)")
    ap.add_argument("--out", default="mcp-client-configs")
    a = ap.parse_args()
    home, lab = str(Path(a.home).expanduser().resolve()), str(Path(a.lab).expanduser().resolve())
    if not Path(lab).exists():
        raise SystemExit(f"lab executable not found: {lab}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = build(home, lab)
    (out / "claude-code-mcp.json").write_text(json.dumps(cfg["claude"], indent=2) + "\n")
    (out / "codex-overrides.json").write_text(json.dumps(cfg["codex"], indent=2) + "\n")
    (out / "codex-overrides.txt").write_text("\n".join(f"-c {shlex.quote(x)}" for x in cfg["codex"]) + "\n")
    (out / "claude-desktop-snippet.json").write_text(json.dumps(cfg["desktop"], indent=2) + "\n")
    print(json.dumps({"written": sorted(p.name for p in out.iterdir()), "dir": str(out.resolve()), "home": home, "lab": lab}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
