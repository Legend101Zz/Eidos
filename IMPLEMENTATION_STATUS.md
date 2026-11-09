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
| Runner/launcher | Reattach-safe handles: pid + start time + boot id, exit file. The dev profile and **linux:bwrap-sealed** (the only hostile-code profile) are runtime-verified, with hostile probes. **macos:seatbelt-sealed** is runtime-verified as a same-OS-user profile on the owner's Mac and in the macOS CI job; it cannot satisfy plans requiring sealed execution. |
| Investigator | context-policy/1, fixture and pydantic-ai runtimes, recovery activities, offline turn replay. Real-model runs: see A8. |
| Pydantic AI adapter | **Anthropic qualified:** receipt #3 passed 8/8, and the v1.0.0 re-run passed 8/8 (`docs/release/evidence/`). **OpenAI-compatible:** the adapter is verified against a local Ollama endpoint. No local model of 14B or less passed full schema conformance (G4: adapter verified, model conformance failure recorded). Hosted OpenAI-compatible services are not separately qualified. |
| Supervisor | init, start, stop, status, doctor, logs, backup, restore and upgrade runtime-verified on Linux. Restore was done into a fresh directory on new ports with re-enrolled credentials. |
| MCP adapter (stdio + loopback HTTP, 34 tools, resources) | runtime-verified with the SDK client, and with **Claude Code 2.1.318 and Codex CLI 0.157.0** on the owner's Mac (read and write paths; `docs/mac/receipts/a14-*.json`). Claude Desktop is not supported in v1. |
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

## Round 11 (2026-11-08): owner decisions; security fix; Mac qualification kit

- **CI on `57dd5bd`:** 6 of 9 jobs green. Three jobs failed in setup on GitHub release-asset 504s (uv and the Temporal download). That was infrastructure, not code.
- **Download robustness (`3a96f64`):** the Temporal download now retries with backoff, and `EIDOS_TEMPORAL_ARCHIVE_CACHE` provides a verified archive cache. The CI lab jobs cache the archive with `actions/cache`, keyed by OS/arch and the pinned table, and fall back to the PyPI uv wheel if setup-uv fails.
  - The v0.1.0 side of the upgrade job still downloads with old code, without retries.
- **SECURITY (`3a094d8`):** model-chosen commands and job processes no longer see provider keys.
  - `run_contained` runs untrusted commands under the strongest sealed profile: empty env, no network, no home or credential-store reads, writes only to work/out, timeouts. Without a sealed profile it refuses, unless `allow_same_user` is set, which records a waiver.
  - The lab-free driver runs its project commands and scorer through it (`--allow-same-user`; `isolation` in the receipt).
  - Same-user job launches get an allowlisted environment (`job_environment`).
  - The supervisor passes credentials only to the investigator worker (`scrub_secrets` for the others).
  - Task-bundle export, environment inspection and prefix probes also use the allowlisted env.
  - Tests in `tests/integration/test_contained_commands.py`, all passing here under bwrap: no key, no planted home file, no network, no host write outside work, same-user only with a waiver, and the D02 baseline plus scorer still run contained.
- **Mac qualification kit**, to be run by the controller (runbook `docs/ops/MAC_QUALIFICATION_RUNBOOK.md`):
  - MCP clients, `scripts/qualify/mcp_clients/`: config generator, the Claude Code / Codex commands, receipt verification, and a Claude Desktop log check. The client flag syntax is unverified here.
  - MPS smoke: `scripts/qualify/mps_smoke.py`.
  - Full-lab real-LLM D02 with caps: `scripts/qualify/full_lab_live.sh`.
  - Lab-free D02/D12 re-run.
  - Storage paths: `docs/ops/STORAGE_PATHS.md`.
- **New CLI options:** `lab investigate start --settings JSON --max-model-calls-per-turn N`.
- **Verified here:** unit + integration + MCP + reporting **124 passed, 1 skipped** at `80c1e2d`.

## Round 12 (2026-11-08): non-Mac completion work

