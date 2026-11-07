# Qualification summary — 7 November 2026

**Reviewed public development preparation on local CPU. Final comparative study and final evaluation admission remain unqualified.** No Eidos implementation, internal investigator, external coding agent or AIDE campaign was run. No benchmark model API calls, GPU rentals, external registration occurred. Public repository delivery is authorized separately and identified by the release manifest and external source-commit receipt.

## Executed public development evidence

The historical local-v0.3 reference run completed in 63.34 seconds after environment installation. Current release rerun evidence is recorded below. This is not total preparation time or full cost: earlier failed/repeated runs, environment installation, downloads and research-assistant chat usage are outside that timer; no end-to-end monetary preparation total was measured. It exercised 14 capability cards grouped into eight correlated project clusters, not 14 independent scientific projects. Twenty-seven numerical baseline/reference pairs (nine variants × three seeds: 11, 23, 37) passed the predeclared fixture acceptance bars. The run also passed 77 numeric-input/export checks, including nine fresh-process model reloads; reordered-ID acceptance and split-disjointness checks executed in addition. Three lifecycle seeds each passed exact fresh-process continuation, an actual exit-17/restart, parent immutability, divergent child state and an intentionally incomplete-state negative control. Exact restore is specific to CPU/PyTorch 2.15.0 in this environment.

| Project | Metric | Baseline range | Reference range | Qualification |
|---|---|---|---|---|
| conditioning | rmse | 2.1082–2.1240 | 0.5030–0.8220 | 3/3 reference checks |
| digits | accuracy | 0.8167–0.8583 | 0.9778–0.9833 | 3/3 reference checks |
| digits_shift | accuracy | 0.6917–0.7361 | 0.9472–0.9500 | 3/3 reference checks |
| forecast | rmse | 0.9404–0.9404 | 0.0218–0.0227 | 3/3 reference checks |
| iris | accuracy | 0.8333–0.9000 | 0.8333–0.9000 | 3/3 reference checks |
| null | accuracy | 0.5000–0.5000 | 0.5000–0.5000 | 3/3 reference checks |
| order | accuracy | 0.5000–0.5059 | 1.0000–1.0000 | 3/3 reference checks |
| synthetic | accuracy | 0.5137–0.5137 | 0.9902–1.0000 | 3/3 reference checks |
| weights | accuracy | 0.0000–0.0333 | 0.8333–0.9000 | 3/3 reference checks |

Scores are development data, available to the builder. The references are authored scripts. D02 uses extra training and a different architecture, so it demonstrates an attainable recipe, not an isolated architecture effect. D08/D13 validate one authored instrument and software reuse; they do not show an LLM created or discovered it. D12 is a constructed null. Three seeds do not establish stochastic detection power or false-positive bounds.

The original draft had four analytically known synthetic estimator controls and four structural-answer controls. The reviewed release extends these to five analytical, 13 malformed-analysis, three valid-answer and 39 malformed-answer controls. These are not real system results. See `development/check_analysis_and_answer.py`.

## Upstream source and numeric execution

Inspected actual task definitions, preparation and graders, pinned in `development/NOTICES.md`. Four small metric fixtures ran against upstream files: MLGym house-price raw RMSE; MLGym image accuracy/row count; AIRS SICK Spearman/constant-prediction behavior; AIRS solar partial-NaN behavior. The inspected house grader aligns by row order and uses raw price RMSE, not Kaggle log-price RMSE. AIRS solar excludes candidate NaNs from its denominator; our strict prediction interface rejects them. SICK constant predictions produce a nonfinite correlation and need an explicit invalid-metric rule. These findings require named adaptations, not an official benchmark-score claim.

The upstream audit uses fabricated data and substitutes the solar data loader with a tiny fixture. The installed datasets version differs from AIRS's pin. Full upstream loading, containers, native GPU tasks, official baseline performance and AIDE execution remain unqualified. No RE-Bench protected solution was extracted.

## Private candidate evidence — aggregate only

