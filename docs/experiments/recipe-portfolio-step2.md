# Step 2: offline recipe portfolio

The component and its library are implemented under ADRs 0045 and 0046. Lab
preparation, `author_source.include`, lock margins and round accounting are step 3.
ADRs retain their draft numbers; 0044 belongs to the separate steering/profile work.

R1 froze the paired-band multiple at 1.0 and the library's structural complexity
order before its holdout scoring. Its design sweep and randomly permuted replay
were diagnostics. R2 subsequently promoted the seeded ordering after R1 exposure,
as disclosed below, and froze it before the epoch-2 run. There is one primary
verdict per candidate, C versus A2, with C versus A1 and selection replay labelled
secondary. The current step-1 guard is imported unchanged and its digest is saved.
Use a fresh output directory when rerunning after a step-1 or candidate change.

Round 1 includes 185 classification or 193 regression recipes before size-based
grid reduction. Generative classification models are inapplicable to regression;
the other seven classes receive regression analogues. Transform fitting is inside
each fold. Screening is not a global preprocessing stage. The default budget is
350 seconds, with 20% reserved for the full-fit validation checks. Above 5,000 rows,
grid density and neural cost are reduced before evaluation. Cost-ranked class representatives and distinct model families precede duplicate settings; HGB includes the strong default (150 iterations, 31 leaves, learning rate .1, no internal early stopping). Duplicate neural specs after cost reduction are removed. This uses no row subsampling
or halving. A coverage metric says whether every applicable class completed.

The optional profile and predictions ports are reserved observations. This release
uses its own target-free domain-AUC check, so it does not depend on step 5. Refinement
accepts explicit typed neighbors (`add`) and parent specs; a family stops after six
sub-noise evaluated neighbors against its preceding anchor. Rescore always requires
an explicit partition seed and uses reproduction purpose. Export accepts one stated
spec and writes only a source bundle marked `explicit-export`.

Run using the research interpreter and all four thread variables set to 1. The
public practice family is generated outside the repository; do not supply private
or sealed cards. Existing A0–A3 cache reuse checks both their source and data keys.
If those checks fail, the existing comparator code recomputes the tables.

```sh
python -m study.practice.build_cards --out /external/practice-data
nice -n 10 python -m study.practice.pipeline_c \
  --data /external/practice-data --out /external/step2-acceptance \
  --comparators /external/step1-gate --stage design --workers 3
nice -n 10 python -m study.practice.pipeline_c \
  --data /external/practice-data --out /external/step2-acceptance \
  --comparators /external/step1-gate --stage holdout --workers 3 \
  --evaluation-id v2-step2-c-v1
```

`--comparators` is optional. `--stage all` preserves design-before-holdout ordering.
The holdout stage registers the frozen candidate in the existing protected programme
ledger before scoring; changing the evaluation identity cannot reset burn limits.
Acceptance JSON contains per-card CV/validation/test replay, poison audits of the
actual fold-fit function, generator effects and inference, null/weak-control claim
metrics through the grader, three fresh evaluator seeds, checkpoint sizes, fit and
cold-process wall clocks, coverage and budget outcomes. A short Markdown summary
states failures plainly. Scientific rejection is a result, not a failed execution
of the measurement script. These are practice measurements, never confirmation of
a study improvement or an investigator claim.

## R1 historical measurements (pre-rebase; superseded by R2)

The frozen R1 run measured all 96 generated cards and all seven public cards.
The base guard accepted named transfer versus A2 (12 holdout generators, mean
3.205 paired-test-SE units, lower 90% bound .675, sign-flip/Holm p=.0126953).
The A1 comparison also returned `accept-named` and remains descriptive. Null and
weak-signal claim rates were zero for both C and A2. All 96 family exports and all
seven public exports passed fresh-process equality, size and clock checks; the
three-seed practical gate passed on 77/96 family cards and 7/7 public cards.

Overall scientific acceptance is **NOT MET**. Kernel coverage failed on four
large-n design cards after three RBF trials each reached the 60-second timeout.
Mean selection regret was .807 chosen-score test-SE units for C versus .630 for
argmax-validation (5.415 for argmax-CV). Mean optimism was -.387 for C versus .557
and 4.813 respectively. These are descriptive, card-weighted means, with the
.001 score-SE floor; raw and per-task values are retained. Regret is relative to
the best completed recipe, excluding budget-stopped recipes. No candidate changes
were made after the registered holdout run began. Author tests and scientific
acceptance are separate results. The full JSON, timing probes and report live in
`claude-orch/v2/step2/` and `claude-orch/logs/V2_STEP2_REPORT.md` beside the worktree.

