# Changelog

All notable changes to Eidos. Tags are annotated, created only on CI-green commits, and never moved. Development tags `eidos-v0.x` were freezes, not releases.

## 1.0.0 (unreleased; release candidate `eidos-v1.0.0-rc1`)

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

## 0.7.0 and earlier

Development freezes. See `HANDOFF.md` and `IMPLEMENTATION_STATUS.md` (rounds 1–17).
