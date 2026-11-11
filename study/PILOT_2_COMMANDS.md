# Pilot 2 controller

Every shell starts with:

```sh
source ~/eidos-internal/env.sh
cd ~/eidos-internal/p2
export EIDOS_STUDY_HOME=~/eidos-internal/p2-lab
# Build a separate fixed installation once; this refuses to overwrite a venv.
if [ ! -f ~/eidos-internal/venvs/pilot-2-lab/pilot-build.json ]; then
  bash study/eidos_arms/build_lab_p2.sh
fi
export EIDOS_STUDY_PYTHON=~/eidos-internal/venvs/pilot-2-lab/bin/python
export P2_OUTPUT=~/eidos-internal/pilot-2
mkdir -p "$P2_OUTPUT"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher plan --cards D03,D04,D05,D06 --runs 3 --seed 20261111 --out "$P2_OUTPUT/design.json"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher run --design "$P2_OUTPUT/design.json" --runs-root "$P2_OUTPUT/runs" --concurrency 2
for block in "$P2_OUTPUT"/runs/*/block_record.json; do
  "$EIDOS_RESEARCH_PYTHON" -m study.grade_block --block-record "$block" --grading-root "$P2_OUTPUT/grading" --results "$P2_OUTPUT/results.json"
done
"$EIDOS_RESEARCH_PYTHON" -m study.analyze --design "$P2_OUTPUT/design.json" --results "$P2_OUTPUT/results.json" --out "$P2_OUTPUT/analysis"
```

These are controller commands for the separately authorized pilot. Tooling qualification makes no model calls. Arms 3/4 allocate UUID projects, with project-scoped tokens; the shared lab start is protected by its existing filesystem lock. D04/D05 have a documented built-in assessor gap.

For the private GitHub grader, submit only after a whole paired block ends.
Use a unique tag for every dispatch; example for D03-r1 arm 1:

```sh
source ~/eidos-internal/env.sh
cd ~/eidos-internal/p2
export P2_OUTPUT=~/eidos-internal/pilot-2
python3 ~/eidos-internal/eidos-grading/tools/submit.py \
  "$P2_OUTPUT/runs/D03-r1/arm1" --card D03 \
  --run-tag p2-D03-r1-a1-20261111 \
  > "$P2_OUTPUT/D03-r1-a1-dispatch.json"
run_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$P2_OUTPUT/D03-r1-a1-dispatch.json")
python3 ~/eidos-internal/eidos-grading/tools/fetch.py \
  --card D03 --run-id "$run_id" --run-tag p2-D03-r1-a1-20261111 \
  --out "$P2_OUTPUT/hosted/D03-r1/arm1" \
  --run-dir "$P2_OUTPUT/runs/D03-r1/arm1" --wait
```

D03–D06 bundles are stored on grading `main`, as required by this ticket.
Pilot-1 cards retain their existing candidates-branch transport by default.
The hosted grade includes joined arm/run/cost metadata when `--run-dir` is used.
Never substitute a workflow conclusion for `grade.json`'s candidate outcome.
