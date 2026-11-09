# Worker SDK author guide

Two packages run **inside your project's own Python environment** (Python >= 3.9):

* `eidos-worker-sdk` (`eidos_worker`): runner, artifact reader/writer, run context. Standard library
  plus `eidos-wire` only. It never pulls in the lab server's dependencies, so it coexists with a
  project that pins Pydantic v1.
* `eidos-pytorch` (`eidos_pytorch`): model adapters, isolated checkpoints, migrations, fingerprints,
  phases and a small training loop. It imports your **already installed** torch lazily and never
  declares or upgrades it.

## 1. Write an artifact component

`eidos-component.json` (or `.toml` on Python 3.11+) declares the contract; `controls` lists fixtures.

```python
# pair_tools/report.py
def run(inputs, config, *, reader, writer, context):
    counts, n = {}, 0
    for row in reader.rows(inputs["predictions"]):          # csv/jsonl by media type, digest-verified
        n += 1
        g = counts.setdefault(row[config["group_by"]], [0, 0])
        g[0] += 1
        g[1] += int(row["pred_a"] == row["label_a"] and row["pred_b"] == row["label_b"])
    context.record("input_rows", n)                          # realized-condition receipt
    context.logger.info("scored %d rows", n)                 # staged as the run log
    writer.stage_json("report", counts, schema="grouped-pair-counts/1")
```

Dependency injection: the runner calls `entry(inputs, config)` and adds only the keyword arguments
the signature accepts among `reader`, `writer`, `context` (all three if you take `**kwargs`).
`inputs` maps input names to `InputBinding` objects. A returned `dict` is merged into the receipt.

* `ArtifactReader` refuses anything that is not a declared input and verifies sha256 (and size) on
  first access. Inputs are read-only.
* `ArtifactWriter` stages into `staging_dir/outputs/` atomically, one file per slot; slot names are
  `[A-Za-z0-9._-]`, symlinks are rejected, and the total is bounded by `limits["max_output_bytes"]`.
* Errors become a *failed* manifest (process still exits 0) with category `numerical`
  (`eidos_worker.check_finite` / `NonFiniteError` / `FloatingPointError`), `resource`
  (`MemoryError`, any `OutOfMemoryError`), `contract` (wire, manifest, reader/writer violations) or
  `user-code`, plus a bounded traceback tail. Failed manifests publish only the log.
* `context.model` is a stdlib JSON client only when the invocation has the `model.client/1` port.

### Test it

```python
from eidos_worker.testing import make_invocation, run_in_process, read_output_json
inv = make_invocation(tmp_path, "pair_tools.report:run", source_dir="src",
                      inputs={"predictions": (csv_bytes, "text/csv")}, config={"group_by": "g"})
manifest = run_in_process(inv)          # or run_subprocess(inv) for the real protocol
assert manifest.status == "succeeded"
assert read_output_json(inv, manifest, "report") == {...}
```

`python -m eidos_worker.validate <component-dir>` loads the manifest through
`eidos_wire.load_manifest` and runs every `controls` entry ending in `.json` as a fixture through the
real subprocess protocol. Fixture shape:

```json
{"inputs": {"predictions": {"file": "data/p.csv", "media_type": "text/csv"}},
 "config": {"group_by": "g"},
 "expect": {"status": "succeeded", "receipt": {"input_rows": 3},
            "outputs": {"report": {"json": {"x": [2, 1]}}}}}
```

Succeeded fixtures must stage only declared output ports (with matching schema if given). Missing
fixture files are reported as *skipped*, not passed. These are author tests, not independent
qualification.

## 2. Write a model adapter

Start from your existing `build(config)` and `predict(model, batch)`. Nothing assumes logits, tensor
batches, or one batch == one update.

```python
class Adapter:
    phases = ("after-backward", "before-optimizer-step")  # phases your trainer fires
    def build(self, config): ...                  # required
    def predict(self, model, batch): ...          # required; any output structure
    def examples(self, config): ...               # optional: fixed reference batch (behavior fixtures)
    def loss(self, model, batch): ...             # optional: scalar tensor -> generic loop
    def make_optimizer(self, model, config): ...  # optional (needed by the generic loop)
    def make_scheduler(self, optimizer, config): ...
    def train_step(self, model, batches, optimizer, step): ...   # optional: you own the update
    def train(self, plan, context): ...                          # optional: whole trainer
```

`load_adapter("pkg.mod:Adapter")` resolves and validates it (classes are instantiated).

