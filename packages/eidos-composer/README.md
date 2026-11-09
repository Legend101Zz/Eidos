# eidos-composer

Optional worker-side adapter that fires Eidos training-extension phases from a
[MosaicML Composer](https://github.com/mosaicml/composer) `Trainer`. Version 0.1.0.

It is not part of the lab server, not a dependency of `eidos-worker-sdk` and not in the root uv
workspace; installing it never changes torch. It depends on `eidos-pytorch>=1.1,<2` (for `PhaseRunner`
and the phase vocabulary) and `mosaicml>=0.32.1,<0.33`; Python 3.10 or newer (Composer 0.32 needs it).

```python
from composer import Trainer
from eidos_composer import EidosPhaseBridge

bridge = EidosPhaseBridge()                       # PhaseRunner(supported=SUPPORTED_PHASES)
bridge.runner.register("after-backward", my_clipper, name="clip")      # hook(**event)
trainer = Trainer(model=model, train_dataloader=dl, callbacks=[bridge], ...)
trainer.fit()
receipt = bridge.receipt()   # Composer version, phase map, phase and hook counts
```

Hooks are called as `hook(**event)` and must accept `**kwargs`. Events carry `step`, `model`
(`state.model`, i.e. the `ComposerModel` as the trainer holds it) and `optimizer`
(`state.optimizers[0]`, when one exists), plus `state` for the checkpoint phases.

## Versions

Composer is archived upstream; 0.32.1 is its final release and the only one this adapter accepts
(`SUPPORTED_COMPOSER_VERSIONS`). `EidosPhaseBridge()` and `check_composer_version()` raise
`ComposerVersionError` for any other version: the mapping below was verified on exactly this release,
not inferred. The tests ran on CPython 3.12, mosaicml 0.32.1, torch 2.7.0 (CPU), torchvision 0.22.0,
numpy 2.2.6, torchmetrics 1.7.4 (`requirements-ci.txt`).

## Verified phase map

Verified by `tests/test_bridge.py` on a synthetic job: 64 samples, batch 16 (4 updates per epoch), 2
epochs, plain SGD, an eval dataloader every epoch (`eval_interval="1ep"`), a checkpoint every epoch
(`save_interval="1ep"`), CPU, seed 0. Observed counts of an uninterrupted run, and of a run resumed from
the epoch-1 checkpoint (`ep1-ba4`) to the same 2 epochs:

| Eidos phase | Composer source | `step` | uninterrupted | resumed |
| --- | --- | --- | --- | --- |
| `after-model-construction` | `Event.INIT` | 0 | 1 | 1 |
| `after-backward` | `Event.AFTER_BACKWARD` | updates completed before this one (0..7) | 8 | 4 |
| `after-optimizer-step` | `Event.BATCH_END` | same index (0..7) | 8 | 4 |
| `on-evaluation` | `Event.EVAL_START` | updates completed (4, 8) | 2 | 1 |
| `on-checkpoint-save` | `Callback.state_dict()` | updates completed (4, 8) | 2 | 1 |
| `on-checkpoint-load` | `Callback.load_state_dict()` | step stored in the checkpoint (4) | 0 | 1 |

The firing order of the uninterrupted run is pinned exactly: `after-model-construction`, then per
update `after-backward` and `after-optimizer-step`, and at each epoch end `on-evaluation` followed by
`on-checkpoint-save`.

What the tests establish for each phase:

- `after-backward`: gradients exist and are non-zero, parameters equal the values before the update, and
  for plain SGD the later parameters equal `old - lr * (gradient seen here)` to 1e-6, so the gradient seen
  is the whole update's. Composer fires `AFTER_BACKWARD` once per microbatch, so this holds only with one
  microbatch per batch (see Refusals). With a `GradScaler` the gradients are still scaled, as in
  `eidos_pytorch.train_loop`.
- `after-optimizer-step`: parameters differ from the `after-backward` snapshot; gradients are still present
  and unchanged (Composer zeroes them at the start of the next batch). Composer steps the scheduler
  (per-batch schedulers) and has advanced the timestamp before `BATCH_END`, whereas
  `eidos_pytorch.train_loop` fires this phase before its scheduler step; `step` is corrected for the
  advanced timestamp (`step` is the update that just finished).
- `on-evaluation`: fires once per evaluator per evaluation round, so two evaluators give two firings per
  round. `trainer.eval()` calls fire it too.
- `after-model-construction`: `Event.INIT` of every `Trainer`, including one that resumes. It fires before
  any checkpoint is loaded, so on a resume the weights are not yet restored; a hook that needs restored
  weights should use `on-checkpoint-load`. The optimizer is present only if you passed one (Composer builds
  its default optimizer after `INIT`).
- `on-checkpoint-save`: Composer calls each callback's `state_dict()` once per full checkpoint written
  (2 calls for 2 checkpoints). `save_weights_only=True` checkpoints do not call it, so that phase does not
  fire for them. Composer's sharded (FSDP) checkpoint loader also calls `state_dict()` to build a load
  template; the bridge does not fire between `BEFORE_LOAD` and `AFTER_LOAD`, so a load is never counted as a
  save (unit-tested; sharded checkpoints themselves are not run, see below).
- `on-checkpoint-load`: fires from `load_state_dict()` only when the checkpoint holds the bridge's state
  with `step > 0` (a fresh run fires Composer's `BEFORE_LOAD`/`AFTER_LOAD` once without a load path, and
  fires nothing here). It fires once. Composer loads callbacks in sorted key order, after the model and
  before the optimizer and the timestamp: observed, a load hook sees restored weights but not yet restored
  optimizer state and `timestamp.batch == 0`. At `AFTER_LOAD` the bridge checks the restored step equals the
  step stored with the extension state and raises `PhaseError` if not.

### Extension state in checkpoints

On `on-checkpoint-save` the event carries `state`, a dict `{"schema", "step", "components": {}}`; a hook
writes its state to `state["components"][name]` (picklable values; clone tensors). That dict is what Composer
stores under `callbacks["EidosPhaseBridge"]` in the checkpoint. On a resume the restored dict is passed as
`state` to `on-checkpoint-load`, and the hook restores itself from `state["components"][name]`. The tests
use an EMA of the parameters plus an update counter: after a resume from the epoch-1 checkpoint the
extension equals what it saved, and the resumed run's final parameters and EMA equal the uninterrupted
run's (`allclose`, 1e-6).

## Not mapped

`SUPPORTED_PHASES` excludes these, so `PhaseRunner(supported=SUPPORTED_PHASES).register(...)` raises
`PhaseError`, and `EidosPhaseBridge(runner)` refuses a runner that already holds hooks for them:

| Eidos phase | Why |
| --- | --- |
| `before-model-construction` | Composer receives an already-built model; nothing runs before it. |
| `before-optimizer-construction` | No event between the model and the optimizer (it is passed in, or a default is built after `INIT`). |
| `after-gradient-unscale` | The only candidate is `Event.AFTER_TRAIN_BATCH`, not part of the verified map. |
| `before-optimizer-step` | Same: no verified event strictly between the unscale and `optimizer.step()`. |

`AFTER_TRAIN_BATCH` fires once per batch even with microbatching (the test shows 4 firings against 8
`AFTER_BACKWARD` with `device_train_microbatch_size=8`), after gradient unscaling and before the step. It is a
candidate for `after-gradient-unscale` and `before-optimizer-step` in a later version, once verified.

## Refusals

- Microbatching: a configuration where `device_train_microbatch_size` is smaller than the batch size is
  refused with `PhaseError` at `FIT_START` and again per batch (Composer would fire `AFTER_BACKWARD` per
  microbatch with partial gradients). `device_train_microbatch_size="auto"` (GPU only) is refused for the
  same reason, because it can split a batch at run time. A second `AFTER_BACKWARD` within one batch (for
  example an optimizer such as LBFGS that calls its closure repeatedly) raises `PhaseError` before the
  hooks run.
- Hooks for the unmapped phases above.
- Any Composer version other than 0.32.1.
- Extension state with another schema.

## Security

Composer 0.32.1 loads checkpoints with `torch.load(weights_only=False)`, which unpickles arbitrary
objects. Load only checkpoints the same run wrote; never point `load_path` at a checkpoint from a source you
do not control. The extension state travels inside that pickle.

## Not verified

Multi-GPU or FSDP runs (sharded checkpoints, per-rank hooks), `GradScaler`/AMP-fp16 runs, closures other
than the default path, `save_weights_only`, `load_weights_only`, several evaluators, iteration-based
durations and non-CPU devices were not run; they are outside what the tests exercise on CPU.

## Tests

```bash
scripts/ci/adapter_composer.sh      # fresh venv from requirements-ci.txt, then the tests
# or, in an environment that already has the pinned stack:
PYTHONPATH=packages/eidos-wire/src:packages/eidos-worker-sdk/src:packages/eidos-pytorch/src:packages/eidos-composer/src \
  python -m pytest packages/eidos-composer/tests -q
```

The tests skip cleanly where torch or Composer is not installed.
