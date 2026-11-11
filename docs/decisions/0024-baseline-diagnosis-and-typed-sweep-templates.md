# 0024: Baseline error analysis and typed recipe-sweep templates

Status: accepted (2026-11-11, round 3, ticket G2; author-tested, not yet exercised in a pilot). Extends ADR 0022 (recipe
search) and ADR 0020 (investigator playbook and pinned facts). D2 report, recommendations 2 and 3.

## Evidence (D2 report)

- In D03 arm 4 treated an intensity offset (a +.35 acquisition pedestal) as a spatial translation, tried CNNs, spatial
  augmentation and SVMs on raw pixels, and stopped with 20 minutes left. Every coding-agent arm that beat it removed the
  offset (per-image centering or standardization, background subtraction); centered 3-NN beat the CNN and SVC
  experiments. Better preprocessing, not a stronger model, was the decisive mechanism.
- The general sweep (`eidos.search.recipe-sweep@1.0.0`) was used in 0 of 8 arm-4 runs: it needs an authored project
  command and CLI glue. Every search was an authored script or job list.

## Decision

### `eidos.diagnose.baseline-errors@1.0.0` (new, numpy)

A reusable first experiment after the baseline. Inputs are NPZ arrays (`x`, `y`, `ids`): `train`, `validation`, and
optionally the baseline's validation `predictions`, `groups` and `task`. Output `report` (`baseline-diagnosis/1`):

- **Split statistics**: per-feature standardized mean difference and standard-deviation ratio between validation and
  train (summaries and top features), per-example mean, standard deviation and L2-norm quantiles, label distributions.
- **Error slices** of the baseline: by class (with top confusions) or target quantile, by input-norm and input-mean
  quantile, by any group (`config.group_keys` in the validation NPZ, or the `groups` port). Without predictions, a quick
  ridge reference fitted on train, and the report says so.
- **Invariance probes** that distinguish mechanisms: quick ridge and k-NN refits on train (each followed by train-fitted
  standardization) under `none`, `per_example_center`, `per_example_standardize`, `split_standardize` and
  `shift_invariant` (Fourier magnitudes of the centered example, 2-D for an image layout). For each: the validation score
  when fitted on train, and cross-validated scores within train and within validation at equal sample size.
- **Ranked hypotheses**, each with a statement, a prediction, a falsifier, evidence and a next step:
  `per-example-additive-offset` (centering gain), `per-example-gain-change` (standardization gain beyond centering),
  `feature-wise-distribution-shift` (split-wise standardization gain beyond per-example normalization),
  `spatial-or-positional-shift` (shift-invariant gain beyond centering; the zero frequency is removed first, so an offset
  is never credited to translation, which is the D03 confusion), `information-loss` (per representation: within-train
  CV minus validation's learnability, the larger of within-validation CV and the train-fitted validation score, less
  one fold-noise standard error; the smallest gap over the reference and every probe that makes validation more
  learnable), and `no-transfer-shift-detected` at `min_effect` (default 0.02, and for accuracy at least five validation
  examples). Review of the author tests showed that comparing only the best scores of each split read a weak but
  transferable shift-invariant representation as lost information; the per-representation gap fixes that.
- **Provenance**: component release, seed, numpy and Python versions, the SHA-256 of every input read, the configuration
  and a digest of the sampled rows. Deterministic for the same inputs, seed and configuration.

It reads only `train`, `validation`, `predictions`, `groups` and `task`; any other port, and an array named after a
test or held-out split, is refused. It is general: tabular, image-as-vector (a 3-D `x` or a square feature count, or an
explicit `image_shape`) and sequence data use the same probes. Development-card kinds appear only in tests.

### Typed templates for `eidos.search.recipe-sweep` (release 1.1.0)

`config.template = {name: quick|broad, task_type?, preprocess?, families?, finalists?, cv_folds?, seeds?, rank_by?,
timeout_seconds?, max_rows?}` replaces `command`, `space`, `objective` and `recipe` (giving both, a `source` bundle, or a
stopping rule other than `none` is refused). One call, no authored code:

- The sweep runs its own bundled runner (`templates/fit_recipe.py`, numpy; its SHA-256 is recorded) on the `train` and
  `validation` NPZ ports, through the same private work directories, scrubbed environment, process groups and redaction
  as any command sweep (the engine loop is shared, `_search`).
- Recipes cross preprocessing (`none`, train-fitted `standardize`, `per_example_center`, `per_example_standardize`) with
  families: `linear` (ridge or L2 multinomial logistic), `knn`, `kernel` (RBF kernel ridge), `mlp` (one hidden layer),
  plus `svm` and `gbm` when scikit-learn is importable in the job's environment; otherwise they are recorded under
  `template.skipped_families`, not silently dropped. `quick` has 24 recipes, `broad` 52.
