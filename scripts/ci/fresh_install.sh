#!/usr/bin/env bash
# Fresh-install proof: wheels -> clean venv -> lab init/start/doctor/project create/status/stop.
# Env: see build_and_install.sh and lab_cycle.sh. LAB_HOME defaults to $CI_WORK/lab.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CI_WORK="${CI_WORK:-${RUNNER_TEMP:-$(mktemp -d)}/eidos-ci}"
"$HERE/build_and_install.sh"
VENV="$CI_WORK/venv"
export LAB_BIN="$VENV/bin/lab"

export PATH="$VENV/bin:$PATH"   # services spawned by the lab must resolve from the installed venv
cd "$CI_WORK"
exec "$HERE/lab_cycle.sh" all
