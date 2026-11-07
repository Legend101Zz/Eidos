"""Runs the real MCP stdio server loop against the fake lab client (fixture-only)."""
import anyio

from fake_client import FakeLab
from eidos.mcp.server import _serve_stdio

if __name__ == "__main__":
    anyio.run(_serve_stdio, FakeLab().client, "good-token")
