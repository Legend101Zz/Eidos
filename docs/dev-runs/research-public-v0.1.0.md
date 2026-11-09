# Development runs on public research release `research-public-v0.1.0`

Status: **hosted-Linux development runs. This execution profile is unqualified by the research release
(only macOS arm64 CPU reference execution is qualified).** These runs exercise Eidos integration and
contracts; they make no capability or superiority claim.

## Release identity

| Item | Value |
|---|---|
| Source commit | `c0c6d7ad09fb1c92296f27b4172ed6fdf5412138` (imported as `f55b9b3`, subtree `research/public/` only). After the 2026-11-09 commit ID rewrite: `6a6594af0e6a1f4baa429a4c080122c023a0cce8`, imported as `ac17cfe`, same trees ([map](../release/COMMIT_ID_MAP.md)) |
| Release id / interfaces | `research-public-v0.1.0`; `eidos-task/0.2`, `eidos-answer/0.2`, `eidos-task-bundle/0.1`, charter draft-0.3 |
| `PUBLIC_MANIFEST.json` sha256 | `767ed63d1bfb23a9718c4e73ce15266c3a1b965e59026116ab05c8998a40e2a7` (54/54 files verified by sha256 and bytes; 0 unlisted) |
| Separately distributed archive (not in repo) | `eda0b28f8ad24448bab6277c6517955bf12a6b805f323dc35ad189bd71fd5a12` |
| Development env | Python 3.12.11, `requirements.lock` installed with uv; torch `2.15.0+cpu` from the local wheelhouse (the lock pins `torch==2.15.0`; the CPU build is a Linux-specific deviation) |

## Commands (outputs outside the source tree: `/workspace/.eidos-dev/lead/research-runs/`)

```bash
PY=/workspace/.eidos-dev/lead/envs/research-dev/bin/python
(cd research/public/development && $PY check_bundles.py --out $RUNS/smoke)      # {"bundle_cases_passed": 3, "negative_controls_passed": 2}
(cd research/public/development && $PY package_task.py --task D02 --out $RUNS/D02-bundle)   # likewise D01, D12
lab project create d02dev --budget compute_seconds=20000 --budget trials=200
lab task import d02dev $RUNS/D02-bundle --python $PY            # verifies bundle hashes; exploration-only charter
lab component install d02dev components/eidos.project.command   # whole-project command escape hatch
lab component install d02dev components/eidos.task.score-classification
lab investigate start d02dev --charter charter:d02-dev --policy bundle-baseline-replicate \
   --input train=... --input validation=... --input task=... --limit compute_seconds=6000 ...
lab task answer d02dev --task-id D02 ... --validator research/public/development/validate_answer.py
```

## Runs (2026-11-07)

All investigator decisions below came from the **fixture provider** (`bundle-baseline-replicate`, a
deterministic, task-agnostic scripted policy). No LLM was used. The investigator received only the bundle's
declared inputs (train/validation arrays, task.json) and the generic components. It never received parent
development references, qualification records or test labels.

| Task | Investigation | Turns | Jobs | Observation | Answer status | Validator |
|---|---|---|---|---|---|---|
| D01 | `inv-d01dev-fixture-1` | 7 | 3 train + 3 score | validation accuracy 0.9667–1.0000 (3 seeds); fresh-process reload: 30/30 identical | `no_improvement_found` / `operational_reliability` | structural pass |
| D02 | `inv-d02-fixture-2` | 6 | 3 train + 3 score | validation accuracy 0.8301–0.8719 (3 seeds); reload 359/359 identical | `no_improvement_found` / `no_positive_claim` | structural pass |
| D12 | `inv-d12dev-fixture-1` | 6 | 3 train + 3 score | validation accuracy 0.5000 on every seed (constant baseline) | `no_improvement_found` / `no_positive_claim` | structural pass |

These results show that the loop runs import → reproduction → measurement → conclusion → common-answer
export. They are not research results. The fixture policy never attempts an intervention, so it cannot
produce an improvement. An LLM-driven run needs a live provider (see `IMPLEMENTATION_STATUS.md`, live
qualification requests).

## Failures observed and repairs (builder interventions, not autonomous behaviour)

| Observed failure | Class | Repair | Regression |
|---|---|---|---|
| `inv-d02-fixture-1`: all three training jobs failed with a contract error (`unknown role 'source'`). The investigator correctly concluded "no successful baseline runs". | execution (builder component defect) | Fixed the staging role in `eidos.project.command`. Released as **1.0.1** because release identity is immutable. The policy now resolves the newest active release from the turn's catalog snapshot instead of pinning a version. | Run `inv-d02-fixture-2` succeeds. The failed run remains in the ledger. |

## Not performed

- A builder-side compatibility check of exported test predictions with the release's `grade.py` was **not run**. The sandbox permission policy denied the command, and that grader reads development test labels.
- Qualification of this hosted-Linux profile, real-LLM runs, and the other 11 cards (reference-only) were not attempted.
