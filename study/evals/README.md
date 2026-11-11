# Eidos improvement ladder

The handoff specification defines Tier 0 offline invariants and coordinator replays, Tier 1 development mini-evals, and Tier 2 prospective pilots on cards sealed before a change is designed. Tier 0 and Tier 1 must pass before a change reaches a pilot. Development scorecards are tuning data. Source [EVALS.md](/Users/comreton/Desktop/eidos-astra-handoff/EVALS.md).

Codex implements a change, runs the core Tier-0 suite, builds its frozen lab and runs Tier 1. Astra reviews the scorecard, replay evidence and comparison with the previous build before pilot approval. Core coordinator tests consume the fixtures described in [fixtures/README.md](fixtures/README.md). The mini-eval reuses `study.launcher.run_block`, the pilot arm adapters, prompts, task packaging and `study.grade_block.grade_block`. Research Python handles packaging, baseline stubs and local independent grading. Arm 4 dispatches through the selected installed lab Python.

## Environment

Run from the repository root. Source the internal environment first, then put all caches and temporary files on the SSD. Load a chosen eval lab's environment after the internal environment.

```bash
source ~/eidos-internal/env.sh
export EIDOS_STUDY_ROOT='/Volumes/Mrigesh SSD/eidos-v1'
export UV_CACHE_DIR="$EIDOS_STUDY_ROOT/tmp/uv-cache"
export UV_PYTHON_INSTALL_DIR="$EIDOS_STUDY_ROOT/tmp/uv-python"
export PIP_CACHE_DIR="$EIDOS_STUDY_ROOT/tmp/pip-cache"
export TMPDIR="$EIDOS_STUDY_ROOT/tmp"
export XDG_CACHE_HOME="$EIDOS_STUDY_ROOT/tmp/xdg"
export MPLCONFIGDIR="$EIDOS_STUDY_ROOT/tmp/mpl"
export PYTHONPYCACHEPREFIX="$EIDOS_STUDY_ROOT/tmp/pycache"
mkdir -p "$TMPDIR"
```

## Frozen build

```bash
study/evals/build_lab.sh HEAD candidate-a
source '/Volumes/Mrigesh SSD/eidos-v1/evals-labs/candidate-a/env.sh'
```

The helper archives the resolved commit, builds workspace wheels, installs a dedicated venv and initializes its lab home under `eidos-v1/evals-labs/<name>/`. It preserves existing build directories through exclusive allocation. `venv/pilot-build.json` records the Eidos commit and wheel hashes. `ports.json` records free ports selected from 33000–58999 for API, PostgreSQL and MCP, with a disjoint nine-port Temporal block in 30000–32767. Existing sibling lab allocations are excluded. Every selected port differs from 28767, 25546, 27635 and 28766, and the older study ports 25544, 27633 and 28765. Concurrent use requires distinct allocations.

The helper uses the caller's environment and its own generated eval environment file. Supply `EIDOS_STUDY_TEMPORAL_SOURCE` to reuse a downloaded Temporal binary directory. Initialization creates and stops its own PostgreSQL instance. Normal arm-4 execution starts services through the existing adapter. To stop that eval lab after use, run its installed Python with `-m study.eidos_arms.lab stop` while its environment is loaded.

## Baseline-stub qualification

The existing stub reads the frozen build receipt and runs the supplied baseline. This path performs zero model calls and uses zero processed model tokens. It can use an existing build receipt without starting services.

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.mini_eval \
  --cards N01,N03 --arms 2,4 --seed-index 1 \
  --out '/Volumes/Mrigesh SSD/eidos-v1/tmp/eval-stub-a' \
  --lab-home '/Volumes/Mrigesh SSD/eidos-v1/evals-labs/candidate-a/home' \
  --lab-python '/Volumes/Mrigesh SSD/eidos-v1/evals-labs/candidate-a/venv/bin/python' \
  --stub
