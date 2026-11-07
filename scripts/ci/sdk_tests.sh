#!/usr/bin/env bash
# Worker SDK tests in a plain venv holding only eidos-wire + eidos-worker-sdk (+ pytest); no server deps.
# Env: CI_WORK (scratch), PYTHON (default 3.12).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-ci}"
mkdir -p "$CI_WORK"; rm -rf "$CI_WORK/sdk-venv"
uv venv --python "${PYTHON:-3.12}" "$CI_WORK/sdk-venv"
uv pip install --python "$CI_WORK/sdk-venv/bin/python" "$REPO/packages/eidos-wire" "$REPO/packages/eidos-worker-sdk" pytest
"$CI_WORK/sdk-venv/bin/python" - <<'PY'
from importlib import metadata
names = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
bad = sorted(names & set("sqlalchemy temporalio fastapi httpx mcp starlette uvicorn asyncpg psycopg".split()))
assert not bad, f"server deps leaked: {bad}"
print("installed:", sorted(names))
PY
cd "$CI_WORK"
"$CI_WORK/sdk-venv/bin/python" -m pytest -q -p no:cacheprovider "$REPO/packages/eidos-worker-sdk/tests"
