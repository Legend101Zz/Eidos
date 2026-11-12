# Practice-family exposure ledger

The manifest was fixed before pipeline C existed and before scoring any generated holdout. This harness is development evidence. It cannot establish held-out improvement or an investigator effect. Under the two-arm pilot both arms receive the same components.

## Prior design exposure

- N01 and N04 training/validation data were inspected by the discovery-report author. The accepted design and its review disclose those probes. This implementation uses the public N01–N04 and D02–D04 generators for explicitly labelled calibration, including their public test partitions in the trusted offline scorer.
- The proposed library and priors copy comparison-arm recipes on N04: arm 1 round 2 correlation screen + MLP ensemble; arm 2 round 1 per-class GMM with BIC; arm 1 round 1 PCA-whiten + SVM; arm 2 round 3 screen + MLP ensemble. D2's per-image centring on D03 is also design exposure.
- No sealed or reserve card and nothing under `eidos-grading/` was used, inspected, listed or hashed. No model/provider calls were made.

## Frozen roles and constants

[MANIFEST.json](MANIFEST.json) and `study/practice/registry.json` identify 15 generators, four deterministic instances each. The paired variants (circles/quantiles, Friedman 2/3, low-rank/sparse regression) alternate within one generator, preserving the specified 15-generator/60-instance size. They are not counted as independent generators for inference.

Design generators: gen-001, gen-003, gen-004, gen-006, gen-009, gen-011. Holdout generators: gen-002, gen-005, gen-008. Holdout classes: gen-007 and gen-010. Controls: gen-012 and gen-013. Calibration-only reenactments: gen-014 and gen-015.

The manifest freezes folds, seed streams, paired-band multiple, support threshold, complexity order, epsilon, null tolerance, calibration tolerances and wall limits. Replayed band/permuted-complexity results are descriptive; no holdout result selects a setting. With fewer than three holdout generators in any declared mechanism class, the harness cannot make a class-level claim. It reports named-generator evidence instead.

## Burns

The initial step-1 run scores the five named holdout generators/classes for comparator calibration. Its registration is retained as the first burn. The final policy also binds the guard, replay, CLI and acceptance source, so the final cached verdict spends the second burn even though no operator, threshold or fitted score is tuned. The primary verdict is harness acceptance, not a pipeline improvement claim. Registrations and exact scoring/policy digests are in `claude-orch/v2/step1/family-final-gate/burns.json`; results, timings and cache identities live beside it. Replaying that identical final policy spends no further burn. No replacement generators were authored.

Every later changed-source holdout run must be preregistered in that programme ledger. At most two burns per class and eight primary verdicts are admitted. Replacement after that requires a different seed stream and an independent author/reviewer who has not seen the failure; this implementation does not create replacements.

## Implementation choices and limits

- A1 imports the unchanged `recipe-sweep@1.1.0` broad grid and runner: 52 recipes selected by validation. Every recipe's train-only five-fold CV and test score is cached so selection diagnostics require no refits. A2 is CV-selected LR/Ridge, SVC/SVR, ExtraTrees and HGB. A3 is a fixed EDA proxy using in-fold correlation screening, five-member sklearn MLP classification ensembles, per-class GMM selected by BIC, and PCA-whiten + SVM. Regression uses a single MLP rather than asserting an unmeasured coding-arm regression ensemble.
- A0 executes the functioning supplied project via the evaluator command. Practice bundles supply a scaled linear baseline. Development calibration preserves its original supplied baseline source. No oracle, generator code or test labels enter a candidate workspace.
- All model fits and fresh subprocesses use one compute thread. The explicit 5,000-row cap for expensive A2/A3 estimators is applied within training folds and recorded by source. A1 retains the original runner's resource semantics.
- Oracle sidecars record mechanism ground truth and analytic Bayes accuracies for the two controls. Other large-sample reference scores remain explicitly unestimated (`null`); no empirical comparator is presented as a Bayes oracle. The easy radial generator's measured accuracy against the 1.0 ceiling supplies the near-reference acceptance check.
- The score cache records unmodified test metrics for every fitted recipe, selection optimism/regret, paired visible intervals, OOF digests, fold-fit identity proofs, timings and errors. The test scorer is controller-only and opens labels after choices are frozen. Portable exports contain fitted transforms and support the ordinary `train`/`infer` CLI. Fresh checks are optional expensive measurements and are explicitly marked when not run.
- Primary differences are raw metric units and test-SE units; the optional normalized-gain helper floors its denominator at twice reference SE. Controls use paired visible validation intervals and all three fresh seeds in the same improvement-card success arithmetic as the grader. Pipeline C is unavailable until step 2 and is never substituted by A3.

Reproduce commands and measured pass/fail counts are in `claude-orch/logs/V2_STEP1_REPORT.md`; JSON and short acceptance summary are under `claude-orch/v2/step1/` outside the repository.
