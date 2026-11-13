# Pilot 4 Addendum 3b — inherited PAYG binding and administrative non-admission

Prospective direct child of R4 `1a260e6ff872b2d6ce427da88d49d01782ed6e80`.
This single combined addendum replaces the unpublished first local R5.

R5 restores the inherited Addendum 2 SHA-256 in FREEZE.v5 and ACTIVATION.v5,
and binds Addendum 2, unchanged Addendum 3, and this Addendum 3b to R5.
The PAYG owner attestation, quota interpretation and scientific policy do not
change. The R4 preparation tooling removed inherited Addendum 2 fields even
though its registered guard correctly required them; this was a tooling defect.

A guarded stage refused BEFORE any worker dispatch (no block start, no worker,
no model call), proven by its stage exit, absence of block/run records and the
bound decision receipt, is an administrative non-admission. Record it as
`refused-pre-worker` in the admission census with its decision id. Preserve the
decision and its batch number; the block stays pending and is admitted in a
later batch. Capture and reconciliation accept a non-contiguous sequence of
actual admission batches only when EVERY gap has a verified
`refused-pre-worker` receipt. A nonzero stage exit by itself is insufficient.
A stage with any block start, worker, model call or ambiguous evidence is not
eligible. Do not fabricate block records, delete decisions or renumber batches.

Batch 13, decision `0013`, 2026-11-13T12:30Z, for N05-r6 is such a case:
the pinned R4 guard exited 2 with “PAYG freeze lacks the registered Addendum 2
binding” before entering stage. It was caused by the tooling defect, decided
before any outcome of that block existed. It remains a refused non-admission;
N05-r6 is eligible for later admission, beginning no earlier than batch 14.

Use new FREEZE.v5, ACTIVATION.v5, entry-values.v6 and parent/launch/watchdog v5.
The preserved v10.4 guard, stage, controller and allowance policy remain
unchanged. A new v10.5 executable closure changes only chain/switchover and
capture/reconciliation handling required for this rule, with refreshed manifests
and focused qualification. Every inherited generation and its bytes remain
preserved. R4 freeze and activation are still exactly
`8efd558cd991c040d994de574623ddb6d477d89a366cc139f80736b671a9a31c` and
`8f80d681f3704c8c61218f13901dee534655f02aa76dfda4ed0cffc0f17189b0`.

The v4→v5 switch retains the stopped-owner, zero-active, exclusive-lock and
immutable-snapshot barrier. Its distinct `control/freeze-switchover.v5.json`
binds `control/parent-state.v4-final.json`, the verified refused decision and
the exact current NEEDS SHA-256
`bb4e6585c71631882e7acecafc21a835d0c1bd84d70dc863208c796a716b6348`.
The v5 takeover receipt acknowledges this particular tooling-defect halt and
keeps its proof in the census. No fabricated acknowledgement request is used;
unrelated halts remain blocking. Publish exact R5, run the isolated v6 smoke,
and pass `verify_addendum3b_ready.py --require-apply-ready` before applying.

No other rule changes. All scientific, random-order, model, grading, budget,
window, concurrency, retry and incident rules remain unchanged. Preparation
and qualification do not authorize push, application, acknowledgement or launch.
