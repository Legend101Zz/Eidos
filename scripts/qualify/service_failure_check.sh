#!/usr/bin/env bash
# A15: service failures on a running lab (macOS or Linux; no sudo). Creates its own lab under WORK on its own
# ports, starts a 150-second job, then SIGKILLs each lab service in turn BY PID (from run/status.json; for
# PostgreSQL the postmaster pid from postmaster.pid) and checks that the supervisor restarts it, the job still
# finishes on its original attempt, and lab doctor is clean. Then runs tests/ops/abrupt_stop.sh (all services
# killed with and without the job). Receipt: WORK/service-failure-receipt.json. Stops the lab at the end.
#
#   LAB=<venv>/bin/lab WORK=<dir on the SSD> bash scripts/qualify/service_failure_check.sh
set -euo pipefail
LAB="${LAB:?set LAB}"; WORK="${WORK:?set WORK}"
PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export EIDOS_HOME="$WORK/svclab"; OUT="$WORK/service-failure-out"
mkdir -p "$OUT" "$WORK/svc-project"
[ -f "$EIDOS_HOME/config/lab.json" ] || "$LAB" init --home "$EIDOS_HOME" --api-port 38765 --pg-port 35544 --temporal-port 37633 --pg-bin "$PG_BIN" > /dev/null
trap '"$LAB" stop --running-jobs cancel >/dev/null 2>&1 || true' EXIT
"$LAB" start > /dev/null
P=svc
"$LAB" project create "$P" --budget compute_seconds=20000 --budget trials=50 > /dev/null 2>&1 || true
LABPY="$(python3 -c "import json; print(json.load(open('$EIDOS_HOME/config/lab.json'))['lab_python'])")"
"$LAB" project register-env "$P" --python "$LABPY" --root "$WORK/svc-project" > /dev/null
"$LAB" component install "$P" "$("$LABPY" -c 'import eidos,pathlib;print(pathlib.Path(eidos.__file__).parent/"_components"/"eidos.project.command")')" > /dev/null 2>&1 || true
REF="$("$LAB" component list "$P" --text eidos.project.command | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")"
J="$("$LAB" run "$P" "$REF" --wall-seconds 900 --purpose reproduction --config '{"argv": ["python", "-c", "import time,os; [time.sleep(1) for _ in range(150)]; os.makedirs(\"{out}\", exist_ok=True); open(\"{out}/done.txt\",\"w\").write(\"ok\")"]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")"
sleep 8
python3 - "$EIDOS_HOME" "$OUT/kills.json" <<'PY'
import json, os, signal, sys, time
home, out = sys.argv[1:3]
def status():
    return json.load(open(f"{home}/run/status.json"))
def pg_pid():
    try:
        return int(open(f"{home}/services/pg/data/postmaster.pid").readline())
    except (OSError, ValueError):
        return None
order = ["api", "workflow-worker", "investigator-worker", "training-runner", "assessor-worker", "sealed-runner", "temporal", "postgres"]
rows = []
for name in order:
    st = status()["services"]
    if name not in st:
        continue
    pid = pg_pid() if name == "postgres" else st[name].get("pid")
    if not pid:
        rows.append({"service": name, "skipped": "no pid"})
        continue
    os.kill(pid, signal.SIGKILL)
    t0, new, state = time.time(), None, None
    while time.time() - t0 < 120:
        time.sleep(1)
        s = status()["services"].get(name, {})
        cur = pg_pid() if name == "postgres" else s.get("pid")
        state = s.get("state")
        if cur and cur != pid and state == "running":
            new = cur
            break
    rows.append({"service": name, "killed_pid": pid, "new_pid": new, "state": state, "seconds": round(time.time() - t0, 1),
                 "restarts": status()["services"].get(name, {}).get("restarts")})
    print(rows[-1], flush=True)
    time.sleep(5)
json.dump(rows, open(out, "w"), indent=2)
PY
for _ in $(seq 1 120); do
  S="$("$LAB" job show "$P" "$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")"
  case "$S" in succeeded|failed|cancelled) break;; esac; sleep 3
done
"$LAB" job show "$P" "$J" > "$OUT/job.json"
sleep 10
"$LAB" doctor --json > "$OUT/doctor-after-kills.json" || true
echo "== abrupt stop (tests/ops/abrupt_stop.sh)"
if LAB="$LAB" bash "$REPO/tests/ops/abrupt_stop.sh" "$P" > "$OUT/abrupt-stop.log" 2>&1; then AB=0; else AB=$?; fi
tail -5 "$OUT/abrupt-stop.log"
python3 - "$OUT" "$AB" <<'PY'
import json, os, sys
out, ab = sys.argv[1], int(sys.argv[2])
kills = json.load(open(f"{out}/kills.json"))
job = json.load(open(f"{out}/job.json"))
doctor = json.load(open(f"{out}/doctor-after-kills.json"))
checks = []
def check(name, ok, **d):
    checks.append({"check": name, "ok": bool(ok), **d})
for k in kills:
    if "skipped" in k:
        continue
    check(f"{k['service']}: SIGKILLed by pid and restarted by the supervisor", bool(k.get("new_pid")), **k)
check("the job finished succeeded on its original attempt across all service kills",
      job["status"] == "succeeded" and [a["status"] for a in job["attempts"]] == ["succeeded"],
      status=job["status"], attempts=[a["status"] for a in job["attempts"]])
check("lab doctor has no failed checks after the kills", not [c for c in doctor if c["status"] == "fail"],
      fails=[c for c in doctor if c["status"] == "fail"])
log = open(f"{out}/abrupt-stop.log").read()
check("abrupt stop A (services killed, job survives, reattached single attempt)", "SCENARIO A OK" in log)
check("abrupt stop B (services and job killed: lost attempt reconciled, new fenced attempt)", "SCENARIO B OK" in log)
check("abrupt stop: doctor clean", "DOCTOR OK" in log and ab == 0)
rec = {"receipt": "eidos-service-failures/1", "kills": kills, "checks": checks, "ok": all(c["ok"] for c in checks)}
json.dump(rec, open(os.path.join(os.path.dirname(out), "service-failure-receipt.json"), "w"), indent=2)
for c in checks:
    print(("PASS " if c["ok"] else "FAIL ") + c["check"])
print("RESULT", "PASS" if rec["ok"] else "FAIL")
sys.exit(0 if rec["ok"] else 1)
PY
