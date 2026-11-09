# Changelog

All notable changes to Eidos. Tags are annotated, created only on CI-green commits, and never moved. Development tags `eidos-v0.x` were freezes, not releases.

## 1.0.1 (2026-11-09)

A design-conformance release. It closes three gaps that a check against the architecture (Revision 4) found and that nobody had recorded. It brings no new features and no ledger migration. Details: `docs/release/RELEASE_NOTES_v1.0.1.md`.

### Type checking (mypy in CI)
- `pyproject.toml` has a `[tool.mypy]` configuration: the server tree and the worker packages at Python 3.12, `warn_unused_ignores`, `no_implicit_optional`, and missing-import tolerance only for `torch` and `numpy` (absent from the server environment).
- `scripts/ci/mypy.sh` also checks `eidos-wire`, `eidos-worker-sdk` and `eidos-pytorch` with a **Python 3.9 target**, so 3.10+ syntax or stdlib APIs cannot slip into the worker packages. It uses a pinned mypy 1.19.1, the last release that targets 3.9. The `lint-and-unit` CI job runs it.
- All 91 errors are fixed with annotations and narrowing, and no new `type: ignore` was added. Three of the four older ones are gone:
  - two in `eidos-wire` were unused;
  - `LabClient.upload` now narrows `path` instead of ignoring it.

  The one left is in `eidos-pytorch`: a fallback for a missing `typing.Protocol`, which cannot happen on Python 3.9+. It stays so that package's code does not change. The check found no runtime bug, and behaviour is unchanged.
- `eidos-wire`: the TOML manifest loader now selects `tomllib` with a `sys.version_info` check, which a 3.9 type check can follow. Its behaviour is the same, including the clear error on Python < 3.11.

### Versioned wire JSON Schemas (design § "The worker compatibility promise"; ADR 0008)
- New Pydantic v2 boundary models (`src/eidos/boundary/wire.py`, server side only) generate `schemas/eidos-wire/v1/{invocation,output-manifest,component-manifest}.schema.json` (JSON Schema 2020-12) through `scripts/dev/gen_wire_schemas.py`. Each file carries an `x-eidos` header (generator, wire major), and generation is deterministic. CI fails if regenerating the schemas changes anything.
- Golden fixtures in `tests/fixtures/wire/v1/` are emitted by the real worker SDK code paths (`scripts/dev/emit_wire_fixtures.py`): valid messages, newer-minor messages with unknown fields, and `invalid-*` messages.
  - In the server environment, the valid fixtures validate against the schemas and the invalid ones fail. `jsonschema` is a dev dependency only.
  - In the worker-compat matrix (Python 3.9 with pydantic<2), the SDK emits exactly the golden fixtures. The worker packages stay standard-library only.
- **The server validates received messages against the schema:**
  - the command service's `publish_outputs`, which refuses a violation as `INVALID` with field-level `problems`;
  - the runner's and the assessor's manifest reads;
  - component registration.

  Before, a manifest with a mistyped field passed `eidos_wire`'s presence checks. For example, a list `receipt` or a numeric `card` was accepted and could fail later with an untyped error. Unknown fields from a newer minor version are still accepted.
- The schema major (1) and the supported worker Python range (3.9+) are stated in `docs/author-guide/worker-sdk.md` and `docs/release/SUPPORTED.md`.

### Deferred integrations recorded (ADR 0009)
- The design's optional **PyGlove** and **MosaicML Composer** adapters are recorded as **deferred for v1**. The supported path is ordinary PyTorch source patches and author-owned trainers. The ADR gives reversal conditions: a user project that needs one, pinned versions, and hook-phase fixtures.
- **CheckList** is recorded as partly adopted: raw predictions are recorded and reused by scorers, `lab.pairs.by_group` provides paired expectations, and `eidos.pytorch.behavior-check` provides one restore-invariance contract. There is no general behaviour-suite capability yet.
- `docs/release/SUPPORTED.md` has a new "Deferred integrations" section.

### CI
- Pushes to `release/**` branches are built.
- `lint-and-unit` adds the mypy step and the schema and fixture regeneration check.

