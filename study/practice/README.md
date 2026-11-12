# Offline practice harness

Read the frozen manifest and exposure ledger in `docs/experiments/practice-family/` before scoring. These are practice/development measurements, not prospective confirmation. Pipeline C is intentionally unavailable in step 1.

Use a Python environment with the bundle's pinned numpy, scikit-learn and joblib versions. For development calibration it also needs the public baseline's torch dependency. Set `EIDOS_RESEARCH_PYTHON` to that interpreter and source the orchestrator SSD environment first. Set OMP/MKL/OPENBLAS/VECLIB thread counts to 1 and use `nice -n 10` for long runs.

```sh
python -m study.practice.build_cards --out /external/practice/data
python -m study.practice.run_gate --data /external/practice/data --out /external/practice/gate
python -m study.practice.run_gate --data /external/practice/data --out /external/practice/gate --fresh
python -m study.practice.run_gate --package-development --data /external/practice/development --out /external/practice/calibration
```

Data and evidence must live outside this repository. Never point these commands at a sealed card or grading directory. `bundles/` contains only visible data and the functioning baseline. `controller/` contains labels and oracle sidecars; it must never be given to an investigator. The candidate sandbox gets only its own bundle, selected recipe source and label-free test inputs.

Every table caches five fold scores and a controller test score per recipe; it freezes the A0–A3 choices before reading test labels. Scores are accuracy for classification and negative RMSE for regression, so larger always wins. `selection_replay.py` derives paired-band and prior/permuted-prior diagnostics from those tables without fitting. `--fresh` executes all three seeds through `study.fresh_check.candidate_command(timeout=120)`, checks reload equality and records checkpoint sizes and fit times. The gate reports which expensive checks actually ran; a cached score table is not a fresh-seed check.

The guard uses t intervals within generators and an exact conditional sign-flip test plus modified Hartung–Knapp random-effects interval across machinery holdouts. Separate operator holdout classes cannot borrow machinery generators. The .5-SE harm screen is multiplicity-controlled; acceptance requires a pooled gain of at least +.5 SE and indicates positive transfer and no detected material harm, not proof of per-generator equivalence. Missing in-fold, null-claim or resource evidence rejects. Effects use paired test SE with a declared .001 floor; this is not normalization by reference headroom. Finite-source wine/digits resplits use effective instance n=1.

`compute_table` poisons held-fold rows and reruns the actual fit workflow once per recipe, comparing fitted state. `controls` executes a supervised pre-CV screen through that path and measures generic helpful/neutral/harmful/card-specific prediction-fault controls. `operating` records Monte Carlo error rates and power at the observed instance SDs and at lower noise. `references` fits independent large-sample attainable references (finite sources use their available train+validation data); `recompute` requires a new cache and recomputes one card per generator. Those receipts are required by the strengthened acceptance report.

```sh
python -m study.practice.references --data /external/practice/data --out /external/practice/evidence
python -m study.practice.controls --data /external/practice/data --references /external/practice/evidence --out /external/practice/evidence
python -m study.practice.operating --family /external/practice/gate/gate.json --out /external/practice/evidence/operating.json
python -m study.practice.recompute --family /external/practice/gate/gate.json --first-gate /external/practice/gate --out /external/practice/cold
```

Candidate evaluations use `--evaluation-id` and `--candidate-digest` to preregister in the fixed protected `claude-orch/v2/programme-burns.json` outside `--out`. One evaluation binds its candidate, primary verdict and generators. A changed candidate needs a new registration; unrelated source changes do not consume burns. Construction/calibration is descriptive and consumes none. Do not reset actual candidate burns; replacements after the two-burn limit need independent review. The r2 construction-only reset has an explicit migration receipt.

`python -m study.practice.acceptance --help` describes the five-criterion acceptance report. `claim_audit.py` audits structured visible claims and their evidence references without generating claim text.
