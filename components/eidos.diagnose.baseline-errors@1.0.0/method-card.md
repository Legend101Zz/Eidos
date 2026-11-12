# eidos.diagnose.baseline-errors — method card

**Question.** Why does the baseline get its validation examples wrong, and what kind of change is most likely to help?
Run it first, after the baseline has been reproduced and before changing the model: it is cheap (seconds on a few
thousand rows) and it tells mechanisms apart that look alike from the outside. A stronger model is not the answer to an
input offset; an input offset is not a spatial translation.

**When to use which component.** Diagnosis first (this component), then a broad sweep (`eidos.search.recipe-sweep` with
`config.template`), then targeted challengers built on what both showed. If the top hypothesis is supported, put its
preprocessing in the sweep (the template already crosses `none`, train-fitted `standardize`, `per_example_center` and
`per_example_standardize` with every model family) and check its falsifier. If nothing is supported
(`no-transfer-shift-detected` ranks first), the sweep over model families is the next step.

**Inputs.** NPZ arrays: `train` and `validation` with `x` (examples first; further dimensions are flattened, and a 3-D
`x` is read as images), `y` and optionally `ids`; optional `predictions` (the baseline's validation predictions: `ids`,
`predictions`; aligned by id), optional `groups` (`ids` and a group array) and optional `task` (`task_type`). Without
baseline predictions, the error slices use a quick ridge reference fitted on train, and the report says so. The task type
comes from `config.task_type`, then `task.task_type`, then the labels (integers with at most 50 values: classification).

**What it reports** (`baseline-diagnosis/1`):

- *Split statistics.* Per-feature standardized mean difference and standard-deviation ratio between validation and train
  (summaries and the ten most shifted features), per-example mean, standard deviation and L2 norm quantiles for both
  splits, and the label distribution.
- *Error slices* of the baseline: overall, by class (and the most frequent confusions) or by target quantile for
  regression, by input-norm quantile, by input-mean quantile, and by any group (`config.group_keys` names arrays in the
  validation NPZ; the `groups` port is another route).
- *Invariance probes.* Quick refits (ridge and k-NN, each followed by train-fitted standardization) under five input
  transformations: `none`; `per_example_center` (removes an additive offset); `per_example_standardize` (also removes a
  gain); `split_standardize` (each split standardized with its own feature statistics: a feature-wise shift); and
  `shift_invariant` (magnitudes of the discrete Fourier transform of each centered example, 2-D for an image layout and
  along the feature order otherwise: unchanged by a circular translation). Each probe records the validation score of a
  model fitted on train, a cross-validated score within train and one within validation, at equal sample sizes. The
  metric is accuracy, or R² for regression.
- *Ranked hypotheses.* `per-example-additive-offset` (centering gain), `per-example-gain-change` (standardization gain
  beyond centering), `feature-wise-distribution-shift` (split-wise standardization gain beyond per-example
  normalization), `spatial-or-positional-shift` (shift-invariant gain beyond centering; centering first removes the zero
  frequency, so an offset is not credited to translation), `information-loss` (judged representation by representation: within-train CV minus validation's learnability, the
  larger of its within-validation CV and the train-fitted model's validation score, less one fold-noise standard error,
  taking the smallest gap over the reference and every probe that makes validation more learnable: if some helpful
  representation is about as learnable in validation as in training, nothing is lost), and `no-transfer-shift-detected`
  at `min_effect` (default 0.02, and for accuracy at least five validation examples' worth). Each has
  a statement, a **prediction** (what a recipe using it should gain) and a **falsifier** (the observation that would
  refute it), its evidence and a suggested next step. `supported` means its score reaches `min_effect`.

**Provenance.** The report records the component release, seed, numpy and Python versions, the SHA-256 digest of every
input it read, its configuration, a digest of the rows sampled for the probes, and the ports read. The same inputs, seed
and configuration give the same report.

**What it refuses.** Only the ports `train`, `validation`, `predictions`, `groups` and `task` are read; another port
(such as `test`) is refused. Every array name it may open (`x_key`, `y_key`, `ids_key`, `group_keys`, `prediction_key`,
`groups_key`) is checked before any input is read, and one with a word beginning `test`, `holdout` or `heldout`
(`test_predictions`, `testfeatures`) is refused, as is a `group_keys` entry
that is not an array of the validation input. It never needs, opens or
names a test file.

**Limits.** Exploratory: it makes no claim and its numbers are selection evidence on visible validation. The probes are
quick models on at most `max_train_rows` / `max_validation_rows` rows (3000 / 2000 by default): a cause the quick models
cannot express (an interaction only a deep model sees) can be missed, and a plant that does not hurt them is reported as
no shift. The shift-invariant probe assumes circular translation and the layout it records (`layout.assumed` is true
when it was inferred); for tabular data pass `layout: tabular` to skip it. Hypotheses are not exclusive: several can be
supported at once, and the ranking says which explains most. Confirm a chosen remedy like any candidate: lock it, then
assess it independently on data the diagnosis never saw.
