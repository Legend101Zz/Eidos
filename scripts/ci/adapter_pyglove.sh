#!/usr/bin/env bash
# eidos-pyglove adapter tests in a throwaway venv (CPython 3.12) holding only the pinned pyglove,
# CPU torch and pytest from packages/eidos-pyglove/requirements-ci.txt, then the local package with
# --no-deps. No lab server, eidos-wire or eidos-worker-sdk code is installed or needed.
# Env: CI_WORK (scratch dir), PYTHON (default 3.12), TORCH_INDEX (default the PyTorch CPU index).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG="$REPO/packages/eidos-pyglove"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-pyglove}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cpu}"
mkdir -p "$CI_WORK"
rm -rf "$CI_WORK/venv"
uv venv --python "${PYTHON:-3.12}" "$CI_WORK/venv"
PY="$CI_WORK/venv/bin/python"

# exact pins; torch's +cpu wheel only exists on the PyTorch CPU index, everything else on PyPI
uv pip install --python "$PY" --extra-index-url "$TORCH_INDEX" --index-strategy unsafe-best-match \
  -r "$PKG/requirements-ci.txt"
uv pip install --python "$PY" --no-deps "$PKG"

"$PY" - <<'PY'
import platform
import sys
from importlib import metadata

import pyglove
import torch

names = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
forbidden = {"eidos-wire", "eidos-worker-sdk", "eidos-pytorch", "eidos", "sqlalchemy", "fastapi", "temporalio"}
leaked = sorted(names & forbidden)
assert not leaked, f"lab or worker packages leaked into the adapter venv: {leaked}"
print("pyglove", pyglove.__version__, "torch", torch.__version__)
assert pyglove.__version__ == "0.4.5", pyglove.__version__
expected_torch = "2.7.0+cpu" if sys.platform == "linux" and platform.machine() == "x86_64" else "2.7.0"
assert torch.__version__ == expected_torch, (torch.__version__, expected_torch)
PY

# run from the scratch dir so nothing but the installed package is importable
cd "$CI_WORK"
"$PY" -m pytest -q -p no:cacheprovider -rs "$PKG/tests" | tee "$CI_WORK/pytest.log"
# no skips allowed: pyglove and torch are installed, so every test must run
if grep -qE '[0-9]+ skipped' "$CI_WORK/pytest.log"; then
  echo "eidos-pyglove tests were skipped" >&2
  exit 1
fi