- **CDN independence (`8a96b49`):** `scripts/ci/temporal_server.sh` tries, in order:
  1. a verified archive cache;
  2. the pinned download, sha256-checked;
  3. a source build via the Go module proxy, which writes `provenance.json` with kind source-built.
  - `lab init --temporal-source` records the binaries as `operator-supplied`, with the declared provenance; `lab doctor` shows it.
  - CI lab jobs restore the archive from `actions/cache`; uv falls back to PyPI `uv==0.13.7`.
  - The new CI job `temporal-source-fallback` proves the source path. It cannot be proven here, because this sandbox blocks the Go toolchain download.
  - User docs: `docs/ops/TEMPORAL_SERVER_SOURCES.md`.
- **Adversarial review 3:** 7 findings fixed with tests (`5df8938`), including a high-severity key leak through symlinked outputs in the lab-free driver. 1 medium item remains open (needs-input is not visible outside the workflow query). Record: `docs/dev-runs/adversarial-review-3-2026-11-08.md`.
- **A15 suspend/resume on Linux (`tests/ops/suspend_resume.sh`):** 14 lab processes plus the running job were SIGSTOPped for 150 s, longer than the heartbeat, the lease and the activity heartbeat timeout, then SIGCONTed.
  - The job **succeeded on its single original attempt**, with no duplicate publications.
  - 217 s of compute was accounted, including the suspension.
  - `lab doctor` was clean.
  - Log: `/mnt/session/outputs/ops/suspend.log`.
- **Soak (`tests/ops/soak.py`, 20 min, Linux):** **ok**. The JSON is at `/mnt/session/outputs/ops/soak.json`.
  - 193 small jobs, all succeeded, plus 19 fixture investigations. 0 service restarts.
  - Open-fd ratio between the last and first quartile was 1.0 for every service.
  - RSS growth: Temporal 1.21×, API 1.13×, all others ≤ 1.03×.
  - The PostgreSQL data directory grew from 91 to 188 MB.
  - Submit-to-terminal latency: p50 0.54 s, p95 0.55 s.
  - Workflow history sizes were **not** captured: the CLI query returned 0.
  - A first attempt aborted when the e2e project's trial budget ran out. The script now records errors and keeps going.
- **macOS CI fix (`e450402`):** `temporal_server.sh` failed under bash 3.2, because a heredoc inside `<(...)` was brace-expanded. `shellcheck -S error` now runs in lint-and-unit, but note that it does not catch that bash-3.2 parse issue; the macOS job does.
- **Licences, NOTICES, troubleshooting, ops docs (merged from `task/ops-docs`):**
  - `docs/ops/LICENSES.md` covers 81 distributions. Copyleft: psycopg is LGPL-3.0, certifi is MPL-2.0. No GPL/AGPL and no unknown licences.
  - `NOTICES.md`: no third-party code was copied.
  - `docs/ops/TROUBLESHOOTING.md`, and updated upgrade/backup/restore procedures.
  - The `pyproject.toml` files no longer claim Apache-2.0. The project licence is an **owner decision**.

## Round 13 (2026-11-08)

- **Tag `eidos-v0.6.0` → `c7b6681`:** CI green on all ten jobs. It is the first tag with CI independent of GitHub's release CDN.
- **Review-3 open item closed:** "needs operator input" is now a durable ledger state.
  - The coordinator calls `mark_needs_input` through a version-guarded activity (`needs-input-visible-v1`). This sets an investigation label and emits an `investigation.needs_input` event.
  - Steer, resume, pause and stop clear it, emitting `investigation.input_received`.
  - It is visible in `get_investigation` (`lab investigate show`, MCP `get_investigation`) and in `project_summary.investigations_needing_input` (`lab status`).
  - Tests: `test_review2_exposure.py::test_needs_input_is_durable_visible_and_cleared_by_an_operator_action`, plus the workflow test asserting exactly one mark.
- **Soak history capture fixed:** the soak now describes each running workflow execution, since the list output carries no history size. A 4-minute re-run passed (156 jobs). A running coordinator was captured at history length 5 / 926 bytes; later samples had no running workflows. JSON: `/mnt/session/outputs/ops/soak5.json`.

## Round 14 (2026-11-08): Mac branch merged, full-lab defect fixed

