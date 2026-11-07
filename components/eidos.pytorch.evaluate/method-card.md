# eidos.pytorch.evaluate 1.0.0 (observe; project environment)

**Question.** What does this checkpoint predict on a deterministic evaluation draw, as paired rows
(original and twin), ready for a pair scorer?

**Config.** `adapter` (required), `builder` (default: the builder saved in the checkpoint's train-meta),
`adapter_config`, `evaluation` `{split, data_seed, pairs, cluster, ...}` handed to
`adapter.evaluation_rows(model, split, config)`, `python_path`. `cluster` defaults to the training seed
recorded in train-meta so rows from independently trained models stay separable for cluster-robust
analysis.

**Output.** `predictions` (labeled-binary-pairs/1; csv columns `pair_id, reference_logit,
variant_logit, reference_label, variant_label, stratum, cluster`). The receipt's `metrics` are a
convenience copy computed from the rows; `lab.pairs.by_group` is the authoritative scorer.

**Observation contract.** Loads weights strictly into a fresh model in eval mode under `no_grad`; the
parent checkpoint is read-only. Wrong builder shapes fail loudly (strict load); a wrong builder that
loads is caught by `eidos.pytorch.behavior-check`, not here.

**Tests.** Author fixtures only.
