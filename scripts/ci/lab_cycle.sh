#!/usr/bin/env bash
# One lab lifecycle phase against an installed `lab` (from a fresh venv).
# Usage: lab_cycle.sh {init|start|doctor|project|status|stop|all}
# Env: CI_WORK (default $RUNNER_TEMP/eidos-ci), LAB_BIN (default $CI_WORK/venv/bin/lab), LAB_HOME (default $CI_WORK/lab), LAB_API_PORT/LAB_PG_PORT/LAB_TEMPORAL_PORT,
#      TEMPORAL_SOURCE (optional dir with temporal-server binaries; else the pinned release is downloaded
#      and sha256-verified), PG_BIN (optional PostgreSQL bin dir), PROJECT (default ci1), LAB_OUT (result dir).
set -euo pipefail
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-/tmp}/eidos-ci}"
LAB_HOME="${LAB_HOME:-$CI_WORK/lab}"
LAB_BIN="${LAB_BIN:-$CI_WORK/venv/bin/lab}"
LAB_API_PORT="${LAB_API_PORT:-8765}"; LAB_PG_PORT="${LAB_PG_PORT:-5544}"; LAB_TEMPORAL_PORT="${LAB_TEMPORAL_PORT:-7633}"
PROJECT="${PROJECT:-ci1}"
LAB_OUT="${LAB_OUT:-$CI_WORK/ci-results}"
mkdir -p "$LAB_OUT"
export EIDOS_HOME="$LAB_HOME"

phase_init() {
  args=(init --home "$LAB_HOME" --api-port "$LAB_API_PORT" --pg-port "$LAB_PG_PORT" --temporal-port "$LAB_TEMPORAL_PORT")
  [ -n "${PG_BIN:-}" ] && args+=(--pg-bin "$PG_BIN")
  [ -n "${TEMPORAL_SOURCE:-}" ] && args+=(--temporal-source "$TEMPORAL_SOURCE")
  "$LAB_BIN" "${args[@]}"
}
phase_start() { "$LAB_BIN" start; }
phase_doctor() {
  rc=0
  "$LAB_BIN" doctor --json > "$LAB_OUT/doctor.json" || rc=$?
  cat "$LAB_OUT/doctor.json"
  python3 - "$LAB_OUT/doctor.json" <<'PY'
import json, sys
checks = json.load(open(sys.argv[1]))
bad = [c for c in checks if c["status"] == "fail"]
for c in checks:
    print(f"[{c['status']:4s}] {c['check']}: {c['detail']}")
sys.exit(1 if bad else 0)
PY
  [ "$rc" = 0 ] || { echo "lab doctor exited $rc"; return 1; }
}
phase_project() {
  "$LAB_BIN" project create "$PROJECT" | tee "$LAB_OUT/project_create.json"
}
phase_status() {
  "$LAB_BIN" status --json > "$LAB_OUT/status.json"; cat "$LAB_OUT/status.json"
}
phase_stop() { "$LAB_BIN" stop; }

case "${1:-all}" in
  init) phase_init ;; start) phase_start ;; doctor) phase_doctor ;; project) phase_project ;;
  status) phase_status ;; stop) phase_stop ;;
  all)
    trap 'echo "--- cleanup: lab stop"; phase_stop || true' EXIT
    phase_init; phase_start; phase_doctor; phase_project; phase_status
    trap - EXIT; phase_stop ;;
  *) echo "usage: $0 {init|start|doctor|project|status|stop|all}" >&2; exit 2 ;;
esac
