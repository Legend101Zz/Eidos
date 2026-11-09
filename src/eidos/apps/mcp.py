"""Entry point for the MCP adapter (stdio by default)."""


def main() -> None:
    from eidos.mcp.server import serve
    serve(transport="stdio", port=8766)
