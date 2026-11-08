#!/usr/bin/env bash
# Ops check: pause/cancel with policy checkpoint-and-stop. The launcher signals the worker
# (SIGUSR1); the training loop checkpoints at the next committed optimizer step and returns; the
# job ends `cancelled` with its checkpoint published (citable partial result, branchable parent).
# Usage: EIDOS_HOME=... tests/ops/checkpoint_and_stop.sh <project with eidos.pytorch.train and a model adapter>
set -euo pipefail
P="${1:?project}"
L="${LAB:-$(cd "$(dirname "$0")/../.." && pwd)/.venv/bin/lab}"
REF=$("$L" component list "$P" --text eidos.pytorch.train | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")
J=$("$L" run "$P" "$REF" --wall-seconds 900 --purpose open-search \
  --config '{"adapter": "eidos_adapter:ADAPTER", "builder": "OrderedSwitchNet", "steps": 200000, "seed": 41}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")
sleep 20
"$L" cmd cancel_run -p "$P" --payload "{\"job_id\": \"$J\", \"policy\": \"checkpoint-and-stop\"}" >/dev/null
sleep 35
"$L" job show "$P" "$J" | python3 -c "
import json,sys; d=json.load(sys.stdin); r=(d['terminal'] or {}).get('receipt',{})
roles=[o['role'] for o in d['outputs']]
print(d['status'], r.get('stopped_by_policy'), r.get('end_step'), roles)
assert d['status']=='cancelled' and r.get('stopped_by_policy')=='checkpoint-and-stop' and 'checkpoint' in roles
print('CHECKPOINT-AND-STOP OK')"