R1 primary round-1 times were budget-censored on 20 cards. Separate visible-input
timing probes used the same frozen library and a 480-second budget, prioritizing
the lowest fraction of planned recipes attempted. 16 probes measured at least
12/96 (12.5%) instances above 350 seconds in that earlier family, which included
20,000-row design cards. This R1-only observation must not be used to describe R2;
halving remains deferred, and diagnostic selections do not replace the primary
results. The original candidate and test scores remain frozen.

## Round 2

Rebased onto accepted step-1 `69e1b38`; only the changelog conflicted. Pipeline C
uses index-bound construction/candidate receipts and the authorized epoch-2 root.
Design-only analysis, freeze, validation and single epoch-2 acceptance evidence
are stored in `claude-orch/v2/step2-r2/`. ADR 0046 records the selection calibration:
one-SE with the predeclared seeded ordering, repeated fivefold CV and shift-aware
train-fit/validation scoring. The large-n kernel representative is a linear SVM.

## R3 corrections and disclosures

Overall acceptance remains **NOT MET**. The launch-pinned summary code originally
returned `halving_trigger_assessment: NOT MET`; the acceptance record and report
restore that result. R2 measured 96 searches: 0 observed walls above 350 seconds,
8 budget-censored searches, all design/calibration cards (gen-011 and gen-014),
and a family with at most 1,800 training rows per card. The trigger did not fire in this
family. Full-grid completion times remain unknown, with no inference about the
earlier 20,000-row family.

**Post-hoc interpretation, not a changed criterion verdict:** the 8/96 = 8.33%
upper bound is below 10% under the fixed timeout policy. It does not change the
original **NOT MET** to PASS. The original wall criterion was poorly specified:
20% of the 350-second budget is reserved for full-fit checks, so observed search
walls above 350 seconds cannot meaningfully measure uncapped grid cost.

`small_n_selection_quality` is **NOT MET — descriptive and under-powered**, added
by the summary code rather than the guard. It uses one 178-row gen-008 source,
four correlated re-splits and 36 test rows per split, with one effective source.
C loses 6 test rows versus argmax-validation's 5 relative to the best completed
recipe across the four splits; this is a one-row difference across correlated
cards. C's regret is 1.091410 versus .909509 test-SE units; optimism is 1.067143
versus 1.163055. Below roughly 200 source rows, this portfolio's selection has
not been shown to beat argmax-validation. This is a method-card supplement;
the component method card remains byte-identical to the frozen candidate.

The production seeded selection rule was chosen **after R1's pre-rebase holdout
exposure**, motivated by C's .807 versus argmax-validation's .630 regret. The R2
rule comparison used design cards only, but fresh epoch-2 instances do not provide
mechanism-level independence from R1. The R1 `v2-step2-c-v1` burn appears only in
`claude-orch/v2/programme-burns.json`, not the current authenticated ledger.
Two candidates have scored these generators across epochs; one of two burns is
the epoch-2 count. The recorded Holm p=.0029296875 omits R1; counting one more
prior primary conservatively at p=1 gives .00390625 (eight-primary maximum:
.0078125), without changing the registered verdict.

Within-band ranks permute positions in the completed eligible set. Reviewer
replay found that dropping one eligible recipe changed the choice on **33/68
multi-recipe-band cards**, so the rule is sensitive to failures, timeouts and
evaluation order. A stable per-recipe hash rank and seed-distribution evidence
are deferred to the next build: this fix requires a new candidate evaluation.

The comparator pin includes `study/practice/pipeline_c.py` via its `*.py` glob.
Its addition changes the observed digest from `9a8d6f3f02df…` to `a2072f253560…`;
removing only that file from the digest reproduces the original pin.
Authenticated ledger disclosure event 481, hash
`981214da499ad380994c61dd6de0f8867c42c1739223a75084b3acb1e4c0f447`,
records this without changing historical entries, the epoch pin or budgets.
Epoch-2 harness-validation still refuses this checkout's mismatch; epoch 3 must
explicitly re-pin the reviewed comparator source. See ADR 0046 for full digests.

Before the next calibration, add a small-n **design generator**, then assess the
stable recipe prior across seeds on design data and update the method card's
small-n limitation. Do not tune on this holdout result. R3 changes documentation
and reporting only; no frozen source, holdout score or candidate burn changes.

R3 validation: ruff passed; the practice and pipeline-C tests passed **232 tests**
in 26.01 seconds, using `nice -n 15` and all four numerical thread caps set to 1.
Logs and exact command arrays are in `claude-orch/v2/step2-r2/r3/checks.json`,
`ruff.log` and `practice-pipeline-tests.log`. The candidate digest remains
`8b0c591c522661dd8f9aad2c03d8e80ef4f387f496313bac6be185a81f2264d3`.
