#!/usr/bin/env bash
# Fetch a CPU-only torch wheel into a local wheelhouse. The PyTorch CPU index links to
# download-r2.pytorch.org, which is not reachable from the build sandbox; the same paths
# are served by download.pytorch.org. Usage: fetch_cpu_torch.sh [version] [cpXY]
set -euo pipefail
VER="${1:-2.15.0}"; PY="${2:-cp311}"
WH="${EIDOS_WHEELHOUSE:-/workspace/.wheelhouse}"
mkdir -p "$WH"
NAME="torch-${VER}+cpu-${PY}-${PY}-manylinux_2_28_x86_64.whl"
[ -f "$WH/$NAME" ] || curl -fsSL -o "$WH/$NAME" "https://download.pytorch.org/whl/cpu/torch-${VER}%2Bcpu-${PY}-${PY}-manylinux_2_28_x86_64.whl"
echo "$WH/$NAME"
