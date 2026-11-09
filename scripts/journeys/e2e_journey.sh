#!/usr/bin/env bash
# Scripted end-to-end journey from a clean wheel install (CLI + MCP SDK client). See e2e_journey.py.
# Env: CI_WORK (scratch; default $RUNNER_TEMP/eidos-e2e or mktemp), RESEARCH_PY (existing interpreter with torch+numpy and
#      the research lock; if unset one is built: CPU torch 2.15.0 wheelhouse + research/public/development/requirements.lock),
#      LAB_API_PORT/LAB_PG_PORT/LAB_TEMPORAL_PORT, PG_BIN, TEMPORAL_SOURCE, PYTHON (default 3.12), KEEP_WORK=1.
# Output: $CI_WORK/out/journey-record.json (+ reports, answer, switch-journey record), $CI_WORK/lab/logs.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-e2e}"
PYTHON="${PYTHON:-3.12}"
export LAB_API_PORT="${LAB_API_PORT:-8765}" LAB_PG_PORT="${LAB_PG_PORT:-5544}" LAB_TEMPORAL_PORT="${LAB_TEMPORAL_PORT:-7633}"
if [ -z "${PG_BIN:-}" ]; then PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n1 || true)"; fi
export PG_BIN
START=$SECONDS
rm -rf "$CI_WORK"; mkdir -p "$CI_WORK/out"
export CI_WORK UV_CACHE_DIR="${UV_CACHE_DIR:-$CI_WORK/uv-cache}"

if [ -z "${RESEARCH_PY:-}" ]; then
  echo "=== research/project env: CPU torch 2.15.0 + research lock (t+$((SECONDS-START))s)"
  export EIDOS_WHEELHOUSE="$CI_WORK/wheelhouse"
  bash "$REPO/scripts/dev/fetch_cpu_torch.sh" 2.15.0 cp312 >/dev/null
  uv venv -q --python "$PYTHON" "$CI_WORK/research-venv"
  grep -v '^torch==' "$REPO/research/public/development/requirements.lock" > "$CI_WORK/lock-without-torch.txt"
  uv pip install -q --python "$CI_WORK/research-venv/bin/python" --find-links "$EIDOS_WHEELHOUSE" "torch==2.15.0+cpu"
  uv pip install -q --python "$CI_WORK/research-venv/bin/python" -r "$CI_WORK/lock-without-torch.txt"
  RESEARCH_PY="$CI_WORK/research-venv/bin/python"
fi
export RESEARCH_PY

echo "=== build wheels, install into a clean venv (t+$((SECONDS-START))s)"
bash "$REPO/scripts/ci/build_and_install.sh" > "$CI_WORK/build.log" 2>&1 || { tail -30 "$CI_WORK/build.log"; exit 1; }
VENV="$CI_WORK/venv"
export PATH="$VENV/bin:$PATH" LAB_BIN="$VENV/bin/lab" EIDOS_HOME="$CI_WORK/lab" OUT="$CI_WORK/out" REPO

cleanup() { rc=$?; "$LAB_BIN" stop >/dev/null 2>&1 || true
  [ -n "${KEEP_WORK:-}" ] || rm -rf "$CI_WORK/venv" "$CI_WORK/research-venv" "$CI_WORK/wheelhouse" "$CI_WORK/uv-cache" "$CI_WORK/dist"; exit $rc; }
trap cleanup EXIT

echo "=== journey (t+$((SECONDS-START))s)"
set +e
"$VENV/bin/python" "$HERE/e2e_journey.py"
rc=$?
set -e
echo "wall time: $((SECONDS-START))s"
exit $rc
