# Development pilot 1, addendum 3: external disk failure and relocation to the internal disk

Written on 11 November 2026 at about 05:35 IST, before block K=5 was restarted. No arm, prompt, envelope, success rule or Eidos source changed.

## What happened
- At about 05:05 IST, the external SSD that held the pilot (all run directories, the shared lab, local grading and results) started returning kernel I/O errors (`IOStorageFamily disk6: I/O error`). Most files became unreadable, and `diskutil verifyVolume` failed with "could not freeze volume: Input/output error".
- Block K=5 (D02-r1) was running at that moment. Every pilot process was stopped to avoid further damage.

## What survived
- **Charter and tooling:** on GitHub (`task/dev-pilot-1`).
- **Results of blocks K=1–4 (16 runs):**
  - the controller's records, made during the night;
  - the independent GitHub grades of every submitted candidate (14; runs that timed out without an answer were not submitted).
- **Raw transcripts and local answer files of K=1–4:** on the failed disk; not available unless the disk recovers.

## Relocation
- **Where:** the same Mac, internal disk, under `~/eidos-internal` (commit `795fe13` makes the study root configurable through `EIDOS_STUDY_ROOT`; no other code change).
- **Eidos build:** rebuilt from source identical to `ba79b91`, the same as the pilot's build; `eidos-lab` 1.1.0.
- **Research environment:** the same lock (`torch` 2.15.0).
- **Other checks:**
  - the block plan regenerated from seed 20261111 gives the identical order;
  - the stub dry run and the sandbox probe passed;
  - one synthetic arm-1 smoke passed; one synthetic arm-4 smoke ended with `needs_input` after repeated refused "wait" proposals (model behaviour, kept on record).

## Charter rules applied
- **K=5** was interrupted by an infrastructure failure. It is an incident and is re-run whole on the internal disk.
- **K=6–9** run as planned.
- **K=1** is replaced under addendum 1.
- **K=2–4** stand as recorded. Their verdicts of record combine the run status (from the controller's records) with the GitHub candidate verdicts, under the corrected D12 rule (`pilot-1-rescue/RECONCILIATION.md`, kept with the results).
- The disk failure is reported as an incident in the paper.
