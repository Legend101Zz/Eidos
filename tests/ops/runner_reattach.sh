#!/usr/bin/env bash
# Ops check: kill the training-runner while a job runs; the job must finish on its ORIGINAL attempt
# (reattach by recorded handle: pid + kernel start time + boot id), with no duplicate launch.
# Usage: EIDOS_HOME=... tests/ops/runner_reattach.sh <project-with-eidos.project.command>
set -euo pipefail
P="${1:?project}"
L="${LAB:-$(dirname "$0")/../../.venv/bin/lab}"
REF=$($L component list "$P" --text eidos.project.command | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")
J=$($L run "$P" "$REF" --wall-seconds 300 --purpose reproduction \
  --config '{"argv": ["python", "-c", "import time,os; time.sleep(45); os.makedirs(\"{out}\", exist_ok=True); open(\"{out}/done.txt\",\"w\").write(\"ok\")"]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")
sleep 8
RP=$(python3 -c "import json,os; print(json.load(open(os.environ['EIDOS_HOME']+'/run/status.json'))['services']['training-runner']['pid'])")
kill -9 "$RP"
sleep 100
$L job show "$P" "$J" | python3 -c "
import json,sys; d=json.load(sys.stdin)
att=[a['status'] for a in d['attempts']]
print(d['status'], att)
assert d['status']=='succeeded' and att==['succeeded'], 'expected one reattached attempt'
print('REATTACH OK')"
