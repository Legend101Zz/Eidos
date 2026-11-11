#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="/Volumes/Mrigesh SSD/eidos-mac/uv-cache"
export TMPDIR="/Volumes/Mrigesh SSD/eidos-v1/tmp"
export EIDOS_RESEARCH_PYTHON="${EIDOS_RESEARCH_PYTHON:-$HOME/eidos-internal/venvs/research/bin/python}"
export PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export EIDOS_PILOT_CHARTER_PATH=docs/experiments/pilot-3/CHARTER.md
export EIDOS_PILOT_CHARTER_COMMIT="$(git log -1 --format=%H -- "$EIDOS_PILOT_CHARTER_PATH")"
# Stubs only read this existing build receipt; they never start or use an Eidos lab.
export EIDOS_STUDY_PYTHON="${EIDOS_P3_STUB_PYTHON:-/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1-lab/bin/python}"
run_root=${1:?Provide an empty SSD output directory}
if [ -d "$run_root" ] && [ -n "$(ls -A "$run_root")" ]; then
  echo 'Output must be empty' >&2
  exit 1
fi
mkdir -p "$run_root"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher plan --cards N01,N03 --runs 1 --seed 20261111 --out "$run_root/design.json"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher run --design "$run_root/design.json" --runs-root "$run_root/runs" --concurrency 2 --stub
for card in N01 N03; do
  "$EIDOS_RESEARCH_PYTHON" -m study.grade_block --block-record "$run_root/runs/$card-r1/block_record.json" --grading-root "$run_root/grading" --results "$run_root/results.json"
done
"$EIDOS_RESEARCH_PYTHON" -m study.analyze --design "$run_root/design.json" --results "$run_root/results.json" --out "$run_root/analysis"
"$EIDOS_RESEARCH_PYTHON" - "$run_root" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); rows=json.loads((root/'results.json').read_text())
assert len(rows)==8 and all(r['status']=='valid' and not r['verified_success'] and r['numeric']['gain']==0 for r in rows),rows
assert all(r['tokens']['total']==0 and r['primary_eligible'] for r in rows)
assert len(list((root/'analysis').glob('*.png')))==2
print('DRY RUN PASSED: 8 valid baseline candidates, zero gain, zero successes, zero tokens, 2 plots; no model calls')
PY
