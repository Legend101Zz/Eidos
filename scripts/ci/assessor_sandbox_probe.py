"""Run the approved paired-CI assessor release through AssessorActivities._run_assessor on this host's
sealed profile (bwrap on Linux, Seatbelt on macOS) with a fake lab client, a planted credential in a
lab-shaped directory and a live control-socket listener. Fails unless the assessment completes, the
realized profile is the host's sealed profile and every sealed control passed.
Usage (from the repo root): python scripts/ci/assessor_sandbox_probe.py   (OUT=dir for JSON)"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

from temporalio.testing import ActivityEnvironment

from eidos.domain.isolation import SEALED_CONTROLS
from eidos.orchestration.activities import AssessorActivities
from eidos.runtime.launcher import detect_sealed_profile

COMP = Path("components/eidos.assessors.paired-ci").resolve()
OUT = Path(os.environ.get("OUT", ".")).resolve()
OUT.mkdir(parents=True, exist_ok=True)


class FakeClient:
    def __init__(self, bundle: Path):
        self.bundle = bundle

    def query(self, name, params=None, *, project=None):
        return {"body": {"manifest": json.loads((COMP / "eidos-component.json").read_text()),
                         "bundle_artifact_id": "art_bundle"}}

    def download(self, project, artifact_id, dest):
        shutil.copyfile(self.bundle, dest)
        return dest


tmp = Path(tempfile.mkdtemp(prefix="eidos-assessor-probe-")).resolve()
lab = tmp / "lab"
(lab / "config" / "secrets").mkdir(parents=True)
secret = lab / "config" / "secrets" / "svc-assessor.token"
secret.write_text("planted credential")
bundle = tmp / "bundle.json"
bundle.write_text(json.dumps({"files": {str(p.relative_to(COMP)): p.read_text() for p in COMP.rglob("*")
                                        if p.is_file() and "__pycache__" not in p.parts}}))
suite = json.loads((COMP / "controls" / "suite.json").read_text())
ev = next(c for c in suite["controls"] if c["type"] == "planted")["input"]["inputs"]["evidence"]
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(8)
threading.Thread(target=lambda: [srv.accept()[0].close() for _ in iter(int, 1)], daemon=True).start()

result = {"host_profile": detect_sealed_profile(), "failures": []}
acts = AssessorActivities(FakeClient(bundle), None, None, sys.executable, credential_path=str(secret),
                          api_port=srv.getsockname()[1], lab_root=str(lab))
try:
    out = ActivityEnvironment().run(acts._run_assessor, "p", "eidos.assessors.paired-ci@1.0.0", ev.get("json", ev))
    iso = out.get("assessor_isolation") or {}
    result.update(outcome=out.get("outcome"), isolation=iso)
    if out.get("outcome") != "protocol-passed":
        result["failures"].append(f"planted-effect assessment outcome {out.get('outcome')}")
    if not result["host_profile"] or iso.get("profile") != result["host_profile"]:
        result["failures"].append(f"realized profile {iso.get('profile')} != host sealed profile {result['host_profile']}")
    for c in SEALED_CONTROLS:
        if (iso.get("controls") or {}).get(c) != "passed":
            result["failures"].append(f"assessor control {c} = {(iso.get('controls') or {}).get(c)}")
except Exception as exc:  # noqa: BLE001 - recorded as evidence
    result["failures"].append(f"assessor run failed: {exc!r}"[:2000])
(OUT / "assessor_sandbox_probe.json").write_text(json.dumps(result, indent=2, default=str))
print(json.dumps(result, indent=2, default=str))
sys.exit(1 if result["failures"] else 0)
