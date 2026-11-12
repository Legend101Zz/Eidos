# Practice-family exposure ledger

The manifest was fixed before pipeline C existed and before scoring any generated holdout. This harness is development evidence. It cannot establish held-out improvement or an investigator effect. Under the two-arm pilot both arms receive the same components.

## Prior design exposure

- N01 and N04 training/validation data were inspected by the discovery-report author. The accepted design and its review disclose those probes. This implementation uses the public N01–N04 and D02–D04 generators for explicitly labelled calibration, including their public test partitions in the trusted offline scorer.
- The proposed library and priors copy comparison-arm recipes on N04: arm 1 round 2 correlation screen + MLP ensemble; arm 2 round 1 per-class GMM with BIC; arm 1 round 1 PCA-whiten + SVM; arm 2 round 3 screen + MLP ensemble. D2's per-image centring on D03 is also design exposure.
- No sealed or reserve card and nothing under `eidos-grading/` was used, inspected, listed or hashed. No model/provider calls were made.

## r2 roles and construction exposure

The original 15-generator family and all its comparator results were exposed during the first build/review. The r2 manifest expands to 24 generators / 96 cards before pipeline C exists. gen-011's N04 mechanism is removed and replaced by large-n Hastie with structured nuisance; gen-014 is the only N04 replica and remains calibration-only. gen-010's formerly saturated accuracy mechanism is changed to meaningful group variation and a .6 threshold. All row-token streams change to eliminate source-index inversion, so r2 recomputes every score table rather than asserting the old tables are unchanged.

Design: gen-001/003/004/006/009/011. Machinery holdouts: gen-002/005/008 and gen-016 through gen-024. Separate operator holdout classes: gen-007 and gen-010. Noise/weak controls: gen-012/013. Calibration replicas: gen-014/015. New machinery holdouts were authored after seeing the review's operating-characteristic failure, to increase generator-level power; they are not independently authored prospective holdouts. Their test scores may not tune pipeline C or thresholds. The two operator holdout classes cannot borrow machinery generators to support a class claim.

[MANIFEST.json](MANIFEST.json) freezes selection constants, seed streams, the .5-SE harm margin, .8-SE generic power target, and calibration/wall tolerances. Thresholds are not chosen from r2 holdout wins. Monte Carlo uses measured exposed variance, explicitly reports homogeneous and heterogeneous profiles, and includes dependence sensitivity for fixed-source wine/digits. Attainable-reference scores and all headroom comparisons are descriptive offline evidence.

## r2 burn reset

The old `<out>/burns.json` ledgers recorded two harness-construction source changes, not two candidate evaluations. They remain intact as historical evidence. The authorized rework resets these construction-only burns in a migration receipt under `claude-orch/v2/step1-r2/primary-registration.json`. The fixed protected programme ledger is `claude-orch/v2/programme-burns.json`, mode 0600 and locked during registration. Descriptive A0–A3 construction/calibration does not consume candidate burns. A registered candidate evaluation binds its ID, candidate digest, primary verdict and generators, regardless of the output directory or unrelated source edits. Changes to an already registered candidate/verdict are refused; the two-burn/eight-primary limits remain.

## Evidence limits

A3 is an EDA proxy, not a proven coding-agent ceiling. References are empirically attainable, not Bayes scores. Finite-source resplits share rows (wine 178, digits 1,797), so effective instance n=1 and wider uncertainty are used. The first-fold poison audit observes the actual fitted workflow, including pre-CV operations, but does not prove the absence of every malicious audit-evading/later-fold-only leak. Null controls use D12's no-positive-claim semantics. Comparator runs lacking fresh claim checks fail closed; the guard cannot infer that missing evidence passed.

Only `bundles/` is candidate-visible. Sequential card numbers and the trusted index still map to generator identity; do not ship the index, controller, references or generation code. IDs are random assignments from a distinct deterministic stream; reproducibility of public synthetic data is not cryptographic concealment.

Exact runs, failures, skips, operating characteristics and all acceptance verdicts are in the report's r2 section and `claude-orch/v2/step1-r2/SUMMARY.md`. Pipeline C is not evaluated.

R2 execution scope note: the initial instruction-file discovery was accidentally rooted at the programme parent. It returned dependency AGENTS.md paths and no grading/sealed file contents, but the restricted subtree was not explicitly excluded, so absence of directory-metadata traversal cannot be certified. This scope error is recorded in the r2 report; subsequent searches were confined to the current worktree and named public inputs. No sealed data enters any generator or score.
