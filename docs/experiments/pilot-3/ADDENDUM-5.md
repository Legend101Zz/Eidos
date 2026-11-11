# Development pilot 3, addendum 5: internal-disk incidents during replacement blocks

Recorded 11 November 2026, approximately 22:07 IST, before another replacement of the affected blocks. The frozen Eidos build, model, task cards, prompts, limits, assessment and success rules are unchanged.

## Observation

The second attempts of N03-r3 and N04-r3 ended as infrastructure incidents. In each, `arm4/worker_error.txt` records the internal-disk guard (`STOP: internal disk has less than 12 GiB free`). The launcher preserves arm 4's `pre_block_status: incident` and the other arms' `pre_block_status: ok`, then propagates the incident to the entire block. The outer missing-model-id reason is not a diagnosis of a served-model change or a model failure.

Evidence is in `/Volumes/Mrigesh SSD/eidos-v1/pilot-3/runs/N03-r3/` and `N04-r3/` at this snapshot. Whole-block replacement moves those directories into `runs/incidents/`, retaining their block start times (`2026-11-11T16:04:38.670556+00:00` and `2026-11-11T16:05:23.411258+00:00`). Earlier attempts of these same assignments failed during the provider quota outage; the attempt-level causes must remain separate.

## Operational correction and retention

- Restore disk headroom by relocating closed pilot-2 artifact/job directories to the external SSD, preserving bytes and original paths through symlinks. Check for open files and verify SHA-256 manifests before removing the copied source. Record the actual relocation in the controller log. No live pilot-3 record or frozen installation is relocated or edited.
- Continue to require at least 12.5 GiB free on the internal disk. The existing launch admission check enforces that floor. The observed arm-4 guard uses a lower threshold; its frozen implementation is not changed during this pilot. A later threshold harmonization is a prospective tooling task.
- Re-run each incident block whole under the charter's existing infrastructure-incident rule. Retain all original attempts, statuses, costs and error records. These incident replacements do not authorize any re-run of an ordinary crash, timeout or token-budget failure.
- The companion grading sweep remains the sole local grading owner. Final reporting will verify every current block's local grade and hosted grade provenance, rather than equating dispatch markers with completed grading.

No retrospective success is inferred from the other arms' completed answers in an excluded incident block. Report the later accepted whole-block attempt and preserve the excluded attempt in the incident inventory.
