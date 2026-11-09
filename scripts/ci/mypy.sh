#!/usr/bin/env bash
# Type checking (CI lint-and-unit job and local development).
#   1. the server tree and the worker packages at the server's Python (pyproject [tool.mypy], locked mypy);
#   2. the worker packages (eidos-wire, eidos-worker-sdk, eidos-pytorch) with a Python 3.9 target, their
#      supported floor, so 3.10+ syntax or stdlib APIs cannot slip in. The locked mypy (2.x) no longer
#      targets 3.9, so this step runs the last mypy release that does, pinned, through uvx.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
MYPY_PY39_PIN="mypy==1.19.1"

echo "== mypy: server + worker packages (python 3.12) =="
uv run mypy

echo "== mypy: worker packages (python 3.9 target, $MYPY_PY39_PIN) =="
uvx --python 3.12 "$MYPY_PY39_PIN" --config-file pyproject.toml --python-version 3.9 \
  packages/eidos-wire/src packages/eidos-worker-sdk/src packages/eidos-pytorch/src
