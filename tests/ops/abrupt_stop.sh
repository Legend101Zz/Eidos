#!/usr/bin/env bash
# Abrupt-stop / power-loss recovery check (Linux).
#
# Scenario A — services killed (kill -9 supervisor, API, workers, Temporal; PostgreSQL immediate
#   shutdown) while a research job runs. The job process lives in its own session and survives.
#   After `lab start`: PostgreSQL crash recovery, Temporal resumes the JobWorkflow, the runner
#   reattaches by (pid, start time, boot id) -> the job finishes on its ORIGINAL attempt.
# Scenario B — as A, but the job process dies too (power-loss equivalent for that work). After
#   restart the recorded handle is dead with no exit status -> attempt reconciled as lost -> a
#   new fenced attempt (epoch 2) runs and succeeds; the lost attempt's elapsed usage stays recorded.
#
# Usage: EIDOS_HOME=... tests/ops/abrupt_stop.sh <project-with-eidos.project.command>
set -euo pipefail
P="${1:?project}"
L="${LAB:-$(cd "$(dirname "$0")/../.." && pwd)/.venv/bin/lab}"
H="${EIDOS_HOME:?}"
REF=$("$L" component list "$P" --text eidos.project.command | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")
CFG='{"argv": ["python", "-c", "import time,os; time.sleep(40); os.makedirs(\"{out}\", exist_ok=True); open(\"{out}/done.txt\",\"w\").write(\"ok\")"]}'

hard_kill_services() {
  python3 - "$H" <<'PY'
import json, os, signal, sys
h = sys.argv[1]
st = json.load(open(f"{h}/run/status.json"))
pids = [st.get("supervisor_pid")] + [s.get("pid") for s in st["services"].values() if s.get("pid")]
for pid in pids:
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (ProcessLookupError, TypeError, ValueError):
        pass
print("killed", pids)
PY
  cfg=$(python3 -c "import json; print(json.load(open('$H/config/lab.json'))['pg_bin'])")
  runuser -u postgres -- "$cfg/pg_ctl" -D "$H/services/pg/data" -m immediate stop >/dev/null 2>&1 || "$cfg/pg_ctl" -D "$H/services/pg/data" -m immediate stop >/dev/null 2>&1 || true
}

run_job() {
  "$L" run "$P" "$REF" --wall-seconds 400 --purpose reproduction --config "$CFG" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])"
}

check() {  # job, expected attempt statuses (python literal)
  "$L" job show "$P" "$1" | python3 -c "
import json,sys; d=json.load(sys.stdin)
att=[a['status'] for a in d['attempts']]
print(d['id'], d['status'], att)
assert d['status']=='succeeded', d['status']
assert att==$2, att"
}

echo "== scenario A: services killed, job survives"
JA=$(run_job); sleep 10
hard_kill_services
sleep 3
"$L" start >/dev/null
sleep 90
check "$JA" "['succeeded']"
echo "SCENARIO A OK (reattached; single attempt)"

echo "== scenario B: services and job process killed"
JB=$(run_job); sleep 10
JPID=$(python3 -c "
import json,glob; print(json.load(open(sorted(glob.glob('$H/jobs/$JB/*/handle.json'))[-1]))['pid'])")
hard_kill_services
kill -9 -- -"$JPID" 2>/dev/null || kill -9 "$JPID" || true
sleep 3
"$L" start >/dev/null
sleep 150
check "$JB" "['lost', 'succeeded']"
echo "SCENARIO B OK (lost attempt reconciled; new fenced attempt succeeded)"
"$L" doctor >/dev/null && echo "DOCTOR OK"
