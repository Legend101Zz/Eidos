# Built-in scientific components

The ten components under `components/` are ordinary component bundles: a directory with
`eidos-component.json`, code, a method card and JSON fixtures in the `eidos_worker.validate` format. The lab
publishes the whole directory as the source bundle. Two example projects under `examples/` show how
an existing custom PyTorch project is adapted.

**Fixture and author-test status.** Every fixture, every `tests/components` test and every control
suite shipped here was written by the same author as the component it exercises. They are *author
tests* (each fixture file carries `"author_test": true`). They show the contract holds and catch
regressions; they are **not** independent qualification and do not make a template or assessor eligible
for authoritative assessment. Fixtures that use the switch example (`fixtures/selftest/`) are
fixture-only copies of the example, nothing more. The only check that is *shaped* like qualification is
`eidos.assessors.paired-ci/controls/suite.json` run by `eidos.qualification-runner`; because the
assessor's author wrote that suite, treat it as an author suite until someone else writes and commits one.

## Environments

| Environment | Components |
|---|---|
| **Project** (the researcher's registered interpreter with torch; CWD = `project_root`) | `eidos.pytorch.train`, `eidos.pytorch.evaluate`, `eidos.pytorch.weight-edit`, `eidos.pytorch.behavior-check`; training extension `lab.learning.group-balanced-ema` (loaded inside the train process) |
| **Lab** (needs `eidos.domain`) | `eidos.assessors.paired-ci`, `eidos.qualification-runner` |
| Either (standard library only) | `lab.pairs.by_group`, `lab.switch.balanced-data`, `eidos.templates.improves-under-budget`, `eidos.task.paired-classification-report` (NPZ inputs need numpy), `eidos.search.recipe-sweep` (its candidate command needs its own project environment) |

Project-environment components need `eidos-worker-sdk` and `eidos-pytorch` on `PYTHONPATH` (the runner
adds them) and an adapter module importable from the project root or from `config.python_path`.

## Reference table

| Component | Effect / kind | Inputs -> outputs | Main config keys | Receipt highlights |
|---|---|---|---|---|
| `lab.pairs.by_group@1.0.0` (`paired-report/1`) | observe / artifact-op | `predictions` (`labeled-binary-pairs/1`) -> `report` (`grouped-pair-counts/1`) | `group_by` (default `stratum`) | `input_rows, unique_pairs, group_counts, rejected_rows, metrics{both_correct_rate, reference_accuracy}` |
| `eidos.pytorch.train@1.0.0` | change-weights / artifact-op | `parent_checkpoint` (optional) -> `checkpoint` (`eidos-checkpoint/1`) | `adapter, steps, seed, builder, adapter_config, balance_groups, checkpoint_every, publish_intermediate_checkpoints, batches_per_update, torch_threads, stream_seed, python_path, extensions, branch, verify_no_change` | `examples, updates, end_step, wall_seconds, final_loss, phase_counts, restore_class, state_fingerprint, model_fingerprint, extensions, checkpoints, metrics` |
| `eidos.pytorch.evaluate@1.0.0` | observe | `checkpoint` -> `predictions` (`labeled-binary-pairs/1`; includes `cluster`) | `adapter, builder, adapter_config, evaluation{split,data_seed,pairs,cluster}, python_path` | `rows, split, cluster, input_checkpoint_step, builder, metrics` |
| `eidos.pytorch.weight-edit@1.0.0` | change-weights | `checkpoint` -> `checkpoint` | `edits[{entry,index,op,value}], reason` | `edits, parent_digest, parent_unchanged, storage_independent, restore_class, checkpoints` |
| `eidos.pytorch.behavior-check@1.0.0` | observe | `checkpoint` -> `behavior` (`behavior-check-report/1`) | `adapter, builder, reference_builder, adapter_config, atol, rtol, seed, python_path` | `behavior_match, strict_load_ok, max_abs_diff, builder, reference_builder, stage` |
| `lab.switch.balanced-data@1.0.0` | generate-data | - -> `dataset` (`switch-sequences/1`) | `examples, batch_size, exception_rate, mode (rounded-per-batch|bernoulli), seed` | `requested_exceptions, realized_exceptions, realized_exception_rate, realized_exceptions_per_batch, group_counts` |
| `lab.learning.group-balanced-ema@1.0.0` | change-learning / training-extension, phase `before-optimizer-step`, state `group-ema/1` | used through `train` `extensions` | `decay` | `extensions.<name>.{updates, groups_seen, extra_forward_backward_passes}` |
| `eidos.templates.improves-under-budget@1.2.0` (`evidence-plan/1`) | plan / planning | `request` (`plan-request/1`) -> `draft` (`evidence-plan/1`) | none (the request carries claim, seeds, evaluation, baseline/candidate; or `classification`: labels and per-seed predictions artifacts, ADR 0018) | `arms, seeds, jobs_planned, open_obligations` |
| `eidos.assessors.paired-ci@1.2.0` (`protocol-assessment/1`) | assess / assessment | `evidence` (`evidence-set/1`) -> `evaluation` (`protocol-evaluation/1`) | none (metrics: `both-correct-rate`, `reference-accuracy`, `accuracy`) | `outcome, units, clusters, metrics{estimate, ci_low, ci_high}` |
| `eidos.task.paired-classification-report@1.0.0` (`paired-classification-report/1`) | observe / artifact-op | `baseline`, `candidate` (`class-predictions/1`), `labels` (`class-labels/1`) -> `predictions` (`classification-pair-rows/1`) | `role`, `cluster`, `classes?`, `id_field?` | `role, cluster, examples, classes, aligned` |
| `eidos.search.recipe-sweep@1.0.0` (`recipe-search/1`) | change-weights / artifact-op | `source`, `train`, `validation`, `task` -> `evidence_table` (`recipe-sweep-table/1`), `best_recipe` (`recipe-sweep-best/1`) | `command, space{mode,parameters,seed?,samples?}, objective{metric,direction?}, seed_policy{seeds,folds}, budget{max_trials,max_seconds}, stopping{rule,patience,min_delta,target}, recipe?` | `stop_reason, evidence_digest, best_recipe_id, metrics` |
| `eidos.qualification-runner@1.0.0` | observe / artifact-op | (target source at `ports["target.source/1"].path`) -> `qualification_results` (`qualification-results/1`) | `suite, target{ref}, result_slot, outcome_key` | `target, controls, invalid_runs` |

## The adapter conventions the PyTorch components rely on

Beyond the SDK's `build(config)` and `predict(model, batch)`:

* `config["builder"]` is passed to `build`; `builder_ref(config)` (optional) names it as `"module:Class"`
  for the checkpoint receipt.
* `train_batches(config) -> (index -> batch)` (a *pure* function of the batch index; the trainer
  checkpoints the cursor, which is what makes resume exact), `loss(model, batch)`,
  `make_optimizer(model, config)`. An adapter that defines `train(plan, context)` owns the loop instead.
* `loss_terms(model, batch) -> (per_example_losses, group_ids)` for group-aware learning rules.
* `evaluation_rows(model, split, config) -> rows` with `pair_id, reference_logit, variant_logit,
  reference_label, variant_label, stratum, cluster`, so the pair scorer works unchanged on any model
  (`examples/image-project` reuses it on a second model).
* `examples(config)` is the fixed reference batch for behaviour checks.

## Checkpoints, branches and state

`train`, `weight-edit` stage a checkpoint with role `checkpoint`, schema `eidos-checkpoint/1` and describe it
in the receipt: `checkpoints: {slot: {step, state_entries, complete: true, builder}}`. `train` always
saves `component.train-meta` (seed, stream seed, builder) plus one `component.<name>` per stateful
extension; those entries must be declared by a branch migration like any other.

`train` with `parent_checkpoint` plus `config.branch = {migration, stream_policy, seed, intervention,
restore_class}` applies `apply_migration`. All-copy means restore class `exact`: the continuation is
bitwise the uninterrupted trajectory on the same torch build (`verify_no_change: true` re-trains from
scratch and records `no_change_restore_match`). Anything else is `warm-start`. `transform`/`init` take
`mapping: {fresh: true | [param names]}` or `{value: ...}`. A learning rule added at a branch needs an
explicit `init` for its state entry. `weight-edit` never touches the parent (digest and fingerprint are
re-checked) and verifies the child shares no storage with it.

## Training extensions

`config.extensions = [{spec: "module:factory", name, phases?, config}]`. `factory(config, context)`
returns an object with `phases`, `on_phase(phase, **event)`, optional `state_dict/load_state_dict`
(saved as `component.<name>`) and optional `receipt()`. `context.adapter` and `context.take_batches()`
give the batches the loss was just computed on. The module is found through `config.python_path`
(relative to the component source) or `ports["extension.source/1"]`.

## Running the tests

```
tests/components/run.sh
```

runs the pytest suite (53 tests: manifests and fixtures for every component, branch exactness,
extension state, weight-edit independence, wrong-builder detection, the handoff image audit reproduced
through the second model, qualification-runner against the assessor's suite with a broken and a blind
assessor, and the end-to-end switch replication: both-correct 0.2625 unedited and 0.844 after zeroing
the recorder weight) and then every manifest's fixtures through `python -m eidos_worker.validate`.


## Fixture data for the PyTorch components (clean checkouts)

The torch components' author fixtures read small checkpoints under `fixtures/data/`. These files are
generated, and not versioned (`*.ckpt` is git-ignored). Regenerate them before `lab component install`:

```bash
PYTHONPATH=packages/eidos-wire/src:packages/eidos-worker-sdk/src:packages/eidos-pytorch/src \
    $TORCH_PY components/build_fixtures.py
```

Source bundles carry binary files as base64 entries. A missing fixture file is reported as
*skipped*, not passed.
