# ADR 0051 — Isolated inference output directory

Status: Accepted.

In Pilot 4, ten arm-4 exported candidates wrote receipt files beside their predictions
during inference. The local isolated pre-check allowed writes only to the designated
predictions file and fresh scratch directory, so Seatbelt raised PermissionError and
the pre-check marked those candidates invalid. The hosted grader accepted these
candidates; its verdict is unaffected.

Isolated inference now permits writes anywhere inside its own output directory
(`output.parent`), including sidecars and subdirectories. The writable set is
`(output.parent, scratch)`, with scratch last because candidate_command uses the
last entry for HOME, TMPDIR and the XDG cache/config directories. The designated
predictions output must still be a regular file and must not be a symlink.

This introduces no channel between separate isolated inferences or toward the
controller. Each inference gets a fresh output directory. All other `*-inference`
and `confirmation-inference-*` directories remain hidden for both reads and writes,
including receipts left by earlier inferences. The controller continues to consume
only the designated predictions file; it does not consume sidecars.

Writes to the workspace outside this inference's output directory remain denied.
Candidate source, `inputs/` and `test_inputs.npz` remain read-only, and test inputs
remain hidden when they are not the current inference's input. Network access
remains denied. The shared sandbox profile and grading controller are unchanged.

The local pre-check accepts candidates that emit harmless receipts beside their
predictions. Sidecars remain local artifacts with no grading meaning. Fabricated
macOS Seatbelt tests cover both sidecar names, nested output writes, sequential
validation/test isolation, denied writes elsewhere and the complete fresh-process
prediction equality check.