**Checkpoints.** `capture_checkpoint(model, optimizer, scheduler, scaler, step=, data_cursor=,
components=)` snapshots (cloned) model, optimizer, scheduler, scaler, Python/torch/numpy/CUDA RNG,
data cursor and component state; `save_checkpoint`/`load_checkpoint` use one `torch.save` file with a
manifest (`eidos-checkpoint/1`). `restore_checkpoint` clones everything it loads and refuses silent
mismatches (state without a target or vice versa). Never branch by feeding one optimizer's
`state_dict()` to another: Adam moments end up *shared*. Prove isolation with
`verify_storage_independence(parent, child)`.

**Migration.** `apply_migration(parent_state, migration, transforms)`; every parent entry must be
declared `copy`, `transform`, `init` or `drop` — undeclared entries are rejected.

**Behavior.** Strict `load_state_dict` is not evidence a builder is right. Record
`record_behavior(adapter, model, config)` next to the weights, then call
`behavioral_restore_check(weights, "pkg.mod:Adapter", config, recorded, builder_spec=...)`, which
loads into the named builder in a **new process** and compares outputs within tolerance.

**Training.** `train_loop(adapter, config, batch_fn, steps=, batches_per_update=,
checkpoint_every=, checkpoint_dir=, resume_from=, phases=, seed=, deterministic=True)` where
`batch_fn(index)` is a pure function of the global batch index (the cursor is checkpointed; this is
what makes resume exact: on CPU with deterministic algorithms, an uninterrupted run and a resume in a
fresh process give bit-identical parameters). The result's `receipt` has examples, updates, wall time,
peak memory and phase counts. Extensions use `PhaseRunner.register(phase, hook, name)` with the
phases from `eidos_wire.manifest.PHASES`; hooks for phases the trainer cannot fire are rejected.

### Whole-trainer escape hatch

If your trainer owns an unusual optimizer, custom autograd function or multi-optimizer schedule,
define `adapter.train(plan, context)`; `run_training(adapter, plan, context)` calls it. Use
`context.record(key, value)` for receipts and `context.checkpoint(model, optimizer, step=...)` at
boundaries you truly support. Eidos then only gets the checkpoints and receipts you publish.

`devices()` reports cpu/cuda/mps. Restoring across devices is never claimed exact; treat it as
"numerically equivalent under protocol" and check tolerances.

## Wire contract: schema major and supported Python

- **Schema major: 1** (`eidos-wire/1`; `eidos_wire.WIRE_MAJOR`). The messages that cross the worker/server boundary have published JSON Schemas (2020-12) in `schemas/eidos-wire/v1/`: `invocation.schema.json`, `output-manifest.schema.json` and `component-manifest.schema.json`. They are generated from the server boundary models (`scripts/dev/gen_wire_schemas.py`; ADR 0008).
- **Supported worker Python: 3.9 and later.** The worker packages use only the standard library (no Pydantic, no jsonschema), so Pydantic v1 projects are fine. The lab server itself needs Python 3.11–3.13.
- **Validation.** The server validates every manifest it receives against the schema, then applies the semantic rules in `eidos_wire`, such as unique output slots. A violation is refused with a field-level message, for example `output-manifest violates the eidos-wire/1 schema: receipt: Input should be an object`.
- **Compatibility.** A newer minor version only adds optional fields. Readers accept unknown fields and ignore them. Removing, renaming or retyping a field is a new major (`v2`), and the server refuses majors it does not speak.
- **Golden fixtures.** `tests/fixtures/wire/v1/` holds messages emitted by the real SDK code paths (`scripts/dev/emit_wire_fixtures.py`), plus deliberately invalid `invalid-*` variants. They are useful examples when you implement the protocol yourself (§ 3).

## 3. Projects outside the SDK's Python range (JSON subprocess protocol)

If your environment cannot install the SDK (e.g. Python < 3.9), implement the protocol directly:

1. The launcher writes `invocation.json` (`wire: "eidos-wire/1"`, `job_id`, `attempt_id`, `epoch`,
   `project_id`, `component{id,entry,...}`, `inputs{name:{artifact_id,digest,path,media_type,...}}`,
   `config`, `staging_dir`, `scratch_dir`, `limits`, `ports`, `seed`) and runs your program with that
   path as its only argument.
2. Read only the declared input paths; verify each sha256 (hex, optionally `sha256:`-prefixed).
3. Write outputs below `staging_dir` (relative filenames, no `..`, no symlinks, within
   `limits.max_output_bytes`).
4. Last, atomically write `staging_dir/eidos-output-manifest.json`:
   `{"wire":"eidos-wire/1","job_id","attempt_id","epoch","status":"succeeded"|"failed",
   "outputs":[{"slot","filename","digest","size","media_type","schema","role"}],"receipt":{},
   "error":{"category":"numerical|resource|user-code|contract","message":"..."}|null}`
   (write to a temp name, then rename). Exit 0 whenever the manifest was written.

The launcher re-verifies every digest and size before anything is published.
