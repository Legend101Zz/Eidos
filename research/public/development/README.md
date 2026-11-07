# Public development collection

Fourteen capability cards exercise nine numerical project variants plus a lifecycle fixture. Variants share **eight project clusters**: iris/readout repair; digits/offset image robustness/instruments; conditioning regression; seasonal forecasting/reuse; order-sensitive sequences; simulator coverage; duplicate-input null; and stochastic training lifecycle. They are not fourteen independent scientific tasks. Real data: UCI iris and the 1,797-image scikit-learn digits subset. Other data are small constructed controls. There is no claim these fixtures represent frontier research difficulty.

## Install and run

Python 3.12 was used on macOS arm64; CPU, one torch thread. MPS was available but not used. CUDA was unavailable. `requirements.lock` records the exact installed version set, not a cross-platform wheel/hash lock. Other operating systems and package builds remain unqualified.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python prepare.py --project digits --out /tmp/eidos-digits-inputs
.venv/bin/python qualify.py --out /tmp/eidos-development-qualification
```

For individual baseline/candidate execution and independent scoring, see `../TASK_INTERFACE.md`. `project.py` is an ordinary custom PyTorch project, callable without Eidos. `fresh_check.py` reloads a model in a separate process and compares predictions exactly. `grade.py` consumes serialized numeric predictions and fixed development labels; it does not import candidate code. `lifecycle.py` exercises stochastic training state, actual process exit and branch independence. `instrument.py` demonstrates one shared descriptive diagnostic on image classification and forecasting. `validate_answer.py` is a limited structural validator.

## Capability cards and references

| Card | Project | Known development reference | Scope |
|---|---|---|---|
| D01 | Iris | Fit/export/reload an ordinary linear PyTorch classifier | Custom import and behavior equality |
| D02 | Digits | Small MLP trained longer than the functioning linear baseline | Existence of a useful recipe; architecture and compute effects are confounded |
| D03 | Digits offset | Per-image centering | Fixed offset robustness; not general corruption robustness |
| D04 | Conditioning | Train-set normalization with Adam | Input scaling, not a novel optimizer |
| D05 | Seasonal forecasting | Train a lag-based linear predictor | Independent series; constructed seasonal family |
| D06 | Sequence order | Position-sensitive linear readout instead of pooled input | Fresh training seeds, specific task only |
| D07 | Iris weights | Undo the planted output permutation | Known construction; repair does not establish a general diagnosis ability |
| D08 | Digits instrument | Validation error by input-norm quartile | Association, not causality; authored reference not autonomous creation |
| D09 | Lifecycle | Save optimizer/RNG/stream/custom state, resume | Exact on tested CPU fixture only |
| D10 | Lifecycle | Private restored child with an explicit weight edit | Parent immutability and child difference |
| D11 | Lifecycle | Exit worker with code 17 after checkpoint, start new worker | Process interruption, not full power loss/Temporal recovery |
| D12 | Duplicate-input null | Constant classifier; zero measured gain | Constructed paired-label control, no global impossibility conclusion |
| D13 | Forecasting instrument | Reuse the identical D08 function | Software reuse across tasks; no scientific transfer result |
| D14 | Simulator coverage | Train on balanced generated points | Generator allowed, source and actual example count recorded |

The shared training/reference code and generator targets are public by design. This permits reproducible engineering work and exposes solutions; never recycle these as final held-out tasks. Train/validation/test are split before transforms. Digits uses a stratified split of the bundled subset; writer IDs are not available in that loader, so this is **not** writer-independent generalization. Iris is too small for a strong real-world result. Three seeds are qualification replicates, not a campaign power estimate.

The references deliberately demonstrate attainable improvements; they do not identify optimal methods, exhaust the hypothesis space, equalize every reference training recipe, or show Eidos superiority. Equal research envelopes apply to the future four-arm study. Investigators may submit different valid methods through the common output contract.

## Upstream conformance audit

`audit_upstream.py` executes selected metric functions with tiny fabricated fixtures. It does not run the full benchmark tasks or agents. Clone the pinned sources below to a scratch directory, then run:

```sh
git clone https://github.com/facebookresearch/MLGym.git /tmp/eidos-sources/MLGym
git -C /tmp/eidos-sources/MLGym checkout 9d40c1b5035202018cd7091fb4e83a9c68b377c0
git clone https://github.com/facebookresearch/airs-bench.git /tmp/eidos-sources/airs-bench
git -C /tmp/eidos-sources/airs-bench checkout 18e4f1d501069cf7d7e2740d81c2ca748c56a6a1
.venv/bin/python audit_upstream.py --sources /tmp/eidos-sources --out /tmp/eidos-upstream-fixtures.json
```

The current package environment has datasets 5.1.0; upstream AIRS metadata pins datasets 4.0.0. The audit calls metric functions with fabricated input, including an explicit solar loader replacement. It does not validate upstream dataset loading, environments, full task execution or official leaderboard comparability.

See `NOTICES.md` for sources and dataset rights. No protected RE-Bench solution is included or extracted. No benchmark source code is copied into this package.

## Public release smoke and resources

For the first integration, use `package_task.py --task D01|D02|D12 --out EMPTY_DIRECTORY` (one task ID per invocation). Only that resulting directory is default investigator input. The parent `project.py` and `data.py` are known-development reference files; the public reference table above must not be supplied to a supposedly unaided run. See TASK_INTERFACE for visibility and migration rules.

```sh
python check_bundles.py --out /tmp/eidos-smoke-NEW
python check_analysis_and_answer.py
```

Use a fresh output directory for each run. Three bundles are checked against their baseline predictions in fresh processes, plus grade-overflow and failing-qualification controls. D01 checks custom import/export, D02 supplies a functioning classifier and independently qualified improving reference, and D12 exercises null/inconclusive reporting. Only the first two have real UCI data. These cases are small development exercises, not final research tasks.

Resource estimates: one CPU thread per training process; reserve 2 GiB RAM and 4 GiB free disk for installation and one sequential smoke/full run. These are conservative planning estimates, not enforced or measured peak limits. The existing installed environment occupies about 1.0 GiB on macOS arm64. No GPU or model API is needed. Native Linux/hosted builder wheels and runtime remain unqualified: if installation fails, report that profile unqualified rather than substituting package versions silently. The lock is an exact installed-version snapshot, not hash-locked transitive wheel selection. Public task outputs are small; do not put generated environments/checkpoints into Git.
