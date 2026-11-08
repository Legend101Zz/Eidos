#!/usr/bin/env bash
# A15: real machine sleep / restart check on macOS (owner-run). No sudo; everything on the given lab home
# (put it on the SSD). It never sleeps or restarts the machine itself.
#
#   source <ssd>/env.sh                      # caches, TMPDIR on the SSD
#   LAB=<venv>/bin/lab bash scripts/qualify/sleep_restart_check.sh start  [--home DIR] [--minutes N]
#   ... the owner sleeps the Mac (Apple menu > Sleep, or close the lid) for >= 5 minutes and wakes it,
#       OR restarts it (Apple menu > Restart) and logs in again ...
#   LAB=<venv>/bin/lab bash scripts/qualify/sleep_restart_check.sh --verify [--home DIR]
#
# start:    creates (once) a lab in DIR on its own ports (API 18865, PostgreSQL 15644, Temporal 17733-17741),
#           starts it, registers a plain environment, installs eidos.project.command, and admits one long job
#           (N minutes of 10-second ticks; default 12). Records the job, attempt, boot time and a lab status
#           snapshot in DIR/sleep-restart-state.json.
# --verify: detects what happened (same boot time = sleep/wake, changed = restart), starts the lab if it is
#           not running (after a restart), waits for the job to end, and checks:
#             sleep:   the job succeeded on its ORIGINAL attempt (reattached, never duplicated);
#             restart: the dead attempt was reconciled as lost with uncertain (unknown) compute usage and a
#                      new fenced attempt succeeded; exactly one successful attempt; one set of outputs;
#             both:    lab doctor has no failures; lab status and job show display the outcome, the lost
#                      attempt (missing progress) and the uncertain usage.
#           Writes DIR/sleep-restart-receipt.json; exit 0 only if every check passed.
set -euo pipefail
MODE="${1:-}"; shift || true
HOME_DIR="${SLEEP_LAB:-${V:-$PWD}/sleeplab}" MINUTES=12
while [ $# -gt 0 ]; do case "$1" in --home) HOME_DIR="$2"; shift 2;; --minutes) MINUTES="$2"; shift 2;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done
LAB="${LAB:-lab}"
export EIDOS_HOME="$HOME_DIR"
STATE="$HOME_DIR/sleep-restart-state.json"
P=sleepchk
boottime() { sysctl -n kern.boottime 2>/dev/null || cat /proc/sys/kernel/random/boot_id; }
running() { "$LAB" status --json >/dev/null 2>&1; }

case "$MODE" in
start)
  if [ ! -f "$HOME_DIR/config/lab.json" ]; then
    "$LAB" init --home "$HOME_DIR" --api-port 18865 --pg-port 15644 --temporal-port 17733 >/dev/null
  fi
  running || "$LAB" start >/dev/null
  LABPY="$(python3 - "$HOME_DIR" <<'PY'
import json, sys
print(json.load(open(sys.argv[1] + "/config/lab.json"))["lab_python"])
PY
)"
  mkdir -p "$HOME_DIR/sleepchk-project"
  "$LAB" project create "$P" --budget compute_seconds=20000 --budget trials=50 >/dev/null 2>&1 || true
  "$LAB" project register-env "$P" --python "$LABPY" --root "$HOME_DIR/sleepchk-project" >/dev/null
  CMD_DIR="$("$LABPY" -c 'import eidos,pathlib;print(pathlib.Path(eidos.__file__).parent/"_components"/"eidos.project.command")')"
  "$LAB" component install "$P" "$CMD_DIR" >/dev/null 2>&1 || true
  REF="$("$LAB" component list "$P" --text eidos.project.command | python3 -c "import json,sys; print(sorted(c['ref'] for c in json.load(sys.stdin)['components'])[-1])")"
  TICKS=$((MINUTES * 6))
  CFG="$(python3 - "$TICKS" <<'PY'
import json, sys
n = int(sys.argv[1])
code = ("import os,time,json; os.makedirs('{out}', exist_ok=True)\n"
        f"for i in range({n}):\n"
        "    time.sleep(10); open('{out}/progress.json','w').write(json.dumps({'tick': i + 1, 'wall': time.time()}))\n"
        "open('{out}/done.txt','w').write('ok')\n")
print(json.dumps({"argv": ["python", "-c", code]}))
PY
)"
  # Wall limit far above the job's own duration: a sleep stops the job's clock but not the wall clock.
  J="$("$LAB" run "$P" "$REF" --wall-seconds $((MINUTES * 60 * 6 + 3600)) --purpose reproduction --config "$CFG" \
       | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['job_id'])")"
  for _ in $(seq 1 90); do
    S="$("$LAB" job show "$P" "$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")"
    [ "$S" = running ] && break; sleep 2
  done
  "$LAB" job show "$P" "$J" > "$HOME_DIR/sleepchk-job-before.json"
  "$LAB" status --json > "$HOME_DIR/sleepchk-status-before.json"
  python3 - "$STATE" "$J" "$(boottime)" "$MINUTES" "$HOME_DIR/sleepchk-job-before.json" <<'PY'
import json, sys, time
state, job, boot, minutes, before = sys.argv[1:6]
b = json.load(open(before))
json.dump({"job_id": job, "boot": boot, "started_wall": time.time(), "minutes": int(minutes), "status_before": b["status"],
           "attempts_before": [(a["id"], a["status"]) for a in b["attempts"]]}, open(state, "w"), indent=2)
print(json.dumps({"job": job, "status": b["status"], "attempts": [a["status"] for a in b["attempts"]]}))
PY
  cat <<EOF