Five separately stored candidate projects have 15 reference comparisons across three training seeds. A versioned candidate-v0.2 review reran six affected comparisons, rescored nine unchanged comparisons, reproduced 12 affected artifacts in fresh interpreters, and passed five standalone baseline and 25 numeric controls (20 malformed, five valid). Original candidate material and its seal were preserved; current aggregate evidence is in `development/qualification/private-review-aggregate.json`. One uses an adapted recognizable public benchmark project; the others fill private coverage/control gaps. Independent numeric positive, null, row-order and malformed-input checks passed in the reviewed candidate revision. Fresh baseline/reference export and standalone baseline-scaffold checks passed for all five. Full task identities, references, split details, source data hashes, graders and audit traces are retained outside the handoff. Three upstream reserve entries remain unqualified.

This is a modest candidate pool, not the final sample-size recommendation or a frozen final evaluation. A null control overlaps development construction and earns no unfamiliar-transfer claim. Reference authorship exposed material to this research conversation/provider; builder separation does not eliminate pretraining or provider exposure. Hostile-code isolation, evaluator credentials, cloud mount denial and Eidos integration are not tested here.

## Failures and exposure history

- v0.1 ran numerical references but failed the diagnostic aggregation assertion: float32 residual aggregation differed at the tight tolerance. v0.2 changed residual accumulation to float64; the tolerance and success bars stayed fixed.
- The first upstream image fixture used a dictionary whose length counted keys. It was corrected to a row-based DataFrame, then rerun. This was a fixture error, not an upstream task failure.
- v0.3 corrected cost metadata for last-value/constant baselines to record zero training steps and added actual examples-seen/generation counts. Numerical behavior and acceptance bars stayed fixed. The full public qualification was rerun.
- All revisions and partial numerical results are retained in `development/qualification/`. Draft 0.1 and its initial selection matrix were written and hashed before new task qualification. Draft 0.2's logistical refinements are exposure-informed, not retroactively preregistered.

## Reproduce and integrate

```sh
cd development
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python qualify.py --out /tmp/eidos-qualification
.venv/bin/python check_analysis_and_answer.py
```

Run upstream fixtures using the pinned clone commands in `development/README.md`. The 45-package installed version snapshot is recorded in `requirements.lock`; this is not a wheel-hash lock or cross-platform certification. Source hashes and environment details are in `development/qualification/results.json`. Supplementary scripts have their hashes in the public manifest.

Before final admission: pin the permitted provider/model and research envelope; retain the approved +10-point practical standard; freeze project/family selection, repetitions, per-task gains and analysis; independently review graders and interval behavior; qualify candidate isolation/access denial; and run all four integrations on development inputs. No preparation result justifies asserting Eidos superiority or declaring the architecture implemented.

## First public release review

Release `research-public-v0.1.0` introduces opaque development IDs, shuffled constructed-control rows, separately owned data/scorer imports, metric-overflow rejection, nonzero failure exits and common answer schema 0.2. Numerical acceptance bars were unchanged. Prior results are preserved in `development/qualification/historical-v0.3-results.json`; their source hashes describe historical source, not the current release. New results and bundle checks pin the reviewed source independently.

Three restricted investigator bundles (D01, D02, D12) passed fresh-process baseline/prediction equality and explicit file-allowlist checks. Default bundles exclude references, graders and test labels; builder access to the wider public source still prevents any sealed/unfamiliar-task claim. Added negative controls reject metric overflow and failed qualification bars. Historical ID/row shortcuts were fixed before publication; old inputs must be regenerated.

The second reviewer independently checked public source/contracts; another reviewed current upstream revisions/licenses. These are research-agent reviews, not independent human scientific adjudication or security certification. Source revisions did not change. AIRS search versus evaluation overhead and MLGym command timeout semantics were clarified; a Titanic ID-alignment concern remains a static finding, not a qualified task.

Current reviewed-source full rerun: 59.11 seconds after environment setup; 27/27 references, 77 input/export checks and three lifecycle seeds passed. The timer excludes bundle/structural checks, prior runs, installation and preparation costs. `release-review.json` identifies the exact scope and source hashes.

A subsequent eight-scenario synthetic interval stress check executed 400 datasets/scenario and 799 bootstrap draws. It found material few-family undercoverage; therefore the draft estimator remains barred from confirmatory admission without an estimand/design/method calibration freeze. This is a negative qualification result, preserved in `interval-calibration.json`, not a powered research campaign or evidence about Eidos.
