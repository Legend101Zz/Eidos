# eidos.pytorch.train 1.0.0 (artifact-op, change-weights; project environment)

**Question.** Train (or resume, or branch) the researcher's own model through its Eidos adapter, with
exactly the requested number of updates, and publish a complete, branchable checkpoint plus a receipt of
what actually happened.

**Config.** `adapter` ("module:attr", required), `steps` (absolute target, required), `seed`, `builder`
(handed to `adapter.build` as `config["builder"]`), `adapter_config` (free dict for the adapter),
`balance_groups` (handed to the adapter), `checkpoint_every`, `publish_intermediate_checkpoints`,
`batches_per_update`, `torch_threads` (default 1), `stream_seed`, `python_path` (extra import
directories, relative to the component source or absolute), `extensions` (list of `{spec:
"module:factory", name?, phases?, config?}`), `branch` (below), `verify_no_change`.
The adapter supplies the data stream (`train_batches(config) -> index -> batch`, a pure function of the
batch index), `loss`, `make_optimizer` (SDK adapter protocol); an adapter that defines `train(plan,
context)` owns the whole loop instead.

**Resume / branch.** Input `parent_checkpoint` plus `config.branch = {migration, stream_policy, seed,
intervention, restore_class}`. Every parent state entry must be declared (copy / transform / init / drop);
`transform` and `init` take `mapping` `{fresh: true | [param names]}` or `{value: ...}`. Dropping optimizer
or scheduler state is rejected (use `transform {fresh: true}`), as the trainer cannot run without them.
`stream_policy: paired` continues the parent's data stream; `independent` switches to `seed`.
All-copy = restore class `exact`; anything else `warm-start`. A stronger claim is refused.
`verify_no_change` additionally retrains from scratch and records whether the no-change continuation
reproduced the uninterrupted trajectory bit for bit (`no_change_restore_match`).

**State.** Saves `component.train-meta` (schema `train-meta/1`: seed, stream_seed, builder, adapter) and
`component.<extension name>` for each stateful extension.

**Receipt.** `examples, updates, start_step, end_step, wall_seconds, final_loss, phase_counts,
restore_class, state_fingerprint, model_fingerprint, extensions, checkpoints, metrics`.
`checkpoints[slot] = {step, state_entries, complete, builder}`.

**Cannot support.** That training recipes generalise; that a checkpoint from a different torch build
restores bitwise; exactness for loaders that cannot be repositioned.

**Tests.** Fixtures use a copy of the switch example under `fixtures/selftest` (author tests).
