#!/usr/bin/env bash
# Optional Composer adapter tests (packages/eidos-composer) in a fresh venv that no other job shares.
# requirements-ci.txt pins CPU torch, mosaicml==0.32.1 and friends; the local worker packages go in with
# --no-deps afterwards, so installing them can never change torch and the lab server's dependencies never
# enter this venv. Linux x86_64 uses the +cpu wheels from the PyTorch index; other hosts (a developer
# laptop) use the plain PyPI wheels of the same versions.
# Env: CI_WORK (scratch), PYTHON (default 3.12).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-ci}"
VENV="$CI_WORK/composer-venv"
PY="$VENV/bin/python"
mkdir -p "$CI_WORK"; rm -rf "$VENV"
uv venv --python "${PYTHON:-3.12}" "$VENV"

req="$REPO/packages/eidos-composer/requirements-ci.txt"
if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
  req="$CI_WORK/composer-requirements.txt"
  grep -v -e '^--extra-index-url' -e '^#' "$REPO/packages/eidos-composer/requirements-ci.txt" | sed 's/+cpu$//' > "$req"
fi
# The +cpu local versions exist only on the PyTorch index while PyPI also hosts torch: look at both.
uv pip install --python "$PY" --index-strategy unsafe-best-match -r "$req"
uv pip install --python "$PY" --no-deps \
  "$REPO/packages/eidos-wire" "$REPO/packages/eidos-worker-sdk" "$REPO/packages/eidos-pytorch" "$REPO/packages/eidos-composer"
uv pip check --python "$PY"

"$PY" - <<'PY'
from importlib import metadata

names = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
bad = sorted(names & set("sqlalchemy temporalio fastapi httpx mcp starlette uvicorn asyncpg psycopg eidos-lab".split()))
assert not bad, f"lab server deps leaked into the adapter venv: {bad}"
torch = metadata.version("torch")
assert torch.split("+")[0] == "2.7.0", f"torch was changed: {torch}"
assert metadata.version("mosaicml") == "0.32.1", metadata.version("mosaicml")
print("installed: torch", torch, "mosaicml", metadata.version("mosaicml"), "eidos-composer", metadata.version("eidos-composer"))
PY
cd "$CI_WORK"
"$PY" -m pytest -q -p no:cacheprovider "$REPO/packages/eidos-composer/tests"
