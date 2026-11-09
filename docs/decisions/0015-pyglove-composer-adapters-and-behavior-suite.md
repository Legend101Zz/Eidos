# 0015: PyGlove and Composer adapters, and a CheckList-style behaviour suite

Status: accepted (2026-11-09, release 1.1.0). **Supersedes ADR 0009** ("PyGlove and MosaicML Composer adapters are
deferred; CheckList is partly adopted").

**Context.** ADR 0009 deferred both optional adapters of the design's table "How the four scientific libraries
enter this design", and recorded CheckList as partly adopted. The owner asked to reverse that for 1.1.0 (brief
"Eidos 1.1.0", item 9): build the adapters as optional worker-side packages, and add a `behavior-suite/1`
capability. ADR 0009's reversal conditions 2 and 3 (pinned versions, hook-phase fixtures run per pinned version)
are met below; condition 1 (a user project that needs them) is replaced by the owner's request.

**Feasibility (evidence before design).** Checked on 2026-11-09 by installing into fresh environments on the
owner's Mac (macOS arm64) and resolving for Linux x86_64 (`tmp/feas9/` scripts, summarised here):
- **PyGlove**: stable releases 0.4.0 (2023-07) to 0.4.5 (2025-07-15); daily `0.5.0.dev` nightlies show active
  development. 0.4.5 is a pure-Python wheel with two small dependencies. Pinned: `pyglove==0.4.5`, CPython 3.12,
  `torch==2.7.0` CPU. Verified: `@pg.symbolize` builders returning `torch.nn` modules, `pg.oneof` spaces,
  deterministic `pg.iter` sweeps, `pg.materialize` by DNA, `pg.patch`/`rebind` transforms, and `to_json`/`from_json`
  round trips with equal `sym_hash`. Gotcha recorded: `pg.sample` without feedback loops forever (not used).
- **MosaicML Composer**: the last release is `mosaicml==0.32.1` (2025-07-26); the upstream repository is archived
  (last push 2026-04-29; a checkpoint-deserialisation hardening commit was never released). 0.32.1 needs Python
  3.10+ and `torch>=2.6,<2.7.1`. Pinned: `mosaicml==0.32.1`, CPython 3.12, `torch==2.7.0`, `torchvision==0.22.0`
  CPU, `numpy==2.2.6`, `torchmetrics==1.7.4`. A tiny CPU `Trainer` run, checkpoint save and resume work and are
  deterministic with a fixed seed. **Security:** 0.32.1 loads checkpoints with pickle (`weights_only=False`), so
  the adapter documents that only checkpoints written by the same run may be loaded. Composer is frozen: no fixes
  will come; a future torch upgrade will end support.

Neither library is blocked in a way that prevents a sound adapter, so neither adapter was stopped.

**Decision.**
1. **Packaging.** Two optional worker-side packages, `packages/eidos-pyglove` (0.1.0) and `packages/eidos-composer`
   (0.1.0). They install into the research project's environment. They are **not** members of the lab's uv
   workspace and are not dependencies of `eidos-lab` or `eidos-worker-sdk`; the lab server and the SDK gain no
   dependency. Each has its own CI job with exact pins in its own fresh environment (`adapter-pyglove`,
   `adapter-composer`; `scripts/ci/adapter_*.sh`).
2. **PyGlove adapter** (`eidos_pyglove`): a pinned search-space and transform adapter for compatible symbolic
   objects. It enumerates and materialises candidates by DNA, applies `pg.patch` transforms on clones and records
   them, and exports the **materialised PyTorch builder** (`eidos-pyglove-builder/1`: the symbolic builder JSON)
   with the **transform revision** (`sha256` over builder JSON, patches and PyGlove version). Loading checks the
   revision and rebuilds with `from_json(auto_import=False)` from allowed modules only; a tampered export is refused.
3. **Composer adapter** (`eidos_composer`): a Composer `Callback` that fires `eidos-pytorch` training-extension
   phases (`PhaseRunner`) only for event phases whose meaning was verified on 0.32.1 with hook-phase fixtures on a
   short synthetic batch (phase counts, gradient/parameter state at each phase, restore of extension state on
   resume, and identical final parameters after resume). Phases without a verified Composer equivalent are
   unsupported: registering a hook for them fails. Gradient accumulation (several microbatches per batch) is
   refused, because Composer fires `AFTER_BACKWARD` per microbatch while Eidos fires `after-backward` once per
   update. The verified map (64 samples, batch 16, 2 epochs, evaluation and checkpoint every epoch; counts for the
   uninterrupted run / the run resumed from epoch 1): `after-model-construction` ← `Event.INIT` (1/1);
   `after-backward` ← `Event.AFTER_BACKWARD` (8/4); `after-optimizer-step` ← `Event.BATCH_END` (8/4);
   `on-evaluation` ← `Event.EVAL_START` (2/1); `on-checkpoint-save` ← the callback's `state_dict()` (2/1, once per
   checkpoint written); `on-checkpoint-load` ← the callback's `load_state_dict()` (0/1, only for a real resume, checked
   against the restored timestamp at `AFTER_LOAD`). After resume the extension state equals what was saved and the
   final parameters match the uninterrupted run within 1e-6. Unsupported: `before-model-construction`,
   `before-optimizer-construction`, `after-gradient-unscale`, `before-optimizer-step`. Differences from
   `eidos-pytorch`'s own loop are listed in `packages/eidos-composer/README.md` (a load hook sees restored weights but
   not yet the optimizer state; Composer has stepped the scheduler by `BATCH_END`).
4. **`behavior-suite/1`** (`components/eidos.behavior.suite` 0.1.0, standard library): CheckList-style invariance
   and direction-of-change tests. Domain authors supply the transformations and groups through the templates
   `eidos_behavior.templates.invariance/direction/suite`; the model predicts once over originals and transformed
   examples; the scorer reads the recorded raw predictions (it never runs a model), so another scorer release can
   re-score them. Missing predictions make a test `incomplete`, never a pass.

**Consequences.**
- `docs/release/SUPPORTED.md` moves PyGlove and Composer from "Deferred integrations" to supported optional
  adapters with their exact pins, and CheckList from "partly adopted" to adopted through `behavior-suite/1`.
- Composer support ends when its pinned torch range does: the adapter refuses other Composer versions.
- Not covered: PyGlove search algorithms with feedback (evolution, RL controllers) as Eidos-managed searches;
  Composer algorithms (`required_on_load`) beyond the callback phases; CheckList's MFT (minimum functionality)
  test type and its capability-matrix UI.
