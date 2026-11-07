"""The assessor runs approved assessment code in its own sandbox (review item: unsandboxed assessor)
and the realized isolation is returned for the assessment record."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from eidos.orchestration.activities import AssessorActivities
from eidos.runtime.launcher import bwrap_available

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "components" / "eidos.assessors.paired-ci"


class FakeClient:
    def __init__(self, bundle: Path):
        self.bundle = bundle

    def query(self, name, params=None, *, project=None):
        man = json.loads((COMP / "eidos-component.json").read_text())
        return {"body": {"manifest": man, "bundle_artifact_id": "art_bundle"}}

    def download(self, project, artifact_id, dest):
        shutil.copyfile(self.bundle, dest)
        return dest


def _bundle(tmp: Path) -> Path:
    files = {str(p.relative_to(COMP)): p.read_text() for p in COMP.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts}
    out = tmp / "bundle.json"
    out.write_text(json.dumps({"files": files}))
    return out


def _evidence() -> dict:
    suite = json.loads((COMP / "controls" / "suite.json").read_text())
    ctl = next(c for c in suite["controls"] if c["type"] == "planted")
    ev = ctl["input"]["inputs"]["evidence"]
    return ev.get("json", ev)


@pytest.mark.skipif(not bwrap_available(), reason="bubblewrap unavailable")
def test_assessor_runs_sandboxed_and_reports_realized_isolation(tmp_path):
    secret = tmp_path / "svc-assessor.token"
    secret.write_text("token")
    acts = AssessorActivities(FakeClient(_bundle(tmp_path)), None, None, sys.executable,
                              credential_path=str(secret), api_port=1)
    out = ActivityEnvironment().run(acts._run_assessor, "p", "eidos.assessors.paired-ci@1.0.0", _evidence())
    iso = out["assessor_isolation"]
    assert out["outcome"] == "protocol-passed"
    assert iso["profile"] == "linux:bwrap-sealed"
    assert iso["controls"]["no-credential-read"] == "passed" and iso["controls"]["no-network-egress"] == "passed"
