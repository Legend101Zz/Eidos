# ADR 0046 — Portfolio sweep v2: diverse round 1, train-matched estimators, a paired band, and a corrected round count

Status: Accepted for implementation (step 3)

Step 2 library and component acceptance remains recorded; step 3 implements the include and lab portions.
Resolves review findings 8, 9, 10, 13, 17, 25, 29. Successive halving is deferred.

## Context

Arm 4's N04 search was wide in count and narrow in kind (all recipes were 20 raw columns by per-example normalisation by SVC, kernel ridge, GBM or ExtraTrees; discovery-gap report 3.1). Selection was argmax on one
validation split: N01 locked a 15-NN with 0.991 validation and 0.953 train-CV over logistic regression with 0.965 and 0.975, about three test examples; N02's attribution is plausible but unproven (report 3.3, 3.4). The
sweep ranked by validation in every transcript that shows a config and cross-validated only the top three by validation. The only split that shows a train-to-validation shift is validation itself, which is why D03 is closed
today by a validation-ranked sweep.

Review findings that change rev 1: (9) the `validation-anchored` scheme fitted on train plus other validation folds, but export and the evaluator fit on train only, so the estimator was not the shipped pipeline; (10) the
lock evidence was a re-score of the data that selected the recipe, and `se_b * sqrt(2 ln m)` is an approximation, not a bound; (13) a mode named `confirm` re-scores the same data; (17) `rounds()` in
`domain/exploration.py:364-396` counts a round as evaluated only when `selection_evidence` finds a receipt from the rule's scorer on the rule's data in the job or its lineage, so a round whose jobs are `refine` or `rescore`
jobs is never evaluated and patience never fires; (25) round 1 unhalved costs about 150-200 s on an N04-sized card [reviewer-measured fit times], so halving can wait.

## Decision

Add `eidos.search.recipe-portfolio` 1.0.0 (modes `portfolio`, `refine`, `rescore`, `export`) and change the lab's re-lock and round accounting.

**Round 1** (`include: "auto"`, about 150-250 recipes, no halving) is diverse by construction across eight classes: `linear`, `screen`, `lowrank`, `generative` (LDA, QDA, per-class GMM k = 1..6), `neighbors`,
`kernel` (SVM-RBF C x gamma grids), `trees` and `neural` (sklearn MLP seed ensembles), plus four scouts ({SVM-RBF, kNN} x {`per_example_center`, `per_example_standardize`}). Profile priors, when a profile exists,
size grids and never remove a class. It is widened (denser grids, more seeds for stochastic models, bigger ET/MLP) rather than halved; successive halving returns only if round 1 exceeds 350 s on more than 10% of
harness instances, and then as family-protected halving with folds as the resource. The component runs recipes in one persistent child killed and restarted on a per-recipe timeout (reusing the process-group and scrubbed-environment
discipline of `recipe_sweep._run_group`) and stores out-of-fold predictions for every fully cross-validated recipe (`recipe-oof/1`).

**Estimators match the shipped fit.** Scheme `cv-train` (default): repeated stratified K-fold on train only (K = 5; R = 2 below 1,000 rows; group-aware with a `groups` port) selects the recipe; the chosen recipe and its
runners-up are then fitted on train and scored once on validation (`validation_check`). Scheme `train-fit-validation-score` (shift detected): every recipe is fitted on train only and scored on validation rows, one fit per
recipe, which is exactly the pipeline export ships; there is no untouched split on such a card and the table says so. `scheme: auto` uses `eidos_recipes/shift.py` (logistic domain classifier, DeLong lower bound on AUC above
0.6), shared with the profile later so the first live smoke does not depend on it.

**Paired band.** The best recipe b has the highest mean. A recipe r is in the band when `mean_b - mean_r <= se(d_r)`, where `d_r` is the paired difference to b (over folds, or over validation rows) and
`se(d_r) = sd(d_r)/sqrt(count)`. The chosen recipe is the in-band, export-eligible recipe with the lowest `complexity` (ties: higher mean, lower se, id). The fold-score SE is known to be optimistic (fold fits share data), so
the band is a heuristic, not a test. R1 froze the multiple (1.0) and structural complexity order on design generators before R1 holdout scoring; the multiple was swept over {0.5, 1, 1.5, 2} on design generators only, and
an ablation applied a random permutation of the complexity order to show how much selection depends on it. The round-2 decision below supersedes this structural prior after R1 exposure and before epoch-2 scoring.

