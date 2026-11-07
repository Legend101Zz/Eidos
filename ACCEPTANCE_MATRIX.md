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
| A1 | Install and operate: fresh install manages services, imports project without dependency upgrades, CLI/MCP, HTML reports, loadable model export | `src/eidos/supervisor`, `src/eidos/cli`, `src/eidos/mcp`, `src/eidos/reporting`, `packaging/` | `tests/journeys/test_install_journey.py` (Linux); macOS | not-started; macOS unqualified |
| A2 | Component and run contracts: unmodified custom model + thin adapter, new scorer published and reused on 2nd model; detect dependency conflicts, absent hook phases, changed metric semantics | `src/eidos/domain/components.py`, `src/eidos/domain/resolver.py`, `packages/eidos-worker-sdk`, `components/` | `tests/unit/test_resolver.py`, journey | not-started |
| A3 | Charter/success-rule integrity: commit before exploration; reject plan/template override and steering bypass; exposure-informed amendment preserving both rules + verdicts across new study ID | `src/eidos/domain/charter.py`, `src/eidos/application/commands.py` | `tests/unit/test_charter.py`, `tests/integration/test_charter_api.py` | not-started |
| A4 | Template/assessor qualification: executed planted-effect + null-case receipts required; statistical bounds; human signature alone rejected; requalify; suspend after withdrawal | `src/eidos/domain/qualification.py`, `src/eidos/application` | unit + integration | not-started |
| A5 | Claims and evidence plans: compile against pinned charter, freeze, execute controls, assess realized conditions; withdraw scorer → affected support, unaffected alternate justification, appended reassessment | `src/eidos/domain/claims.py`, `support.py` | unit + journey | not-started |
| A6 | Single authoritative write path: planner/assessor/worker credentials cannot mutate ledger tables (PG grants); revisions, input identities, idempotency, fences; lost reply → same receipt | `src/eidos/persistence/grants.sql`, `src/eidos/api` | `tests/integration/test_grants.py`, `test_idempotency.py` | not-started |
| A7 | Checkpoint branching: fork at two stages, no-change continuation + deliberate migration, parent isolation; optimizer-alias fixture; additional vs inherited budget; siblings not independent seeds | `packages/eidos-pytorch`, `src/eidos/domain/branching.py` | `tests/worker_compat/test_branching.py`, journey | not-started |
| A8 | Bounded autonomous investigation: diagnosis, new component, trial/branch, assessment, export via internal loop; disconnect; crash after committed tool effect; same recorded key; hand off; one logical admission; charges visible; next turn reconstructs from evidence | `src/eidos/orchestration`, `src/eidos/investigator` | `tests/journeys/test_autonomous_switch_order.py` | not-started (fixture-only at best without provider key) |
| A9 | Runtime compatibility and recovery: real providers, large catalogs, cancellation, component change between turns, overflow rejection, parent history rollover, child/runtime + parent workflow upgrades; budgets and live jobs survive | `src/eidos/orchestration`, `src/eidos/investigator/runtime_*` | `tests/integration/test_workflows_*.py`; replay tests | not-started; real providers unqualified (no key) |
| A10 | Worker independence: SDK in old/current project envs incl. a Pydantic v1 project; no server deps installed; torch unchanged; wire fixtures across schema versions | `packages/eidos-wire`, `packages/eidos-worker-sdk`, `schemas/` | `tests/worker_compat/run_matrix.sh` | not-started |
| A11 | Achieved isolation: positive and hostile probes per advertised profile; same-user receipt cannot satisfy sealed plan; missing/false receipt blocks protocol-passed; candidate never gets scorer authority | `src/eidos/runtime/isolation`, sealed runner | `tests/integration/test_isolation_probes.py` | not-started; macOS/Lima VM unqualified |
| A12 | Turn evaluation: reconstruct historical view, alternate context/model policies without future leakage, isolated benchmark commands, unexecuted counterfactuals distinguished | `src/eidos/investigator/replay.py` | unit + integration | not-started |
| A13 | Training and architecture changes: weight edit, stateful learning rule, architecture patch, synthetic data generation, whole custom trainer; restore classes per device profile; strict-load/wrong-builder fixture fails behavioral acceptance | `packages/eidos-pytorch`, `components/`, `examples/` | worker tests + journey | not-started; CUDA/MPS unqualified |
| A14 | Operator/API integrity: authenticated HTTP + stdio, capability/version conformance, cross-project access/exfiltration, lost-reply dedup, stale revisions, cooperative cancellation, detach/reattach; real external clients | `src/eidos/api`, `src/eidos/mcp` | `tests/integration/test_mcp_*.py` | not-started; real Claude/Codex clients unqualified |
| A15 | Local operations: disk exhaustion, restart/sleep, service failures, installation upgrade, backup/restore on fresh lab dir; reconcile jobs; show missing progress, unknown spend, restoration limits | `src/eidos/supervisor` | `tests/journeys/test_ops_*.py` | not-started |
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
| Client disconnects after start → same command key returns same receipt | `tests/integration/test_idempotency.py` | not-started |
| LLM timeout after possible charge → request ID + possible spend recorded | `tests/unit/test_runtime_accounting.py` | not-started |
| Investigator dies during training → reattach via handle | journey | not-started |
| Node preempted / rank lost | single-node: process kill + reconcile | not-started; distributed unqualified |
| Duplicate/reordered/stale completion → epoch + unique publication key | `tests/integration/test_fencing.py` | not-started |
| Storage/DB lag → verify staged content before publish | `tests/integration/test_artifacts.py` | not-started |
| Human changes question / withdraws release | integration | not-started |
| Assessor restart / evidence twice → dedupe by plan+evidence digest+assessor | integration | not-started |
| Assessment method withdrawn during confirmation | integration | not-started |
| Branch request loses reply | integration | not-started |
| Controller upgrade during multi-day work → replay tests, versioning | `tests/integration/test_workflow_replay.py` | not-started |
| Turn dies after tool effect commits → same key, existing receipt | journey | not-started |
| Payload overflow / history limit → reject + rollover | integration | not-started |
| Component/investigator upgrade during turn | integration | not-started |
| Machine sleep/reboot/power loss → reconcile, don't trust PID | ops journey | not-started |
| Disk full / incomplete backup / service corruption | ops journey | not-started |