- **Merge:** `origin/mac/local-qualification` (e966344) merged into `build/eidos-local-v1` as merge commit 99be3ba. `docs/mac/` is kept as history.
- **Suites on the head (097d83a):** unit + integration + MCP + reporting **135 passed, 4 skipped** (3 skips are macOS-only; 1 is the live-lab MCP test).
- **Suites on the merged result:**
  - 133 passed, 2 failed. Both failures were a Linux false negative in the new home-read probe (`os.listdir` saw the bwrap bind-mount scaffold). It now reads a planted file in the real home instead.
  - Re-run: the isolation, assessor, sealed-home and contained suites give 13 passed, 3 skipped (macOS-only).
  - Components: 61 passed.
- **Fix for the A8 full-lab defect (a39ab1a):**
  - `eidos.project.command` 1.0.7 stages every declared input as `<port><ext>` unless `config.files` maps it. The rule and an example are documented in its summary and `config_schema`.
  - It records `staged_inputs`, and its failure message lists the work-dir contents and the stderr tail.
  - The lab-free driver applies the identical rule.
  - The catalog and investigator context show optional inputs and a 600-character summary, built by the shared `catalog_row`.
  - Job errors in the context show category and message, not a 600-character clipped dump.
  - Regression test: `tests/components/test_project_command_staging.py`. A fixture policy that reads only the packed context builds a working call.
- **REQUESTS processed:**
  1. Merged; the note on pre-b7d9ed1 receipts is in the matrix.
  2. Temporal download falls back to the GitHub REST asset endpoint (02ef6e8, digest still pinned).
  3. Already fixed.
  4. and 5. a39ab1a.
  6. Recorded but not done: `build_fixtures.py` rewrites tracked fixtures when numpy is present, and there is no pytest for the Seatbelt probes.
  7. Matrix updated.

## Round 15 (2026-11-08): full-lab re-run findings 8–10 fixed

- **Merge:** `mac/local-qualification` (648cd6b) merged again; it carries the re-run receipt and STATUS.
- **Re-run summary:** the a39ab1a fix worked (baseline val acc 0.838 on the first try), then the run stalled at turn 3.
- **Fixes (851a019):**
  - **8.** `execute_decision` reports `nothing_outstanding` together with the failed calls. The turn then counts it like a refused proposal (patched `failed-calls-no-wait-v1`), so the bounded retry-then-surface rule applies.
  - **9.** `environment` must be the name of a registered environment. Anything else is refused at acceptance, and the message lists the names. The names are also shown in the context, and the tool text gives the type and an example.
  - **10.** New tool `author_source`: candidate project files that become `run_component inputs.source`. It is the same in the lab and the lab-free driver. `author_component` (build a component) exists only in the lab, and the driver refuses it.
- **Tests:**
  - turn-recovery: 10 passed, including the failed-call wait and the environment/`author_source` tests;
  - workflows: 10 passed, including `test_wait_after_failed_calls_is_not_a_park` and the replay of recorded histories;
  - unit + MCP + reporting: 68 passed, 1 skipped;
  - staging regression: 2 passed.

## Round 16 (2026-11-08): re-run 2 findings 11–13 fixed

- **Merge:** `mac/local-qualification` (e07d905) merged.
- **Fixes (8ad9519):**
  - **11.** `investigation_view.created_artifacts` lists the artifacts the investigator authored, with the turn and which jobs used them. The packed context shows them as "Artifacts you created". The tool guide says an authored id appears in the next turn: same-decision references were **not** implemented, and the tool guide says so.
  - **12.** The bounded-outcome exemption is removed, so a request-budget-exhausted outcome that waits with nothing outstanding is refused and goes through the bounded retry-then-surface rule.
  - **13.** The tool-free final request now gets exactly the reserved remainder. The cap is exact: the test asserts calls == 3 for a cap of 3, where the old code made 4.
- **Tests:**
  - turn-recovery: 12 passed, including the context-only test that authors a source and reads its id next turn, and the bounded-outcome refusal;
  - unit + MCP + reporting: 68 passed, 1 skipped.

## Round 17 (2026-11-08): final handoff

