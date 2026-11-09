# Eidos v1.0.0-rc1 release notes (2026-11-09)

This is the release candidate for v1.0.0 (package version `1.0.0rc1`). The final v1.0.0 follows after the owner's machine sleep/restart receipts. It will differ from this RC only by the version string, unless that check finds a defect. Gates: `ACCEPTANCE_MATRIX.md` § E.

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

## Metrics: v1.0.0-rc1 compared with eidos-v0.7.0

| Measure | eidos-v0.7.0 | v1.0.0-rc1 |
|---|---|---|
| Test functions (unit, integration, MCP, reporting, packages, components) | 218 | 241 |
| Local fast + integration suite on the qualification Mac | 140 passed, 4 skipped | see G9 in `ACCEPTANCE_MATRIX.md` § E (158 passed, 4 skipped at `329d9c3`, before the last fixes) |
| CI jobs per push (all green) | 10 | 10, plus a wheel licence check and a setup fallback that no longer masks failures |
| Release gates G1–G9 defined and recorded | none | G1–G5, G7, G8 pass; G6 passes except the hardware sleep/restart check; G9 on the final tree |
| Licence | none declared | Apache-2.0 in every package and wheel |
| A8 with a real model | a full-lab D02 exploration run concluded once (exploration only: no qualified assessment, no export) | **full A8 journey** with `claude-sonnet-5-5`, all 15 checks: diagnosis, new component published and run, trial, branch, qualified assessment (`inconclusive`), behaviour check, strict-loading export, worker SIGKILL and hand-off; USD 1.81 |
| OpenAI-compatible provider | `qwen2.5:3b`: 3/8 scenarios | `qwen2.5:14b`: 4/8 (adapter verified; conformance failure recorded); Anthropic 8/8 |
| Local operations on macOS | not exercised on the Mac | disk exhaustion 13/13 on a real 2 GiB volume; every service SIGKILLed and restarted, including PostgreSQL; abrupt stop A/B pass |
| Switch-order acceptance journey on macOS | 154/154 in CI (Linux) | 154/154 on the Mac as well |
| Defects found and fixed with regression tests in this cycle | — | more than 20 (each listed in the CHANGELOG): isolation honesty, ledger writer fence, a budget double release, reconciler loop, budget envelope, three HTTP 500s found by real runs, PostgreSQL supervision, doctor disk display, Temporal port limit, among others |

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
- **Machine sleep and OS restart recovery not yet verified on hardware (owner check R1 pending); abrupt-stop and service-kill recovery are verified.** The final v1.0.0 follows after the owner's sleep/restart receipts; it differs from this RC only by the version string unless R1 finds a defect.
- **Hosted OpenAI-compatible services** are protocol-compatible but not separately qualified. The adapter was verified against a local Ollama endpoint. No local model of 14B or less passed full decision-schema conformance on the qualification Mac.
- **Multi-day investigations** and real **distributed** or **CUDA** runs were not exercised.
- **Qualification suites are author-written.** Independent scientific controls belong to the A16 evaluation.
- **Performance targets** were measured once on a hosted host, not on a declared reference machine.
- **Backups** have a loss window: commands after the watermark are not in the bundle. A single machine is not highly available.

## Qualification evidence

The gate table is `ACCEPTANCE_MATRIX.md` § E. Receipts are in `docs/release/evidence/`, plus `docs/mac/STATUS.md` § v1.0.0 release session.

- **G5, A8 with a real model.** Pass. `claude-sonnet-5-5` completed the whole autonomous journey on the switch-order project through the internal loop, with no intervention:
  - diagnosis;
  - a new component authored, validated, published and run;
  - a trial and a checkpoint branch;
  - a frozen, qualified evidence plan assessed `inconclusive`;
  - a behaviour check, and an export that loads strictly;
  - the investigator worker SIGKILLed after its first committed effect and handed off, with one admission per job, unique keys and visible charges (USD 1.81).

  Two earlier runs exposed six Eidos defects, all fixed with tests. This is development evidence, not a scientific result.
- **G4, OpenAI-compatible provider.** Adapter verified and model conformance failure recorded. Against our own local Ollama endpoint the adapter's error, timeout, request-limit and 404 handling pass, and `qwen2.5:14b` produced schema-valid decisions. No local model of 14B or less passed full conformance (14B: 4/8; 3B: 3/8; the 7B run did not complete). Anthropic passed 8/8.
- **G5, local-model attempt.** `qwen2.5:14b` produced one refused decision in about 2 h. This failure is recorded as is.
- **G6, local operations.** Disk exhaustion on a real 2 GiB APFS volume, SIGKILL of every service including PostgreSQL, and abrupt stop with and without the job all pass on macOS. A real machine sleep and restart is the owner's step (`scripts/qualify/sleep_restart_check.sh`).
- **G3 and G7.** Tests are listed in the CHANGELOG.
- **Switch-order acceptance journey on macOS:** 154/154.

## Install

```bash
uv venv eidos && uv pip install --python eidos/bin/python --no-cache --find-links <release wheels dir> eidos-lab
eidos/bin/lab init && eidos/bin/lab start && eidos/bin/lab doctor
```

The release wheels are attached to the GitHub release. Upgrading an existing lab: install the new wheels, then run the new installation's `lab upgrade` (`docs/ops/INSTALL_AND_OPERATE.md`).
