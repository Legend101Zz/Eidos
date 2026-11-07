# Eidos local v1: implementation status

Updated 2026-11-07 by the lead session, on branch `build/eidos-local-v1`.
- Criterion-level status: `ACCEPTANCE_MATRIX.md`.
- Build sandbox: `docs/ops/SANDBOX_CAPABILITIES.md`.
- Public research release runs: `docs/dev-runs/research-public-v0.1.0.md`.

## State by area

| Area | State |
|---|---|
| Wire contract (`packages/eidos-wire`) | implemented, unit-tested |
| Worker SDK and eidos-pytorch (stdlib; torch imported lazily) | implemented. Compat matrix passes: py3.9 + pydantic 1.10, and py3.12 + torch 2.15 cpu with torch unchanged. |
| Domain rules (charter, plans, assessment, qualification, support, resolver, branching, isolation, turns) | implemented, unit-tested |
| Ledger: PostgreSQL schema, project-scoped records, append-only triggers, role grants | runtime-verified (`tests/integration/test_command_path.py`) |
| Command service | ~55 commands and ~22 queries. Idempotency, revisions, fences, budgets and outbox are runtime-verified. |
| HTTP API, artifact CAS, upload/download, maintenance mode, disk-pressure refusal | runtime-verified |
| Temporal workflows (coordinator with Continue-As-New, bounded turns, independent jobs, assessment) | runtime-verified on the **regular Temporal Server 1.32.2 + PostgreSQL**. Rollover, retries and recorded-history replay are tested on the dev server (tests only). |
| Runner/launcher | Reattach-safe handles: pid + start time + boot id, exit file. dev profile and **linux:bwrap-sealed** profile runtime-verified, with hostile probes. |
| Investigator | context-policy/1, fixture runtime, recovery activities, offline turn replay. Fixture-only. |
| Pydantic AI adapter | implemented. Qualified against FunctionModel/TestModel only (fixture). No live provider. |
| Supervisor | init, start, stop, status, doctor, logs, backup, restore and upgrade runtime-verified on Linux. Restore was done into a fresh directory on new ports with re-enrolled credentials. |
| MCP adapter (stdio + loopback HTTP, 34 tools, resources) | runtime-verified with the SDK client. External clients (Claude Code/Desktop, Codex) **not exercised**. |
| HTML reports | implemented, tested with a fake client and smoke-tested on a live lab |
| Built-in components (12) and example projects (switch-order, image) | implemented, author-tested (`tests/components/run.sh`) |
| Public task adapter (`eidos-task-bundle/0.1` → `eidos-answer/0.2`) | D01/D02/D12 run through the internal loop (fixture provider) with structurally valid answers |
| Switch-order acceptance journey | helper assignment `task/switch-journey` (active) |

## Active helper assignments and worktrees

- `/workspace/wt/switch-journey`, branch `task/switch-journey`. Uses its own lab at `/workspace/.eidos-dev/h1/lab` (ports 8865/5644/7933).

## How to run

```bash
export UV_CACHE_DIR=/workspace/.uv-cache
uv sync
.venv/bin/ruff check src tests && .venv/bin/lint-imports
.venv/bin/python -m pytest tests/unit -q
EIDOS_TEST_PG_ADMIN=postgresql+psycopg://postgres:devpass@127.0.0.1:5432/postgres .venv/bin/python -m pytest tests/integration -q
bash tests/worker_compat/run_matrix.sh
bash tests/components/run.sh
export EIDOS_HOME=/workspace/.eidos-dev/lead/lab   # lead lab
.venv/bin/lab init --home $EIDOS_HOME --temporal-source /opt/temporal/server && .venv/bin/lab start && .venv/bin/lab doctor
```

## Last verified results (2026-11-07)

- unit + integration (PostgreSQL 16, Temporal dev server for workflow tests): **44 passed**.
- mcp + reporting: 26 passed. Worker matrix: MATRIX OK. Components: 53 passed.
- Import Linter: 5/5 contracts kept. Ruff clean.
- Live lab:
  - Component validate → publish → bind → run.
  - Sealed bwrap job, with 6/6 access probes passed and separate-identity failed (truthfully: the candidate user cannot read the root-owned interpreter).
  - Backup, then restore on new ports.
  - D01/D02/D12 fixture investigations concluded, and their answers passed `validate_answer.py`.
- CI status could not be read from the sandbox (GitHub API rate limit). `.github/workflows/ci.yml` runs lint, unit, PostgreSQL + Temporal integration and the worker matrix.

## Live-provider qualification runs needed (controller-run; no keys here)

| Provider / model | Calls | Max tokens per call | Purpose |
|---|---|---|---|
| Anthropic `claude-sonnet-5-5` through `PydanticAIRuntime` (`model: anthropic:claude-sonnet-5-5`, `max_retries=0`) | ≤ 24 | 4,096 output; ≤ 60k input | Decision-schema conformance on D02/D12 bundles, timeout/possible-spend accounting, worker kill after an accepted decision |
| One OpenAI-compatible endpoint (`openai-compatible:<model>`, `base_url`) | ≤ 10 | 2,048 output | Adapter compatibility and error mapping |

Command (operator side, key in the environment of the investigator-worker only):
`lab investigate start <project> --runtime pydantic-ai --model anthropic:claude-sonnet-5-5 --limit model_tokens=600000 --limit turns=8 ...`

## Builder interventions log (distinct from autonomous investigator behaviour)

- 2026-11-07, D02 development run: the builder-authored component `eidos.project.command@1.0.0` staged with an invalid role, so all baseline jobs failed. The fixture investigator correctly concluded "no successful runs". The builder fixed the defect and released 1.0.1. Both runs remain in the ledger.
- No autonomous investigation with a real LLM has been run. Every investigator decision so far comes from deterministic fixture policies.

## Next concrete action

1. Merge `task/switch-journey` after review.
2. Update `ACCEPTANCE_MATRIX.md` statuses from its run record.
3. Tag `eidos-v0.1.0` once the journey passes.
4. Remaining gaps: macOS/Lima profile, external MCP clients and live providers are unqualified here.
