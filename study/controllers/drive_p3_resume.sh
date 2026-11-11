#!/bin/bash
# drive-p3.sh: development pilot 3 (charter ecf85db). 12 blocks (N01–N04 × 3 runs), two blocks at a time. Each block is
# graded by the companion sweep; hosted evidence is independently reconciled. Incident blocks are re-run whole, with concurrency 2,
# after the provider quota is confirmed available. Admission/wait window ends 12 Nov 03:00 IST.
set -u
source "$HOME/eidos-internal/env.sh"; source "$HOME/eidos-internal/bin/p3-env.sh"
I="$HOME/eidos-internal"; P="$P3_OUTPUT"; LOG="$P/drive-astra-resume.log"
# Addendum 6: absolute UTC deadline avoids HHMM comparison across midnight.
STOP_AT_EPOCH=$(python3 - <<'PYDEADLINE'
from datetime import datetime
print(int(datetime.fromisoformat("2026-11-12T03:00:00+05:30").timestamp()))
PYDEADLINE
)
for predecessor in "$P/drive.pid" "$P/sweep.pid" "$P/drive-astra-resume.pid" "$P/sweep-astra-resume.pid"; do
  if [ -f "$predecessor" ]; then
    prior_pid=$(cat "$predecessor")
    if [[ "$prior_pid" =~ ^[0-9]+$ ]] && kill -0 "$prior_pid" 2>/dev/null; then
      echo "Refusing overlap: controller in $predecessor is still active" >&2; exit 2
    fi
  fi
done
command grep -q 'DRIVE-P3 DONE' "$P/drive.log" || { echo 'Original driver has not completed cleanly' >&2; exit 2; }
for prior_port in 28767 25546 27635 28766; do
  if /usr/sbin/lsof -nP -iTCP:"$prior_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Refusing to take over a surviving pilot service on port $prior_port" >&2; exit 2
  fi
done
[ "$(date +%s)" -lt "$STOP_AT_EPOCH" ] || { echo 'Resume window has ended' >&2; exit 2; }
python3 - <<'PYDISK'
import shutil
if shutil.disk_usage('/').free < 12.5 * 1024**3:
    raise SystemExit('STOP: internal disk has less than 12.5 GiB free')
PYDISK
[ $? -eq 0 ] || exit 2
if [ -f "$LOG" ] || [ -f "$P/sweep-astra-resume.log" ]; then
  history="$P/controller-history/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$history"
  for prior_log in "$LOG" "$P/sweep-astra-resume.log"; do
    [ ! -f "$prior_log" ] || mv "$prior_log" "$history/"
  done
fi
echo $$ > "$P/drive-astra-resume.pid"
cd "$S/p3" || exit 1
export EIDOS_PILOT_CHARTER_COMMIT=ecf85dba189d477605e2c1b28e20ef55d516f2b9
RP="$EIDOS_RESEARCH_PYTHON"; mkdir -p "$P/graded" "$P/github"
log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$LOG" >> "$I/heartbeat-p3.log"; }
quota_ok() {
  python3 - <<'PY'
import json, os, subprocess, urllib.request, sys
key = subprocess.run(["security", "find-generic-password", "-a", os.environ["USER"], "-s", "minimax_api_key", "-w"], capture_output=True, text=True).stdout.strip()
req = urllib.request.Request("https://api.minimax.io/v1/chat/completions",
    data=json.dumps({"model": "MiniMax-M3.1-Flash", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 20}).encode(),
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r: sys.exit(0 if r.status == 200 else 1)
except Exception: sys.exit(1)
PY
}
# Only the companion sweep grades. Dispatch markers never imply a verified grade.
grade_ready() { :; }
incident_blocks() {
  "$RP" - "$P" <<'PY'
import json, sys, glob, os
for rec in glob.glob(sys.argv[1] + "/runs/*/block_record.json"):
    r = json.load(open(rec))
    if r.get("status") == "incident": print(os.path.basename(os.path.dirname(rec)))
PY
}

log "drive-p3 start; charter $EIDOS_PILOT_CHARTER_COMMIT; tooling $(git rev-parse --short HEAD); eidos build $("$EIDOS_STUDY_PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["eidos_commit_sha"][:10])' "$(dirname "$(dirname "$EIDOS_STUDY_PYTHON")")/pilot-build.json")"
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab start >> "$LOG" 2>&1 || { log "LAB START FAILED"; exit 1; }
[ -f "$P/design.json" ] || "$RP" -m study.launcher plan --cards N01,N02,N03,N04 --runs 3 --seed 20261111 --out "$P/design.json" >> "$LOG" 2>&1
log "design: $("$RP" -c 'import json,sys;r=json.load(open(sys.argv[1]));print(sorted({(x["block_order"],x["block_id"]) for x in r}))' "$P/design.json")"
round=0
while :; do
  round=$((round+1))
  [ "$(date +%s)" -lt "$STOP_AT_EPOCH" ] || { log "absolute resume deadline reached; retaining open incidents"; break; }
  "$RP" -m study.launcher run --design "$P/design.json" --runs-root "$P/runs" --concurrency 2 --replace-incident >> "$LOG" 2>&1 &
  LP=$!; echo $LP > "$P/launcher.pid"
  while kill -0 $LP 2>/dev/null; do grade_ready; sleep 60; done
  grade_ready
  INC=$(incident_blocks)
  [ -z "$INC" ] && { log "all blocks complete without open incidents"; break; }
  [ "$round" -ge 4 ] && { log "incidents remain after $round rounds: $INC"; break; }
  log "incident blocks after round $round: $(echo $INC | tr '\n' ' '); waiting for the provider quota"
  while :; do
    now_epoch=$(date +%s); [ "$now_epoch" -ge "$STOP_AT_EPOCH" ] && { log "absolute resume deadline reached; incidents stay explicit: $INC"; break 2; }
    quota_ok && break
    remaining=$((STOP_AT_EPOCH - $(date +%s)))
    [ "$remaining" -gt 0 ] || continue
    [ "$remaining" -lt 600 ] || remaining=600
    sleep "$remaining"
  done
  for B in $INC; do touch "$P/graded/$B"; done
  log "quota available; re-running incident blocks (round $((round+1)))"
done
"$RP" -m study.analyze --design "$P/design.json" --results "$P/results.json" --out "$P/analysis" >> "$LOG" 2>&1
log "local analysis written; waiting for GitHub grading"
wait
"$EIDOS_STUDY_PYTHON" -m study.eidos_arms.lab stop >> "$LOG" 2>&1
log "DRIVE-P3 DONE"
