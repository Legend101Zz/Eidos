# Recipe portfolio 1.0.0

Author-tested exploratory search, not qualification or held-out evidence. Requires
numpy and scikit-learn. All jobs and model members run sequentially on one thread.

`portfolio` covers every applicable class before widening its grids: linear,
screened, low-rank, generative (classification only), neighbors, kernels, trees,
and neural, plus per-row invariance scouts. Above 5,000 training rows, density
and neural cost are reduced before fitting, recorded in `grid_plan`. Cost-ranked representatives and distinct model families precede duplicate settings; HGB retains its compact strong default. No halving.
The overall default budget is 350 seconds; 20% is reserved for full-fit validation
checks. Incomplete coverage and per-recipe failures/timeouts remain visible.

`cv-train` uses five train-only folds, twice below 1,000 rows, with group-aware
folds when groups exist. Each fold constructs and fits the complete pipeline.
`auto` checks logistic domain AUC (DeLong lower 95% bound > .6). On detected
shift, `train-fit-validation-score` fits only train and scores validation rows.
Its regression paired band uses RMSE influence functions. No untouched split
exists for this estimator. Profile and predictions ports are reserved observations;
version 1 uses its own target-free shift check and does not interpret those ports.

Selection uses paired differences to the best recipe, a frozen one-SE multiple
and complexity prior, then higher mean, lower SE and stable id. The SE from shared
folds is optimistic: this is a heuristic, not a hypothesis test. The displayed
winner's-curse scale assumes independent draws; it is neither a bound nor a
correction for fold sharing. Validation checks score the choice and up to two
runners-up; the table records the count. Later calls reuse visible data.

`refine` takes `add` or `parents` specs and stops a family if the last six evaluated
neighbors do not improve its preceding anchor by a paired SE. `rescore` takes an
explicit `partition_seed`, uses train-only partitions and purpose `reproduction`;
it is never independent confirmation. `export` takes a stated `spec` and emits
only `promotion_source`, marked `explicit-export`.

List-valued step/model parameters form grid axes, with a hard refusal above
400 recipes per request. The manifest validates coarse shape; the library checks
operators, types and cross-field rules at job start, with short errors. No named
callables, file-reading operators, custom feature functions or soft-vote operator.
Seeded MLP members belong to a single recipe; cross-recipe ensembling is deferred.

The persistent child receives a scrubbed environment and its own process group;
timeouts terminate only that child and restart it for later recipes. Only the six
visible ports in the manifest are accepted, before any input is opened. Files
named for test/held-out data are refused. This is not an adversarial sandbox.

Outputs: `portfolio_table`, `oof` (all completed CV fits with row/fold ids), and
`promotion_source` (source-bundle/1). The library is copied byte for byte. Generated
`project.py` supports train/infer/check/compare; train never cross-validates or
pools validation. One `model.pt` uses an explicit-class restricted unpickler and a
64 MB guard. Restricted pickle is a robustness measure, not a security boundary.
Train fit must finish within 60 seconds; the evaluator's 120-second cold-process
clock is separately measured by acceptance. Step 3 supplies lab preparation,
producer-pin handling, lock authority, margins, and include support; none is
claimed here. No component locks or writes an improvement claim.

Round-2 selection freezes the paired multiple at 1.0 and uses the previously
predeclared seed-81723 permutation of completed export-eligible recipes as the
within-band prior. Design replay favored this over structural complexity and
half-SE. `selection.prior` records `seeded-permutation/81723`; the table still
reports structural complexity. The order depends on evaluation order and the
completed recipe set. This is a design-calibrated heuristic with limited evidence.
Five folds, repeated twice below 1,000 training rows, and the shift-aware scheme
remain. Above 3,000 rows a linear SVM kernel-class scout precedes RBF density,
so slow RBF fits need not remove that hypothesis class. No halving is performed.

Dependency preflight runs for every component at validation, before its fixtures,
and never changes a build lock or release digest. This portfolio deliberately
pins exact numpy, scikit-learn and joblib versions in its manifest. A different
measured environment fails closed; install the declared versions before validation.
