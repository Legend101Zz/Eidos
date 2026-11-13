# Offline practice harness

Read the frozen manifest and exposure ledger in `docs/experiments/practice-family/` before scoring. These are practice/development measurements, not prospective confirmation. Pipeline C is intentionally unavailable in step 1.

Use a Python environment with the bundle's pinned numpy, scikit-learn and joblib versions. For development calibration it also needs the public baseline's torch dependency. Set `EIDOS_RESEARCH_PYTHON` to that interpreter and source the orchestrator SSD environment first. Set OMP/MKL/OPENBLAS/VECLIB thread counts to 1 and use `nice -n 10` for long runs.

```sh
python -m study.practice.build_cards --out /external/practice/data
python -m study.practice.run_gate --data /external/practice/data --out /external/practice/gate --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST --kind construction
python -m study.practice.run_gate --data /external/practice/data --out /external/practice/gate --fresh --evaluation-id comparator-v1 --candidate-digest FROZEN_DIGEST --kind construction
python -m study.practice.run_gate --package-development --data /external/practice/development --out /external/practice/calibration --evaluation-id calibration-v1 --candidate-digest FROZEN_DIGEST --kind construction
```

Data and evidence must live outside this repository. Never point these commands at a sealed card or grading directory. `bundles/` contains only visible data and the functioning baseline. `controller/` contains labels and oracle sidecars; it must never be given to an investigator. The candidate sandbox gets only its own bundle, selected recipe source and label-free test inputs.

Every table caches five fold scores and a controller test score per recipe; it freezes the A0–A3 choices before reading test labels. Scores are accuracy for classification and negative RMSE for regression, so larger always wins. `selection_replay.py` derives paired-band and prior/permuted-prior diagnostics from those tables without fitting. `--fresh` executes all three seeds through `study.fresh_check.candidate_command(timeout=120)`, checks reload equality and records checkpoint sizes and fit times. The gate reports which expensive checks actually ran; a cached score table is not a fresh-seed check.

The guard uses t intervals within generators and an exact conditional sign-flip test plus modified Hartung–Knapp random-effects interval across machinery holdouts. Separate operator holdout classes cannot borrow machinery generators. The .5-SE harm screen is multiplicity-controlled; acceptance requires a pooled gain of at least +.5 SE and indicates positive transfer and no detected material harm, not proof of per-generator equivalence. Missing in-fold, null-claim or resource evidence rejects. Effects use paired test SE with a declared .001 floor; this is not normalization by reference headroom. Finite-source resplits use effective instance n=1 and prior observed SD floors rounded up: wine 1.04, rotated digits 4.1, offset digits 32. A generic 1.04 floor alone understated digits uncertainty in the correlated-source stress.

`compute_table` poisons held-fold rows and reruns the actual fit workflow once per recipe, comparing fitted state. `controls` executes a supervised pre-CV screen through that path and measures generic helpful/neutral/harmful/card-specific prediction-fault controls. `operating` records Monte Carlo error rates and power at the observed instance SDs and at lower noise. `references` fits independent large-sample attainable references (finite sources use their available train+validation data); `recompute` requires a new cache and recomputes one card per generator. Those receipts are required by the strengthened acceptance report.

```sh
python -m study.practice.references --data /external/practice/data --out /external/practice/evidence --evaluation-id references-v1 --candidate-digest FROZEN_DIGEST
python -m study.practice.controls --data /external/practice/data --references /external/practice/evidence --out /external/practice/evidence --evaluation-id controls-v1 --candidate-digest FROZEN_DIGEST
python -m study.practice.operating --family /external/practice/gate/gate.json --out /external/practice/evidence/operating.json --evaluation-id operating-v1 --candidate-digest FROZEN_DIGEST
python -m study.practice.recompute --family /external/practice/gate/gate.json --first-gate /external/practice/gate --out /external/practice/cold --evaluation-id cold-v1 --candidate-digest FROZEN_DIGEST
```

Every scoring run, including construction, calibration, references and controls, must preregister before scoring. CLI and API refuse missing evaluation IDs/digests; `compute_table`, supplied baseline and fresh export entry points verify a matching receipt. `evaluate` verifies the candidate and exact registered generator set (omitting a harmed generator is refused) and only returns after durably appending its verdict. A changed candidate needs a new ID; unrelated policy edits do not consume candidate burns. `--kind construction` labels exposed comparator measurements; it still registers and logs every verdict and cannot supply evidence for pipeline C.

The fixed absolute ledger directory is `/Volumes/Mrigesh SSD/eidos-v1/claude-orch/practice-ledger/`. Scoring never creates or resets a missing ledger. A trusted controller provisions it once using `ledger.initialize()`. Entries are append-only JSONL, with sequence numbers, previous-entry hashes and HMAC authentication; an independent pinned head detects tail deletion. Every run verifies the full chain. The controller also provisions a random 32-byte `practice-secrets/row-token-salt` beside this directory. Keep both trusted trees out of candidate mounts. HMAC row IDs are deterministic only with that secret and cannot be reconstructed from public seed streams. Backup trusted state; do not regenerate it during a reproducibility run. These checks detect corruption and casual rewriting, not an administrator who can replace both secrets and all trusted state.

In addition to the per-generator Holm loss screen, independent pooled loss tests cover all design/operator-class generators and each role separately at a zero harm margin. A conventional one-sided .05 test labels credible loss. A more sensitive **one-sided .15 risk screen** rejects when the upper 70% HK interval is negative. The latter level was calibrated using exposed r2 variance probes; it is not a 95% confidence claim. `operating.json` assesses the complete rule on r3 variances, including mixed +.8 holdout/−.5, −1, −2 elsewhere cells, each actual nuisance-template patch, all Wilson bounds, and unmet targets. Operator-class pools of two remain weak; no per-generator equivalence is claimed.

The nine newer machinery holdouts now use nine nuisance structures (none, correlated Gaussian, heavy tails, near-duplicates, heteroskedastic, categorical, periodic, low-rank uniform, independent uniform). No template covers over a third of machinery holdouts. Dev lint additionally checks a statistical N04 signature: independent Gaussian noise count, exact redundant rank and class-conditional moments; a perturbed off-trace copy is tested.


`python -m study.practice.acceptance --help` describes the five-criterion acceptance report. `claim_audit.py` audits structured visible claims and their evidence references without generating claim text.
