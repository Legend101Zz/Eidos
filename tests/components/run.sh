#!/usr/bin/env bash
# Built-in component tests. Needs: the lab interpreter (pytest + eidos) and a project interpreter with
# torch (Python 3.12, torch 2.15 CPU for the numeric replication). Author tests, not qualification.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
LAB_PY="${EIDOS_LAB_PYTHON:-/workspace/Eidos/.venv/bin/python}"
export EIDOS_TORCH_PYTHON="${EIDOS_TORCH_PYTHON:-/workspace/.eidos-dev/h1/envs/py312/bin/python}"
SDK="$ROOT/packages/eidos-wire/src:$ROOT/packages/eidos-worker-sdk/src:$ROOT/packages/eidos-pytorch/src"
export PYTHONPATH="$ROOT/src:$SDK"

# 1. pytest suite (manifests/fixtures, every component, the end-to-end switch replication)
"$LAB_PY" -m pytest tests/components -q -p no:cacheprovider "$@"

# 2. the same fixtures through the SDK's own validator, each in its declared environment
for c in lab.pairs.by_group lab.switch.balanced-data eidos.templates.improves-under-budget \
         eidos.assessors.paired-ci eidos.qualification-runner; do
  echo "== validate $c (lab environment)"; "$LAB_PY" -m eidos_worker.validate "components/$c"
done
for c in eidos.pytorch.train eidos.pytorch.evaluate eidos.pytorch.weight-edit eidos.pytorch.behavior-check; do
  echo "== validate $c (project environment)"
  PYTHONPATH="$SDK" "$EIDOS_TORCH_PYTHON" -m eidos_worker.validate "components/$c" 2>&1 | grep -v -E "NumPy|_conversion_method_template"
done
