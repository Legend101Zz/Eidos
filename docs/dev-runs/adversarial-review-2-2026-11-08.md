# Adversarial review 2 (2026-11-08): changes since eidos-v0.3.0

- **Reviewer:** a read-only "Eidos reviewer" helper.
- **Scope:** `eidos-v0.3.0..679df88`.
- **Evidence:** reproduction scripts were executed in the reviewer's scratch directory (`/workspace/.eidos-dev/h2/review/`, not committed). The full reviewer report is kept with them as `REPORT.md`.
- **Labels:** "confirmed" means a reproduction was executed; "inferred" means the finding comes from reading the code.

## Fixed, each with a regression test

| # | Finding | Severity | Fix | Regression test |
|---|---|---|---|---|
| 1 | The assessor output read escaped the sandbox. A hostile assessor replaced its staged `evaluation.json` with a symlink to a host JSON file. The unsandboxed activity followed it into the assessment record. (confirmed) | high | `_read_staged_json` in `src/eidos/orchestration/activities.py` applies the runner's checks: a regular file inside staging, the declared digest and size, a size cap, and a JSON object. A symlinked manifest is refused. | `tests/integration/test_assessor_sandbox.py::test_staged_assessor_output_must_be_a_contained_regular_file`. The reviewer's reproduction now ends in `ASSESSOR_FAILED`. |
| 2 | A retried `decide` collided with the reservation of its own dead earlier attempt. It returned `budget-exhausted`, which ended the investigation with its budget unspent. (confirmed) | medium | `reserve_model_budget` releases any open reservation left by earlier attempts of the same turn and slot. A late settlement from the dead attempt is capped at the amount still open, so nothing is released twice. | `tests/integration/test_turn_recovery.py::test_retried_decide_does_not_collide_with_its_own_dead_reservation`. It fails without the fix. |
| 3 | Exposure bypasses: `list_events` returned assessment outcomes, claim records carried outcome labels, `get_investigation` showed evidence-arm statuses, and `investigation_view` showed claims. Assessment reads were attributed to no study. (confirmed) | medium | Exposures are now recorded on these paths. Outcome-bearing event types are listed in `SENSITIVE_EVENTS`. Exposures are attributed through the investigation or the record's `charter_ref`. | `tests/integration/test_review2_exposure.py::test_outcome_revealing_reads_record_exposures` |
| 4 | Seatbelt `(allow default)` left same-user escape routes: reading other processes' arguments and environments (lab daemons may hold provider keys), launchd/osascript, and securityd. (inferred) | medium | The profile now denies `process-info*` on other processes, exec of `launchctl`/`osascript`/`open`, and the securityd mach services. It also covers more credential paths (`.pgpass`, `.git-credentials`, `.config/gh`, `.config/git`). A new reported control, `no-process-inspection`, plants a marker in a same-user peer's environment. It is required for Seatbelt in the macOS CI probe. | The CI step `seatbelt_probes.py`. On Linux, a same-user run detects the planted marker and bwrap passes. |
| 5a | `CommandService` had no retry on a deadlock (40P01) or serialization failure (40001). A lock-order deadlock was possible between `_terminal` and `submit_assessment`. (inferred) | low–medium | The whole transaction is retried up to 3 times on 40P01/40001. | `test_review2_exposure.py::test_transient_deadlock_is_retried_once_and_commits_once` |
| — | PostgreSQL tests were skipped silently when `EIDOS_TEST_PG_ADMIN` was set but unusable. | test infrastructure | They now fail instead of skipping in that case. | `tests/conftest.py` |

## Open questions (not fixed)

**Seatbelt:**
- The same user can still reach launchd and XPC by other routes, for example a copied `launchctl` binary or direct mach messages.
- Hard links into exempted directories are possible (low confidence).
- Signals to other same-user processes are not denied, because training code must be able to signal its own children.
- Conclusion: macOS candidates are **not isolated from a hostile same-user program**. Seatbelt raises the bar against accidental or unsophisticated access, and a VM profile is the real answer. This is recorded in ADR 0006 and HANDOFF.

**Exposure:**
- `artifact_preview` and artifact downloads of evidence-arm outputs do not record exposure (inferred).

**Idle poll:**
- Quiet polls grow the workflow history. Past `history_limit`, the Continue-As-New run starts a model turn with no news (about every 3 h with defaults).
- `waited_since` resets at rollover, so the 6 h max-idle timer is not honoured across rollovers (inferred).

**Assessor:**
- With a distinct candidate user under root, the assessor's staging is not chowned, so it fails closed with `ASSESSOR_FAILED`.
- `evidence.json` is mode 0644 in a 0755 temporary directory (inferred).

**Upgrade:**
- An old-version writer that is still running after a migration-0003 upgrade hits a primary-key collision on the event sequence.
- `lab upgrade` stops all services first. The documented path is safe; a manual partial upgrade is not.

## Checked and clean

- Gap-free sequence under 16 concurrent writers.
- Upgrade from 0002, where the counter is set to max(seq).
- Grants.
- `workflow.patched` placement; a wake during `check_news` is not lost.
- `open_reservations` accounting.
- Seatbelt parameter handling, ancestor-path exceptions, inherited file descriptors and the wrapper's exit-code write.
- The bwrap assessor's isolation settings.
- `test_temporal_realtime.py`: 17/17 passes, including under 60 CPU burners (load average about 72). The tightest margin is start-to-close: under 4 s asserted, against about 2.2 s of timeouts.
