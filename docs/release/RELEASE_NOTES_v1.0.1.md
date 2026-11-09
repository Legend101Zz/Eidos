# Eidos v1.0.1 release notes (2026-11-09)

v1.0.1 is a **design-conformance release** after v1.0.0. A check against the architecture (`model-research-harness-design.md`, Revision 4) found three gaps that no document had recorded, and this release closes them. There are no new features, no ledger migration and no change to the supported scope (`SUPPORTED.md`). As with v1.0.0, there is **no scientific claim**: A16 is a separate later release.

## What changed

1. **Type checking in CI.**
   - mypy is configured in `pyproject.toml` and runs in the `lint-and-unit` job (`scripts/ci/mypy.sh`). It checks the server tree and the worker packages at Python 3.12, and the worker packages a second time at a **Python 3.9 target**, their supported floor.
   - All 91 errors are fixed with annotations and narrowing. No `type: ignore` was added, and behaviour is unchanged.
2. **Versioned wire JSON Schemas** (design § "The worker compatibility promise"; ADR 0008).
   - Server-side Pydantic boundary models generate `schemas/eidos-wire/v1/*.schema.json`.
   - The real worker SDK emits golden fixtures in `tests/fixtures/wire/v1/`. They are checked against the schemas in the server test environment, and checked for exact SDK output in the Python 3.9 / pydantic<2 worker environment.
   - **The server now validates every received manifest against the schema.** Before, a manifest with a mistyped field (for example a list `receipt`) passed the presence checks and failed later with an untyped error. Now it is refused with field-level problems. Unknown fields from a newer minor version are still accepted.
   - Schema major: **1**. Worker Python: **3.9+**.
3. **Deferred integrations recorded** (ADR 0009).
   - The design's optional PyGlove and MosaicML Composer adapters are **deferred for v1**, with reversal conditions. The supported path is source patches and author-owned trainers.
   - CheckList-style behavioural contracts are **partly adopted**: raw-prediction reuse, paired expectations, and the restore-invariance behaviour check.
   - `SUPPORTED.md` has a "Deferred integrations" section.

## Packages

| Package | 1.0.0 | 1.0.1 |
|---|---|---|
| `eidos-lab` | 1.0.0 | **1.0.1** |
| `eidos-wire` | 1.0.0 | **1.0.1**. Typing-only code changes and a `sys.version_info` selection of `tomllib`. The wire contract is unchanged (`eidos-wire/1`) |
| `eidos-worker-sdk` | 1.1.0 | 1.1.0 (unchanged; new tests only) |
| `eidos-pytorch` | 1.1.0 | 1.1.0 (unchanged) |

## Metrics: v1.0.1 compared with v1.0.0

| Measure | v1.0.0 | v1.0.1 |
|---|---|---|
| mypy errors (`src/eidos` + `packages/*/src`) | 91 (not run in CI) | **0**, run in CI; also 0 for the worker packages at a Python 3.9 target |
| `type: ignore` comments in checked code | 4 (2 unused) | 1 (pre-existing, `eidos-pytorch`, unchanged) |
| Wire JSON Schema files | 0 | **3** (`invocation`, `output-manifest`, `component-manifest`) |
| Wire golden fixtures | 0 | **15**: 6 valid (2 of them newer-minor) and 9 `invalid-*` |
| Server-side schema validation points | 0 | 4: `publish_outputs`, runner manifest read, assessor manifest read, component registration |
| Test functions (all of `tests/`, packages and components) | 244 | 259 |
| Local unit + integration + MCP + reporting suite | 167 passed, 4 skipped | 198 passed, 4 skipped (no new skips), plus worker packages 22 passed, 1 skipped |
| Worker SDK test functions (`packages/eidos-worker-sdk/tests`; they also run in the worker-compat matrix) | 13 | 15 |
| CI jobs per push | 10 | 10; `lint-and-unit` adds the mypy and schema-regeneration steps |
| ADRs | 7 | 9 |

## Upgrading

Upgrading from 1.0.0 is a package update with no migration: `lab upgrade` from the new wheels, as in `docs/ops/INSTALL_AND_OPERATE.md`. Workers on `eidos-wire` 1.0.0 stay compatible, because the wire contract did not change.

A worker that writes a manifest with a mistyped field is now refused at publication with a field-level message. The real SDK never did this: its output is exactly the golden fixtures.

## Limitations

These are unchanged from v1.0.0 (`RELEASE_NOTES_v1.0.0.md` § v1 limitations). They include the unverified machine sleep/restart recovery and same-user macOS isolation.
