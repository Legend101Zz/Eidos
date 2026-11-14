#!/usr/bin/env bash
# Opt-in installed-lab smoke. Supply an already packaged public-development arm4
# directory with arm.json/prompt receipts for autopilot-v2, and an unused lab home.
set -euo pipefail
: "${EIDOS_STUDY_HOME:?choose a new isolated lab home}"
: "${EIDOS_STUDY_PYTHON:?choose the frozen lab interpreter built with autopilot-v2}"
: "${EIDOS_RESEARCH_PYTHON:?choose the research interpreter}"
if [[ $# != 2 || -e "$EIDOS_STUDY_HOME" ]]; then
  echo "Usage: smoke_autopilot.sh PREPARED_ARM4_RUN_DIR GRADING_DIR (lab home must not exist)" >&2
  exit 2
fi
export EIDOS_STUDY_TREATMENT=autopilot-v2
nice -n 10 "$EIDOS_STUDY_PYTHON" - "$1" <<'PY'
import json, sys
from pathlib import Path
from study.exp.treatment import for_run
from study.eidos_arms import lab, worker
from eidos.investigator.runtime import build_runtime
root = Path(sys.argv[1]).resolve()
meta = json.loads((root / "arm.json").read_text())
resolved = for_run(meta)
assert resolved.id == "autopilot-v2"
assert resolved.investigator["runtime"] == "fixture"
assert resolved.investigator["settings"]["policy"] == "autopilot-v2"
assert meta["card"] in {"N01", "N02", "N03", "N04", "D02", "D03", "D04", "D12"}
assert not (root / "run_record.json").exists(), "use a fresh prepared run"
build_runtime(dict(runtime="fixture", **resolved.investigator["settings"]))
# Fail closed at the controller's provider/credential boundaries.
from study.eidos_arms import provider_bridge
def forbidden(*a, **k):
    raise AssertionError("provider calls and Keychain reads are forbidden in the autopilot smoke")
provider_bridge.start = forbidden
original_output = lab.subprocess.check_output
def check_output(args, *a, **kw):
    if Path(str(args[0])).name == "security":
        forbidden()
    return original_output(args, *a, **kw)
lab.subprocess.check_output = check_output
sys.argv = ["worker", "--arm", "4", "--run-dir", str(root)]
try:
    assert worker.main() == 0
    record = json.loads((root / "run_record.json").read_text())
    assert record["provider"] == "none (fixture runtime)"
    assert record["policy"] == "autopilot-v2"
    assert record["provider_outer_kill"] is False
finally:
    # This script required a new home, so this is its own lab.
    if lab.home().exists():
        lab.stop()
PY
nice -n 10 "$EIDOS_RESEARCH_PYTHON" - "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
from study.grade_block import grade_run
result = grade_run(Path(sys.argv[1]), Path(sys.argv[2]))
print(json.dumps(result, indent=2))
assert result["status"] == "valid", result
assert not result["false_positive_claim"], result
PY
