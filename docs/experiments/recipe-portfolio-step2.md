# Step 2: offline recipe portfolio

The component and its library are implemented under ADRs 0045 and 0046. Lab
preparation, `author_source.include`, lock margins and round accounting are step 3.
ADRs retain their draft numbers; 0044 belongs to the separate steering/profile work.

The implementation freezes the paired-band multiple at 1.0 and the library's
complexity order before holdout scoring. The design-only sweep and the randomly
permuted complexity replay are diagnostics; neither changes the candidate. There
is one primary verdict, C versus A2, with C versus A1 and selection replay labelled
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

The frozen step-2 run measured all 96 generated cards and all seven public cards.
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

Primary round-1 times were budget-censored on 20 cards. Separate visible-input
timing probes used the same frozen library and a 480-second budget, prioritizing
the lowest fraction of planned recipes attempted. 16 probes measured at least
12/96 (12.5%) instances above 350 seconds. The more-than-10%
halving reopen trigger is therefore confirmed. This is an operational finding;
halving remains deferred, and diagnostic selections do not replace the primary
results. The original candidate and test scores remain frozen.
