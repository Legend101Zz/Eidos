#!/usr/bin/env bash
# The full switch-order acceptance journey from a clean wheel install: build wheels, fresh venv, fresh lab, run every step.
# Env: CI_WORK (default $RUNNER_TEMP/eidos-switch or mktemp), EIDOS_TORCH_PYTHON (existing interpreter with torch+numpy; else a CPU
#      torch 2.15.0 venv is built from the wheelhouse), LAB_API_PORT/LAB_PG_PORT/LAB_TEMPORAL_PORT, PG_BIN, TEMPORAL_SOURCE, PYTHON.
# Output: $CI_WORK/out/journey-run.json and journey.md; lab logs in $CI_WORK/lab/logs.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-switch}"
PYTHON="${PYTHON:-3.12}"
LAB_API_PORT="${LAB_API_PORT:-8765}"; LAB_PG_PORT="${LAB_PG_PORT:-5544}"; LAB_TEMPORAL_PORT="${LAB_TEMPORAL_PORT:-7633}"
[ -n "${PG_BIN:-}" ] || PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n1 || true)"
START=$SECONDS
rm -rf "$CI_WORK"; mkdir -p "$CI_WORK/out"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$CI_WORK/uv-cache}"
if [ -z "${EIDOS_TORCH_PYTHON:-}" ]; then
  export EIDOS_WHEELHOUSE="$CI_WORK/wheelhouse"
  bash "$REPO/scripts/dev/fetch_cpu_torch.sh" 2.15.0 cp312 >/dev/null
  uv venv -q --python "$PYTHON" "$CI_WORK/project-venv"
  uv pip install -q --python "$CI_WORK/project-venv/bin/python" --find-links "$EIDOS_WHEELHOUSE" "torch==2.15.0+cpu" numpy
  EIDOS_TORCH_PYTHON="$CI_WORK/project-venv/bin/python"
fi
export EIDOS_TORCH_PYTHON EIDOS_NUMPY_PYTHON="$EIDOS_TORCH_PYTHON"
bash "$REPO/scripts/ci/build_and_install.sh" > "$CI_WORK/build.log" 2>&1 || { tail -30 "$CI_WORK/build.log"; exit 1; }
VENV="$CI_WORK/venv"
export EIDOS_HOME="$CI_WORK/lab" PATH="$VENV/bin:$PATH"
trap 'lab stop >/dev/null 2>&1 || true' EXIT
init=(init --home "$EIDOS_HOME" --api-port "$LAB_API_PORT" --pg-port "$LAB_PG_PORT" --temporal-port "$LAB_TEMPORAL_PORT")
[ -n "$PG_BIN" ] && init+=(--pg-bin "$PG_BIN")
[ -n "${TEMPORAL_SOURCE:-}" ] && init+=(--temporal-source "$TEMPORAL_SOURCE")
lab "${init[@]}" >/dev/null
lab start >/dev/null
echo "=== journey (t+$((SECONDS-START))s)"
rc=0
"$VENV/bin/python" "$REPO/tests/journeys/switch_journey.py" --project sw1 --out "$CI_WORK/out/journey-run.json" \
  --workdir "$CI_WORK/work" --doc "$CI_WORK/out/journey.md" || rc=$?
echo "wall time: $((SECONDS-START))s"
exit $rc
