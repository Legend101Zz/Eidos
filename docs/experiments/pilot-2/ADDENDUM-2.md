# Development pilot 2, addendum 2: the launcher's disk guard stopped blocks before they started

Written on 11 November 2026 at about 14:10 IST. No arm, prompt, envelope, success rule or Eidos source changed.

## What happened
- The study launcher refuses to prepare a block when the internal disk has less than a fixed amount of free space.
- At 14:03 IST (08:33 UTC) free space fell below the launcher's 14 GiB threshold. The launcher then recorded the seven not-yet-started blocks (D03-r3, D04-r2, D04-r3, D05-r2, D05-r3, D06-r1, D06-r3) as incidents ("launcher: RuntimeError: STOP: less than 14 GiB free").
- **No arm of these blocks started.** There are no agent runs, model calls or outcomes for them.
- Completed by then: D03-r1, D03-r2, D04-r1, D05-r1. D06-r2 was running and was unaffected.

## Fix
- The controller freed about 3 GB of internal disk: build caches, finished and pushed worktrees, and a duplicate of the external-disk records. Pilot 1's lab ledger was mirrored to the external disk first.
- The launcher threshold moved from 14 GiB to 12.5 GiB. The owner's standing floor is 12 GiB, and this adds a 0.5 GiB margin.

## Charter consequence
The seven blocks never started, so re-running them is not a re-roll: no outcome existed. They run whole, unchanged, two at a time, after D06-r2 completes. Their placeholder "incident" grades are replaced by the real grades.
