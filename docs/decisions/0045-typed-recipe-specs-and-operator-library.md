# ADR 0045 — Typed recipe specs, one vetted operator library, and `include` for authored source

Status: Accepted for implementation (step 2)
Resolves review findings 15, 22, 25 (MLP speed), 30; orchestrator decision Q5 (sklearn MLP, no torch).

## Context

In arm 4 a new modelling idea costs an authoring turn: up to 51% of wall on N04 (730 s of 1418 s; an upper bound, since those turns also contain waits), 235-828 s to re-author the sweep winner, one turn
emitting 18.6k output tokens in 370 s with a JSON repair (discovery-gap report 5.1, 5.2). Decision arguments travel as `args_json` strings (`pydantic_ai_runtime.Call`), so authored source is JSON inside a JSON
string. Request latency is about 11.0 s plus 8.18 s per 1,000 output tokens (latency report 2.5). The template sweep (`recipe-sweep@1.1.0`) is typed but small (six families by four preprocessings, one SVM
parameter, a one-layer 40-epoch MLP, no screening, PCA, mixture family or ensemble), and the arm-4 exporter needs a `project.command` job with a `project.py train` argv.

[reviewer-measured, synthetic N04-shaped data] ExtraTrees(500) fit 0.42 s, per-class GMM 0.06 s, RBF SVC 0.02 s, sklearn `MLPClassifier((64,64), adam, 300 epochs, batch 256)` 0.64 s per member
(the report's torch probe: about 0.6 s), so torch is not needed at this scale; the accuracy of sklearn's MLP against the torch recipe was not measured, only its speed.

## Decision

Define `recipe-spec/1` and implement every operator it can name in one library, `eidos_recipes/` (numpy and scikit-learn only), vendored inside `components/eidos.search.recipe-portfolio/` and copied
byte for byte into generated source bundles (ADR 0047). The library digest is recorded in every table and every prepared job's receipt; a parity test requires identical validation predictions from
the search component and the generated `project.py` for the same seed.

A spec is `{"schema": "recipe-spec/1", "task_type": ..., "steps": [{"op": ..., ...}], "model": {"family": ..., "params": {...}}}`; list-valued parameters are grid axes, the cartesian product is capped at
400 recipes per call, and a larger request is refused, never truncated. **Every fitted step (`standardize`, screens, `pca`, models) is fitted inside each training fold only.** Screening or projecting on all
of train before cross-validation is a leak and cannot be expressed in the grammar. Each operator is declared in a registry with its hypothesis class, parameter schema, task types, a cost model `cost(n, d)`, a
`complexity(spec)` ordinal used only as the tie-break inside the one-SE band (ADR 0046), a checkpoint-bytes model and determinism notes.

Initial vocabulary (extension is by ADR, not by prompt): `standardize`, `robust_scale`, `quantile_normal`, `log1p_signed`, `tfidf`, `row_l2`, and the scout-only `per_example_center` and `per_example_standardize`;
`variance_screen`, `corr_dedupe`, `shadow_screen`, `importance_topk`; `pca` (a number, a variance fraction or `auto_rank`, optional whitening); `onehot_lowcard`, `binarize`; `ridge`, `logistic`, `lda`, `qda`,
`gmm_class` (k or `bic`); `knn`, `svm_rbf`, `svm_linear`, `kernel_ridge`, `svr`; `extra_trees`, `random_forest`, `hgb`; and `mlp` implemented with scikit-learn's `MLPClassifier`/`MLPRegressor` with seeded
members. `soft_vote` is deferred with ensembling (ADR 0050).

**Spec validation is two-layered and only partly at acceptance.** `validate_config` (`domain/contracts.py`) supports `type/enum/properties/required/additionalProperties/items/anyOf/min/max/pattern`,
has no `oneOf`, `const` or cross-field rules, and skips a config that contains a `$ref`, so the manifest `config_schema` covers the coarse shape (mode, include, budget, `add[]` entries as objects). Operator-level
checks (unknown operator, bad parameter, grid above the cap) are a hand-written `eidos_recipes.spec.validate()` that the component runs in under a second at job start and that returns short one-line errors, so a mistyped
operator costs one failed job and about one decision turn rather than a 120-370 s authoring turn.

**Checkpoint safety and size.** `model.pt` is a restricted-unpickle container holding the spec, fitted state and library digest. Its unpickler allow-lists *explicit classes*, not packages: this is robustness, not a
security boundary, since `numpy` and `sklearn` expose many callables. Size is an export guard because the evaluator writes one checkpoint per fresh seed and a job output is capped at 512 MiB (`runtime/runner.py`):
[reviewer-measured] ExtraTrees(500) pickles are 39 MB (classification, n = 1800, d = 20), 130 MB (regression, same size), and 112 MB and 432 MB at n = 6000, d = 40. An export-eligible recipe needs
`checkpoint_bytes <= 64 MB` (initial value; tree count and leaf size are capped for export-eligible tree recipes).

**`include` for authored source.** `author_source {files, include: ["eidos_recipes"]}` merges the library files from the pinned portfolio release bundle into the registered `source-bundle/1` (path collisions
refused; library digest recorded). Authored code can then import `eidos_recipes.cv` and `eidos_recipes.select`, so an authored challenger shares the CV protocol and the paired band at a few hundred tokens of new code,
and open-ended hypotheses stay cheap. A bounded custom-feature step (a function of at most 40 lines fitted inside each fold) is recorded as measure-first, not built.

The escape hatch is otherwise unchanged: `author_source` with `eidos.project.command`, and the command sweep of `recipe-sweep@1.1.0`.

## Rejected alternatives

- Extending the `recipe-sweep@1.1.0` template: its subprocess-per-evaluation engine is the wrong shape for 100 or more recipes with stored out-of-fold predictions, and its bundle digest is pinned in the adapter.
- A free-form pipeline DSL or string spec: it moves the authoring problem into a parser and cannot be schema-checked.
- Recipes naming arbitrary callables or module paths: authoring with extra steps and no vetting.
- Two copies of the operators (search and export): drift would silently change the exported candidate relative to the searched one.
- torch for the MLP class at this scale (Q5).

## Verification and limits

Tests: grid expansion, the 400 cap and its refusal; `spec.validate()` messages; registry completeness (schema, cost model, complexity, bytes model, determinism note for every operator); **in-fold fitting** (a planted leak
inflates CV, the real pipeline does not); the parity test; the explicit-class unpickler rejects an arbitrary callable; the checkpoint-bytes guard; no operator reads a path or spawns a process; `include` merge, collision refusal
and digest record. They establish plumbing and equality of two executors, not the scientific value of any operator.

Limits: the vocabulary is finite, so a mechanism outside it still needs authoring (hence `include`); operators inherit scikit-learn version behaviour (the environment lock is recorded); acceptance-time refusal of mistyped
operators is partial; the `complexity` order is a documented prior and the ablation in ADR 0046 shows how much selection depends on it.

## Incremental scope

Step 2 implements the library, portfolio, generated project interface, toolkit parity and model-free evidence. Lab margin/round accounting and author_source include support belong to step 3. Draft numbering is retained: 0044 is reserved for the steering/profile ADR in the sibling step-0 change. Scientific acceptance is measured separately from implementation; see the step-2 evidence report.

Implementation uses a decimal 64,000,000-byte checkpoint cap, explicit class globals, and float64 feature conversion shared by search, export and the acceptance controller. No operator reads a path or starts a process; the bounded recipe worker is infrastructure around the operators.
