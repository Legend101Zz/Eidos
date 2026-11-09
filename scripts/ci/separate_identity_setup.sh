#!/usr/bin/env bash
# Root setup for linux:bwrap-sealed with separate-identity (ADR 0011; docs/ops/INSTALL_AND_OPERATE.md).
# Run as root:  sudo env "PATH=$PATH" bash scripts/ci/separate_identity_setup.sh <repo or lab install dir>
# 1. bubblewrap, and unprivileged user namespaces for the candidate user (Ubuntu 24.04 restricts them via AppArmor);
# 2. the system user eidos-cand (no home, no login shell);
# 3. a candidate-readable interpreter under /opt/eidos (a root-owned or home-directory interpreter is not);
# 4. read access for eidos-cand to the worker SDK sources the runner binds into the sandbox.
set -euo pipefail
SRC="${1:?usage: separate_identity_setup.sh <dir holding the worker SDK (repo checkout or lab venv)>}"
PYVER="${EIDOS_CANDIDATE_PYVER:-3.12}"
VENV="${EIDOS_CANDIDATE_VENV:-/opt/eidos/envs/sealed-py312}"
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 2; }
command -v bwrap >/dev/null || { apt-get update -qq && apt-get install -y -qq bubblewrap; }
if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
id eidos-cand >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin eidos-cand
mkdir -p /opt/eidos/python /opt/eidos/envs
UV_PYTHON_INSTALL_DIR=/opt/eidos/python uv python install "$PYVER"
[ -x "$VENV/bin/python" ] || UV_PYTHON_INSTALL_DIR=/opt/eidos/python uv venv --python "$PYVER" "$VENV"
chmod -R a+rX /opt/eidos
# Traverse (not list) every parent of the SDK sources, then read the sources themselves.
d="$(cd "$SRC" && pwd)"
while [ "$d" != "/" ]; do chmod o+x "$d"; d="$(dirname "$d")"; done
chmod -R o+rX "$SRC"
runuser -u eidos-cand -- "$VENV/bin/python" -I -c 'import sys; print("candidate interpreter ok", sys.version.split()[0])'
runuser -u eidos-cand -- bwrap --ro-bind / / --unshare-all --die-with-parent true && echo "bwrap as eidos-cand ok"