"$EIDOS_RESEARCH_PYTHON" -m unittest discover -s study/tests
"$EIDOS_STUDY_PYTHON" -m unittest study.eidos_arms.test_contract study.eidos_arms.test_parity
```

A stub scorecard reports baseline gain zero, verified success false and `stub=true`. Its `tier1.eligible` is false. A two-card test qualifies infrastructure and sets `full_development_set=false`.

## Live Tier 1

The following command spends model quota and requires authorization for live evaluation. Ticket T1 validation uses `--stub`.

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.mini_eval \
  --cards N01,N02,N03,N04,D02,D03,D04 --arms 4 --seed-index 1 \
  --out '/Volumes/Mrigesh SSD/eidos-v1/tmp/eval-candidate-a' \
  --lab-home "$EIDOS_STUDY_HOME" --lab-python "$EIDOS_STUDY_PYTHON"
```

Use `--arms 2,4` to add the coding reference arm. `--seed-index` selects the pilot block index 1, 2 or 3. Training seeds and reproduction rules follow the pilot adapters and grader. Blocks run at concurrency 2. Each requested arm receives 1800 seconds, 8,000,000 processed tokens and one CPU thread. Arm-4 setup and teardown accounting follow the pilot launcher. Each block is graded after the runs finish. Each output directory must start empty.

Outputs include `design.json`, `runs/`, `grading/`, `results.json`, `scorecard.json` and `scorecard.md`. Live scorecards are also stored at `evals/history/<controller-git-sha>.json` in the checkout. Repeated live evals of the same SHA replace that history entry, with individual output directories retaining each scorecard.

Each run records completion, verified success, measured test gain, verified test gain, gain ratio, tokens, wall time, unused time at conclusion, failure classes and invariant evidence. Arm 4 requires a recorded conclusion. Arm 2 and stubs use a successful final answer as completion. Unused time uses the conclusion ledger timestamp when available, otherwise final wall time. A run without a conclusion has null unused time.

Gain ratio divides verified gain by the median verified gain of coding arms 1–3. Unverified runs contribute zero to the ratio, retaining their measured gain separately. Missing or nonpositive reference medians produce null ratios. Aggregate counts cover completion, verification, crashes, timeouts, token budgets, incidents, invalid output and invariant flags. `by_arm` provides the same counts separately. Provenance includes the controller git SHA and dirty flag, full build receipt, its SHA-256, and the reference snapshot SHA-256.

Invariant observations detect a locked candidate ending with `needs_input`, runtime deadline-limit usage receipts, fabricated empty waits, and known held-out IDs/digests in recorded model-facing contexts, replies and read receipts. Held-out scanning is best effort. Its coverage, missing blobs and matches are recorded, and derived information can evade this scan.

## Reference snapshots

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.regenerate_references
```

The generator reads pilot-3 local `grading/N0*-r*/arm[123]/grade.json` and pilot-2 `regrade-addendum4/results-corrected.json`. It pools verified numeric test gains over coding arms and runs, storing per-card median, min/max, sample count and individual samples, plus source-file hashes. Pilot 3 is still finishing, so the committed snapshot is partial and reproducible from those hashes. Refresh it as local grades arrive.

The specified pilot-2 corrected file has D03–D06 rows and contains no D02 rows. D02's reference median and range are null, with a missing-source reason. Its run and local grade remain available. To compare build quality, use the same reference snapshot for both scorecards.

## Regression comparison

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.compare previous/scorecard.json candidate/scorecard.json --tolerance 0.05
```

The command exits 0 for a passing comparison, 1 for a regression and 2 for incompatible inputs. The tolerance permits an absolute median gain-ratio decrease of 0.05 ratio points by default. Completion or verification decreases, increases in dead ends or other failure counts, and median gain-ratio decreases exceeding tolerance fail the comparison. Per-card completion losses and new dead ends also fail when aggregate counts balance. Cards, arms, seed index, envelope, stub mode, reference snapshot and ratio coverage must match.

Tier 1 requires arm 4 to conclude on all seven development cards, zero dead ends and a passing comparison against the previous build. The scorecard records completion and dead-end checks. Run the comparator to establish the regression check.
