# 0009: PyGlove and MosaicML Composer adapters are deferred; CheckList is partly adopted

Status: **superseded by ADR 0015** (2026-11-09, release 1.1.0), at the owner's request. Was: accepted (2026-11-09, release 1.0.1). PyGlove and Composer adapters: **deferred for v1**.

**Context.**
- The design's table "How the four scientific libraries enter this design" offers two optional adapters:
  - PyGlove: a pinned transform and search-space adapter for teams with compatible symbolic objects, which exports the materialized PyTorch builder and the transform revision;
  - MosaicML Composer: an adapter for projects already using its trainer, mapping only verified event phases and restore semantics, tested per supported version against the hook fixtures.
- Both are optional in the design ("Optional adapters ... No mandatory research framework migration for ordinary custom PyTorch models"). Neither is part of acceptance areas A1–A16 (`ACCEPTANCE_MATRIX.md`).
- v1.0.0 built neither, and no document said so. A check against the design found the gap.
- The same table adopts CheckList's behavioural contracts and paired expectations: invariance and direction-of-change cases, domain-supplied transformations and groups, and raw predictions recorded so new scorers can reuse them.

**Decision.**
1. In v1, the supported path for model changes is **ordinary PyTorch source patches** (the design calls them a first-class candidate) plus the `eidos-pytorch` adapter. For training it is **author-owned trainers**, extended through `eidos-pytorch` training-extension phases or the whole-trainer escape hatch (`docs/author-guide/worker-sdk.md` § 2). Neither PyGlove nor Composer is imported, pinned or tested anywhere in the release.
2. A project that uses PyGlove or Composer can still run in v1 as ordinary code: its own trainer or builder sits behind a project adapter. Eidos then sees only the PyTorch builder and checkpoints. It does not see PyGlove symbolic transforms or Composer events.

**What exists today (checked against the code).** CheckList is **partly adopted**:
- **Raw predictions are recorded and reused.** `eidos.pytorch.evaluate` publishes raw per-pair predictions (`predictions`, `labeled-binary-pairs/1`). Scorers consume that artifact without rerunning the model. `lab.pairs.by_group` is reused unchanged on a second, image project (switch journey step 6).
- **Paired expectations.** `lab.pairs.by_group` scores each original example against its twin (`both_correct_rate` against `reference_accuracy`, per group, with the grouping column chosen by the caller).
- **One invariance contract.** `eidos.pytorch.behavior-check` asserts that a checkpoint behaves the same, within tolerances, when restored in a fresh process through a named builder. The method card says the comparison covers only the adapter's reference batch.
- **Not adopted:** a general behavioural test-suite contract with author-supplied input transformations, CheckList's invariance and directional-expectation test types over domain inputs, or test-capability matrices. Domain authors can write such checks as ordinary `observe` components. Eidos ships no capability or template for them.

**Consequences.**
- No PyGlove search space or Composer event appears in Eidos records. Search over architectures happens through source patches and the investigator's own proposals.
- The hook-ordering guarantees (phases, required-on-load restore) apply to `eidos-pytorch` training extensions only.
- `docs/release/SUPPORTED.md` lists both adapters under "Deferred integrations", and CheckList as partial.

**Reversal conditions.** Build an adapter when all of the following hold:
1. a user project actually uses PyGlove symbolic objects, or the Composer trainer, for the model under investigation;
2. a supported version range is pinned, PyGlove at a release with a stable `pyglove.core.patching` rule API and Composer at a release whose `Event` set and algorithm `required_on_load` semantics are fixed;
3. hook-phase fixtures exist that run every mapped phase on a short synthetic batch, validate phase counts and restore behaviour against the `eidos_wire.manifest.PHASES` meanings, and run for each pinned version in the worker-compat matrix, as the design requires ("test each supported version against our hook fixtures").

A CheckList-style behavioural capability (`behavior-suite/1`, with invariance and direction-of-change templates) is the matching step for CheckList. It needs a domain author who supplies valid transformations and groups.
