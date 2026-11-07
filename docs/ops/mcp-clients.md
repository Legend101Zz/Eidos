# MCP adapter: running it and registering it in clients

The MCP adapter (`src/eidos/mcp/`) is an operator interface over the lab's HTTP command API. It holds
no state and no database credentials; every tool is one lab command or query authorized by the lab
under the caller's own token. Closing a client only detaches: investigations, jobs and receipts live in
the lab. See "MCP control surface" in the design document.

## Qualification status (read this first)

| Item | Status |
|---|---|
| Adapter exercised with the official `mcp` Python SDK client (in-process, stdio subprocess, streamable HTTP on loopback) | **Done**, automated: `tests/mcp/` |
| Live stdio session against a running lab: commit charter, start investigation, retry with the same key, disconnect, reconnect in a new process, read it back | **Done**, `tests/mcp/test_mcp_live.py` (needs `EIDOS_HOME`) |
| Claude Code, Claude Desktop, Codex (or any other external MCP host) | **NOT exercised.** The sandbox has no client binaries and no API keys. The config snippets below follow each client's documented format but are **unverified**. Qualification against each targeted client release is **pending** and must be done in a real integration test before this adapter is advertised as supporting it. |
| OAuth resource-server behaviour (issuer/audience/scope/expiry validation) | **Not implemented.** HTTP mode accepts a lab bearer token validated by the lab, not an OAuth access token. Do not expose it beyond loopback. |
| MCP Tasks extension, subscriptions, prompts, elicitation | **Not implemented.** Long work returns a durable job/investigation id; poll `get_run` / `get_investigation` / `list_events`. |

### Protocol revisions

The installed SDK is `mcp` 2.3.0 (`mcp-types` 2.3.0). The revisions it implements
(`mcp_types.version`): handshake era `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`, and the
modern per-request era `2026-07-28`. The adapter adds no protocol code of its own and serves whatever the SDK
serves; tests verify the two revisions the design names:

* `2026-07-28` — negotiated by an SDK client in `auto` mode over stdio and over streamable HTTP.
* `2025-11-25` — negotiated by an SDK client in `legacy` (initialize handshake) mode over stdio and HTTP.

Older handshake revisions are accepted by the SDK but were not tested here; do not rely on them. The
design's "advertise only revisions that passed conformance" rule is met only for these two, and only against
the SDK's own client; the conformance of third-party hosts is the pending item above.

## Running

```bash
# stdio (local clients launch this themselves); protocol on stdout, logs on stderr
EIDOS_HOME=~/.eidos lab mcp --transport stdio

# streamable HTTP, loopback only, bearer token required
EIDOS_HOME=~/.eidos lab mcp --transport http --port 8766   # endpoint: http://127.0.0.1:8766/mcp
```

**stdio credential:** `EIDOS_TOKEN` if set, else the lab's `config/secrets/admin.token` (print its path with
`lab token`). The adapter then acts as that principal; the lab enforces that principal's grants and
project access. For a narrower operator, create a principal with `create_principal` and put its token in
`EIDOS_TOKEN`.

**HTTP credential:** every request must carry `Authorization: Bearer <lab token>`. The adapter validates it
by calling the lab's `/v1/whoami` and then uses that same token for the lab call; it never substitutes its
own, stronger credential, and it never forwards the token anywhere but the lab API. Requests with a
missing or rejected token get `401` + `WWW-Authenticate: Bearer`. The listener binds `127.0.0.1` only. A
browser `Origin` header must be loopback (`localhost`, `127.0.0.1`, `::1`) or listed in
`EIDOS_MCP_ALLOWED_ORIGINS` (comma separated); other origins get `403`. Host-header (DNS-rebinding) checks are
the SDK's.

## Registering the stdio server (UNVERIFIED snippets)

Use absolute paths; replace `/path/to/Eidos` and the lab home.

**Claude Code** (`.mcp.json` in a project, or `claude mcp add`):

```json
{
  "mcpServers": {
    "eidos-lab": {
      "type": "stdio",
      "command": "/path/to/Eidos/.venv/bin/lab",
      "args": ["mcp", "--transport", "stdio"],
      "env": { "EIDOS_HOME": "/home/me/.eidos" }
    }
  }
}
```

