# Adversarial review 3 (2026-11-08): changes since eidos-v0.5.2

- **Reviewer:** a read-only "Eidos reviewer" helper.
- **Evidence:** executed reproductions in `/workspace/.eidos-dev/h2/review3/` (not committed).
- **Labels:** "confirmed" means a reproduction was executed.

## Fixed in `5df8938`, with regression tests

| # | Severity | Finding | Fix | Test |
|---|---|---|---|---|
| 1 | high, confirmed | The lab-free driver followed a symlink left in `out/` by a contained command. `out/x -> /proc/self/environ` handed the driver's environment, including the provider key, to the next command and back to the model through stderr. | Outputs must be regular files inside `out/`, the same rule the lab runner applies. | `test_contained_commands.py::test_lab_free_driver_refuses_symlinked_outputs` |
| 2 | medium, confirmed | The needs-input wait did not survive Continue-As-New. Quiet polls grew the history, and each rollover started fresh turns (3 → 15 turns in 25 s at `history_limit=80`). | The refused-turn counter is carried across rollover, and a rolled-over run keeps waiting for operator news. Guarded by `workflow.patched("needs-input-survives-rollover-v1")`. | `test_workflows.py::test_needs_input_survives_continue_as_new` |
| 4 | medium, confirmed | `run_contained` bound `Path(python).parent.parent`. An interpreter at `$HOME/bin/python` therefore exposed all of HOME to a sealed job. | Interpreter prefixes that equal or contain HOME or a credential store are refused. | contained tests |
| 5 | low/medium, confirmed | With `--allow-same-user`, a job can read `/proc/<launcher>/environ`, which holds the key. | The waiver text and the docstring now state the exposure. A sealed profile is the only real protection. | — |
| 6 | low, confirmed | `control.bounded_outcome` could be set by any proposer. | `accept_decision` strips it unless the trusted runtime path sets it, together with a `limit_reached` usage. | contained/turn tests |
| 7 | low, confirmed | A bad Temporal archive in the cache was sticky. | The bad entry is deleted and the archive re-downloaded; the cache is written via temp file and rename. | `test_temporal_provenance.py::test_cached_archive_with_wrong_digest_is_rejected` |
| 8 | low, confirmed | The full-lab key-leak check skipped binary files and put the key on `grep`'s argv. | A byte scan of every file under the lab home, with no argv exposure. | `bash -n` only; the Mac session runs it |

## Open

- **3 (medium, inferred):** "needs operator input" is visible only through the workflow `state` query. No ledger event or investigation status shows it. A `lab investigate show` field or event should be added.
- **9 (low):** the CI uv fallback was unpinned; it is now pinned to `uv==0.13.7`. `continue-on-error` on setup-uv still masks other setup failures, although the fallback step makes the job fail later if uv is missing.

## Checked clean

- The `job_environment` allowlist: no `LD_PRELOAD`, `PYTHONPATH` or `DYLD_*`.
- Supervisor scrubbing: only the investigator worker holds keys, and it spawns no subprocesses.
- Replay placement of `invalid-turn-retry-v1`.
- Archive digest verification before use, and `filter="data"` extraction.
- bwrap PID, network and HOME isolation.
- `full_lab_live.sh` never writes the key to argv, files or output.
