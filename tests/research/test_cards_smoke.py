"""End-to-end smoke of the cheap capability-card subset through a RUNNING Eidos lab.

Runs only with EIDOS_RESEARCH=1, a started lab (EIDOS_HOME) and the research dev interpreter
(EIDOS_RESEARCH_PYTHON, default /workspace/.eidos-dev/lead/envs/research-dev/bin/python).
Execution is through eidos.project.command jobs; scoring uses the release's grade.py/fresh_check.py as a
reference-assisted builder check (labelled development data). Fixture/reference only: no LLM.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.slow, pytest.mark.skipif(os.environ.get("EIDOS_RESEARCH") != "1",
                                                   reason="set EIDOS_RESEARCH=1 (and EIDOS_HOME) to run")]


def test_cheap_card_subset(tmp_path):
    assert os.environ.get("EIDOS_HOME"), "EIDOS_HOME must point at a started lab"
    out = tmp_path / "run"
    project = f"cardsmoke{int(time.time()) % 1000000}"
    proc = subprocess.run([sys.executable, str(REPO / "scripts/research/cards.py"), "--out", str(out), "--cards",
                           "cheap", "--seeds", "11", "--setup", "--project", project],
                          capture_output=True, text=True, timeout=1500)
    sys.stdout.write(proc.stdout[-3000:] + proc.stderr[-3000:])
    res = json.loads((out / "results.json").read_text())
    assert set(res["cards"]) == {"D01", "D07", "D12", "D04"}
    bad = {k: v.get("error") or "failed" for k, v in res["cards"].items() if not v["passed"]}
    assert not bad, bad
    for card in res["cards"].values():
        for row in card["rows"]:
            assert row["fresh_check"]["passed"] and row["eidos_score_matches_grade_validation"]
    assert proc.returncode == 0
