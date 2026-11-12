#!/bin/bash
# Companion to drive-p3.sh. Re-run blocks get stale "graded" markers when the driver re-logs their old incident
# records at the start of a round, so the driver would skip grading them. This sweep grades any complete, ok block
# whose local grades are missing or older than its block record. Then, if $P/.hosted-ok exists, it submits the
# block's arms to the GitHub grader (arms one after another, blocks in parallel) into $P/github-sweep/.
set -u
source "$HOME/eidos-internal/env.sh" >/dev/null 2>&1; source "$HOME/eidos-internal/bin/p3-env.sh"
P="$P3_OUTPUT"; LOG="$P/sweep-astra-resume.log"; RP="$EIDOS_RESEARCH_PYTHON"; cd "$S/p3" || exit 1
[ -f "$P/drive-astra-resume.pid" ] || { echo 'Start the resume driver first' >&2; exit 2; }
resume_pid=$(cat "$P/drive-astra-resume.pid")
[[ "$resume_pid" =~ ^[0-9]+$ ]] && kill -0 "$resume_pid" 2>/dev/null || { echo 'Resume driver is not active' >&2; exit 2; }
if [ -f "$P/sweep-astra-resume.pid" ]; then
  prior_sweep=$(cat "$P/sweep-astra-resume.pid")
  if [[ "$prior_sweep" =~ ^[0-9]+$ ]] && kill -0 "$prior_sweep" 2>/dev/null; then
    echo 'Resume sweep is already active' >&2; exit 2
  fi
fi
if [ -f "$P/sweep.pid" ]; then
  prior_sweep=$(cat "$P/sweep.pid")
  if [[ "$prior_sweep" =~ ^[0-9]+$ ]] && kill -0 "$prior_sweep" 2>/dev/null; then
    echo 'Original sweep is still active' >&2; exit 2
  fi
fi
sweep_lock="$P/.resume-sweep.lock"
mkdir "$sweep_lock" || { echo 'Resume sweep lock is held' >&2; exit 2; }
trap 'rmdir "$sweep_lock"' EXIT
mkdir -p "$P/github-sweep" "$P/sweep-hosted" || exit 2
echo $$ > "$P/sweep-astra-resume.pid" || exit 2
while :; do
  # Observe completion before scanning so a block that closes mid-scan gets a final pass.
  driver_done=0
  command grep -q "DRIVE-P3 DONE" "$P/drive-astra-resume.log" 2>/dev/null && driver_done=1
  for rec in "$P"/runs/*/block_record.json; do
    [ -f "$rec" ] || continue
    B=$(basename "$(dirname "$rec")")
    st=$("$RP" -c 'import json,sys;r=json.load(open(sys.argv[1]));print(r.get("complete"), r.get("status"))' "$rec" 2>/dev/null)
    [ "$st" = "True ok" ] || continue
    g="$P/grading/$B/arm4/grade.json"
    if [ ! -f "$g" ] || [ "$g" -ot "$rec" ]; then
      [ -d "$P/grading/$B" ] && mkdir -p "$P/grading-archive-stale" && mv "$P/grading/$B" "$P/grading-archive-stale/$B-$(date +%s)"
      if ! "$RP" -m study.grade_block --block-record "$rec" --grading-root "$P/grading" --results "$P/results.json" >> "$LOG" 2>&1; then
        echo "$(date '+%H:%M:%S') local grader failed $B" >> "$LOG"
        continue
      fi
      # Local grading can turn a block into an infrastructure incident.
      st=$("$RP" -c 'import json,sys;r=json.load(open(sys.argv[1]));print(r.get("complete"), r.get("status"))' "$rec" 2>/dev/null)
      [ "$st" = "True ok" ] || continue
      echo "$(date '+%H:%M:%S') sweep graded $B: $(command grep "GRADE $B" "$LOG" | tail -4 | sed 's/GRADE //' | tr '\n' ';')" | tee -a "$LOG" >> "$P/drive-astra-resume.log"
    fi
    if [ -f "$P/.hosted-ok" ] && [ ! -f "$P/sweep-hosted/$B" ]; then
      printf 'dispatch attempts reserved\n' > "$P/sweep-hosted/$B" || continue
      ( for ARM in 1 2 3 4; do
          [ -f "$P/runs/$B/arm$ARM/out/answer.json" ] || continue
          helper_exit=0
          "$HOME/eidos-internal/bin/regrade-gh.sh" "$P/runs" "$P/github-sweep" "$B" "$ARM" p3s >> "$LOG" 2>&1 || helper_exit=$?
          state=$("$RP" -m study.controllers.resume_incidents hosted-attempt-state \
            --attempt "$P/github-sweep/$B/arm$ARM") || state=receipt_inspection_failed
          echo "$(date '+%H:%M:%S') hosted attempt $B arm$ARM helper_exit=$helper_exit state=$state" >> "$LOG"
        done; echo "$(date '+%H:%M:%S') hosted attempt batch returned $B; reconciliation pending" >> "$LOG" ) &
    fi
  done
  [ "$driver_done" -eq 0 ] || { wait; echo "$(date '+%H:%M:%S') SWEEP DONE" >> "$LOG"; exit 0; }
  if ! kill -0 "$resume_pid" 2>/dev/null; then
    # A clean driver can exit while this pass is scanning. Take its final pass.
    command grep -q "DRIVE-P3 DONE" "$P/drive-astra-resume.log" 2>/dev/null && continue
    wait
    echo "$(date '+%H:%M:%S') resume driver stopped before clean completion" >> "$LOG"
    exit 1
  fi
  sleep 60
done
