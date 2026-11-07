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
| Pydantic AI adapter | implemented. One real-provider qualification run (receipt #1) found four defects, now fixed; receipt #2 pending. |
| Supervisor | init, start, stop, status, doctor, logs, backup, restore and upgrade runtime-verified on Linux. Restore was done into a fresh directory on new ports with re-enrolled credentials. |
| MCP adapter (stdio + loopback HTTP, 34 tools, resources) | runtime-verified with the SDK client. External clients (Claude Code/Desktop, Codex) **not exercised**. |
| HTML reports | implemented, tested with a fake client and smoke-tested on a live lab |
| Built-in components (12) and example projects (switch-order, image) | implemented, author-tested (`tests/components/run.sh`) |
| Public task adapter (`eidos-task-bundle/0.1` → `eidos-answer/0.2`) | D01/D02/D12 run through the internal loop (fixture provider) with structurally valid answers |
| Switch-order acceptance journey | **runtime-verified**: 8 steps, 149 checks (fixture investigator for step 7) — `docs/dev-runs/switch-journey.md` |

## Active helper assignments and worktrees

None. All task branches are merged and the worktrees are removed. Tag `eidos-v0.1.0` is the current development freeze; it is not v1 acceptance.

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

- Round 2 (c75c814): unit 27, integration incl. workflows and pipelines, MCP 19. Every suite passes.
- Earlier: unit + integration + mcp + reporting **70 passed** (78e4597). Switch journey: 149 checks passed. A fresh wheel install (`uv build` → new venv → init/start/doctor) was all ok.
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

**Standalone check (no PostgreSQL/Temporal/lab):** `scripts/qualify/live_provider_check.py`.
- Hard caps: at most 24 requests in total and 4,096 output tokens per request; temperature is not set.
- The key is read only from `ANTHROPIC_API_KEY`.
- It prints a JSON receipt with the served model id, per-request usage and request ids, schema conformance, and the timeout, request-limit and unknown-model failure cases.
- It was dry-run here against Pydantic AI's `TestModel` only.

```bash
python3.12 -m venv /tmp/eidos-live && /tmp/eidos-live/bin/pip install -r scripts/qualify/requirements-live.txt
ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src \
  /tmp/eidos-live/bin/python scripts/qualify/live_provider_check.py --model anthropic:claude-sonnet-5-5 > live-receipt.json
```

Full-lab command (key in the investigator-worker's environment only):
`lab investigate start <project> --runtime pydantic-ai --model anthropic:claude-sonnet-5-5 --limit model_tokens=600000 --limit turns=8 ...`

## Real-provider adapter qualification runs (development; NOT autonomous investigations)

### Receipt #1 (controller-run, 2026-11-07)

- **Identity:** run id `live-1794081737`, script at `0edf7e3`.
- **Environment:** macOS arm64, Python 3.12.8, pydantic-ai-slim 2.66.0, anthropic 2.6.0.
- **Model:** requested `anthropic:claude-sonnet-5-5`; served `claude-sonnet-5-5`.
- **Usage recorded:** 4 calls, 12,996 input and 1,976 output tokens. Controller-accounted spend was USD 0.27, including a USD 0.22 allowance for unrecorded requests.
- **Results:**
  - 4/4 conformance decisions validated, but probably with **empty args**. Anthropic strict mode forces `{}` for open `dict` fields, and the validator accepted calls without arguments.
  - read-tools scenario: failed on `request_limit=3` with usage lost.
  - timeout: correctly retryable.
  - unknown model (404): wrongly classified retryable.
- **Fixes (general adapter changes, this commit):**
  - Tool args and claim scope are now JSON strings, parsed and validated. A unit test checks the output schema has no open objects.
  - `validate_decision` rejects calls missing required args, so a valid decision is also an executable one.
  - The receipt now records bounded args.
  - Partial usage and request ids are taken from captured run messages on any failure.
  - Failure classification: 4xx not-found, invalid-request and auth errors are not retryable; timeouts, connection errors, 429 and 5xx are.
  - The request budget is stated in the prompt (default 8). Hitting it is a bounded no-effect decision with its usage reported.

### Receipt #2 (to run after CI is green on the fix commit; same caps)

```bash
python3.12 -m venv /tmp/eidos-live && /tmp/eidos-live/bin/pip install -r scripts/qualify/requirements-live.txt
ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src \
  /tmp/eidos-live/bin/python scripts/qualify/live_provider_check.py --model anthropic:claude-sonnet-5-5 > live-receipt-2.json
```

- Caps: at most 24 requests (the scenario budgets sum to 21) and 4,096 output tokens per request; temperature unset.
- Pass means the decision has calls or a control, every call has its required args, and the args are listed in the receipt.

### Lab-free real-LLM investigation driver (for the controller; capped)

`scripts/qualify/live_bundle_investigation.py` drives one generated task bundle with the real runtime port, `context-policy/1` and decision validation. It executes a restricted tool set locally: the bundle's own CLI, the validation scorer, authored candidate files, notes and conclude. It does **not** use the ledger, Temporal or isolation, so its records are labelled "lab-free real-LLM development run".
- The investigator sees only the bundle and the two generic components. Test inputs and labels are never used.
- Caps: `--max-requests` (hard limit 60), `--max-turns`, 4,096 output tokens per request, temperature unset.
- Dry-run here via `replay:` scripted decisions on D02. Baseline plus scorer ran and concluded, with validation accuracy 0.838.

```bash
cd research/public/development && PY=<release env python> && $PY package_task.py --task D02 --out /tmp/eidos-D02 && cd -
$PY -m pip install -r scripts/qualify/requirements-live.txt      # adds pydantic-ai-slim/anthropic; no lock pins change
ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src $PY scripts/qualify/live_bundle_investigation.py \
  --bundle /tmp/eidos-D02 --python $PY --out /tmp/eidos-live-D02.json --model anthropic:claude-sonnet-5-5 \
  --max-requests 40 --max-turns 8        # repeat with D12 for the null case
```

Expected cost per bundle: at most 40 requests, each roughly 3–8k input and ≤ 4k output tokens.

## Builder interventions log (distinct from autonomous investigator behaviour)

- 2026-11-07, D02 development run: the builder-authored component `eidos.project.command@1.0.0` staged with an invalid role, so all baseline jobs failed. The fixture investigator correctly concluded "no successful runs". The builder fixed the defect and released 1.0.1. Both runs remain in the ledger.
- No autonomous investigation with a real LLM has been run. Every investigator decision so far comes from deterministic fixture policies.

## Helper fixes merged from the journey (builder interventions)

These are separate commits in the journey merge:
- Environment-provided capabilities recorded in the lock. The lead then tightened it: a missing adapter is an error.
- `compare_trials` now uses the branch record to detect shared history.
- `assess_plan` reads the output manifest correctly.
- Investigator activities heartbeat, so a killed worker is recovered in about 30 s.

## Round 2 (operator continuation), done

- CI is merged. Third-party actions are pinned to commit SHAs. New jobs: fresh-install (Linux) and macOS arm64 runner smoke, labelled runner evidence. Results come from the controller.
- Product gaps closed: checkpoint-and-stop, plan pipelines, published extensions, separate identity, abrupt-stop recovery, and stress tests including the wake-storm fix. See ACCEPTANCE_MATRIX §D.
- Research release: all 14 cards ran as reference smoke through Eidos (builder checks reference-assisted, labelled as such). `qualify.py` passed 27/27. Task issues are in `docs/dev-runs/research-task-issues.md`.
- MCP: the official SDK client is verified over stdio and streamable HTTP. The named external hosts remain untested.
- Live provider: `scripts/qualify/live_provider_check.py` is ready for the controller (command above).

## Next concrete action

1. The controller checks CI on head `d7c5e99`: relocked `uv.lock`, and a single run per commit. Tag `eidos-v0.2.0` only once lint-and-unit, integration, fresh-install, worker-compat and macOS are all green on one SHA.
2. The controller runs live receipt #2 (`scripts/qualify/live_provider_check.py`, same caps).
3. Then lab-free real-LLM runs on D02 and D12 (`scripts/qualify/live_bundle_investigation.py`). Record them as lab-free real-LLM development runs, not autonomous Eidos lab runs.
4. Open gaps that need hardware or clients: macOS researcher machine and Lima VM, CUDA/MPS, external MCP hosts, sleep/suspend.
