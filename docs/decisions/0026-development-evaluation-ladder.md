# 0026 Development evaluation ladder

Accepted 2026-11-11 for ticket T1. The handoff `EVALS.md` specifies offline core invariants and replays, a seven-card development mini-eval, and prospective sealed-card pilots.

## Decision

Tier 1 reuses pilot launch, prompts, envelopes, arm adapters and independent local grading. It selects arm 4 with optional arm 2, executes two blocks concurrently and records completion, verified gain, efficiency, failure classes and build provenance. The block grader accepts an explicit arm subset while retaining the four-arm default.

References pool verified coding-arm test gains from the specified local pilot results. Each snapshot records its source hashes and missing cards. Unverified runs contribute zero to gain ratios. Reference snapshots must match across a regression comparison. The default comparison tolerance permits a median gain-ratio decrease of 0.05 absolute ratio points, with completion losses and new dead ends failing independently.

Tier-0 fixtures retain all registered model-response artifacts, including replies absent from accepted-decision exports, request timings, turn outcomes, receipts and terminal state. Blob hashes are checked during extraction. Eidos core owns the coordinator replay assertions.

Frozen build wheels, environments, lab homes and local caches live on the SSD. Eval labs receive dedicated recorded ports. The builder uses its caller's environment and a generated eval environment file. Baseline stubs qualify the infrastructure with zero model calls. A macOS study CI job exercises the existing Seatbelt grading path. The loopback provider bridge supplies its local server name during binding, avoiding startup delays from hosted-runner reverse DNS.

## Verification

`study/tests/test_evals.py` exercises a two-card stub eval with optional arm 2, injected regressions and extraction from reconstructed real-record blobs. `study/evals/README.md` documents commands, schema and the development-data scope.
