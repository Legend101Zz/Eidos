#!/bin/bash
set -euo pipefail
source ~/eidos-internal/env.sh
cd "$(dirname "$0")/.."
run_root=${1:?Provide an empty output directory}
if [ -d "$run_root" ] && [ -n "$(ls -A "$run_root")" ]; then
  echo 'Output must be empty' >&2
  exit 1
fi
mkdir -p "$run_root"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher plan --cards D03,D04 --runs 1 --seed 20261111 --out "$run_root/design.json"
"$EIDOS_RESEARCH_PYTHON" -m study.launcher run --design "$run_root/design.json" --runs-root "$run_root/runs" --concurrency 2 --stub
for card in D03 D04; do
  "$EIDOS_RESEARCH_PYTHON" -m study.grade_block --block-record "$run_root/runs/$card-r1/block_record.json" --grading-root "$run_root/grading" --results "$run_root/results.json"
done
"$EIDOS_RESEARCH_PYTHON" -m study.analyze --design "$run_root/design.json" --results "$run_root/results.json" --out "$run_root/analysis"
"$EIDOS_RESEARCH_PYTHON" - "$run_root" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);rows=json.loads((root/'results.json').read_text())
assert len(rows)==8 and all(r['status']=='valid' and not r['verified_success'] and r['numeric']['gain']==0 for r in rows),rows
assert len(list((root/'analysis').glob('*.png')))==2
print('DRY RUN PASSED: 8 valid baseline candidates, zero gain, zero successes, 2 plots; no model calls')
PY
