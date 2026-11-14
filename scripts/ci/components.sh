#!/usr/bin/env bash
# Built-in component tests in CI: lab venv (pytest) + one project venv with CPU torch 2.15 and numpy
# used as both EIDOS_TORCH_PYTHON and EIDOS_NUMPY_PYTHON (scikit-learn too, for the recipe-sweep template's svm and gbm
# families; the tests also cover an environment without it). Env: CI_WORK (scratch dir),
# EIDOS_WHEELHOUSE (CPU torch wheelhouse; fetched into it when the wheel is missing).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-/tmp}/eidos-components}"
export EIDOS_WHEELHOUSE="${EIDOS_WHEELHOUSE:-$CI_WORK/wheelhouse}"
mkdir -p "$CI_WORK" "$EIDOS_WHEELHOUSE"

uv sync
bash scripts/dev/fetch_cpu_torch.sh 2.15.0 cp312
uv venv --python 3.12 --clear "$CI_WORK/project-venv"
uv pip install --python "$CI_WORK/project-venv/bin/python" --find-links "$EIDOS_WHEELHOUSE" "torch==2.15.0+cpu" numpy==2.5.4 scikit-learn==1.9.1 joblib==1.6.0

export EIDOS_TORCH_PYTHON="$CI_WORK/project-venv/bin/python"
export EIDOS_NUMPY_PYTHON="$EIDOS_TORCH_PYTHON"
export EIDOS_LAB_PYTHON="$ROOT/.venv/bin/python"

# the torch-free lab venv must stay torch-free
if "$EIDOS_LAB_PYTHON" -c 'import torch' 2>/dev/null; then echo "lab venv unexpectedly has torch" >&2; exit 1; fi

# fixture checkpoints (git-ignored), generated in the same env the tests use
PYTHONPATH="$ROOT/packages/eidos-wire/src:$ROOT/packages/eidos-worker-sdk/src:$ROOT/packages/eidos-pytorch/src" \
  "$EIDOS_TORCH_PYTHON" components/build_fixtures.py

# no skips allowed: numpy/torch-dependent tests must actually run
bash tests/components/run.sh -rs 2>&1 | tee "$CI_WORK/components.log"
if grep -qE '[0-9]+ skipped' "$CI_WORK/components.log"; then echo "component tests were skipped" >&2; exit 1; fi
