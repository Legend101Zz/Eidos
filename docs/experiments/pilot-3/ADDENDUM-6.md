# Development pilot 3, addendum 6: prospective overnight incident-resumption window

Recorded 11 November 2026, approximately 22:18 IST, before any overnight replacement is launched. This is a controller-only amendment. The existing assigned roster, block order, concurrency, model, resource envelope, frozen Eidos build and scoring rules remain fixed.

## Authorized continuation

The Astra handoff authorizes continuing unresolved incident blocks until 03:00 IST on 12 November if the original controller stops at its 23:30 waiting cutoff. This addendum records that prospective window. It permits whole-block replacement of provider/infrastructure incidents only. Ordinary crashes, timeouts, token-budget stops and invalid model outputs are never re-run.

The original controller compares `date +%H%M` numerically with `STOP_AT`. Consequently, setting `STOP_AT=0300` before midnight would treat 23:30 as already beyond the next morning's cutoff. The prepared `study/controllers/drive_p3_resume.sh` uses the absolute instant `2026-11-12T03:00:00+05:30` for both replacement-pass admission and quota waiting. An already admitted block is allowed its original envelope; the clock cutoff is not an extra per-run limit.

## Controller boundaries

- The original live controller and sweep are not modified or stopped. The replacement refuses to overlap their PIDs, requires their clean completion, and refuses to take over surviving listeners on the pilot's four service ports.
- The replacement uses the same launcher, design and `--replace-incident` rule. The launcher archives each replaced block before running all four arms again. Original logs, artifacts and outcomes remain available.
- Only `study/controllers/sweep_p3_resume.sh` grades locally during this continuation. The driver's grading function is disabled, and it retains the driver markers rather than reopening simultaneous grading. The sweep uses distinct continuation logs and a distinct completion check.
- The independent reconciliation step verifies current block identity, candidate hashes, local/hosted receipts, missing grades and any failed hosted dispatch. A dispatch marker alone is never completion evidence.
- Internal-disk admission requires at least 12.5 GiB free. Bulk outputs remain on the external SSD. The frozen arm implementation is not changed.

Continuation logs are `pilot-3/drive-astra-resume.log` and `pilot-3/sweep-astra-resume.log`; prior continuation logs are preserved under `pilot-3/controller-history/` before another invocation. The actual start, exact tooling commit and open incident list must be recorded in the controller log. If no eligible incidents remain, no continuation is launched.

Validation before commit: both scripts pass `bash -n`. Invoking the replacement while the original controller is active exits with code 2 before any lab start, launcher or provider probe. The absolute cutoff is independently checked as 21:30 UTC on 11 November. Live continuation has not yet run at the time of this addendum.
