# 0055: Experiment runner and pool card profiles

Date: 2026-11-14. Status: accepted. Rule: R-infra.

Programme pools extend the existing private-card registry through `pool:<POOL>`
profiles. Both published manifest shapes are accepted; card collisions fail at
import. Without manifests the historical three profiles and N05–N16 roster are
unchanged. Seal roots resolve through profiles, choosing the first configured
root containing the pool seal. Class counts use the sealed card manifest when present and must agree with task
metadata. Without a manifest count, Pilot 4 retains its binary default and other
sets require the historical valid task.json count. Existing commitment, prediction,
bar and isolated artifact checks remain in place.

The controller strips seal roots, extra manifest paths and the private-root
variable from all lab and worker environments. Workers receive only minimal safe
rosters via `EIDOS_STUDY_POOL_PROFILES`. Prompt bar and charter generation,
component selection, launcher planning and isolated grading use the registry.
Historical public and Pilot 4 prompt/charter/plan goldens remain unchanged.
The standing programme charter is a provenance amendment, never prompt text.

`study.exp.run` binds each treatment/provider to a full build commit, requires a
clean controller containing that build's history, and reuses the dedicated lab
builder and existing build verifier. Every lab home is explicit. Independent
block subprocesses isolate concurrent treatment/provider environments, while
per-lab capacity and pair barriers preserve the planned paired execution. Incident
replacement and grading reuse the launcher and grader. Descriptive paired output
uses compare's statistics helper because its A/A mode requires identical variant
identities. No decision is taken by the runner. Smoke evidence has a separate
schema that the unchanged scientific comparator refuses.

Live preflight enforces disk/output/pilot exclusions and prospective budget
bounds. Spend combines provider families and includes permitted retries in its
worst case. MiniMax requires fresh quota evidence or a recorded pay-as-you-go
approval. Live dispatches reserve worst-case spend under a shared ledger lock and settle
against archived attempt receipts; missing receipts retain uncertain exposure.
SIGTERM/SIGINT cancel dispatches, stop labs and settle reservations. Fixture and baseline stub paths require no credentials. Run
manifests bind controller/build commits, lab receipts, charter, plan and timings;
accounting includes archived attempts. Pool scorecards receive only logical pool
identities and reference numbers, never private provenance or table dimensions.

`study.exp.heldout` validates variants and non-private preflight before reserving
at most three consultations atomically (child preflight refusals are refunded), and redirects
all child OS output into private scratch. H service homes are under the private
root, using a verified shared frozen interpreter. Lab services receive no pool
roster. H per-run accounting and reservation order stay private; the shared ledger
holds one aggregate reservation, replaced by aggregate totals. Only rounded
aggregate receipts and aggregate spend leave that root; even errors disclose only exception type. Thresholds are pinned before
execution and evaluated on unrounded per-card deltas. Missing upstream novelty-win
observations remain unknown. All tests use fabricated private roots; no genuine
held-out material is part of development or validation.

Strong public legacy references are generated from the same datasets, baseline
recipes and practical bars used by the grader, with train-only selection and fresh
three-seed evaluation. Mechanical stub and fixture evidence does not establish
scientific improvement or provider availability.

Non-D grading scratch stays under EIDOS_IMP_PRIVATE_ROOT; only grade summaries
are copied into public results. Dry-run prints without writing. D03 has no
established legacy reference: the measured recipe predicts one class on all
shifted test rows (scores .1/.1/.1). Using D03 requires an explicit reference file;
changing the reference recipe remains an owner decision.

Final scorecard integration writes one scorecard per arm via explicit builder
selection, retaining root aliases only for single-arm runs. Tier, pool role, look
and mode stamp every row. The manifest indexes scorecards and descriptive pairs
by arm. Usage JSON accounts for all executed attempts (including incident
archives), known costs, quota deltas and summed wall time, with per-variant totals;
held-out accounting stays private. Decisions consume registered experiments and
one or more tier/look scorecards with their complete usage evidence. The current
comparator's exact final-row count check cannot accept extra attempts from retries
or omitted arms; the runner preserves these costs and documents the API limit.


Second review separates fresh-quota admission from per-dispatch floor and USD
checks. Quota directories accept timestamped programme readings; stale dispatch
readings warn, and a floor refusal retains completed evidence with an incomplete
scorecard and a nonzero exit. Accounting marks incomplete outcomes explicitly,
uses only fresh newer quota deltas, and isolates uncertain charges by provider
family. Mixed held-out pairs settle model-free variants at zero. Cleanup ignores
further signals; the blind wrapper gives its child 60 seconds to clean up.
A persisted started marker prevents crash refunds after dispatch. Public grading
summaries hash private paths. Arm-2 controller build assertions use a distinct
field; scorecards continue to require independently produced run build evidence,
and scoring refusals do not prevent other arms from being scored.
