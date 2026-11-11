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
  --lab-home "$EIDOS_STUDY_HOME" --lab-python "$EIDOS_STUDY_PYTHON" \
  --expected-build-sha "$FROZEN_BUILD_SHA" \
  --reference-path "$FROZEN_REFERENCES" --reference-sha256 "$REFERENCE_SHA"
```

Use `--arms 2,4` to add the coding reference arm. `--seed-index` selects the pilot block index 1, 2 or 3. Training seeds and reproduction rules follow the pilot adapters and grader. Blocks run at concurrency 2. Each requested arm receives 1800 seconds, 8,000,000 processed tokens and one CPU thread. Arm-4 setup and teardown accounting follow the pilot launcher. Each block is graded after the runs finish. Each output directory must start empty.

Outputs include `design.json`, `runs/`, `grading/`, `results.json`, `scorecard.json` and `scorecard.md`.
Gain ratio uses the median verified gain of coding arms 1–3. Unverified runs contribute zero to the
ratio and retain their measured gain separately. A missing or nonpositive reference produces a null
ratio. Each scorecard records the reference snapshot hash, controller commit, frozen build receipt and
per-run evidence hashes.

Compare attempts with:

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.compare "$BASELINE_SCORECARD" "$CANDIDATE_SCORECARD"
```

The comparison prints per-assignment and aggregate changes, blockers and both scorecard hashes.
Exit zero for live scorecards requires the gate to pass. A stub comparison remains diagnostic.

## Evidence and readiness controls

For both builds use the same clean controller commit, model-facing identifier audit and hash-pinned
reference file. Freeze the file once from the complete Pilot 3 verdict-of-record package and corrected
Pilot 2 source.

```bash
"$EIDOS_RESEARCH_PYTHON" -m study.evals.regenerate_references \
  --pilot3-package "$PILOT3_FINAL_PACKAGE" --out "$FROZEN_REFERENCES"
REFERENCE_SHA=$(shasum -a 256 "$FROZEN_REFERENCES" | cut -d' ' -f1)
```

Pass `--reference-path "$FROZEN_REFERENCES" --reference-sha256 "$REFERENCE_SHA"` and an independently
selected `--expected-build-sha <full-commit>` to each live run. The same arguments with `--preflight`
verify provenance without starting a lab. Preflight checks the dedicated home, interpreter, source archive,
wheel hashes, installed wheel payload and install origins, and configured ports. Checks run before
execution and disk admission checks recur before build phases, launch and independent grading.

Each live attempt allocates its own output and history path
`evals-history/<frozen-build-sha>/<controller-sha>-seed<index>-<attempt-id>/` on the SSD. An admission
receipt preserves the path and build/reference provenance even when the attempt ends without a scorecard.

Arm 4's opt-in bridge scans each outgoing provider payload for known evaluator identifiers before
forwarding. It stores request hashes and matched identifiers without retaining bodies or headers.
The scorecard also scans registered packed contexts, replies and read receipts. Stored internal provenance
manifests are outside the model-facing scan. Unavailable coverage is unknown and blocks the gate.
The audit has limited coverage of derived information. Core Tier-0 boundary tests remain the primary
isolation check. The required investigator invariant gate covers arm 4. The optional coding arm's private provider context
is unavailable to this audit. Its unknown coverage remains visible, and its completion, verification and
quality are reported separately and compared with the matching reference arm.

Tier 1 uses a prospective tolerance of zero, with an absolute `1e-12` floating-point epsilon. Check
every card and arm for a lost investigator conclusion, verified outcome, verified native-unit gain, or
gain ratio where a reference exists. Candidate dead ends, fabricated waits and held-out identifier
matches block the gate. Investigator reasoned abstention and controller fallback are reported
separately, and controller fallback contributes zero investigator conclusions. Deadline receipts
remain diagnostic. Receipt counts accompany token and wall-time totals. Missing D02 references remain
null and its native-unit verified gain remains part of comparison. One seed gives descriptive
development evidence.


The shared arm-4 controller records outer deadline, requested lab deadline, bridge startup and
start-request timestamps, the server-registered deadline, and the observed margin in
`investigation.json` and the scorecard. Round 3 registers a relative deadline at server receipt.
Round 4 honors the absolute deadline. Interpret timing with each run's recorded margin and
the scorecard's frozen build and controller identities.
