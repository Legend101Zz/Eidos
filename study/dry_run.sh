#!/bin/bash
set -euo pipefail
source "/Volumes/Mrigesh SSD/eidos-v1/env.sh"
free_kib=$(df -k / | awk 'NR==2 {print $4}')
if [ "$free_kib" -lt 12582912 ]; then
  echo "STOP: root filesystem has less than 12 GiB free" >&2
  exit 1
fi
study_dir=$(cd "$(dirname "$0")" && pwd)
run_root=${1:-"/Volumes/Mrigesh SSD/eidos-v1/pilot-1/lane-a-dry-run"}
mkdir -p "$run_root"
"$EIDOS_RESEARCH_PYTHON" "$study_dir/launcher.py" plan --seed 711 --out "$run_root/design.json"
"$EIDOS_RESEARCH_PYTHON" "$study_dir/launcher.py" run --design "$run_root/design.json" --runs-root "$run_root/runs" --block D02-r1 --stub
"$EIDOS_RESEARCH_PYTHON" "$study_dir/grade_block.py" --block-record "$run_root/runs/D02-r1/block_record.json" --grading-root "$run_root/grading" --results "$run_root/results.json"
"$EIDOS_RESEARCH_PYTHON" "$study_dir/analyze.py" --design "$run_root/design.json" --results "$run_root/results.json" --out "$run_root/analysis"
"$EIDOS_RESEARCH_PYTHON" - "$run_root" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
results = json.loads((root / 'results.json').read_text())
assert len(results) == 4
assert all(r['status'] == 'valid' and not r['verified_success'] and r['numeric']['gain'] == 0 for r in results), results
assert len(list((root / 'analysis').glob('*.png'))) == 2
print('CHECK: 4 valid baseline candidates, zero improvement successes, 2 plots')
PY
echo "DRY RUN PASSED: baseline candidates graded; no model calls."
