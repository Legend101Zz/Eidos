# Eidos local v1: implementation status

Updated 2026-11-08 by the lead session, on branch `build/eidos-local-v1`.
- Start here: `HANDOFF.md`, which lists what v0.4.0 is and isn't, the evidence classes, open items and pending owner decisions.
- Latest freeze: tag **`eidos-v0.4.1` → `e328608`**, with all six CI jobs green (run `42429763457`). After it there is only a documentation-only commit with this update.
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
| Temporal workflows (coordinator with Continue-As-New, bounded turns, independent jobs, assessment) | runtime-verified on the **regular Temporal Server 1.32.2 + PostgreSQL**. Rollover, retries and recorded-history replay are tested on the dev server (tests only). Real-time timeout/retry behaviour (heartbeat timeout, start-to-close, retry policy, non-retryable errors, worker SIGKILLed mid-activity taken over by a second worker) passes on the dev server **and** on the lab's regular server: `tests/integration/test_temporal_realtime.py`. |
| Runner/launcher | Reattach-safe handles: pid + start time + boot id, exit file. The dev profile and **linux:bwrap-sealed** are runtime-verified, with hostile probes. **macos:seatbelt-sealed** (`sandbox-exec`) is implemented and unit-tested for profile generation. Its hostile probes passed in the macOS CI job (GitHub-runner evidence, run `42429763457`). |
| Investigator | context-policy/1, fixture runtime, recovery activities, offline turn replay. Fixture-only. |
| Pydantic AI adapter | **Anthropic qualified (development, lab-free):** receipt #3 passed 8/8 after the fixes from receipts #1 and #2. OpenAI-compatible endpoints are unqualified. |
| Supervisor | init, start, stop, status, doctor, logs, backup, restore and upgrade runtime-verified on Linux. Restore was done into a fresh directory on new ports with re-enrolled credentials. |
| MCP adapter (stdio + loopback HTTP, 34 tools, resources) | runtime-verified with the SDK client. External clients (Claude Code/Desktop, Codex) **not exercised**. |
| HTML reports | implemented, tested with a fake client and smoke-tested on a live lab |
| Built-in components (12) and example projects (switch-order, image) | implemented, author-tested (`tests/components/run.sh`) |
| Public task adapter (`eidos-task-bundle/0.1` → `eidos-answer/0.2`) | D01/D02/D12 run through the internal loop (fixture provider) with structurally valid answers |
| Switch-order acceptance journey | **runtime-verified**: 8 steps, 149 checks (fixture investigator for step 7) — `docs/dev-runs/switch-journey.md` |

## Active helper assignments and worktrees

None (round 7 helpers finished; worktrees removed). All task branches are merged and the worktrees are removed. Tags `eidos-v0.1.0`…`eidos-v0.4.0` are development freezes, not v1 acceptance.

## How to run

```bash
export UV_CACHE_DIR=/workspace/.uv-cache
uv sync
.venv/bin/ruff check src tests && .venv/bin/lint-imports
.venv/bin/python -m pytest tests/unit -q
EIDOS_TEST_PG_ADMIN=postgresql+psycopg://postgres:devpass@127.0.0.1:5432/postgres .venv/bin/python -m pytest tests/integration -q
.venv/bin/python -m pytest tests/unit tests/integration tests/mcp tests/reporting -q      # 113 passed, 1 skipped (2026-11-08)
EIDOS_TEMPORAL_TARGET=127.0.0.1:7633 .venv/bin/python -m pytest tests/integration/test_temporal_realtime.py -q   # against a running lab's regular server
EIDOS_NUMPY_PYTHON=<numpy env python> EIDOS_TORCH_PYTHON=<torch env python> .venv/bin/python -m pytest tests/components -q
bash tests/worker_compat/run_matrix.sh
bash tests/components/run.sh
OUT=/tmp/sb python scripts/ci/seatbelt_probes.py      # macOS only: seatbelt hostile probes + same-user planted-effect control
export EIDOS_HOME=/workspace/.eidos-dev/lead/lab   # lead lab
.venv/bin/lab init --home $EIDOS_HOME --temporal-source /opt/temporal/server && .venv/bin/lab start && .venv/bin/lab doctor
```

## Last verified results (2026-11-08)

