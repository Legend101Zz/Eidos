#!/usr/bin/env bash
# Reference smoke of the public development release on the current platform (CPU, one thread).
# Env: OUT (result dir, default $RUNNER_TEMP/research-smoke), PYTHON (default 3.12, a uv python request).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${OUT:-${RUNNER_TEMP:-$(mktemp -d)}/research-smoke}"
PYTHON="${PYTHON:-3.12}"
DEV="$REPO/research/public/development"
mkdir -p "$OUT"
uv venv --seed --python "${PYTHON:-3.12}" "$OUT/venv"
PY="$OUT/venv/bin/python"
"$PY" -m pip install --quiet -r "$DEV/requirements.lock"
"$PY" --version | tee "$OUT/python_version.txt"
"$PY" -m pip freeze > "$OUT/pip_freeze.txt"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONHASHSEED=0
# GNU timeout is `gtimeout` on macOS (coreutils); fall back to a python-based limiter.
limit() { s="$1"; shift; "$PY" - "$s" "$@" <<'PY'
import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=int(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    print("TIMEOUT", file=sys.stderr); sys.exit(124)
PY
}
cd "$DEV"
limit 600 "$PY" check_bundles.py --out "$OUT/bundles"
limit 300 "$PY" package_task.py --task D02 --out "$OUT/D02"
limit 900 "$PY" "$OUT/D02/project.py" train --inputs "$OUT/D02" --out "$OUT/D02-baseline" --seed 11
find "$OUT" -name '*.json' -not -path '*/venv/*' | sort
