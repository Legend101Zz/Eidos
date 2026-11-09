# eidos-pyglove

An optional, worker-side adapter that lets an Eidos project describe a model family as a
[PyGlove](https://github.com/google/pyglove) symbolic object, search it, patch it, and hand the
materialized PyTorch builder plus a stable **transform revision** to Eidos.

It is not part of the lab server, `eidos-wire`, `eidos-worker-sdk` or the Eidos uv workspace and
adds no dependency to any of them. Install it only in the project environment that runs your
experiments.

## Pinned versions

| Package  | Pin                                   | Notes |
|----------|---------------------------------------|-------|
| pyglove  | `>=0.4.5,<0.5` (`SUPPORTED_PYGLOVE`)  | Verified with 0.4.5 on CPython 3.12. Any other version raises `UnsupportedPyGloveError` before pyglove is imported. Pre-release and dev versions are refused. |
| torch    | not declared                          | Like `eidos-pytorch`, this package never installs or upgrades torch; your project environment supplies it. CI exercises it with `torch==2.7.0+cpu`. |
| Python   | `>=3.9`                               | CI runs 3.12. |

`requirements-ci.txt` holds the exact pins the CI job installs (`scripts/ci/adapter_pyglove.sh`).

## Use

```python
import pyglove as pg
import torch
import eidos_pyglove as ep


# my_models.py  (a module, so the exported JSON can name it)
@pg.symbolize
def build_mlp(in_dim, hidden, out_dim, act=torch.nn.ReLU):
    return torch.nn.Sequential(torch.nn.Linear(in_dim, hidden), act(), torch.nn.Linear(hidden, out_dim))


space = ep.SearchSpace(
    build_mlp(
        in_dim=8, hidden=pg.oneof([16, 32, 64]), out_dim=2, act=pg.oneof([torch.nn.ReLU, torch.nn.Tanh])
    )
)
space.size  # 6
candidates = space.candidates()  # deterministic sweep, every run the same order; limit=N for a prefix
space.materialize([1, 0])  # explicit decisions
space.sample(3, seed=7)  # reproducible random candidates

c = space.materialize([1, 0]).transform("widen?width=128")  # str rule or dict rule, on a deep clone
module = c.build()  # fresh torch module every call
c.transform_log  # ["widen?width=128"]
c.revision()  # adapter, pyglove_version, dna, patches, sym_hash, builder_json, transform_revision

ep.export(c, "builder.json")
build, revision = ep.builder_for_eidos("builder.json", allowed_modules=["my_models", "torch.nn"])
```

`build` is a callable usable as an Eidos named builder: `build()` and `build(config)` both return a
fresh module (the config is ignored; the architecture is the exported object). A module of your
project exposes it with a one-liner, then a task or training request names it as `module:attr`:

```python
# my_project_builder.py
import eidos_pyglove as ep

build, REVISION = ep.builder_for_eidos("builder.json", ["my_models", "torch.nn"])
```

Put `revision["transform_revision"]` in the receipt. It is
`"sha256:" + sha256(canonical JSON of {builder_json, patches, pyglove_version})` (sorted keys, no
whitespace), so the same architecture reached by the same patches under the same pyglove always has
the same revision, and any change to a width, an activation, a patch or the pyglove version changes
it. `load(..., expected_revision=...)` pins a revision.

## Patches

`transform()` takes a string rule (`"name?arg=value"`, resolved against patchers registered with
`@pg.patching.patcher`) or a dict of symbolic path to value, singly or as a list. Only these two
forms are accepted because every patch is recorded in `transform_log` and hashed into the revision;
callables (rebind functions) cannot be recorded and are refused. A patcher must be registered in the
process that applies it; the patch log of a loaded export is history only and is never re-applied.

## Gotchas

- **Never use `pg.sample` here.** Without a feedback loop it proposes forever. `SearchSpace` uses
  `pg.iter` with `pg.geno.Sweeping` (enumeration) and `pg.geno.Random(seed)` (the exact stream
  `pg.random_sample(space, n, seed=seed)` yields), and keeps the DNA of every candidate.
- **The builder must live in an importable module.** Exported JSON names it by module-qualified
  `_type` (`my_models.build_mlp`); a builder defined in `__main__`, a notebook or a function body
  cannot be loaded again. Lambdas and local functions serialize as marshalled bytecode, which
  `export()` and `load()` both refuse: use module-level functions and classes.
- **`sym_hash` is process-local.** It is built from Python's per-process hashing, so it is equal
  after a reload in the same process but never comparable across processes. `transform_revision` is
  the stable identity; `sym_hash` is recorded only as a diagnostic.
- **`dna` is provenance, not integrity.** The hash covers the builder JSON, the patches and the
  pyglove version; the DNA that led to the object is recorded next to it but is not part of the
  revision (the builder JSON is the object).
- Spaces that are not finite (`pg.floatv`) have `size is None`; `candidates()` then needs `limit=N`.

## Security: loading an export is loading untrusted input

`pg.from_json` instantiates whatever types the JSON names, and with its default `auto_import=True`
it imports the module of any `_type` it meets. A `"function"` or `"type"` reference can also carry
marshalled bytecode that PyGlove turns back into a callable. An export file must therefore be
treated like a pickle from the same source, and `load()` does the following before PyGlove sees it:

1. The schema must be `eidos-pyglove-builder/1`, the file at most 16 MiB, with no `NaN`/`Infinity`.
2. `transform_revision` is recomputed from the file's own `builder_json`, `patches` and
   `pyglove_version`; a mismatch refuses the file (`ExportIntegrityError`). This catches edits and
   corruption. **It is not authentication**: anyone who edits the file can recompute the hash, so
   pin `expected_revision` from a receipt you trust when the file's origin matters.
3. `allowed_modules` is mandatory. Every `_type`, and every `type`/`function`/`method` reference
   anywhere in the tree, must be inside one of those modules (a module or its submodules), and
   references must also resolve to objects *defined* there, so `my_models.torch.load` is refused
   although it starts with an allowed name. Marshalled code, pickle-backed PyGlove opaque values and
   non-string `_type`s are refused outright (`DisallowedTypeError`).
4. Only then are the allowed modules imported and `pg.from_json(..., auto_import=False)` run.

List narrow modules: your own builder modules and library submodules such as `torch.nn` (for
`torch.nn.ReLU`, whose module is `torch.nn.modules.activation`). `builtins`, `os`, `sys`,
`subprocess`, `pickle`, `importlib`, `pyglove`, and a bare `torch` are refused as entries, because
allowing them would let an export name `os.system`, `eval`, `pickle.loads` or `torch.load`. Allowed
modules and the builder functions in them are code you trust: loading calls their constructors.

## Tests and CI

```sh
PYTHONPATH=packages/eidos-pyglove/src python -m pytest packages/eidos-pyglove/tests -q
bash scripts/ci/adapter_pyglove.sh      # fresh 3.12 venv from requirements-ci.txt, then the tests
```

`test_pins.py` needs neither pyglove nor torch; `test_adapter.py` is skipped where either is
missing (`pytest.importorskip`). The CI script fails if any test is skipped.
