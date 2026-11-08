# Eidos local v1 — acceptance matrix

Source: `docs/design/eidos-claude-handoff/architecture/model-research-harness-design.md` (Revision 4),
section "Complete local v1 and scientific acceptance" plus the build-assignment requirements 1–9 and
the "Failures a production installation must handle" table.

Status vocabulary: `not-started` · `implemented` (code exists, not yet exercised end to end) ·
`runtime-verified` (exercised against real PostgreSQL/Temporal/processes in this sandbox) ·
`fixture-only` (verified with deterministic fixture providers, not a real LLM) ·
`unqualified` (needs hardware/OS/provider/client not available here) · `scientifically-unproven`.

Paths are relative to the repository root. Verification commands are listed in
`IMPLEMENTATION_STATUS.md` with dates and results.

## A. Release acceptance areas (design §"Complete local v1")

| # | Area | Code location | Verification | Status |
|---|---|---|---|---|
| A1 | Install and operate: fresh install manages services, imports project without dependency upgrades, CLI/MCP, HTML reports, loadable model export | `src/eidos/supervisor`, `src/eidos/cli`, `src/eidos/mcp`, `src/eidos/reporting`, `packaging/` | `lab init/start/doctor/status`, backup/restore runs (2026-11-07, sandbox); D01 export + fresh reload | runtime-verified on hosted Linux; macOS unqualified |
| A2 | Component and run contracts: unmodified custom model + thin adapter, new scorer published and reused on 2nd model; detect dependency conflicts, absent hook phases, changed metric semantics | `src/eidos/domain/components.py`, `src/eidos/domain/resolver.py`, `packages/eidos-worker-sdk`, `components/` | unit resolver tests; `tests/journeys/switch_journey.py` (EIDOS_JOURNEY=1; 149 checks passed 2026-11-07; record docs/dev-runs/switch-journey.md) steps 1, 6 (torch>=99 conflict, absent phase, scorer reused on image project) | runtime-verified |
| A3 | Charter/success-rule integrity: commit before exploration; reject plan/template override and steering bypass; exposure-informed amendment preserving both rules + verdicts across new study ID | `src/eidos/domain/charter.py`, `src/eidos/application/commands.py` | `tests/unit/test_domain_rules.py`, `tests/integration/test_command_path.py` (override, steering bypass, amendment across new study ID) | runtime-verified |
| A4 | Template/assessor qualification: executed planted-effect + null-case receipts required; statistical bounds; human signature alone rejected; requalify; suspend after withdrawal | `src/eidos/domain/qualification.py`, `src/eidos/application` | unit gates; `tests/journeys/switch_journey.py` (EIDOS_JOURNEY=1; 149 checks passed 2026-11-07; record docs/dev-runs/switch-journey.md) step 5 (promotion refused without/with failing receipt; promoted after passing author suites) | runtime-verified. Qualification suites are author-written, but the invariants they rely on were adversarially reviewed by a separate read-only reviewer; 11 confirmed findings were fixed with regression tests, and the rest are open (`docs/dev-runs/adversarial-review-2026-11-07.md`). Independent scientific controls still pending. |
| A5 | Claims and evidence plans: compile against pinned charter, freeze, execute controls, assess realized conditions; withdraw scorer → affected support, unaffected alternate justification, appended reassessment | `src/eidos/domain/claims.py`, `support.py` | `tests/journeys/switch_journey.py` (EIDOS_JOURNEY=1; 149 checks passed 2026-11-07; record docs/dev-runs/switch-journey.md) step 5: plan frozen against charter rev 1, 6 arms, AssessmentWorkflow → inconclusive (guardrail), withdrawal → affected/suspended/reassessment, alternate justification | runtime-verified |
| A6 | Single authoritative write path: planner/assessor/worker credentials cannot mutate ledger tables (PG grants); revisions, input identities, idempotency, fences; lost reply → same receipt | `src/eidos/persistence/grants.sql`, `src/eidos/api` | `tests/integration/test_command_path.py` (grants, triggers, concurrent key, lost reply, fencing) | runtime-verified |
| A7 | Checkpoint branching: fork at two stages, no-change continuation + deliberate migration, parent isolation; optimizer-alias fixture; additional vs inherited budget; siblings not independent seeds | `packages/eidos-pytorch`, `src/eidos/domain/branching.py` | eidos-pytorch tests; `tests/journeys/switch_journey.py` (EIDOS_JOURNEY=1; 149 checks passed 2026-11-07; record docs/dev-runs/switch-journey.md) step 3 (branches at 45/180, exact no-change fingerprint, edit 0.2625→0.844 / 1.0, parent digest unchanged, siblings share history) | runtime-verified (CPU) |
| A8 | Bounded autonomous investigation: diagnosis, new component, trial/branch, assessment, export via internal loop; disconnect; crash after committed tool effect; same recorded key; hand off; one logical admission; charges visible; next turn reconstructs from evidence | `src/eidos/orchestration`, `src/eidos/investigator` | `test_turn_recovery.py`; `tests/journeys/switch_journey.py` (EIDOS_JOURNEY=1; 149 checks passed 2026-11-07; record docs/dev-runs/switch-journey.md) step 7 (scripted policy authors+publishes a component, branch/edit, claim, export; investigator-worker SIGKILL → one admission per call, charges visible); D01/D02/D12 runs | fixture-only; real LLM unqualified |
| A9 | Runtime compatibility and recovery: real providers, large catalogs, cancellation, component change between turns, overflow rejection, parent history rollover, child/runtime + parent workflow upgrades; budgets and live jobs survive | `src/eidos/orchestration`, `src/eidos/investigator/runtime_*` | `tests/integration/test_workflows.py` (rollover, replay guard, retries/cancel), `tests/unit/test_pydantic_ai_runtime.py` | implemented. Anthropic adapter qualified for development, lab-free (live receipts #1–#3; #3 passed 8/8). OpenAI-compatible endpoints unqualified. No real-LLM lab investigation yet. |
| A10 | Worker independence: SDK in old/current project envs incl. a Pydantic v1 project; no server deps installed; torch unchanged; wire fixtures across schema versions | `packages/eidos-wire`, `packages/eidos-worker-sdk`, `schemas/` | `tests/worker_compat/run_matrix.sh` | runtime-verified (py3.9+pydantic1, py3.12+torch2.15) |
| A11 | Achieved isolation: positive and hostile probes per advertised profile; same-user receipt cannot satisfy sealed plan; missing/false receipt blocks protocol-passed; candidate never gets scorer authority | `src/eidos/runtime/isolation`, sealed runner | `tests/integration/test_isolation_probes.py`; sealed job on live lab | linux:bwrap runtime-verified, including separate-identity with a candidate-readable interpreter under /opt (it fails, and is reported, for root-owned interpreters). macos:seatbelt-sealed is implemented: profile generation and selection are unit-tested (`tests/unit/test_seatbelt_policy.py`), and the hostile probes plus a same-user planted-effect control run in the macOS CI job (`scripts/ci/seatbelt_probes.py`). They **passed** on GitHub macOS arm64 in CI run `42429763457` on 2026-11-08 (GitHub-runner evidence).. macOS researcher machine and Lima are unqualified. |
| A12 | Turn evaluation: reconstruct historical view, alternate context/model policies without future leakage, isolated benchmark commands, unexecuted counterfactuals distinguished | `src/eidos/investigator/replay.py` | `tests/integration/test_turn_recovery.py::test_offline_replay...` | runtime-verified |
| A13 | Training and architecture changes: weight edit, stateful learning rule, architecture patch, synthetic data generation, whole custom trainer; restore classes per device profile; strict-load/wrong-builder fixture fails behavioral acceptance | `packages/eidos-pytorch`, `components/`, `examples/` | components tests; `tests/journeys/switch_journey.py` (EIDOS_JOURNEY=1; 149 checks passed 2026-11-07; record docs/dev-runs/switch-journey.md) steps 3-4 (ema rule, builder change, generator, wrong builder behavior_match=false with strict load ok); task bundles via whole-project command | runtime-verified on CPU; CUDA/MPS unqualified |
| A14 | Operator/API integrity: authenticated HTTP + stdio, capability/version conformance, cross-project access/exfiltration, lost-reply dedup, stale revisions, cooperative cancellation, detach/reattach; real external clients | `src/eidos/api`, `src/eidos/mcp` | `tests/mcp/*`, `tests/integration/test_command_path.py` | runtime-verified with SDK client; external clients unqualified |
| A15 | Local operations: disk exhaustion, restart/sleep, service failures, installation upgrade, backup/restore on fresh lab dir; reconcile jobs; show missing progress, unknown spend, restoration limits | `src/eidos/supervisor` | backup/restore/upgrade runs; `test_disk_pressure.py`; `tests/ops/{runner_reattach,abrupt_stop,checkpoint_and_stop}.sh` | runtime-verified on Linux (sleep/suspend not exercised) |
| A16 | Scientific release: locked success rules, blinded results on different tasks/hardware, losing candidates and nulls, reproduction package | external evaluation workstream | — | out of builder scope; scientifically-unproven |

## B. Assignment requirements 1–9 (cross-reference)

| # | Requirement | Covered by |
|---|---|---|
| B1 | One authoritative command/commit path, PG grants | A6 |
| B2 | Durable coordination, bounded turns, persisted decisions/ordered calls, stable command keys, job lifetimes independent of turns, bounded payloads/histories, rollover + upgrades | A8, A9 |
| B3 | Versioned component contract, DI, immutable run bindings, artifact ops / training extensions / decision policies / managed providers, bounded specialist LLM calls | A2, A13 |
| B4 | Thin stdlib worker packages, whole-trainer/subprocess escape hatch | A10, A13 |
| B5 | Charters before exploration, plan inheritance, exposure history, executed planted/null qualification | A3, A4 |
| B6 | Separate roles, realized isolation receipts, sealed vs same-user, no scorer authority for candidate, assessor checks realized conditions | A11, A5 |
| B7 | Checkpoint branches: immutable parents, state mappings, restore classes, behavioral checks, cost accounting, withdrawal/reassessment | A7, A5 |
| B8 | Versioned context policies, turn specs, horizons, snapshots, decisions/receipts, offline evaluation without leakage | A12 |
| B9 | Local installation: supervisor, regular Temporal Server (persistent config), local PG, artifacts, per-project envs, CLI, MCP, HTML reports, doctor/status/logs, restart/reconciliation, upgrade, backup/restore | A1, A15 |

## C. Interruption table (design §"Failures a production installation must handle")

| Interruption | Test | Status |
|---|---|---|
| Client disconnects after start → same command key returns same receipt | `test_command_path.py`, `tests/mcp/test_mcp_live.py` | runtime-verified |
| LLM timeout after possible charge → request ID + possible spend recorded | `tests/unit/test_pydantic_ai_runtime.py::test_provider_error_maps_to_retryable_failure_with_possible_spend`; live receipts #2–#3 (timeout classified retryable) | unit-verified (fixture model) + live classification verified |
| Investigator dies during training → reattach via handle | `tests/ops/runner_reattach.sh` (runner killed mid-job; same attempt finished, 2026-11-07) | runtime-verified (runner crash); investigator crash in journey |
| Node preempted / rank lost | single-node: process kill + reconcile | not-started; distributed unqualified |
| Duplicate/reordered/stale completion → epoch + unique publication key | `test_command_path.py::test_fencing...` | runtime-verified |
| Storage/DB lag → verify staged content before publish | `tests/integration/test_artifacts.py` | not-started |
| Human changes question / withdraws release | integration | not-started |
| Assessor restart / evidence twice → dedupe by plan+evidence digest+assessor | integration | not-started |
| Assessment method withdrawn during confirmation | integration | not-started |
| Branch request loses reply | integration | not-started |
| Controller upgrade during multi-day work → replay tests, versioning | `test_workflows.py::test_recorded_histories_replay...`, `lab upgrade` | partially verified |
| Turn dies after tool effect commits → same key, existing receipt | `test_turn_recovery.py` | runtime-verified (activity level); worker kill in journey |
| Payload overflow / history limit → reject + rollover | `test_workflows.py`, envelope checks | runtime-verified (dev server) |
| Activity heartbeat lost / worker vanishes mid-activity / start-to-close exceeded / retry exhaustion | `tests/integration/test_temporal_realtime.py` (real timers; dev server and the lab's regular server, 4/4 on 2026-11-08) | runtime-verified |
| Component/investigator upgrade during turn | `test_workflows.py::test_inflight_turn_with_invalid_proposal_replays_after_upgrade` (workflow code upgrade with an in-flight turn) | partially verified (component release swap during a live turn not exercised) |
| Machine sleep/reboot/power loss → reconcile, don't trust PID | `tests/ops/abrupt_stop.sh` (kill -9 services ± job; boot id + start time) | runtime-verified for kill/power-loss equivalent; sleep not exercised |
| Disk full / incomplete backup / service corruption | `test_disk_pressure.py`; backup refuses missing blobs | partially verified |

## D. Known limitations (updated 2026-11-08, after eidos-v0.4.0; see also `HANDOFF.md`)

Closed since v0.1.0; each item names its evidence:
- **checkpoint-and-stop pause/cancel policy:** SIGUSR1 → checkpoint at a committed step → `cancelled` with a published checkpoint. Evidence: `tests/ops/checkpoint_and_stop.sh`, verified live at step 22,654.
- **Multi-step evidence-plan pipelines:** DAG admission, reference resolution and blocking on a failed predecessor. Evidence: `tests/integration/test_plan_pipelines.py`.
- **Training extensions bound as published releases:** pinned by digest, mounted by the launcher, phase/writer conflicts rejected, retired releases refused. Evidence: `tests/integration/test_extensions.py`.
- **Distinct candidate identity:** passes with an interpreter under /opt. Evidence: `test_isolation_probes.py`.
- **Abrupt stop / power-loss equivalent:** both scenarios pass. A: services killed, then reattach on the same attempt. B: services and the job killed, then lost-attempt reconciliation and a new fenced attempt. Evidence: `tests/ops/abrupt_stop.sh`, log in `docs/dev-runs/abrupt-stop-2026-11-07.log`.
- **History and payload stress, plus a wake-storm fix:** the coordinator now rolls over even while waiting on signals. Evidence: `test_workflows.py::test_stress_...`, which passes together with the frozen-history replay.
- **Worker death:** a job-private resume directory persists across attempts; `eidos.pytorch.train` 1.2.0 resumes from the latest committed checkpoint. Verified live: worker `kill -9`, the new attempt resumed at step 28,000 and finished. A failed run's staged checkpoints are published as citable partial results, branchable only if vouched.
- **Live-provider receipt #1 defects fixed:** strict-mode-safe args, required-arg validation, partial usage on failure, 4xx non-retryable, bounded request budget.
- **All 14 public development cards:** reference smoke through Eidos jobs plus reference-assisted builder checks; `qualify.py` 27/27. Evidence: `docs/dev-runs/research-cards.md`.

- **Real-time Temporal timeouts and retries (round 6):** heartbeat timeout, start-to-close, retry policy, non-retryable errors, and a worker SIGKILLed mid-activity taken over by another worker. Evidence: `tests/integration/test_temporal_realtime.py`, on the dev server and the regular server.
- **macOS sealed candidate profile (round 6):** `macos:seatbelt-sealed` is implemented. Its hostile probes and the planted-effect control passed on a GitHub macOS arm64 runner (CI run `42429763457`). See "Still open" for its limits.

- **Round 7 (eidos-v0.5.0, CI green on `e822744`):**
  - the macOS assessor runs under Seatbelt;
  - adversarial review 2: five findings fixed with regression tests (`docs/dev-runs/adversarial-review-2-2026-11-08.md`);
  - A15 installation upgrade across releases: v0.1.0 wheels → head, 33 checks (`scripts/ci/upgrade_across_releases.sh`, CI job);
  - A1/A14 clean-install journey through the CLI and the MCP SDK client: D01 fixture investigation and validated answer, branch, export (`scripts/journeys/e2e_journey.sh`, CI job). The qualified assessment in this journey is being added.

Still open:
- **Adversarial review:** all listed open items are fixed (round 5). What remains is platform-bound.
  - A distinct candidate identity needs a readable interpreter.
  - On macOS, seatbelt runs as the same user, so `separate-identity` fails; mach IPC is unfiltered; `sandbox-exec` is deprecated; the assessor runs same-user; Lima VM is not implemented.
  - See `docs/dev-runs/adversarial-review-2026-11-07.md`.
- **macOS:** an owner-run qualification kit is ready (`scripts/qualify/macos_owner_kit.sh`, `docs/ops/MACOS_OWNER_QUALIFICATION.md`). Its Linux dry run passes every step; the owner decides whether to run it.
- **Real LLM providers:** a script is ready for the controller (`scripts/qualify/live_provider_check.py`).
- **Not qualified:** macOS on a researcher machine (a runner smoke job exists), Lima VM, CUDA/MPS, and external MCP hosts (Claude Code/Desktop, Codex). The official SDK client is verified over stdio and HTTP.
- **Installation upgrade:** migration 0001 → 0002 (`records_project_kind_created`) applied to a lab with existing data, and the running-lab path (stop → migrate → restart) left records and investigations intact. The frozen workflow history replays on the changed coordinator.
- **Performance targets:** a smoke run met them on this host (p95 command 6.4 ms under training load; evidence visible ≤ 1.3 s; `docs/dev-runs/perf-smoke-2026-11-07.md`). Not measured on a declared reference machine.
- **Not exercised:** sleep/suspend, multi-day runs and distributed training.
- **Scientific release (A16):** out of builder scope.
