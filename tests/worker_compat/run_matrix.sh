#!/usr/bin/env bash
# Worker-independence evidence (acceptance A10/A13 support).
#   env 1: Python 3.9 + pydantic<2 pinned  -> wire + worker-sdk install, no server deps, SDK tests
#   env 2: Python 3.12 + CPU torch 2.15.0  -> wire + sdk + pytorch install, torch unchanged, pytorch tests
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ART="${EIDOS_COMPAT_ROOT:-/workspace/.eidos-dev/h1}"
ENVS="$ART/envs"
WHEELHOUSE="${EIDOS_WHEELHOUSE:-/workspace/.wheelhouse}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/.uv-cache}"
TORCH_PIN="torch==2.15.0+cpu"
PYTEST_ARGS=(-q -W "ignore::pytest.PytestConfigWarning" -p no:cacheprovider)
mkdir -p "$ENVS"

FORBIDDEN="sqlalchemy temporalio fastapi httpx mcp starlette uvicorn asyncpg psycopg"

assert_no_server_deps() {  # $1 = python
  "$1" - "$FORBIDDEN" <<'PY'
import sys
from importlib import metadata
forbidden = set(sys.argv[1].split())
installed = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
bad = sorted(forbidden & installed)
assert not bad, f"server/agent dependencies leaked into the worker env: {bad}"
print("no server dependencies installed:", sorted(forbidden), "absent")
PY
}

lint_sources() {  # worker-facing sources must not import server/agent packages
  python3 - "$ROOT" <<'PY'
import ast, pathlib, sys
root = pathlib.Path(sys.argv[1])
banned = {"pydantic", "sqlalchemy", "temporalio", "httpx", "mcp", "fastapi", "eidos"}
bad = []
for pkg in ("eidos-wire", "eidos-worker-sdk", "eidos-pytorch"):
    for f in (root / "packages" / pkg / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            bad += [f"{f}: {n}" for n in names if n.split(".")[0] in banned]
assert not bad, bad
print("source import lint ok")
PY
}

lint_sources

echo "== env 1: Python 3.9 + pydantic<2 =="
uv python install 3.9 >/dev/null
rm -rf "$ENVS/py39"
uv venv -q --python 3.9 "$ENVS/py39"
PY39="$ENVS/py39/bin/python"
uv pip install -q -p "$PY39" "pydantic<2" pytest
uv pip install -q -p "$PY39" "$ROOT/packages/eidos-wire" "$ROOT/packages/eidos-worker-sdk"
"$PY39" - <<'PY'
import sys
from importlib import metadata
assert sys.version_info[:2] == (3, 9), sys.version
v = metadata.version("pydantic")
assert v.startswith("1."), f"pydantic was changed to {v}"
print("python", sys.version.split()[0], "pydantic", v, "(unchanged, 1.x)")
PY
assert_no_server_deps "$PY39"
"$PY39" -c "import pydantic, eidos_wire, eidos_worker; assert pydantic.VERSION.startswith('1.')"
(cd "$ROOT" && "$PY39" -m pytest "${PYTEST_ARGS[@]}" packages/eidos-worker-sdk/tests)

echo "== env 2: Python 3.12 + CPU torch 2.15.0 =="
rm -rf "$ENVS/py312"
uv venv -q --python 3.12 "$ENVS/py312"
PY312="$ENVS/py312/bin/python"
uv pip install -q -p "$PY312" --find-links "$WHEELHOUSE" "$TORCH_PIN"
BEFORE="$("$PY312" -c 'import torch; print(torch.__version__)')"
uv pip install -q -p "$PY312" pytest
uv pip install -q -p "$PY312" --find-links "$WHEELHOUSE" \
  "$ROOT/packages/eidos-wire" "$ROOT/packages/eidos-worker-sdk" "$ROOT/packages/eidos-pytorch"
AFTER="$("$PY312" -c 'import torch; print(torch.__version__)')"
[ "$BEFORE" = "$AFTER" ] || { echo "torch changed: $BEFORE -> $AFTER"; exit 1; }
[ "$AFTER" = "2.15.0+cpu" ] || { echo "unexpected torch $AFTER"; exit 1; }
echo "torch $AFTER unchanged by installing eidos packages"
assert_no_server_deps "$PY312"
(cd "$ROOT" && "$PY312" -m pytest "${PYTEST_ARGS[@]}" packages/eidos-worker-sdk/tests packages/eidos-pytorch/tests)

echo "MATRIX OK"
