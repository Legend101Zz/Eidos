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

The guard bootstraps instances only within each generator, then uses a directional sign test and DerSimonian–Laird interval across generators in test-SE units. An explicit declared class needs at least three holdout generators before a class claim is possible. Holdout preregistration binds both scoring and acceptance source, is idempotent for an unchanged policy, and refuses a third changed-source burn. Do not reset the programme ledger to bypass the limit; replacements need independent review.

`python -m study.practice.acceptance --help` describes the five-criterion acceptance report. `claim_audit.py` audits structured visible claims and their evidence references without generating claim text.
