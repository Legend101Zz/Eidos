#!/bin/bash
# Build an immutable eval lab from a committed ref, entirely on the SSD.
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo 'Usage: study/evals/build_lab.sh <git-ref> <name>' >&2
  exit 2
fi
ref=$1
name=$2
case "$name" in
  ''|*[!A-Za-z0-9_-]*|pilot-3*|p3|r4)
    echo 'Choose a simple, non-pilot lab name' >&2
    exit 2
    ;;
esac
repo=$(cd "$(dirname "$0")/../.." && pwd)
ssd_root='/Volumes/Mrigesh SSD/eidos-v1'
lab_root="$ssd_root/evals-labs/$name"
export UV_CACHE_DIR="$ssd_root/tmp/uv-cache"
export UV_PYTHON_INSTALL_DIR="$ssd_root/tmp/uv-python"
export PIP_CACHE_DIR="$ssd_root/tmp/pip-cache"
export TMPDIR="$ssd_root/tmp"
export XDG_CACHE_HOME="$ssd_root/tmp/xdg"
export PYTHONPYCACHEPREFIX="$ssd_root/tmp/pycache"
export PYTHONNOUSERSITE=1
mkdir -p "$TMPDIR" "$ssd_root/evals-labs"
sha=$(git -C "$repo" rev-parse --verify "$ref^{commit}")
# mkdir is the exclusive allocation. A previous build is never replaced.
mkdir "$lab_root"
mkdir "$lab_root/source" "$lab_root/wheels"
git -C "$repo" archive "$sha" | tar -x -C "$lab_root/source"
(
  cd "$lab_root/source"
  uv build --all-packages -o "$lab_root/wheels"
)
uv venv --python 3.12 "$lab_root/venv"
uv pip install --python "$lab_root/venv/bin/python" "$lab_root/wheels"/*.whl
"$lab_root/venv/bin/python" - "$lab_root" "$sha" <<'PY'
import hashlib
import importlib.metadata
import json
import shlex
import socket
import sys
from pathlib import Path

root = Path(sys.argv[1])
record = dict(eidos_commit_sha=sys.argv[2], python=str(root / 'venv/bin/python'),
              wheels={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root / 'wheels').glob('*.whl'))},
              packages={n: importlib.metadata.version(n) for n in ('eidos-lab', 'eidos-wire', 'eidos-worker-sdk', 'eidos-pytorch')})
(root / 'venv/pilot-build.json').write_text(json.dumps(record, indent=2) + '\n')
# Allocate four free ports in 31000..58999. Persisted sibling labs keep their allocations.
reserved = {28767, 25546, 27635, 28766, 25544, 27633, 28765}
for path in root.parent.glob('*/ports.json'):
    reserved.update(json.loads(path.read_text()).values())
base = 31000 + int(hashlib.sha256(root.name.encode()).hexdigest()[:8], 16) % 27000
ports = {}
sockets = []
try:
    for offset in range(28000):
        port = 31000 + (base - 31000 + offset) % 28000
        if port in reserved:
            continue
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            probe.close()
            continue
        sockets.append(probe)
        ports[('api', 'pg', 'temporal', 'mcp')[len(ports)]] = port
        if len(ports) == 4:
            break
    if len(ports) != 4:
        raise RuntimeError('could not allocate four eval ports')
    (root / 'ports.json').write_text(json.dumps(ports, indent=2) + '\n')
finally:
    for probe in sockets:
        probe.close()
env = dict(EIDOS_STUDY_ROOT=str(root), EIDOS_STUDY_HOME=str(root / 'home'),
           EIDOS_STUDY_PYTHON=str(root / 'venv/bin/python'), EIDOS_STUDY_MCP_PORT=str(ports['mcp']))
(root / 'env.sh').write_text(''.join(f'export {key}={shlex.quote(value)}\n' for key, value in env.items()))
print(json.dumps(dict(build=record, ports=ports), indent=2))
PY
# This file contains only this eval's paths and port, and preserves the caller's research interpreter.
source "$lab_root/env.sh"
read -r api_port pg_port temporal_port < <("$EIDOS_STUDY_PYTHON" -c \
  'import json,sys; p=json.load(open(sys.argv[1])); print(p["api"],p["pg"],p["temporal"])' "$lab_root/ports.json")
init_args=(init --home "$EIDOS_STUDY_HOME" --api-port "$api_port" --pg-port "$pg_port" --temporal-port "$temporal_port")
if [ -n "${EIDOS_STUDY_TEMPORAL_SOURCE:-}" ]; then
  init_args+=(--temporal-source "$EIDOS_STUDY_TEMPORAL_SOURCE")
fi
"$lab_root/venv/bin/lab" "${init_args[@]}"
echo "Eval lab initialized. Load $lab_root/env.sh before a live mini-eval."
