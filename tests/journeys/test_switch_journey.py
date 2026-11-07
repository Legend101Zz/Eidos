"""Runs the switch-order journey against a running lab when EIDOS_JOURNEY=1.

Needs ``EIDOS_HOME`` (a started lab: ``lab init`` + ``lab start``) and a torch project interpreter
(``EIDOS_TORCH_PYTHON``). Takes several minutes; step 7 is fixture-only (scripted policy, no LLM).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

pytestmark = [
    pytest.mark.slow,
    pytest.mark.journey,
    pytest.mark.skipif(os.environ.get("EIDOS_JOURNEY") != "1", reason="set EIDOS_JOURNEY=1 (and EIDOS_HOME) to run"),
]


def test_switch_order_journey(tmp_path):
    assert os.environ.get("EIDOS_HOME"), "EIDOS_HOME must point at a started lab"
    out = Path(os.environ.get("EIDOS_JOURNEY_OUT", tmp_path / "journey-run.json"))
    cmd = [sys.executable, str(HERE / "switch_journey.py"), "--project", f"journey{int(time.time()) % 1000000}",
           "--out", str(out), "--workdir", str(tmp_path / "work")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3000)
    sys.stdout.write(proc.stdout[-6000:])
    rec = json.loads(out.read_text())
    failed = {k: v["status"] for k, v in rec["steps"].items() if v["status"] != "pass"}
    bad_checks = [(k, c["label"]) for k, v in rec["steps"].items() for c in v["checks"] if not c["ok"]]
    assert not failed and not bad_checks, (failed, bad_checks)
    assert proc.returncode == 0
