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
from eidos.runtime.launcher import detect_sealed_profile

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


@pytest.mark.skipif(not detect_sealed_profile(), reason="no sealed profile on this host")
def test_assessor_runs_sandboxed_and_reports_realized_isolation(tmp_path):
    secret = tmp_path / "svc-assessor.token"
    secret.write_text("token")
    acts = AssessorActivities(FakeClient(_bundle(tmp_path)), None, None, sys.executable,
                              credential_path=str(secret), api_port=1)
    out = ActivityEnvironment().run(acts._run_assessor, "p", "eidos.assessors.paired-ci@1.0.0", _evidence())
    iso = out["assessor_isolation"]
    assert out["outcome"] == "protocol-passed"
    assert iso["profile"] == detect_sealed_profile()
    assert iso["controls"]["no-credential-read"] == "passed" and iso["controls"]["no-network-egress"] == "passed"


def test_staged_assessor_output_must_be_a_contained_regular_file(tmp_path):
    """Review 2 finding 1: a hostile assessor swapped its staged evaluation for a symlink to a host JSON
    file, which the unsandboxed activity followed into the assessment record."""
    from temporalio.exceptions import ApplicationError

    from eidos.orchestration.activities import _read_staged_json
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "evaluation.json").write_text('{"outcome": "protocol-passed"}')
    import hashlib
    data = (staging / "evaluation.json").read_bytes()
    from types import SimpleNamespace
    man = SimpleNamespace(outputs=[SimpleNamespace(slot="evaluation", filename="evaluation.json",
                                                   digest="sha256:" + hashlib.sha256(data).hexdigest(), size=len(data))])
    assert _read_staged_json(staging, man, "evaluation", 1 << 20)["outcome"] == "protocol-passed"
    secret = tmp_path / "host.json"
    secret.write_text('{"sealed_labels": [1, 0]}')
    (staging / "evaluation.json").unlink()
    (staging / "evaluation.json").symlink_to(secret)
    with pytest.raises(ApplicationError, match="regular file"):
        _read_staged_json(staging, man, "evaluation", 1 << 20)
    (staging / "evaluation.json").unlink()
    (staging / "evaluation.json").write_text('{"outcome": "protocol-passed", "x": 1}')
    with pytest.raises(ApplicationError, match="digest"):
        _read_staged_json(staging, man, "evaluation", 1 << 20)
