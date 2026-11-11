# Pilot 2 controller

Every shell starts with:

```sh
source ~/eidos-internal/env.sh
cd ~/eidos-internal/p2
export EIDOS_STUDY_HOME=~/eidos-internal/p2-lab
# Uses the installed fixed wheel; this does not start or change pilot-1's lab.
export EIDOS_STUDY_PYTHON=~/eidos-internal/venvs/pilot-1-lab/bin/python
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
