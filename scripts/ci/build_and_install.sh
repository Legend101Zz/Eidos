#!/usr/bin/env bash
# Build every workspace package and install eidos-lab into a fresh venv that cannot see the repo.
# Env: CI_WORK (scratch dir, default $RUNNER_TEMP/eidos-ci or a mktemp dir), PYTHON (default 3.12).
# Output: $CI_WORK/dist (wheels/sdists), $CI_WORK/venv (the venv). Venv path: $CI_WORK/venv.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-ci}"
PYTHON="${PYTHON:-3.12}"
mkdir -p "$CI_WORK"
rm -rf "$CI_WORK/dist" "$CI_WORK/venv"
( cd "$REPO" && uv build --all-packages -o "$CI_WORK/dist" )
ls -l "$CI_WORK/dist"
# Run from the scratch dir so nothing in the repo (cwd, src/, editable installs) is importable.
cd "$CI_WORK"
uv venv --python "$PYTHON" venv
# --no-cache: never install a cached wheel of the same version instead of the one just built
env -u PYTHONPATH uv pip install --no-cache --python venv/bin/python --find-links dist eidos-lab
# Prove the install is from the wheel, not the checkout.
venv/bin/python - <<PY
import eidos, pathlib, sys
p = pathlib.Path(eidos.__file__).resolve()
repo = pathlib.Path("$REPO").resolve()
assert repo not in p.parents, f"eidos imported from the repo checkout: {p}"
print("eidos imported from", p)
PY
venv/bin/lab --help >/dev/null
