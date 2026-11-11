#!/bin/bash
# Companion to drive-p3.sh. Re-run blocks get stale "graded" markers when the driver re-logs their old incident
# records at the start of a round, so the driver would skip grading them. This sweep grades any complete, ok block
# whose local grades are missing or older than its block record. Then, if $P/.hosted-ok exists, it submits the
# block's arms to the GitHub grader (arms one after another, blocks in parallel) into $P/github-sweep/.
set -u
source "$HOME/eidos-internal/env.sh" >/dev/null 2>&1; source "$HOME/eidos-internal/bin/p3-env.sh"
P="$P3_OUTPUT"; LOG="$P/sweep-astra-resume.log"; RP="$EIDOS_RESEARCH_PYTHON"; cd "$S/p3" || exit 1
mkdir -p "$P/github-sweep" "$P/sweep-hosted"
[ -f "$P/drive-astra-resume.pid" ] || { echo 'Start the resume driver first' >&2; exit 2; }
resume_pid=$(cat "$P/drive-astra-resume.pid")
[[ "$resume_pid" =~ ^[0-9]+$ ]] && kill -0 "$resume_pid" 2>/dev/null || { echo 'Resume driver is not active' >&2; exit 2; }
if [ -f "$P/sweep-astra-resume.pid" ]; then
  prior_sweep=$(cat "$P/sweep-astra-resume.pid")
  if [[ "$prior_sweep" =~ ^[0-9]+$ ]] && kill -0 "$prior_sweep" 2>/dev/null; then
    echo 'Resume sweep is already active' >&2; exit 2
  fi
fi
echo $$ > "$P/sweep-astra-resume.pid"
while :; do
  for rec in "$P"/runs/*/block_record.json; do
    [ -f "$rec" ] || continue
    B=$(basename "$(dirname "$rec")")
    st=$("$RP" -c 'import json,sys;r=json.load(open(sys.argv[1]));print(r.get("complete"), r.get("status"))' "$rec" 2>/dev/null)
    [ "$st" = "True ok" ] || continue
    g="$P/grading/$B/arm4/grade.json"
    if [ ! -f "$g" ] || [ "$g" -ot "$rec" ]; then
      [ -d "$P/grading/$B" ] && mkdir -p "$P/grading-archive-stale" && mv "$P/grading/$B" "$P/grading-archive-stale/$B-$(date +%s)"
      "$RP" -m study.grade_block --block-record "$rec" --grading-root "$P/grading" --results "$P/results.json" >> "$LOG" 2>&1
      echo "$(date '+%H:%M:%S') sweep graded $B: $(command grep "GRADE $B" "$LOG" | tail -4 | sed 's/GRADE //' | tr '\n' ';')" | tee -a "$LOG" >> "$P/drive-astra-resume.log"
    fi
    if [ -f "$P/.hosted-ok" ] && [ ! -f "$P/sweep-hosted/$B" ]; then
      touch "$P/sweep-hosted/$B"
      ( for ARM in 1 2 3 4; do
          [ -f "$P/runs/$B/arm$ARM/out/answer.json" ] || continue
          "$HOME/eidos-internal/bin/regrade-gh.sh" "$P/runs" "$P/github-sweep" "$B" "$ARM" p3s >> "$LOG" 2>&1
        done; echo "$(date '+%H:%M:%S') hosted done $B" >> "$LOG" ) &
    fi
  done
  command grep -q "DRIVE-P3 DONE" "$P/drive-astra-resume.log" 2>/dev/null && { wait; echo "$(date '+%H:%M:%S') SWEEP DONE" >> "$LOG"; exit 0; }
  sleep 60
done
