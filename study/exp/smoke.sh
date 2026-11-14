#!/usr/bin/env bash
# Model-free connectivity qualification; requires a buildable, dedicated ip lab.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${EIDOS_RESEARCH_PYTHON:?source the programme env.sh first}"
out=${1:-"/Volumes/Mrigesh SSD/eidos-v1/improvement/runs/smoke-$(date -u +%Y%m%dT%H%M%SZ)"}
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.run --treatment stub --cards N01,N03 --out "$out/stub"
cat "$out/stub/scorecard.md"
nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.run --treatment autopilot-v2 --cards N01 --smoke --wall-seconds 60 --out "$out/autopilot-legacy"
cat "$out/autopilot-legacy/scorecard.md"
if "$EIDOS_RESEARCH_PYTHON" -c 'from study.private_card_registry import PROFILES; p=PROFILES.get("pool:D"); assert p is not None; p.pool_root()' >/dev/null 2>&1; then
  nice -n 10 "$EIDOS_RESEARCH_PYTHON" -m study.exp.run --treatment autopilot-v2 --pool D --card-limit 1 --smoke --wall-seconds 60 --out "$out/autopilot-pool"
  cat "$out/autopilot-pool/scorecard.md"
fi