- **Bounded screening, then training CV of finalists.** Every recipe is fitted on train and scored on visible validation
  (every seed). The budget defaults to the recipe count and the job's wall limit caps it. The best `finalists` (default
  3) are cross-validated within train (`cv_folds`, default 3). The winner is the best finalist by `rank_by`:
  `validation` by default, because cross-validation within train cannot see a train-validation shift (in D03 it would
  have hidden exactly the offset that mattered); `train-cv` is available. Each finalist records `transfer_gap`. If no
  finalist was cross-validated (none requested, the budget ended first, or every CV failed), the best screened recipe
  wins on validation with `cross_validated: false` (none under `train-cv`). The evidence digest excludes wall seconds
  in the finalists' CV records too, so a re-run with the same seeds gives the same digest.
- Every attempted recipe, failed ones included, is in `evidence_table` (`stage` finalist or screened); `best_recipe`
  carries a `reproduce` command (`fit_recipe.py train`), which writes `validation_predictions.npz`, so the winner can be
  run as a project command, locked and assessed independently.

The command-sweep mode is unchanged, and its tests pass unchanged.

### Config contracts: `anyOf`

`eidos.domain.contracts.validate_config` gains `anyOf`: the value must satisfy at least one listed schema as well as
the rest of the schema. The sweep's schema is now `anyOf: [{required: [template]}, {required: [command, space,
objective, budget]}]`, so a decision with neither is still refused at acceptance; nothing that was refused before is
accepted now, except the new template form. The template object is typed (`additionalProperties: false`, enums for
name, families, preprocessing and `rank_by`).

### Guidance: diagnose, sweep, challenge

The investigator tool guide (context, `TOOL_GUIDE`) now says: (1) after the baseline, run the diagnosis with the
baseline's validation predictions; (2) run a broad template sweep in one call; (3) then targeted challengers (a command
sweep or authored candidates) that test the top hypothesis's prediction. All exploratory; lock the winner before
confirming it. Diagnosis jobs are pinned with sweeps in the context's search facts (top hypothesis, ranked causes,
report id). The playbook text is owned by ADR 0023 and unchanged here. Both method cards describe when to use each
component.

### Lab and standalone

Both components are standard component bundles run through the worker SDK, so they work in the lab (arm 4 now also
installs `eidos.diagnose.baseline-errors` in the `default` environment) and in the standalone toolkit
(`study/toolkit/build_toolkit.py` copies `components/`; `run_component.py` runs them), so arm 2 gets the same code.
CI's component job installs scikit-learn in the project environment; the tests also cover an environment without it.

## Not done, and limits

- The probes are quick linear and nearest-neighbour models on bounded rows: a cause only a deep model expresses can be
  missed, and a plant that does not hurt the quick models is reported as no shift. Hypotheses are not exclusive.
- The shift-invariant probe assumes circular translation and a recorded layout (inferred layouts say so).
- Template families are deliberately small and CPU-only; they are a broad first screen, not a tuned search. A
  scikit-learn-free environment has four families.
- Selecting among many recipes on validation makes the winner's validation score optimistic. The template records it as
  selection evidence; confirmation stays independent (ADR 0021).
- No pilot has used either component yet; the expected effect (better hypotheses and preprocessing, fewer irrelevant
  trials) is a hypothesis.

## Addendum (review R16): held-out names, repeated seeds, pinned releases

- **Every array name the diagnosis may open is checked before any input is read.** `prediction_key` and `groups_key`
  now go through the same held-out check as `x_key`, `y_key`, `ids_key` and `group_keys`, so `prediction_key:
  "test_predictions"` or `groups_key: "test_labels"` is refused before a file is opened, and provenance's "no test data
  read" stays true. Array names are also refused when any word of the name begins with `test`, `holdout` or `heldout`
  (`testfeatures`, `x_test1`).
- **A repeated seed is refused, and one evaluation's failure never discards the sweep.** `seed_policy.seeds` and
  `template.seeds` refuse repeats at validation (a repeated seed measures nothing new). Independently, each evaluation
  takes its own directory (a repeated label gets `-r2`, `-r3`, ...), and an error while preparing a working directory
  is recorded as that evaluation's failure; the evidence already gathered is still written.
- **Data files whose names begin with a held-out word are refused.** The no-source `command.copy` list and non-code
  source-bundle paths are refused when any word of a name begins with `test`, `holdout` or `heldout` (`test1.csv`,
  `testfeatures.csv`, `data/x_test2.npy`), not only when the word is exactly a split name. Code files (`.py`, `.sh`, `.md`,
  `.toml`) keep their exemption. Metric names keep the exact-word rule, so `contest_score` is still a metric.
- **Release 1.0.0 stays installable.** `components/eidos.search.recipe-sweep@1.0.0/` is the 1.0.0 release byte for byte
  (its source bundle digest is pinned in `tests/components/test_pinned_releases.py` and equals main 7e6641d's), beside the
  current `components/eidos.search.recipe-sweep/` (1.1.0). A directory is named either `<id>` (the current release) or
  `<id>@<release>` (an earlier release kept so pinned references resolve). A fresh installation publishes both; the
  study arms bind only the current release. The standalone toolkit lists both, and keeps `<id>@<release>` directories off
  its shared module path so each runs its own module. The pinned 1.0.0 is not patched: changing it would make it a
  different release. The fixes above are in 1.1.0; a study that wants them pins 1.1.0.