- At `3aa9192`: unit + integration + MCP + reporting **113 passed, 1 skipped**. That count includes 4 real-time Temporal tests and 5 seatbelt policy/selection tests.
- `test_temporal_realtime.py` also passed 4/4 against the lead lab's regular Temporal Server (`EIDOS_TEMPORAL_TARGET=127.0.0.1:7633`).
- Ruff is clean and Import Linter keeps 5/5 contracts.
- CI on `c57dba9` (tag `eidos-v0.4.0`): six jobs green.
- CI on `e328608` (tag `eidos-v0.4.1`, run `42429763457`): six jobs green, including the macOS step "Seatbelt candidate-sandbox probes" and the integration job with the real-time Temporal tests. The run for `3aa9192` was cancelled because the next push superseded it.

Earlier:

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

### Receipt #2 (controller-run, run id `live-1794082625`, source `37bd0c5`)

- **Usage:** 11 requests, 41,696 input and 4,326 output tokens, USD 0.13.
- **Passed:**
  - All four conformance decisions were valid and executable, with populated args, one request each. The strict-mode `dict` warning is gone.
  - timeout: retryable.
  - unknown model (404): not retryable.
  - request-limit scenario: decided in one request.
- **Failed: read-tools.** The model used all 6 requests on `tool_call` and never decided; usage was counted correctly.
- **Smaller finding:** one decision proposed `argv ["train"]`, which is not runnable.
- **Fixes (general, this commit):**
  - The last request of a decision is reserved and made **without read tools** over the same history, so a tool loop must end in a decision. Unit test `test_tool_loop_gets_a_tool_free_final_request_and_decides`.
  - Read-tool errors and "unavailable" results now carry an explicit "no further evidence; do not re-query" note.
  - New component configuration contracts (`config_schema`, `src/eidos/domain/contracts.py`):
    - shown in the catalog and in the context;
    - enforced when a decision is accepted, so a refused proposal is fed back to the next slot within the same turn;
    - enforced again at resolution;
    - used by both live scripts.
  - `eidos.project.command` 1.0.4 declares that argv's first item is `python` or an absolute executable.

### Receipt #3 (controller-run, run id `live-1794083519`, source `55fd0e2`): **8/8 pass**

- **Usage:** 11 requests, 46,863 input and 6,499 output tokens, USD 0.16. Served `claude-sonnet-5-5`, transport retries off.
- **Conformance:** all four decisions were valid and executable with full argv contracts. Examples:
  - project CLI with `{seed}`, a timeout and `allow_nonzero_exit`;
  - a `python -c` diagnostic;
  - a scorer call plus a retrain.
- **read-tools:** 5 tool rounds, then the tool-free final request gave a valid, executable decision.
- **request-limit:** valid in 1 request.
- **timeout:** retryable. **404:** not retryable.
- **Staging note from the controller:** the script reads `components/*/eidos-component.json`, so stage `components/` alongside `src/`, `packages/eidos-wire/src` and `scripts/qualify/`.
- **Status:** the Anthropic path through the Pydantic AI adapter is qualified for development, lab-free. OpenAI-compatible endpoints are still unqualified.

### Re-run command (same caps)