### Packages
- `eidos-lab` 1.0.1. `eidos-wire` 1.0.1 (typing-only changes and the `tomllib` selection above; wire contract unchanged, still `eidos-wire/1`). `eidos-worker-sdk` 1.1.0 and `eidos-pytorch` 1.1.0 are unchanged; only their tests changed.

## 1.0.0 (2026-11-09)

The final v1 software release. It is the same as 1.0.0-rc1 except for the version string and one CI-only change. Released by the owner's decision without the hardware sleep/restart check (R1). Machine sleep and OS restart recovery are a documented limitation; abrupt-stop and service-kill recovery are verified.

### CI
- CI only, no package change: the macOS arm64 job now sets up Go when the Temporal archive cache is cold, so the source-build fallback works there too. The first CI run on `main` (cold cache) failed when the pinned release download returned HTTP 504 and the runner had no Go. `scripts/ci/temporal_server.sh` now says so clearly when no Go toolchain is on PATH.

## 1.0.0-rc1 (2026-11-09; package version 1.0.0rc1)

The v1 **software** release: acceptance areas A1–A15 within the scope of `docs/release/SUPPORTED.md`. It makes **no scientific claim**: A16, the scientific comparison, is a separate later release with `eval-*` tags. Details and evidence: `docs/release/RELEASE_NOTES_v1.0.0.md`.

### Licence and scope
- Licensed under **Apache-2.0** (`LICENSE`, `NOTICE`). Every package declares `License-Expression: Apache-2.0` and ships both files. `research/public/` is excluded.
- New `docs/release/SUPPORTED.md`: Linux x86_64 and macOS arm64; CPU, plus MPS on macOS; Anthropic and OpenAI-compatible providers; Claude Code, Codex CLI and the MCP Python SDK; sealed evaluation on Linux bwrap only. Out of scope, with reasons: Windows, CUDA, distributed training, Lima VM, Claude Desktop.

### Isolation honesty (G3)
- **A same-OS-user Seatbelt receipt no longer satisfies a plan that requires sealed execution.** Profiles now carry `same_os_user` and `hostile_code`. Only `linux:bwrap-sealed` (and the unimplemented Lima profile) are hostile-code profiles.
- On a lab without a hostile-code profile (macOS Seatbelt, or no sealed profile), `freeze_evidence_plan` and `launch_evidence_plan` refuse plans that require sealed execution, with a clear message.
- Isolation receipts and the assessor's receipt record `same_os_user` and `hostile_code_isolation`, derived from the profile name. For job receipts the command service derives them and overwrites any value in the launcher's payload. For the assessor, the assessment activity derives them. Assessments also record each arm's realized isolation profiles and `same_os_user`.
- Fixed: a requirement that named only a profile (`required_isolation.profile` with no controls) was satisfied by any receipt.

### Ledger and upgrades (G7)
- **Migration 0004, writer-generation fence.** Writers that do not declare the current generation are refused on every ledger table. So an old-version writer still running after an upgrade can no longer collide with the migration-0003 event counter or corrupt state. The current command service declares generation 4.
- Fixed a double release of model-budget reservations. `complete_turn` released a turn's unsettled reservations under a separate ref, so a late settlement from a zombie attempt released the same amount again and freed budget held by other reservations.

### Orchestration
- A quiet wait that rolls over (Continue-As-New) no longer starts a model turn with no news, and the max-idle timer counts from the original wait start. The change is patched (`quiet-rollover-keeps-waiting-v1`), and recorded histories still replay.

### Investigator (found preparing the real-model A8 journey)
- The investigation view and the packed context list:
  - the results of the investigator's own recent calls (the `trial_id` returned by `plan_trial`/`plan_branch`, plan ids, validation jobs, export ids, and call errors);
  - its evidence plans with their assessments;
  - the approved (qualified) protocols.

  Before, these ids reached the investigator only inside events, which the packer drops, so `launch_trial`, `launch_branch` and `freeze_evidence_plan` could not be called from evidence.
- The tool guide documents the evidence-plan draft and the trial/branch arguments.

