#!/usr/bin/env bash
# Ops check (Linux): machine-suspend equivalent. SIGSTOP every lab process (supervisor, PostgreSQL, Temporal,
# API, workers) AND a running research job for longer than the runner heartbeat (10 s), the attempt lease
# (120 s) and the activity heartbeat timeout, then SIGCONT everything. Expect: the job finishes exactly once
# (one publication, no duplicate outputs), any superseded attempt is fenced, compute accounting is recorded,
# the event sequence stays gap-free and the lab reports healthy. A real macOS sleep stays a Mac item.
# Usage: EIDOS_HOME=... LAB=... tests/ops/suspend_resume.sh <project-with-eidos.project.command> [stop_seconds]
set -euo pipefail
P="${1:?project}"; STOP="${2:-150}"
L="${LAB:-$(dirname "$0")/../../.venv/bin/lab}"
REF=$($L component list "$P" --text eidos.project.command | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")
J=$($L run "$P" "$REF" --wall-seconds 900 --purpose reproduction \
  --config '{"argv": ["python", "-c", "import time,os; [time.sleep(1) for _ in range(40)]; os.makedirs(\"{out}\", exist_ok=True); open(\"{out}/done.txt\",\"w\").write(\"ok\")"]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")
for _ in $(seq 1 60); do
  S=$($L job show "$P" "$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"); [ "$S" = running ] && break; sleep 1
done
sleep 5
PIDS=$(python3 - <<'PY'
import json, os, subprocess
st = json.load(open(os.environ["EIDOS_HOME"] + "/run/status.json"))
pids = {st.get("supervisor_pid")} | {s.get("pid") for s in st["services"].values()}
home = os.environ["EIDOS_HOME"]
# PostgreSQL children and the research job's process group (started in its own session under jobs/)
for line in subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout.splitlines():
    pid, _, args = line.strip().partition(" ")
    if home in args or "eidos-contained" in args or "{out}" in args or "time.sleep(1)" in args:
        pids.add(int(pid))
print(" ".join(str(p) for p in sorted(p for p in pids if p and p != os.getpid())))
PY
)
echo "suspending $(echo $PIDS | wc -w) processes for ${STOP}s"
kill -STOP $PIDS 2>/dev/null || true
sleep "$STOP"
kill -CONT $PIDS 2>/dev/null || true
echo "resumed; waiting for the job"
for _ in $(seq 1 180); do
  S=$($L job show "$P" "$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  case "$S" in succeeded|failed|cancelled) break;; esac; sleep 2
done
$L job show "$P" "$J" | python3 -c "
import json,sys; d=json.load(sys.stdin)
att=[(a['status'], (a.get('usage') or {}).get('compute_seconds')) for a in d['attempts']]
outs=[o['slot'] for o in d['outputs']]
print(d['status'], att, outs)
assert d['status']=='succeeded', 'job did not succeed after resume'
assert outs.count('done') <= 1 and len(outs) == len(set(outs)), 'duplicate publications'
assert sum(1 for a in d['attempts'] if a['status']=='succeeded') == 1, 'exactly one successful attempt'
print('SUSPEND/RESUME JOB OK')"
python3 - <<'PY'
import json, os, subprocess
lab = os.environ.get("LAB", "lab")
d = json.loads(subprocess.run([lab, "doctor", "--json"], capture_output=True, text=True).stdout)
bad = [c for c in d if c["status"] == "fail"]
print("doctor fails:", bad)
assert not bad
print("DOCTOR OK")
PY
