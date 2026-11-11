# Development pilot 3, addendum 6 — prospective overnight incident resumption

Recorded on 11 November 2026 before any overnight replacement. The assigned roster, randomized block order, concurrency, model, resource envelope, frozen Eidos build and scoring rules remain fixed.

The Astra handoff authorizes incident replacements through 03:00 IST on 12 November if the original controller stops at its 23:30 waiting cutoff. The continuation uses the absolute cutoff `2026-11-12T03:00:00+05:30` (21:30 UTC on 11 November). Admitted blocks finish under their original envelope.

## Dispatch and grading

- The continuation refuses an active original or continuation controller and surviving listeners on the four pilot service ports. Both original completion logs must be present. It starts the existing pilot lab after checking that an eligible incident remains and at least 12.5 GiB is free internally. Bulk output remains on the external SSD.
- The dispatcher checks the original design file against SHA-256 `f44a174c5bdcd3e1595ae61894b9cee215a37de38b3a25e67ef1d79d3dce5903` and reads its 48 assignments in the existing randomized order. It admits only complete current incident records with matching assignment and attempt identity. Missing or incomplete blocks and ordinary failures are outside this replacement window.
- The dispatcher keeps at most two paired blocks in flight and checks the current attempt identity, disk headroom and absolute cutoff when a slot becomes available. An already admitted block finishes even if the cutoff passes. Each admitted replacement uses the original launcher to archive the incident attempt and run all four assigned arms again. The continuation retains the previous four-pass cap and checks provider availability before each pass.
- Before dispatch, the dispatcher archives any stale hosted submission marker with the old block start time and block-record hash. The continuation sweep can then submit the replacement candidate. If the final admission check fails, the marker is restored. Existing completed blocks keep their markers.
- The continuation companion owns grading. It checks the current block status after local grading and performs a final pass after observing the driver's completion. The sweep marker reserves a hosted dispatch batch. The sweep logs the helper exit and observed receipt state for each attempted arm. Independent reconciliation determines hosted completion from current grade evidence.
- The driver stops the lab after admitted replacements settle and publishes its completion log. Analysis follows only after both the driver and the companion finish, using independent reconciliation of the current local and hosted evidence.

Continuation logs are `pilot-3/drive-astra-resume.log` and `pilot-3/sweep-astra-resume.log`. Prior continuation logs and replaced dispatch markers are retained under `pilot-3/controller-history/`. The driver records the tooling commit and initial eligible incident list.

## Offline validation

The dispatcher tests use a fake launcher. They check cutoff at entry, cutoff while both paired slots are occupied, cutoff during marker archival, exclusion of missing or incomplete blocks and ordinary outcomes, attempt changes before dispatch, the two-slot limit, and the frozen design hash. Both shell scripts pass `bash -n`. This validation does not launch the lab, call the provider or run a replacement.