```bash
claude mcp add eidos-lab --env EIDOS_HOME=/home/me/.eidos -- /path/to/Eidos/.venv/bin/lab mcp --transport stdio
```

**Claude Desktop** (`claude_desktop_config.json`): same `mcpServers` object as above (without `type`).

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.eidos-lab]
command = "/path/to/Eidos/.venv/bin/lab"
args = ["mcp", "--transport", "stdio"]
env = { EIDOS_HOME = "/home/me/.eidos" }
```

None of these was run. Expect to adjust field names to the client release you target, and record the
release and result in the qualification log.

## Tool contract

34 tools (`eidos.mcp.tools.TOOLS`; the list is also the `tools/list` response). Components are *data*:
`list_components` / `describe_component` return releases with contracts and effects; there is never a tool
per component.

* **Every mutating tool requires `command_key`** (string, ≤200 chars) and accepts `expected_revision`.
  Reusing a key with identical arguments returns the original receipt (`replayed: true`), which is how a
  client safely retries after a lost reply. The same key with different arguments fails with
  `COMMAND_KEY_CONFLICT`. Idempotency is scoped to (project, authenticated caller, key); use a fresh key for
  a deliberately new operation. A receipt prevents duplicate admission; it does not promise exactly-once
  execution by an external backend (`OUTCOME_UNKNOWN` is reported, not hidden).
* Arguments are the named fields of each tool plus an optional `payload` object for further documented fields
  of the underlying command; named arguments win.
* **Errors.** Domain and validation failures are tool results with `isError: true` and
  `structuredContent.error = {code, message, next_action, details, http_status}` (also as JSON text), e.g.
  `STALE_REVISION` (with `details.observed_revision`), `BUDGET_EXHAUSTED`, `COMMAND_KEY_CONFLICT`,
  `CHARTER_REQUIRED`, `THRESHOLD_OVERRIDE`, `NOT_FOUND`, `FORBIDDEN`, `UNAUTHENTICATED`, `INVALID_ARGUMENTS`,
  `LAB_UNAVAILABLE`. Malformed protocol requests and unknown methods remain JSON-RPC errors. Resource reads
  that fail are JSON-RPC errors (`-32002` for not found, `-32602` for a bad URI).
* **Long work.** `validate_component`, `run_component`, `launch_trial`, `launch_evidence_plan`, `qualify_protocol`
  return durable ids promptly. Poll `get_run`, `get_investigation`, `list_events` (resume from `next_cursor`;
  pass the returned `horizon` back for a stable snapshot). `cancel_run` records the request;
  `cancel_requested` persists until the backend confirms termination. Cancelling a transient MCP request is
  not cancelling an admitted job.
* **`get_artifact_access`** returns metadata, a bounded preview, or (for `content`) the lab API path to stream
  the bytes with your own bearer token. No credential-bearing URL is minted and large bytes do not pass
  through MCP.
* Results are marked non-cacheable (`ttlMs: 0`, `cacheScope: private`), so project-private data is not shared
  across callers by SDK caches.

### Resources (read-only)

`lab://projects/{p}/investigations/{i}`, `.../runs/{job}`, `.../artifacts/{a}/manifest`, `.../claims/{c}`,
`.../evidence-plans/{ep}`, `.../assessments/{as}`, `.../charters/{c}`. They are locators subject to the same
access checks; the equivalent read tools return the same data for tools-only clients. Resources are exposed
as templates (`resources/templates/list`); there is no enumeration of all ledger objects.

### Known limits

* Reading `get_claim` / `get_assessment` / `compare_trials` records an exposure for the calling principal in the
  lab (that is lab behaviour, not the adapter's).
* The lab has no `list_components` query; the tool wraps `search_components`. Its `get_study_charter`,
  `get_evidence_plan` wrap `get_record`.
* Pending scientific judgments / review queue are lab records; there is no MCP elicitation convenience yet.
* `eidos-mcp` in `pyproject.toml` points to `eidos.apps.mcp:main`, which does not exist; use `lab mcp`.
