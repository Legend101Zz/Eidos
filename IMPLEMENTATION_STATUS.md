# Eidos local v1 — implementation status

Updated: 2026-11-07 (lead session). Branch `build/eidos-local-v1`. See `ACCEPTANCE_MATRIX.md` for
criterion-level status and `docs/ops/SANDBOX_CAPABILITIES.md` for the build environment.

## Current state (summary)

| Area | State |
|---|---|
| Wire contract (`packages/eidos-wire`) | implemented, unit-tested |
| Worker SDK + eidos-pytorch (stdlib; torch lazily) | implemented; compat matrix passes (py3.9 + pydantic 1.10, py3.12 + torch 2.15 cpu) |
| Domain rules (charter, plans, assessment, qualification, support, resolver, branching, isolation, turns) | implemented, unit-tested |
| Ledger (PostgreSQL schema, append-only triggers, role grants) | implemented, runtime-verified (tests/integration) |
| Command service (idempotency, revisions, fences, budgets, outbox) + ~50 commands, ~20 queries | implemented; core paths runtime-verified |
| HTTP API + artifact upload/download (CAS store) | implemented, runtime-verified (smoke) |
| Temporal workflows (coordinator w/ Continue-As-New, bounded turns, jobs, assessment) | implemented; turn loop runtime-verified on the **regular Temporal Server 1.32.2 + PostgreSQL** |
| Runner/launcher (reattach-safe handles, bwrap sealed profile, isolation probes) | implemented; dev profile runtime-verified; sealed profile not yet exercised end to end |
| Investigator (context-policy/1, fixture runtime, activities) | implemented; fixture-only |
| Pydantic AI adapter | not started |
| Supervisor (init/start/stop/status/doctor/logs) | implemented, runtime-verified on Linux sandbox |
| Backup/restore/upgrade | in progress (lead) |
| MCP adapter + HTML reports | helper assignment `task/mcp-reports` (active) |
| Built-in components + example projects | helper assignment `task/components` (active) |

## Active helper assignments / worktrees

- `/workspace/wt/components` — branch `task/components` (helper slot 2, artifacts `/workspace/.eidos-dev/h2`).
- `/workspace/wt/mcp-reports` — branch `task/mcp-reports` (uses lead lab read-mostly, own `h1-*` projects).

## How to run

```bash
export UV_CACHE_DIR=/workspace/.uv-cache
uv sync                                   # lab env (Python 3.12, no torch)
.venv/bin/python -m pytest tests/unit -q  # pure domain
# PostgreSQL-backed tests need a cluster: EIDOS_TEST_PG_ADMIN=postgresql+psycopg://postgres:devpass@127.0.0.1:5432/postgres
.venv/bin/python -m pytest tests/integration -q
bash tests/worker_compat/run_matrix.sh    # worker SDK independence matrix
# A full lab (regular Temporal + PostgreSQL), Linux sandbox:
export EIDOS_HOME=/workspace/.eidos-dev/lead/lab
.venv/bin/lab init --home $EIDOS_HOME --temporal-source /opt/temporal/server   # or omit to download pinned release
.venv/bin/lab start && .venv/bin/lab doctor && .venv/bin/lab status
```

## Last verified results

- 2026-11-07: `pytest tests/unit tests/integration` → 27 passed. Worker matrix → MATRIX OK (20 + 34 tests).
- 2026-11-07: `lab init` + `lab start` on the sandbox: all services running; echo component validate→publish→bind→run
  succeeded via JobWorkflow; fixture investigation `inv-smoke` turn 1 completed (`waiting`).

## Live-provider qualification runs needed (controller-run; none possible here — no keys)

Planned once the Pydantic AI adapter lands (fill exact numbers then):
- Anthropic `claude-sonnet-5-5` via Pydantic AI: ~20 calls, max 4k output tokens each — decision-schema conformance,
  timeout/`max_retries=0`, interruption after accepted decision.
- One OpenAI-compatible endpoint (local or hosted): ~10 calls, max 2k output tokens — adapter compatibility.

## Builder interventions log (distinct from autonomous investigator behaviour)

- No autonomous investigation with a real LLM has been run. All investigator behaviour so far is fixture-only.

## Next concrete action

Backup/restore/upgrade (`src/eidos/supervisor/maintenance.py`), then the switch-order fixture journey once
`task/components` merges, then Pydantic AI runtime, Temporal replay/rollover/payload tests, CI workflow.