```bash
python3.12 -m venv /tmp/eidos-live && /tmp/eidos-live/bin/pip install -r scripts/qualify/requirements-live.txt
ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src \
  /tmp/eidos-live/bin/python scripts/qualify/live_provider_check.py --model anthropic:claude-sonnet-5-5 > live-receipt-3.json
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

## Round 4 (2026-11-07)

- **Live receipt #3:** 8/8 pass. The Anthropic adapter is qualified for development, lab-free.
- **Adversarial review (two read-only passes):** 11 confirmed findings, all fixed with regression tests, including one critical: sealed labels could be bound into candidate jobs. Several inferred issues were also fixed. Open items are recorded in `docs/dev-runs/adversarial-review-2026-11-07.md`.
- **macOS owner qualification kit:** ready, and its Linux dry run passes every step.
- **Verified:** unit + integration + MCP + reporting 96 passed; components 59 passed; import contracts 5/5; ruff clean.

## Round 5 (2026-11-07): operator items 1–8, all closed

- **Strongest-available isolation by default:** weaker profiles need a recorded waiver. Proven live with the real admin token unreadable.
- **Sandboxed assessor:** its realized isolation is recorded in the assessment.
- **In-turn model-budget reservation:** settled on every path, with leaked reservations released.
- **Gap-free commit-ordered event sequence:** migration 0003.
- **Idempotent context snapshots.**
- **Exposure on all evaluation-revealing reads.**
- **Idle polls without model turns.**
- **Component tests in CI.**
- **Verified:** unit + integration + MCP + reporting 104 passed; workflow tests incl. stress and two frozen histories 7 passed; components 59 passed locally (CI job added); ruff clean; import contracts 5/5. A live D02 fixture investigation on the upgraded lab concluded with 0 tokens left reserved.

## Round 6 (2026-11-08): final scripted round

- **Tag:** `eidos-v0.4.0` on `c57dba9`, annotated and pushed.
- **Real-time Temporal tests:** `tests/integration/test_temporal_realtime.py`, 4 tests in about 13–21 s with real timers and no time skipping.
  - heartbeat timeout → retry on attempt 2 in under 6 s;
  - start-to-close timeout with finite attempts;
  - retry policy (3 attempts) and a non-retryable error (1 attempt);
  - worker A (a subprocess) SIGKILLed mid-activity → worker B finishes it on attempt 2 via a 2 s heartbeat timeout.
  - They pass on the dev server and on the lab's regular server.
- **macOS candidate sandbox, `macos:seatbelt-sealed`:**
  - Code: `src/eidos/runtime/launcher.py` (`seatbelt_policy`, `seatbelt_argv`, `detect_sealed_profile`), `src/eidos/domain/isolation.py`, runner/roots/settings/doctor.
  - Paths are passed as `-D` parameters. Reads of the lab home and credential directories are denied, except the attempt's own files inside them; an ancestor path such as a project root cannot reopen them.
  - Writes are allowed only to staging, scratch, resume, a private tmp, /dev and the logs. Network is denied and the environment is cleared.
  - The same OS user runs the candidate, so `separate-identity` fails. Mach IPC is not filtered.
  - Supervisor init detects it on darwin, so `strongest_profile` selects it and the sealed runner serves it. Weaker profiles still need a recorded waiver.
  - Unit tests: `tests/unit/test_seatbelt_policy.py`.
  - The macOS CI job runs `scripts/ci/seatbelt_probes.py`: hostile probes with a live control-socket listener, a same-user planted-effect control, and a permitted computation.
  - **Not done:** the macOS assessor still runs `dev:same-user`; Lima VM is not implemented.
- **Handoff:** `HANDOFF.md` written; this file and `ACCEPTANCE_MATRIX.md` updated.
- **CI:** run `42429763457` on `e328608` passed all six jobs, including the seatbelt probes on GitHub macOS arm64. Tag `eidos-v0.4.1` → `e328608`.

## Round 7 (2026-11-08, full-cap continuation)

- **Tag `eidos-v0.5.0` → `e822744`:** CI was green on all eight jobs.
- **macOS assessor under Seatbelt (`8c7e481`):** the CI step `assessor_sandbox_probe.py` is green on macOS arm64.
- **Cross-release upgrade test:** v0.1.0 wheels → head, 33 checks. It runs as the CI job `upgrade-across-releases`. `lab upgrade` now switches the interpreter (`556fe66`).
- **Clean-install end-to-end journey (`scripts/journeys/e2e_journey.sh`, CI job `e2e-journey`):** 9/9 steps at `993b8fd`, 116 s locally.
  - CLI: D01 fixture investigation, a validated answer, branch, export.
  - CLI: qualified assessment, with the assessor and template qualified by executed controls, a claim, a frozen plan, six evidence jobs and an AssessmentWorkflow outcome of `inconclusive`.
  - MCP SDK client: start, detach, read back, `get_assessment`.
  - Record: `docs/dev-runs/e2e-journey.md`.
- **Regression found by the journey and fixed (`8771bb3`):** `submit_assessment` crashed for every evidence plan on `eidos-v0.2.0`…`v0.5.0`.
- **Adversarial review 2:** six items fixed with regression tests (`181e50d`, `0754d92`, `67855b5`, `378ff0f`). Open questions are in `docs/dev-runs/adversarial-review-2-2026-11-08.md`.
- `lab component install` is now idempotent (`e822744`).
- **Not done:**
  - Lima VM profile (item 5). Whether GitHub macOS arm64 runners allow nested virtualization was not verified from here; GitHub documentation was unreachable.
  - Repair of `tests/journeys/switch_journey.py` drift (published-extension checks, intermediate-checkpoint branching, default-env reinstall). The e2e journey replaces its role in CI.
- **Last full local run:** 117 passed, 1 skipped at `67855b5`. Later commits were each followed by their focused tests and the e2e journey.

## Drift sweep (2026-11-08, round 8)

Each script and driver was either run once at the current code or shown to be covered by a CI job. All runs used a fresh lab built from head wheels, with the clean-install journey left in place (`KEEP_WORK=1`). Logs are in `/mnt/session/outputs/drift-sweep/`.

| Script / driver | How checked | Result |
|---|---|---|
| `scripts/ci/*`: build_and_install, lab_cycle, fresh_install, components, sdk_tests, research_smoke, seatbelt/assessor probes, upgrade_across_releases | CI jobs | green on `12b4822` |
| `scripts/journeys/e2e_journey.sh` | CI job `e2e-journey`, plus a local run | pass, 9/9 |
| `tests/unit`, `tests/integration` | CI | green |
| `tests/mcp`, `tests/reporting` | **Were not in CI.** Now in `lint-and-unit` (`734ea42`). | 25 passed, 1 skipped. The skipped test is the live-lab MCP test, which the e2e journey covers. |
| `tests/worker_compat/run_matrix.sh` | CI `worker-compat` | green |
| `tests/ops/runner_reattach.sh` | run | REATTACH OK |
| `tests/ops/checkpoint_and_stop.sh` | run | **FAILED: stuck in `cancel-requested`.** SIGUSR1 cannot cross bwrap's session/PID namespace, and sealed has been the default profile since round 5. Fixed with a stop-request file the SDK polls (`1a1c621`); re-run: OK at step 18,794. |
| `tests/ops/abrupt_stop.sh` | run | scenarios A and B OK, doctor OK |
| `tests/ops/perf_smoke.py` | run | **FAILED: evidence visibility timed out.** The sealed runner allowed only 1 concurrent attempt, so a long training job serialized everything else. Fixed with sealed slots equal to the dev runner's (`cabbecd`); re-run: p95 command 7.9 ms, evidence visible ≤ 1.36 s. |
| `scripts/qualify/macos_owner_kit.sh` (Linux dry run) | run | **FAILED in substance:** the fixture investigation stopped `budget-exhausted` on turn 1, because the 120k in-turn reservation exceeded the kit's 100k limit. Fixed: fixture runtimes reserve nothing unless an envelope is configured (`4dfd56d`). Re-run: all steps ok, except preflight, which fails as expected on non-macOS. |
| `scripts/research/cards.py` | run with `--cards cheap --seeds 11` | D01, D07, D12, D04 PASS |
| `scripts/qualify/live_bundle_investigation.py` | `--model test` dry run on a D02 bundle | ok (2 requests, turn cap) |
| `scripts/qualify/live_provider_check.py` | needs a key and refuses without one | correct refusal. The controller ran it live (receipts #1–#3). |
| `tests/journeys/switch_journey.py` | Had drifted and stopped running; **restored** (merged from `task/switch-restore`) and runs as the new CI job `switch-journey` | 154/154 checks, 8/8 steps, 254 s (lead re-run at `2fedb42`, no steer workaround) |

Lesson: three of the four failures came from round 5 making sealed the default profile. No CI job exercised checkpoint-and-stop, sealed-runner concurrency or the kit's fixture budget, so they failed silently.

## Round 8 (2026-11-08, final)

- **Tag:** `eidos-v0.5.1` → `12b4822`.
- **Drift sweep:** see above. Three silent failures were fixed: checkpoint-and-stop under bwrap, sealed-runner concurrency, and the fixture budget in the macOS owner kit. `tests/mcp` and `tests/reporting` were added to CI.
- **Switch-order acceptance journey restored:** 154 checks, with the same scientific assertions.
  - Both-correct 0.2625 → 0.844 after the edit at 45, and 0.7185 → 1.0 at 180.
  - The exact no-change fingerprint holds, the wrong builder is refused, and the promotion gate holds.
  - The assessment is `inconclusive` (effect 0.6452). The withdrawal drill and the step-7 worker kill and recovery pass.
  - The step-45 parent is now the final checkpoint of a run stopped at 45, because intermediate slots are not branchable. Its digest equals the control's step-45 checkpoint.
  - Runs that load test-tree extension code use `dev:same-user` with recorded waivers.
- **Fixture-policy stall fixed (`2fedb42`):** the `switch-order-scripted` policy parked on `wait_for_jobs` with nothing outstanding, so it idled until the max-idle timer. It now concludes in the claim/export decision, and the journey's steer workaround is removed.
- **Final local verification at `2fedb42`:**
  - unit + integration + MCP + reporting: **117 passed, 1 skipped**. The skip is the live-lab MCP test, which the e2e journey covers.
  - components: **59 passed**;
  - worker compat: **MATRIX OK** (34 passed);
  - ruff clean; import contracts **5/5**;
  - switch journey **154/154**; e2e journey 9/9 (sweep run).

## Round 9 (2026-11-08, final)

- **Tag `eidos-v0.5.2` → `e4558e2`:** CI green on nine jobs.
- **Idle-trap fix (`64ad288`):** `accept_decision` refuses `wait_for_jobs` when the investigation has nothing outstanding. That means no non-terminal job, no executing evidence plan, and no launching call (run/launch/author) in the same decision.
  - The refusal is the existing invalid-proposal path: the message reaches the next slot of the same turn and counts against `MAX_CONSECUTIVE_INVALID`.
  - Runtime bounded outcomes (`control.bounded_outcome`, set when the per-decision request budget is spent) are exempt, so a refusal cannot buy extra model requests.
  - The `note-then-wait` fixture now uses `request_input`.
  - The check is command-side only, so no workflow code changed and the recorded histories replay as before.
  - Regression test: `tests/integration/test_turn_recovery.py::test_wait_for_jobs_with_nothing_outstanding_is_refused_within_the_turn`. It fails without the fix.
- **Verification at `64ad288`:**
  - unit + integration + MCP + reporting **118 passed, 1 skipped**;
  - switch journey **154/154** (265 s, no steer);
  - e2e journey **9/9** (121 s);
  - cross-release upgrade **33/33** (35 s);
  - ruff clean; import contracts 5/5.

## Round 10 (2026-11-08): red CI on `8301799`, fixed

- **What failed:** CI on `8301799` was red in `switch-journey`. The step-7 scripted investigation ended turn 5 on `invalid-proposals` and never woke.
- **Cause, reproduced locally:**
  - The `64ad288` rule refused **whole decisions**, including the scripted policy's `publish_component` + `wait_for_jobs`. That dropped the publish, so the policy re-proposed it in every later turn.
  - A turn ending on the invalid-proposal limit also had no wake.
- **Why local runs had passed:** `uv pip install --find-links dist` with the shared uv cache **silently installed a cached wheel of the same version** instead of the one just built. The local journey runs at `64ad288` and `2fedb42` therefore tested stale code, and CI was the authoritative result. Fixed with `--no-cache` in `build_and_install.sh`, the upgrade script and the macOS kit.
- **Fixes (`08f9e01`, `63f7cac`, ADR 0007):**
  - only pure waits (no calls) with nothing outstanding are refused;
  - a refused-proposal turn gets 2 bounded immediate retries with a fresh view, then the coordinator waits for operator news only and reports "needs operator input" (patched `invalid-turn-retry-v1`);
  - `investigation_view` job visibility no longer depends on a 5,000-event window starting at seq 0;
  - the investigator's own effects (`claim.filed`, `model.exported`, `component.retired`, `evidence_plan.*`) count as idle-poll news.
- **Regression tests:**
  - `test_workflows.py::test_refused_proposal_turns_retry_boundedly_then_surface_for_input` (3/3 repeated passes);
  - `test_turn_recovery.py::test_wait_refused_when_launched_jobs_finished_before_the_decision_and_next_view_sees_them`.
- **Verification on fresh wheels (no cache):**
  - switch journey **154/154 twice** (271 s, 242 s);
  - unit + integration + MCP + reporting **120 passed, 1 skipped**. The skipped test is the live-lab MCP test; in one run it failed only because a stale `EIDOS_HOME` pointed at a stopped lab.

## Next concrete action

1. The controller relays CI for the current head of `build/eidos-local-v1`. `8301799` is RED, for the cause above. If all nine jobs are green, a later session or the owner tags `eidos-v0.5.3`.
2. Owner decisions (`HANDOFF.md`): the macOS kit, real-LLM D02/D12 runs, OpenAI-compatible provider qualification, external MCP clients, and Seatbelt vs Lima.
3. Engineering backlog: the review-2 idle-poll rollover issue, a CI test for checkpoint-and-stop under bwrap, and an audit of any other `uv pip install --find-links` sites for the cached-wheel hazard.
