#!/bin/bash
set -euo pipefail
source ~/eidos-internal/env.sh
free_kib=$(df -k / | awk 'NR==2 {print $4}')
if [ "$free_kib" -lt 14680064 ]; then
  echo 'STOP: root filesystem has less than 14 GiB free' >&2
  exit 1
fi
cd "$(dirname "$0")/../.."
wheel_dir="$EIDOS_STUDY_ROOT/dist-pilot-2"
lab_venv="$EIDOS_STUDY_ROOT/venvs/pilot-2-lab"
if [ -e "$lab_venv" ]; then
  echo 'Refusing to replace an existing pilot lab venv' >&2
  exit 1
fi
if [ -n "$(git status --porcelain -- src/eidos components packages pyproject.toml uv.lock)" ]; then
  echo 'Commit Eidos build inputs before building' >&2
  exit 1
fi
uv build --all-packages -o "$wheel_dir"
uv venv --python 3.12 "$lab_venv"
uv pip install --python "$lab_venv/bin/python" "$wheel_dir"/*.whl
"$lab_venv/bin/python" - "$wheel_dir" "$lab_venv" <<'PY'
import hashlib, importlib.metadata, json, subprocess, sys
from pathlib import Path
wheel_dir, venv = map(Path, sys.argv[1:])
record = {'eidos_commit_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
          'python': str(venv / 'bin/python'),
          'wheels': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wheel_dir.glob('*.whl'))},
          'packages': {n: importlib.metadata.version(n) for n in ('eidos-lab', 'eidos-wire', 'eidos-worker-sdk', 'eidos-pytorch')}}
(venv / 'pilot-build.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record, indent=2))
PY