**Table content that replaces rev 1's optimism bound.** `recipe-portfolio-table/1` (slot `portfolio_table`, not `evidence_table`, which the study adapter would match and reject) records `winners_curse_scale_indep = se_b *
sqrt(2 ln m)` labelled "approximate expected maximum of m independent draws under no true differences; neither an upper bound nor corrected for fold-sharing", always shown beside the validation check. It also records
`class_contrasts` (linear vs best nonlinear, qda vs best gmm, screened vs unscreened, low-rank vs standardised) as estimated achievable gain in the task metric with paired intervals, which order the investigator's hypotheses after
round 1. `validation_check` is untouched by selection only for the first portfolio; the packet states how many candidates have been scored on validation since.

**`rescore`** (renamed from `confirm`) re-scores the incumbent and runners-up on resampled train partitions: same data, lower partition variance. Its job purpose is `reproduction`; it is never given the `confirmation` purpose
(reserved by `domain/exploration.py` for finalisation and by ADR 0021 for assessor evidence), and every output says "re-scored on resampled train partitions". `refine` stops a family when its last six evaluated neighbours
gain less than one paired SE (`family-gain-below-se`). `train --seed` controls all model randomness; `rescore` takes an explicit `partition_seed`.

**Lab part.** The rule metric for the prepared candidate is the exported pipeline's own validation score: the training job's receipt carries `validation_accuracy` and `validation_accuracy_se` (binomial), or `validation_rmse` and
`validation_rmse_se` (delta method), under the rule scorer `eidos.project.command@<study pin>` on `data.inputs {train, validation}`, split `"validation"`. Because the metric is the same under both schemes, a mis-detected scheme
cannot silently pick the wrong lock estimator, and the rule is immutable once recorded. In `domain/exploration.py` and `handlers/exploration.py`: (1) `parse_rule` accepts `margin: {"metric", "multiple"}`; `selection_evidence`
returns `se`; `check_note` refuses a re-lock unless `challenger.value - incumbent.value > multiple * max(se_inc, se_ch)` in the rule's direction (conservative, since unpaired SEs over the same rows overstate the SE of a difference);
a rule without margin behaves exactly as today. (2) `rounds()`/`evaluated()` changes: a succeeded job that consumes outputs of an evidence-bearing challenger job in its lineage (`check`, `compare`) is evaluated; jobs of components
under `eidos.diagnose.` are observations and excluded from challenger accounting; a succeeded `eidos.search.recipe-portfolio` job whose receipt carries `round_evaluated` is evaluated, so a sub-noise `refine` or `rescore` round counts toward
patience. (3) `DEFAULT_POLICY` gains `selection_margin_multiple`, `project_command_ref` (the study sets the 1.0.9 pin) and `planner`; `rules.state` reports `exploration-policy/3` only for investigations that select it.

## Rejected alternatives

- `validation-anchored` K-fold over validation rows with all of train in each fit set (rev 1): it scored a pipeline that is not exported.
- A CV re-score job as the lock evidence (rev 1): it compares two selected maxima and is not independent of selection.
- Pooling train and validation for CV: it shrinks the SE by about 13% and makes validation part of selection; pooled refit is deferred (Q4).
- Row-subsampled halving or Hyperband fidelity: it penalises data-hungry classes first. Step 2 measures the reopen trigger before this deferred feature is reconsidered.
- Argmax on mean CV, and an unpaired band around the best recipe's SE.
- Leaving `rules.state` unchanged (rev 1's claim): verified false.

## Verification and limits

Tests: fold-digest stability; **in-fold fitting**; paired-band arithmetic on hand-built tables; train-fit scheme per-row pairing; complexity tie-break and permutation hook; OOF stored with row ids; per-recipe timeout; stable
`evidence_digest`; held-out guard (`train`, `validation`, `task`, `profile`, `groups`, `predictions` only); parity with the generated project; margin parse/accept/refuse; a property test that a rule without margin is unchanged and
replay-identical; **`rounds()` tests built from ledger-shaped jobs** (component, inputs, outputs, receipt), including a round whose only job is a `refine` and a round with the macro's `check` and `compare` jobs. Acceptance on the
harness (ADR 0043): pipeline C against A1 and A2 under the generator-level rule; selection regret and optimism against argmax-validation and argmax-CV by replay; claim-level null metrics no worse than A2; round-1 wall; evaluator-clock
versus lab-clock fit seconds.

Limits: the one-SE band is a heuristic with an optimistic SE; `complexity` is a prior; the train-fit scheme has no untouched split on shifted cards; round-1 cost is an estimate until step 2 measures it; the margin uses unpaired SEs
and is conservative; the lab margin cannot see the portfolio's CV, so the portfolio's choice and the investigator's judgement remain the pre-filter for what is prepared and locked.

## Incremental scope

Step 2 implements the library, portfolio, generated project interface, toolkit parity and model-free evidence. Lab margin/round accounting and author_source include support belong to step 3. Draft numbering is retained: 0044 is reserved for the steering/profile ADR in the sibling step-0 change. Scientific acceptance is measured separately from implementation; see the step-2 evidence report.

Step-2 design-only measurements exposed a large-n density bug: duplicate forest settings crowded out HGB. The frozen implementation now protects cost-ranked class representatives and distinct model families before duplicate settings, deduplicates reduced neural specs, and includes the compact HGB strong default (150 iterations, 31 leaves, learning rate .1, no internal early stopping). The one-SE multiple and complexity prior stay unchanged. This correction preceded all holdout scoring. Group-aware replay uses the same train partitions as the component.

Portfolio and harness scores use accuracy or negative RMSE so higher always wins; generated train receipts also expose positive `validation_rmse` and its SE for the native lower-is-better selection rule. The step-3 macro must normalize the portfolio score by task type before comparing it with a native RMSE incumbent. Multiple sweeps are design-only; holdout replay fixes the multiple at 1.0 and retains the predeclared complexity-permutation diagnostic.

The pre-rebase R1 resource probes belonged to a different family, including 20,000-row design cards; their at-least-12/96 above-350-second result does not describe R2. R2 measured 96 searches, with 8 budget-censored searches, all design/calibration cards (gen-011 and gen-014). The R2 family has at most 1,800 training rows per card. Its halving trigger did not fire: 0 observed walls exceeded 350 seconds. Full-grid completion times remain censored; no large-n completion claim follows. Halving remains deferred.

**R3 correction to the acceptance record:** the frozen, launch-pinned summary code returned `halving_trigger_assessment: NOT MET`, because uncapped timings were not assessable. That verdict is restored and retained. The later 8/96 = 8.33% upper-bound argument is a **post-hoc, family-specific interpretation**, not a replacement PASS. The original criterion was poorly specified: the 350-second overall budget reserves 20% for full-fit checks, making an observed search-wall-above-350 metric uninformative about uncapped grid cost. The post-hoc bound assumes the fixed timeout policy; it does not recover full-grid timings.

## Step-2 round-2 design calibration

The accepted step-1 API is retained at `69e1b38`. Pipeline C now passes a ledger
receipt at every scoring entry, including cache admission, controller replay and
fresh export, binds the authorized full epoch-2 index, and uses construction-only
receipts on a separate design view. Design aggregation emits no candidate verdict.

On 32 design cards, the structural complexity prior's one-SE rule had mean regret
0.72089 in chosen-recipe test-SE units; half-SE gave 0.57452 and argmax-validation
0.46819. The predeclared seed-81723 permutation at one SE gave 0.42996. We select
that **single, already measured ordering** as the production prior. The paired
band stays at 1.0; seed 81723 was not searched. `choose` retains its explicit
complexity-order API; `portfolio_choice` supplies the frozen permutation of the
completed export-eligible rows, in evaluation order. Tables retain structural
complexity as a diagnostic and identify the selected prior explicitly. This
supersedes the structural-complexity default above, not the paired arithmetic.

Five-fold single-repeat versus repeated-CV regret was 15.76062 versus 15.59568 SE
units across design cards. On IID design cards repeated CV improved 0.64714 to
0.42722; the only design regression generator improved 0.63624 to 0.0. Ten-fold
refits of outcome-blind finalists did not justify a default fold-count change.
CV train fractions are .8 (fivefold), .9 (tenfold), versus 1.0 for export; every
estimator still fits the same preprocessing and model, on train only. Full-train
visible scores, CV gaps and tenfold failures are retained in the evidence. The
refit subset is a diagnostic, not a new full-portfolio comparison.

Shifted-validation design cards have CV-argmax regret 58.28876 SE units versus
0.45314 for argmax-validation: train-only repartitioning cannot see a changed
validation distribution. The target-free shift detector and train-fit/validation
scheme remain. This design set is small, has only one regression mechanism and
no design small-n wine generator; the seeded prior depends on the completed set.
None of these observations promises improved holdout selection regret.

Four old large design cards missed the kernel class because all RBF trials hit
60-second timeouts. Above 3,000 rows, a standardised linear SVM is now the first
kernel-class representative; RBF recipes remain in the plan. This is an existing
registry family, uses every training row and every fold, and introduces no halving.

Reproduction: `claude-orch/v2/step2-r2/design_analysis.py`, its `design-analysis/`
JSON, and `coverage_probe.py`. Only design/calibration generators were scored;
this analysis spent no candidate burn. However, the production selection rule
was chosen **after the R1 pre-rebase holdout exposure**: R1's C regret .807
versus argmax-validation .630 motivated the calibration. Restricting the replay
to design cards does not make the rule independent of that earlier exposure.
Epoch 2 provides fresh instances of the same target generators, not independent
mechanisms. The R1 `v2-step2-c-v1` burn is recorded only in
`claude-orch/v2/programme-burns.json`, and is absent from the current authenticated
ledger. These generators have been scored by two candidates across epochs;
the one-of-two candidate-burn count is specific to epoch 2. The recorded Holm
p=.0029296875 omits R1; conservatively counting one additional prior primary at
p=1 gives .00390625 (eight-primary maximum: .0078125). These are sensitivity
calculations, not replacement registered verdicts.

## R3 disclosures and next-build backlog

The within-band ordering permutes **positions** in the completed export-eligible
list. It depends on evaluation order and the completed set, which timeouts can
change. Reviewer replay found that dropping one eligible recipe changed the
choice on **33/68 multi-recipe-band cards**. This fragility remains in the frozen
candidate. A fixed per-recipe hash rank and design evidence across seeds are
deferred to the next build; changing the ordering requires a new frozen candidate
and candidate evaluation, not a reporting correction.

The summary-added `small_n_selection_quality` criterion remains **NOT MET** and
is **descriptive and under-powered**. It covers one 178-row gen-008 source,
four correlated re-splits, and 36 test rows per split (one effective source).
Regret corresponds to C losing 6 test rows versus argmax-validation losing 5
relative to the best completed recipe across the four splits; these are not
independent observations. Below roughly 200 source rows, selection has not been
shown to beat argmax-validation. This supplements the frozen component method
card, whose bytes cannot be edited in this round. Add a small-n **design
generator before the next calibration**, and carry this limit into the next
method card; do not tune on gen-008's holdout result.

The epoch-2 comparator pin also includes `study/practice/*.py`, so adding
`pipeline_c.py` changed the observed digest from `9a8d6f3f02df0774efed8cf28f46cdc9d50355ce331736bdbe00c567447923a7`
to `a2072f253560bcf188417dc1ec8d43c1bf6413fbdd74bef582b2d540c55de218`.
Excluding only that file exactly reproduces the pin. Authenticated ledger event
481 (`comparator-source-digest-disclosure`, hash
`981214da499ad380994c61dd6de0f8867c42c1739223a75084b3acb1e4c0f447`)
records the change without altering any past entry or epoch pin. Epoch-2
harness-validation on this checkout still refuses the source mismatch; epoch 3
must explicitly re-pin the reviewed comparator source. No burn or evaluation
was performed for this disclosure.
