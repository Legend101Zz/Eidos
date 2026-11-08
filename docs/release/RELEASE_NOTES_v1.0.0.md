# Eidos v1.0.0 release notes

**Status of this document:** draft for the release candidate `eidos-v1.0.0-rc1`. The "Qualification evidence" section is completed as each gate's evidence arrives. The final `eidos-v1.0.0` tag needs every gate in `ACCEPTANCE_MATRIX.md` § Release gates, including the owner's machine sleep/restart evidence.

## What v1.0.0 is

v1.0.0 is the **software release** of Eidos: acceptance areas **A1–A15** of the design's "Complete local v1" table, within the supported scope of [`SUPPORTED.md`](SUPPORTED.md):
- Linux x86_64 and macOS arm64;
- CPU on both, and MPS on macOS;
- Anthropic and OpenAI-compatible model endpoints;
- Claude Code, Codex CLI and the MCP Python SDK as clients;
- sealed (hostile-code) evaluation on Linux with bubblewrap only.

## What v1.0.0 is not

- **Not a scientific claim.** A16, the scientific comparison, is a **separate later release** with `eval-*` tags. It asks whether Eidos helps an LLM do model research, under locked success rules, blinded results on different tasks and hardware, preserved losing candidates and nulls, and a reproduction package. The real-model runs below are development evidence that the software works with real models. They are not evidence of research benefit.
- Not a hosted service, cluster scheduler, TUI or web app. Not Windows, CUDA, distributed training, a Lima VM, or Claude Desktop (see `SUPPORTED.md` for why).

## Licence

Apache License 2.0 (`LICENSE`, `NOTICE`). `research/public/` keeps its own notices and is not covered.

## Changes since eidos-v0.7.0

See `CHANGELOG.md`. In short:
- **Isolation honesty.** Same-OS-user Seatbelt receipts no longer satisfy sealed plans; macOS labs refuse sealed plans; receipts report `same_os_user`.
- **Ledger safety.** Migration 0004 fences old-version writers after an upgrade. A double release of model-budget reservations is fixed.
- **Orchestration.** A quiet wait no longer spends a model turn at rollover.
- **Investigator context (found by the real-model A8 runs).** The investigator now sees the results of its own calls (trial ids, plan ids, validation jobs, export ids, errors), its evidence plans with their assessments, and the approved protocols. The tool guide documents the evidence-plan draft.
- **Local operations (found on the Mac).**
  - The supervisor restarts a dead PostgreSQL.
  - `lab doctor` fails the disk check when the store refuses new bytes.
  - `lab init` refuses a Temporal port block that Temporal cannot store.
  - The ops scripts work from paths with spaces.
- **CI.** The setup-uv fallback no longer masks other failures. Wheel licence metadata is checked.

## Known-affected earlier tags

| Tags | Problem | Fixed in |
|---|---|---|
| `eidos-v0.2.0` … `eidos-v0.5.0` | Qualified assessment of evidence plans was broken | `8771bb3` (v0.5.1) |
| `eidos-v0.4.0` … `eidos-v0.5.1` | Sealed checkpoint-and-stop was broken | v0.5.2 |
| receipts before `b7d9ed1` | Under `macos:seatbelt-sealed`, `no-credential-read` did not cover the home directory | `b7d9ed1` |
| `eidos-v0.4.1` … `eidos-v0.7.0` | A `macos:seatbelt-sealed` (same-OS-user) receipt could satisfy a plan requiring sealed execution | 1.0.0 |
| `eidos-v0.4.0` … `eidos-v0.7.0` | A late model-usage settlement after `complete_turn` could release a reservation twice, freeing budget held by others | 1.0.0 |
| all earlier tags | A plan requirement naming only an isolation profile, with no controls, was satisfied by any receipt | 1.0.0 |
| all earlier tags | An old-version ledger writer still running after a migration could collide with the new schema (e.g. the 0003 event counter) | 1.0.0 (migration 0004) |
| all earlier tags | A dead PostgreSQL was not restarted by the supervisor | 1.0.0 |

## v1 limitations (documented, not defects)

- **macOS isolation is same-user.** Seatbelt does not contain a hostile same-user program: launchd/XPC stay reachable by other routes, mach IPC is unfiltered, signals are allowed, hard links may be possible, and `sandbox-exec` is deprecated. Sealed evaluation is Linux-only.
- **Distinct candidate identity** on Linux needs root, the `eidos-cand` user and a candidate-readable interpreter. Otherwise `separate-identity` is reported as failed (truthfully). With a distinct identity the assessor fails closed.
- **Hosted OpenAI-compatible services** are protocol-compatible but not separately qualified. The adapter was qualified against a local Ollama endpoint.
- **Multi-day investigations** and real **distributed** or **CUDA** runs were not exercised.
- **Qualification suites are author-written.** Independent scientific controls belong to the A16 evaluation.
- **Performance targets** were measured once on a hosted host, not on a declared reference machine.
- **Backups** have a loss window: commands after the watermark are not in the bundle. A single machine is not highly available.

## Qualification evidence

Filled in from the receipts in `docs/mac/receipts/` and `docs/release/evidence/` as the gates complete; see `ACCEPTANCE_MATRIX.md` § Release gates for the gate table.

## Install

```bash
uv venv eidos && uv pip install --python eidos/bin/python --no-cache --find-links <release wheels dir> eidos-lab
eidos/bin/lab init && eidos/bin/lab start && eidos/bin/lab doctor
```

The release wheels are attached to the GitHub release. Upgrading an existing lab: install the new wheels, then run the new installation's `lab upgrade` (`docs/ops/INSTALL_AND_OPERATE.md`).
