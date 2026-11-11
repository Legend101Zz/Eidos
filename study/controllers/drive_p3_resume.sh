#!/bin/bash
# Resume completed Pilot 3 incident blocks under Addendum 6.
set -u
source "$HOME/eidos-internal/env.sh"
source "$HOME/eidos-internal/bin/p3-env.sh"
I="$HOME/eidos-internal"
P="$P3_OUTPUT"
LOG="$P/drive-astra-resume.log"
RP="$EIDOS_RESEARCH_PYTHON"
cd "$S/p3" || exit 1
dispatcher() { "$RP" -m study.controllers.resume_incidents "$@"; }
STOP_AT_EPOCH=$(dispatcher cutoff) || exit 2

lock="$P/.resume-driver.lock"
mkdir "$lock" || { echo 'Resume driver lock is held' >&2; exit 2; }
lab_started=0
cleanup() {
  if [ "$lab_started" -eq 1 ]; then
    "$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab stop >> "$LOG" 2>&1
  fi
  rmdir "$lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for predecessor in "$P/drive.pid" "$P/sweep.pid" "$P/drive-astra-resume.pid" "$P/sweep-astra-resume.pid"; do
  if [ -f "$predecessor" ]; then
    prior_pid=$(cat "$predecessor")
    if [[ "$prior_pid" =~ ^[0-9]+$ ]] && kill -0 "$prior_pid" 2>/dev/null; then
      echo "Refusing overlap: controller in $predecessor is still active" >&2
      exit 2
    fi
  fi
done
command grep -q 'DRIVE-P3 DONE' "$P/drive.log" || { echo 'Original driver has not completed cleanly' >&2; exit 2; }
command grep -q 'SWEEP DONE' "$P/sweep.log" || { echo 'Original sweep has not completed cleanly' >&2; exit 2; }
for prior_port in 28767 25546 27635 28766; do
  if /usr/sbin/lsof -nP -iTCP:"$prior_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Refusing to take over a surviving pilot service on port $prior_port" >&2
    exit 2
  fi
done
[ "$(date +%s)" -lt "$STOP_AT_EPOCH" ] || { echo 'Resume window has ended' >&2; exit 2; }
INC=$(dispatcher list --pilot "$P") || exit 2
[ -n "$INC" ] || { echo 'No complete incident blocks are eligible for continuation'; exit 2; }
python3 - <<'PYDISK' || exit 2
import shutil
if shutil.disk_usage('/').free < 12.5 * 1024**3:
    raise SystemExit('STOP: internal disk has less than 12.5 GiB free')
PYDISK

history="$P/controller-history/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$history" || exit 2
for prior_log in "$LOG" "$P/sweep-astra-resume.log"; do
  [ ! -f "$prior_log" ] || mv "$prior_log" "$history/" || exit 2
done
echo $$ > "$P/drive-astra-resume.pid" || exit 2
export EIDOS_PILOT_CHARTER_COMMIT=ecf85dba189d477605e2c1b28e20ef55d516f2b9
log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG" >> "$I/heartbeat-p3.log"; }
quota_ok() {
  python3 - "$STOP_AT_EPOCH" <<'PY'
import json, os, subprocess, sys, time, urllib.request
cutoff = int(sys.argv[1])
if time.time() >= cutoff:
    raise SystemExit(1)
key = subprocess.run(["security", "find-generic-password", "-a", os.environ["USER"], "-s", "minimax_api_key", "-w"], capture_output=True, text=True).stdout.strip()
request = urllib.request.Request("https://api.minimax.io/v1/chat/completions",
    data=json.dumps({"model": "MiniMax-M3.1-Flash", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 20}).encode(),
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
remaining = cutoff - time.time()
if remaining <= 0 or not key:
    raise SystemExit(1)
try:
    with urllib.request.urlopen(request, timeout=min(60, remaining)) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except OSError:
    raise SystemExit(1)
PY
}

log "resume start; tooling $(git rev-parse HEAD); charter $EIDOS_PILOT_CHARTER_COMMIT; complete incidents $(echo "$INC" | tr '\n' ' ')"
# The ports were idle on entry. Mark ownership before start so partial startup gets cleaned up.
lab_started=1
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab start >> "$LOG" 2>&1 || { log "LAB START FAILED"; exit 1; }
round=0
failed=0
while [ "$round" -lt 4 ]; do
  if ! INC=$(dispatcher list --pilot "$P"); then
    log "eligible incident inventory failed"
    failed=1
    break
  fi
  [ -n "$INC" ] || { log "no eligible complete incidents remain"; break; }
  [ "$(date +%s)" -lt "$STOP_AT_EPOCH" ] || { log "absolute deadline reached; retained incidents: $INC"; break; }
  log "waiting for quota before replacement pass $((round + 1))"
  while ! quota_ok; do
    remaining=$((STOP_AT_EPOCH - $(date +%s)))
    [ "$remaining" -gt 0 ] || break 2
    [ "$remaining" -lt 600 ] || remaining=600
    sleep "$remaining"
  done
  round=$((round + 1))
  if ! dispatcher dispatch --pilot "$P" --history "$history/pass-$round" >> "$LOG" 2>&1; then
    log "incident dispatcher failed; admitted blocks settled"
    failed=1
    break
  fi
done
if ! "$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab stop >> "$LOG" 2>&1; then
  log "LAB STOP FAILED"
  exit 1
fi
lab_started=0
log "DRIVE-P3 DONE; dispatcher_failed=$failed; analysis deferred until companion sweep completes"
exit "$failed"
