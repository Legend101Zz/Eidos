# Pilot 3 controller commands

P3T executed no model calls. The non-stub commands below are for a separately
authorized pilot. Use a separate, fixed round-3 Eidos installation at the SSD
path shown below, with its real `pilot-build.json`; installation of that build
is outside this tooling ticket. Do not point these variables at the pilot-2 lab
or output directories. A model run must not start until the task/bar freeze
commit `5bedf94b66da08c00610d742d261abcd715774f0` is present.

Each controller shell starts with:

```sh
source ~/eidos-internal/env.sh
cd "/Volumes/Mrigesh SSD/eidos-v1/p3"
export UV_CACHE_DIR="/Volumes/Mrigesh SSD/eidos-mac/uv-cache"
export TMPDIR="/Volumes/Mrigesh SSD/eidos-v1/tmp"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export EIDOS_STUDY_ROOT="/Volumes/Mrigesh SSD/eidos-v1"
export EIDOS_RESEARCH_PYTHON="$HOME/eidos-internal/venvs/research/bin/python"
export EIDOS_STUDY_HOME="$EIDOS_STUDY_ROOT/pilot-3-lab"
export EIDOS_STUDY_PYTHON="$EIDOS_STUDY_ROOT/venvs/pilot-3-lab/bin/python"
export EIDOS_PILOT_CHARTER_PATH="docs/experiments/pilot-3/CHARTER.md"
export EIDOS_PILOT_CHARTER_COMMIT=5bedf94b66da08c00610d742d261abcd715774f0
export P3_OUTPUT="$EIDOS_STUDY_ROOT/pilot-3"
mkdir -p "$TMPDIR" "$P3_OUTPUT"
test -f "$EIDOS_STUDY_ROOT/venvs/pilot-3-lab/pilot-build.json"
```

Initialize the separate lab once, using dedicated ports and the existing
qualified Temporal binary source on the SSD:

```sh
if [ ! -f "$EIDOS_STUDY_HOME/config/lab.json" ]; then
  "$(dirname "$EIDOS_STUDY_PYTHON")/lab" init --home "$EIDOS_STUDY_HOME" \
    --api-port 28767 --pg-port 25546 --temporal-port 27635 \
    --temporal-source "$EIDOS_STUDY_ROOT/a8lab/services/temporal/bin"
fi
```

The existing lab adapter validates the fixed build receipt, serializes lab
startup with its lock, and allocates new UUID projects per invocation. Ensure
those dedicated ports are available. Arms 3/4 retain the pilot-2 setup and provenance mechanisms;
the stub qualification used an existing pilot-1 build receipt only and started
no lab, so it does not qualify the eventual round-3 installation.

Freeze the randomized design once, then execute paired blocks after run authorization:

```sh
"$EIDOS_RESEARCH_PYTHON" -m study.launcher plan \
  --cards N01,N02,N03,N04 --runs 3 --seed 20261111 \
  --out "$P3_OUTPUT/design.json"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher run \
  --design "$P3_OUTPUT/design.json" --runs-root "$P3_OUTPUT/runs" --concurrency 2
```

Expected plan output: `Planned 12 randomized paired blocks, 48 assignments.`
The launcher skips complete blocks. Retrying an incident archives and replaces
all four arms through its existing `--replace-incident` switch. Do not use that
switch to replace ordinary failures or null results.

Grade only after each whole paired block completes, then analyze:

```sh
for block in "$P3_OUTPUT"/runs/*/block_record.json; do
  "$EIDOS_RESEARCH_PYTHON" -m study.grade_block \
    --block-record "$block" --grading-root "$P3_OUTPUT/grading" \
    --results "$P3_OUTPUT/results.json"
done
"$EIDOS_RESEARCH_PYTHON" -m study.analyze \
  --design "$P3_OUTPUT/design.json" --results "$P3_OUTPUT/results.json" \
  --out "$P3_OUTPUT/analysis"
```

For hosted grading, submit each completed arm using a unique tag. This example
grades N01-r1 arm 1; repeat for each assigned card/block/arm:

```sh
python3 ~/eidos-internal/eidos-grading/tools/submit.py \
  "$P3_OUTPUT/runs/N01-r1/arm1" --card N01 \
  --store "$EIDOS_STUDY_ROOT/p3-artifacts/candidate-store" \
  --run-tag p3-N01-r1-a1-20261111 \
  > "$P3_OUTPUT/N01-r1-a1-dispatch.json"
run_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' \
  "$P3_OUTPUT/N01-r1-a1-dispatch.json")
python3 ~/eidos-internal/eidos-grading/tools/fetch.py \
  --card N01 --run-id "$run_id" --run-tag p3-N01-r1-a1-20261111 \
  --out "$P3_OUTPUT/hosted/N01-r1/arm1" \
  --run-dir "$P3_OUTPUT/runs/N01-r1/arm1" --wait
```

N01–N04 candidate transport uses grading `main`, like D03–D06. The private
workflow runs candidate code in its existing no-network isolation and scores
serialized predictions in a separate VM. Only `grade.json` establishes the
candidate outcome. A successful workflow can still contain a candidate failure.
The score job regenerates labels from the pinned offline dataset/generator;
candidate-VM labels and candidate metadata never determine the test targets.

The completed P3T no-model dry run is under
`/Volumes/Mrigesh SSD/eidos-v1/p3-artifacts/dry-run`. To run a new infrastructure
dry run in a fresh SSD directory, use this independent command (no lab setup or
model authorization required):

```sh
bash study/dry_run_p3.sh "/Volumes/Mrigesh SSD/eidos-v1/p3-artifacts/dry-run-new"
```

The script runs one N01 block and one N03 block, grades all eight baseline
stubs, analyzes the results, and asserts zero gains, successes and model tokens
plus two plots. Set `EIDOS_P3_STUB_PYTHON` only if selecting another existing
valid build receipt. The fixed cards and bars are never changed by a dry run.