### Defects found by the real-model A8 runs (claude-sonnet-5-5), fixed with tests
- `plan_branch` with a malformed `migration` or a prose `intervention` raised a `TypeError` (HTTP 500). It is now `INVALID` and names the parent's state entries, with an example.
- `author_component` with a malformed manifest raised a JSON decode error (HTTP 500). It is now `INVALID` with the parse error.
- A coordinator that stopped on budget exhaustion or the turn limit left the investigation `active`. The reconciler then restarted it at turn index 0, which replayed turn 1's recorded `begin_turn` receipt and looped on `COMMAND_KEY_CONFLICT`.
  - Now the investigation is marked as needing the operator (patched `terminal-outcome-needs-input-v1`), and the reconciler skips it.
  - A re-dispatched coordinator continues after the ledger's last turn.
- The per-decision `model_usd` reservation priced every token at the output rate. That is about 3× the real worst case, and it ended a run budget-exhausted with a third of its budget unused. The reservation is now a tight worst case.
- The tool guide now documents the component entry calling convention, training-extension binding, and the branch migration, stream and intervention shapes.
- Releases without a manifest `summary` (the built-in `eidos.pytorch.*` components) show their method card's Question and Config paragraphs in the catalog.

### Release review fixes
- `start_investigation` refuses a `model_usd` limit for a real-model runtime without `price_per_mtok`. Without prices no decision reserves or reports USD, so the limit could never apply.
- The needs-input mark of a stopped coordinator is keyed per coordinator run, so it surfaces again after an operator action.
- The per-decision USD envelope uses the runtime's defaults (8 requests per decision). With no output cap, every token is priced as output.
- A manifest authored as an object is materialized as JSON for the validation job.

### Local operations (found on the Mac)
- The supervisor now restarts a PostgreSQL cluster that died (crash recovery). A sixth death within five minutes leaves it `failed` in `run/status.json` and in `lab doctor`.
- `lab status` reports `disk.refusing_new_bytes`, and `lab doctor` fails the disk check while the store refuses uploads and job outputs (below the reserve). It used to warn only.
- `lab init` and `lab restore` refuse a Temporal port block that ends above 32767 (Temporal stores ports as PostgreSQL `smallint`, and the server exited at startup) and API/PostgreSQL ports inside the Temporal block.
- `tests/ops/*.sh` quote the lab executable, so they run from paths with spaces.
- New owner/operator checks: `scripts/qualify/disk_full_check.sh`, `service_failure_check.sh` and `sleep_restart_check.sh`.

### Other
- The assessor's evidence file and its staged copy are mode 0600.
- CI: the setup-uv PyPI fallback runs only when the GitHub release download is actually unavailable. Any other setup-uv failure fails the job (`scripts/ci/uv_fallback.sh`).
- CI checks the licence metadata of every built wheel (`scripts/ci/check_wheel_licences.py`).

### Known-affected earlier tags
- `eidos-v0.2.0` … `eidos-v0.5.0`: qualified assessment of evidence plans was broken (fixed in `8771bb3`, v0.5.1).
- `eidos-v0.4.0` … `eidos-v0.5.1`: sealed checkpoint-and-stop was broken.
- `eidos-v0.4.1` … `eidos-v0.7.0`: a `macos:seatbelt-sealed` receipt could satisfy a plan requiring sealed execution (fixed in 1.0.0).
- Receipts recorded under `macos:seatbelt-sealed` before `b7d9ed1`: their `no-credential-read` control did not cover the home directory.
- `eidos-v0.4.0` … `eidos-v0.7.0`: a late model-usage settlement after `complete_turn` could release a reservation twice (fixed in 1.0.0).
- All earlier tags: a plan requirement naming only a profile, with no controls, was satisfied by any receipt (fixed in 1.0.0).
- All earlier tags: an old-version ledger writer still running after a migration could collide with the new schema, and a dead PostgreSQL was not restarted by the supervisor (fixed in 1.0.0).

### Release record
- G9 final verification passed on tree `ac30bfc3`. Evidence: `docs/release/evidence/g9-*` and `ACCEPTANCE_MATRIX.md` § E.
- Commit IDs were rewritten on 2026-11-09 (metadata only; trees unchanged). Earlier IDs cited in documents map to current ones via `docs/release/COMMIT_ID_MAP.md`.
- CI also runs on pushes to `main`.

## 0.7.0 and earlier

Development freezes. See `HANDOFF.md` and `IMPLEMENTATION_STATUS.md` (rounds 1–17).
