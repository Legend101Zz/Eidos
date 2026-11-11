# 0028 Development evaluation ladder

Accepted 2026-11-11 for ticket T1. The handoff `EVALS.md` specifies offline core invariants and replays, a seven-card development mini-eval, and prospective sealed-card pilots.

## Decision

Tier 1 reuses pilot launch, prompts, envelopes, arm adapters and independent local grading. It selects arm 4 with optional arm 2, executes two blocks concurrently and records completion, verified gain, efficiency, failure classes and build provenance. The block grader accepts an explicit arm subset while retaining the four-arm default.

References pool verified coding-arm test gains from the reconciled Pilot 3 verdicts and corrected Pilot 2 source. Each snapshot records source hashes and missing cards. Unverified runs contribute zero to gain ratios. Both builds use the same frozen reference snapshot and the comparison contract below.

Tier-0 fixtures retain all registered model-response artifacts, including replies absent from accepted-decision exports, request timings, turn outcomes, receipts and terminal state. Blob hashes are checked during extraction. Eidos core owns the coordinator replay assertions.

Frozen build wheels, environments, lab homes and local caches live on the SSD. Eval labs receive dedicated recorded ports. The builder uses its caller's environment and a generated eval environment file. Baseline stubs qualify the infrastructure with zero model calls. A macOS study CI job exercises the existing Seatbelt grading path. The loopback provider bridge supplies its local server name during binding, avoiding startup delays from hosted-runner reverse DNS.

## Verification

`study/tests/test_evals.py` exercises a two-card stub eval with optional arm 2, injected regressions and extraction from reconstructed real-record blobs. `study/evals/README.md` documents commands, schema and the development-data scope.

## Tier 1 comparison contract

Baseline and candidate use the same controller and hash-pinned reference snapshot. A new scorecard
uses prospective zero tolerance per assignment and in aggregate. Verified native-unit test gain
remains comparable when a coding-arm reference is missing. Missing required arm-4 invariant coverage blocks Tier 1. Optional arm-2 coverage remains explicit and its outcomes are compared separately.
The arm-4 eval bridge records identifier matches and hashes from outbound provider requests, and
the independent scan retains its limits for derived information. Controller abstention remains
distinct from investigator conclusions. Each attempt preserves separate controller and verified
frozen-build provenance, including source archive, wheel and installed-payload identity.


## Paired round-3 and round-4 controller

Both builds use the same evaluation controller, outer wall envelope, provider bridge audit and
declared-checkpoint grader. The controller supplies the absolute lab deadline and the remaining
relative wall limit. The round-3 build reads the relative limit and registers a deadline at server
receipt. The round-4 build honors the absolute deadline. Each scorecard retains the observed
server deadline, startup timing, measured margin, controller and build identity, and receipt
hashes. The measured margin belongs to the evaluated build and run.

Bounded runtime replies and finalization export receipts remain visible in evidence. A controller
abstention preserves terminal outcome attribution and stays separate from investigator completion.