Job $J is running ($MINUTES minutes of work). Now, within the next $((MINUTES - 2)) minutes:
  EITHER sleep the Mac for at least 5 minutes, then wake it and log in,
  OR restart the Mac and log in.
Then run:  LAB=$LAB bash $0 --verify --home "$HOME_DIR"
EOF
  ;;
--verify)
  [ -f "$STATE" ] || { echo "no state file $STATE; run 'start' first" >&2; exit 2; }
  J="$(python3 -c "import json; print(json.load(open('$STATE'))['job_id'])")"
  BOOT0="$(python3 -c "import json; print(json.load(open('$STATE'))['boot'])")"
  if [ "$(boottime)" = "$BOOT0" ]; then EVENT=sleep; else EVENT=restart; fi
  WAS_RUNNING=1; running || WAS_RUNNING=0
  [ "$WAS_RUNNING" = 1 ] || "$LAB" start >/dev/null
  echo "event: $EVENT (lab was running after it: $WAS_RUNNING); waiting for job $J"
  MAXWAIT=$(( $(python3 -c "import json; print(json.load(open('$STATE'))['minutes'])") * 60 + 1800 ))
  T0=$SECONDS
  while [ $((SECONDS - T0)) -lt "$MAXWAIT" ]; do
    S="$("$LAB" job show "$P" "$J" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")"
    case "$S" in succeeded|failed|cancelled) break;; esac; sleep 15
  done
  "$LAB" job show "$P" "$J" > "$HOME_DIR/sleepchk-job-after.json"
  "$LAB" status --json > "$HOME_DIR/sleepchk-status-after.json"
  "$LAB" doctor --json > "$HOME_DIR/sleepchk-doctor-after.json" || true
  "$LAB" query list_events -p "$P" --params '{"cursor": 0, "limit": 5000}' > "$HOME_DIR/sleepchk-events.json" || echo '{}' > "$HOME_DIR/sleepchk-events.json"
  python3 - "$STATE" "$EVENT" "$WAS_RUNNING" "$HOME_DIR" "$(boottime)" <<'PY'
import json, sys, time
state, event, was_running, home, boot_now = sys.argv[1:6]
st = json.load(open(state))
job = json.load(open(f"{home}/sleepchk-job-after.json"))
status = json.load(open(f"{home}/sleepchk-status-after.json"))
doctor = json.load(open(f"{home}/sleepchk-doctor-after.json")) if open(f"{home}/sleepchk-doctor-after.json").read().strip() else []
events = (json.load(open(f"{home}/sleepchk-events.json")) or {}).get("events", [])
atts = job["attempts"]
outs = [o["slot"] for o in job.get("outputs") or []]
checks = []
def check(name, ok, **detail):
    checks.append({"check": name, "ok": bool(ok), **detail})
check("job ended succeeded", job["status"] == "succeeded", status=job["status"])
check("exactly one successful attempt", sum(1 for a in atts if a["status"] == "succeeded") == 1,
      attempts=[a["status"] for a in atts])
check("no duplicate outputs", len(outs) == len(set(outs)), outputs=outs)
if event == "sleep":
    check("sleep: the original attempt finished (reattached, no new attempt)",
          len(atts) == 1 and atts[0]["id"] == st["attempts_before"][-1][0], attempts=[(a["id"], a["status"]) for a in atts])
else:
    lost = [a for a in atts if a["status"] == "lost"]
    check("restart: the dead attempt was reconciled as lost", len(lost) == 1 and lost[0]["id"] == st["attempts_before"][-1][0],
          attempts=[(a["id"], a["status"], a.get("epoch")) for a in atts])
    check("restart: the lost attempt's usage is recorded as uncertain (unknown spend shown)",
          bool(lost) and bool((lost[0].get("usage") or {}).get("uncertain")), usage=(lost[0].get("usage") if lost else None))
    check("restart: the new attempt is fenced with a higher epoch",
          len(atts) == 2 and int(atts[1].get("epoch") or 0) > int(atts[0].get("epoch") or 0))
fails = [c for c in doctor if c.get("status") == "fail"] if isinstance(doctor, list) else []
check("lab doctor: no failed checks", not fails, fails=fails)
check("lab status displays the lab and job counts", bool(status) and "projects" in json.dumps(status), keys=sorted(status)[:10])
check("job show displays every attempt with its status and error", all("status" in a for a in atts) and
      all(a.get("error") for a in atts if a["status"] == "lost"))
check("events: the job's history is in the ledger", any(e.get("subject") == job["id"] for e in events), n_events=len(events))
rec = {"receipt": "eidos-sleep-restart/1", "event": event, "lab_was_running_after_event": was_running == "1",
       "boot_before": st["boot"], "boot_after": boot_now, "minutes_since_start": round((time.time() - st["started_wall"]) / 60, 1),
       "job": job["id"], "attempts": [(a["id"], a["status"], a.get("epoch"), a.get("usage")) for a in atts],
       "checks": checks, "ok": all(c["ok"] for c in checks)}
open(f"{home}/sleep-restart-receipt.json", "w").write(json.dumps(rec, indent=2, default=str))
for c in checks:
    print(("PASS " if c["ok"] else "FAIL ") + c["check"])
print("RESULT", "PASS" if rec["ok"] else "FAIL", f"({event}); receipt: {home}/sleep-restart-receipt.json")
sys.exit(0 if rec["ok"] else 1)
PY
  ;;
*) echo "usage: $0 start|--verify [--home DIR] [--minutes N]" >&2; exit 2;;
esac
