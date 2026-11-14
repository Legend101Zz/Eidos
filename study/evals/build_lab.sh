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
disk_guard() {
  python3 - <<'PYDISK'
import shutil
if shutil.disk_usage('/').free < 12.5 * 1024**3:
    raise SystemExit('STOP: internal disk has less than 12.5 GiB free')
PYDISK
}
disk_guard
mkdir -p "$TMPDIR" "$ssd_root/evals-labs"
sha=$(git -C "$repo" rev-parse --verify "$ref^{commit}")
# mkdir is the exclusive allocation. A previous build is never replaced.
mkdir "$lab_root"
mkdir "$lab_root/source" "$lab_root/wheels"
git -C "$repo" archive "$sha" > "$lab_root/source.tar"
source_sha=$(shasum -a 256 "$lab_root/source.tar" | cut -d' ' -f1)
tar -xf "$lab_root/source.tar" -C "$lab_root/source"
rm "$lab_root/source.tar"
disk_guard
(
  cd "$lab_root/source"
  uv build --all-packages -o "$lab_root/wheels"
)
disk_guard
uv venv --python 3.12 "$lab_root/venv"
disk_guard
uv pip install --python "$lab_root/venv/bin/python" "$lab_root/wheels"/*.whl
disk_guard
"$lab_root/venv/bin/python" - "$lab_root" "$sha" "$source_sha" <<'PY'
import fcntl
import hashlib
import importlib.metadata
import json
import shlex
import socket
import sys
from pathlib import Path

root = Path(sys.argv[1])
record = dict(eidos_commit_sha=sys.argv[2], source_archive_sha256=sys.argv[3],
              python=str(root / 'venv/bin/python'),
              wheels={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root / 'wheels').glob('*.whl'))},
              packages={n: importlib.metadata.version(n) for n in ('eidos-lab', 'eidos-wire', 'eidos-worker-sdk', 'eidos-pytorch')})
(root / 'venv/pilot-build.json').write_text(json.dumps(record, indent=2) + '\n')
# Temporal owns nine consecutive ports and stores membership ports as PostgreSQL smallint.
from eidos import settings
block_size = getattr(settings, 'TEMPORAL_PORT_BLOCK', 9)
max_temporal = getattr(settings, 'TEMPORAL_MAX_PORT', 32767)
with (root.parent / '.ports.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    reserved = {28767, 25546, 27635, 28766, 25544, 27633, 28765}
    # Only programme labs (ip-*): sibling lab homes that served sealed runs must never be opened. Ports held by
    # other labs are protected only by the bind probe below, i.e. only while their services run; a STOPPED
    # non-programme lab can later find its ports taken (it then fails to start; nothing is read or exposed).
    for path in root.parent.glob('ip-*/ports.json'):
        sibling = json.loads(path.read_text())
        reserved.update(sibling.values())
        reserved.update(range(sibling['temporal'], sibling['temporal'] + block_size))
    ports = {}
    sockets = []
    try:
        for key in ('temporal', 'api', 'pg', 'mcp'):
            width = block_size if key == 'temporal' else 1
            candidates = list(range(30000, max_temporal - width + 2, width)) if key == 'temporal' else list(range(33000, 59000))
            offset = int(hashlib.sha256((root.name + key).encode()).hexdigest()[:8], 16) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
            for port in candidates:
                block = range(port, port + width)
                if reserved.intersection(block):
                    continue
                trial = []
                try:
                    for member in block:
                        probe = socket.socket()
                        trial.append(probe)
                        probe.bind(('127.0.0.1', member))
                except OSError:
                    for probe in trial:
                        probe.close()
                    continue
                ports[key] = port
                reserved.update(block)
                sockets.extend(trial)
                break
            else:
                raise RuntimeError(f'could not allocate eval ports for {key}')
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
disk_guard
"$lab_root/venv/bin/lab" "${init_args[@]}"
disk_guard
echo "Eval lab initialized. Load $lab_root/env.sh before a live mini-eval."