- **Merge:** `mac/local-qualification` (aae1d25) merged.
- **Re-run 3:** the full-lab D02 run concluded autonomously with no intervention. 26 requests, USD 0.74, 12 jobs, no claim. It is an exploration-only observation, not scientific evidence.
- **Docs:** the 4-run table and the final handoff are at the top of HANDOFF.md, and A8 is updated.
- **Verification:** unit + integration + MCP + reporting **140 passed, 4 skipped** on the merged head (3 skips are macOS-only; 1 is the live-lab MCP test).

## Round 19 (2026-11-09): v1.0.1 design-conformance release

A check against the design (Revision 4) found three unrecorded gaps. All three are closed on branch `release/1.0.1`:
- **mypy in CI:**
  - `[tool.mypy]` in `pyproject.toml`, and `scripts/ci/mypy.sh` with a 3.9 target for the worker packages;
  - 91 errors fixed to 0, with no new `type: ignore` and no behaviour change.
- **Wire JSON Schemas (ADR 0008):**
  - `src/eidos/boundary/wire.py` is the source of truth;
  - 3 schema files and 15 golden fixtures from the SDK;
  - fixtures are checked by jsonschema in the server environment, and SDK output is checked against them in the 3.9/pydantic<2 matrix;
  - `publish_outputs`, the runner, the assessor and component registration validate received messages;
  - the A10 row in `ACCEPTANCE_MATRIX.md` now matches the code.
- **Deferred adapters (ADR 0009):** PyGlove and Composer are deferred. CheckList is partly adopted (raw-prediction reuse, paired expectations, restore-invariance behaviour check).
- Versions: `eidos-lab` 1.0.1, `eidos-wire` 1.0.1. Release notes: `docs/release/RELEASE_NOTES_v1.0.1.md`.

## Round 18 (2026-11-08/09): v1.0.0 release session (local Mac controller)

Gates and evidence: `ACCEPTANCE_MATRIX.md` § E; receipts in `docs/release/evidence/`; Mac details in `docs/mac/STATUS.md` § v1.0.0 release session.
- **Licence and scope:** Apache-2.0 everywhere (G1); `docs/release/SUPPORTED.md` (G2).
- **Isolation honesty (G3):** a same-user Seatbelt receipt never satisfies a sealed plan; macOS labs refuse sealed plans.
- **Review open items (G7):**
  - migration 0004 fences old writers;
  - a reservation double release found and fixed;
  - quiet-wait rollover fixed;
  - the CI setup-uv fallback no longer masks failures.
- **Real-model A8 (G5):** three Sonnet runs. Runs 1 and 2 exposed six Eidos defects, each fixed with tests. Run 3 concluded with every A8 check passing.
- **Local operations (G6):**
  - disk exhaustion on a real 2 GiB APFS image, service SIGKILLs and abrupt stop all pass on macOS;
  - defects fixed: PostgreSQL supervision, the doctor disk check, the Temporal port-block limit, quoting in the ops scripts;
  - machine sleep and restart is an owner item.
- **Local suite:** 158 passed, 4 skipped at `329d9c3`; it is re-run on the RC SHA (G9). The switch journey on macOS passed 154/154.

How to run, additions:
- `PYTHONPATH=src:packages/eidos-wire/src .venv/bin/python scripts/qualify/live_provider_check.py --model openai-compatible:<model> --base-url http://127.0.0.1:11535/v1 --request-timeout 900`
- `EIDOS_HOME=<lab> <lab venv>/bin/python tests/journeys/a8_live.py --model <model> [--base-url ...] --price '{"input": 2, "output": 10}' --model-usd <cap> ...`; add `--setup-only` once first.
- `LAB=<venv>/bin/lab WORK=<ssd dir> bash scripts/qualify/disk_full_check.sh`, and the same for `service_failure_check.sh`.
- `bash scripts/qualify/sleep_restart_check.sh start|--verify` (owner).

## Next concrete action

1. Owner: machine sleep and restart (`REQUESTS.md` R1 on the SSD; `scripts/qualify/sleep_restart_check.sh`).
2. With both receipts `ok`: Phase 5 of the release brief. Bump to 1.0.0, get CI green, tag `eidos-v1.0.0`, merge PR #1 with a merge commit, and create the GitHub release with the wheels.
3. Later, separately: A16, the scientific evaluation (`eval-*` tags).
