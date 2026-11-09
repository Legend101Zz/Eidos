"""`linux:bwrap-sealed` with `separate-identity`, end to end (ADR 0011).

Needs root, the `eidos-cand` user, bubblewrap and a candidate-readable interpreter (CI job
`separate-identity`, `scripts/ci/separate_identity_setup.sh`; manual steps in `docs/ops/INSTALL_AND_OPERATE.md`).
A sealed job runs through the real runner and command service: candidate code runs as `eidos-cand` and sees no
labels or lab credential. The approved assessor then runs under the lab identity inside bwrap, reads the evidence
(predictions joined with labels only the lab holds) and passes. The receipts report `separate-identity` passed
for both roles. Elsewhere the test skips.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from eidos.domain.isolation import SEALED_CONTROLS, evaluate_receipt
from eidos.runtime.launcher import CANDIDATE_USER, bwrap_available, candidate_uid

pytestmark = pytest.mark.pg

CAND_PY = os.environ.get("EIDOS_CANDIDATE_PYTHON", "/opt/eidos/envs/sealed-py312/bin/python")
ROOT = Path(__file__).resolve().parents[2]
ASSESSOR = ROOT / "components" / "eidos.assessors.paired-ci"


def _ready() -> bool:
    return (sys.platform.startswith("linux") and os.geteuid() == 0 and bwrap_available()
            and os.path.exists(CAND_PY) and candidate_uid(CAND_PY) is not None)


CANDIDATE_CODE = '''
import os
def run(inputs, config, *, reader=None, writer=None, context=None):
    rows = reader.json("pairs")
    leaks = {}
    for name, path in (config.get("probe_paths") or {}).items():
        try:
            open(path, "rb").read(1)
            leaks[name] = True
        except OSError:
            leaks[name] = False
    writer.stage_json("predictions", [{k: r[k] for k in ("pair_id", "cluster", "reference_logit", "variant_logit")}
                                      for r in rows], schema="pair-logits/1")
    return {"uid": os.getuid(), "euid": os.geteuid(), "leaks": leaks, "saw_labels": any("reference_label" in r
                                                                                          for r in rows)}
'''


class _AssessorClient:
    def __init__(self, bundle: Path):
        self.bundle = bundle

    def query(self, name, params=None, *, project=None):
        return {"body": {"manifest": json.loads((ASSESSOR / "eidos-component.json").read_text()),
                         "bundle_artifact_id": "art_bundle"}}

    def download(self, project, artifact_id, dest):
        shutil.copyfile(self.bundle, dest)
        return dest


def test_sealed_job_runs_as_candidate_user_and_assessor_as_lab_identity(lab):
    if not _ready():
        if os.environ.get("EIDOS_REQUIRE_SEPARATE_IDENTITY"):  # the CI job: a skip would hide a broken setup
            pytest.fail(f"separate-identity prerequisites missing (root={os.geteuid() == 0}, "
                        f"bwrap={bwrap_available()}, interpreter={os.path.exists(CAND_PY)})")
        pytest.skip("needs root, bwrap, the eidos-cand user and a candidate-readable interpreter")
    from temporalio.testing import ActivityEnvironment
    from test_command_path import _publish, _register_lab_env, project
    from test_turn_recovery import ShimClient

    from eidos.orchestration.activities import AssessorActivities
    from eidos.runtime.runner import AttemptRunner

    cand = pwd.getpwnam(CANDIDATE_USER)
    base = Path(tempfile.mkdtemp(prefix="eidos-sepid-", dir="/tmp"))
    os.chmod(base, 0o755)
    lab_root = base / "lab"
    (lab_root / "config" / "secrets").mkdir(parents=True)
    os.chmod(lab_root, 0o711)  # the candidate traverses to its own attempt directory, nothing else
    os.chmod(lab_root / "config", 0o700)
    cred = lab_root / "config" / "secrets" / "svc-launcher.token"
    cred.write_text("planted lab credential")
    os.chmod(cred, 0o600)
    proj = base / "project"
    proj.mkdir()
    os.chmod(proj, 0o755)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    threading.Thread(target=lambda: [srv.accept()[0].close() for _ in iter(int, 1)], daemon=True).start()
    try:
        pid = project(lab)
        _register_lab_env(lab, pid)  # validation of the published bundle uses the lab interpreter
        lab.cmd(lab.admin, "register_environment", {"name": "sealed", "python_executable": CAND_PY,
                "project_root": str(proj), "python_version": "3.12", "installed": {}}, project=pid)
        ref = _publish(lab, pid, {"id": "cand.logits", "release": "1.0.0", "implements": "predict/1",
                                  "kind": "artifact-op", "entry": "m:run", "scientific_effects": ["observe"],
                                  "input": {"pairs": "pair-inputs/1"}, "output": {"predictions": "pair-logits/1"}},
                       {"m.py": CANDIDATE_CODE})["component_ref"]
        suite = json.loads((ASSESSOR / "controls" / "suite.json").read_text())
        planted = next(c for c in suite["controls"] if c["type"] == "planted")["input"]["inputs"]["evidence"]
        planted = planted.get("json", planted)
        labels = {r["pair_id"]: r for r in planted["arms"]["candidate"]["rows"]}
        # The candidate's input has no labels: only the lab holds them.
        unlabeled = [{k: r[k] for k in ("pair_id", "cluster", "reference_logit", "variant_logit")}
                     for r in planted["arms"]["candidate"]["rows"]]
        d, n = lab.store.put_bytes(json.dumps(unlabeled).encode())
        art = lab.cmd(lab.admin, "register_artifact", {"digest": d, "size": n, "schema": "pair-inputs/1",
                      "media_type": "application/json"}, project=pid)["result"]["artifact_id"]
        job = lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": "sealed",
                      "inputs": {"pairs": art}, "isolation_profile": "linux:bwrap-sealed",
                      "config": {"probe_paths": {"credential": str(cred), "store": lab.store.path_for(d)}}},
                      project=pid)["result"]["job_id"]
        launcher = lab.principal(f"svc-sealed-launcher-{pid}", ["launcher"], [pid], extra=["sealed:read"])
        runner = AttemptRunner(ShimClient(lab, launcher), lab.store, lab_root=lab_root, launcher_id=launcher.id,
                               api_port=srv.getsockname()[1], credential_path=str(cred),
                               allowed_profiles=("dev:same-user", "linux:bwrap-sealed"))
        res = runner.run_job(job, pid, activity_attempt=1)
        got = lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)
        assert res["job_status"] == "succeeded", got
        receipt = got["terminal"]["receipt"]
        assert receipt["uid"] == cand.pw_uid and receipt["euid"] == cand.pw_uid  # candidate code ran as eidos-cand
        assert receipt["leaks"] == {"credential": False, "store": False} and receipt["saw_labels"] is False
        iso = lab.q(lab.admin, "get_record", {"kind": "isolation_receipt", "id": got["current_attempt"]},
                    project=pid)["body"]
        assert iso["identity"] == "candidate" and iso["probe_uid_inside"] == cand.pw_uid, iso
        for c in (*SEALED_CONTROLS, "separate-identity", "no-process-inspection"):
            assert iso["controls"][c] == "passed", (c, iso)
        need = {"sealed": True, "controls": [*SEALED_CONTROLS, "separate-identity"]}
        assert evaluate_receipt(need, iso, trusted_launchers={launcher.id})["ok"]

        # Assessment: predictions from the sealed job joined with the lab's labels, under the lab identity.
        out = next(o for o in got["outputs"] if o["slot"] == "predictions")
        preds = json.loads(Path(lab.store.path_for(out["digest"])).read_text())
        rows = [{**labels[p["pair_id"]], **p} for p in preds]
        evidence = {"plan": planted["plan"], "arms": {"baseline": planted["arms"]["baseline"],
                                                      "candidate": {**planted["arms"]["candidate"], "rows": rows}}}
        bundle = base / "assessor-bundle.json"
        bundle.write_text(json.dumps({"files": {str(p.relative_to(ASSESSOR)): p.read_text()
                                                for p in ASSESSOR.rglob("*")
                                                if p.is_file() and "__pycache__" not in p.parts}}))
        acts = AssessorActivities(_AssessorClient(bundle), None, None, sys.executable, credential_path=str(cred),
                                  api_port=srv.getsockname()[1], lab_root=str(lab_root))
        ev = ActivityEnvironment().run(acts._run_assessor, pid, "eidos.assessors.paired-ci@1.0.0", evidence)
        a_iso = ev["assessor_isolation"]
        assert ev["outcome"] == "protocol-passed", ev
        assert a_iso["profile"] == "linux:bwrap-sealed" and a_iso["identity"] == "lab"
        assert a_iso["probe_uid_inside"] == os.geteuid() and a_iso["candidate_uid"] == cand.pw_uid
        for c in (*SEALED_CONTROLS, "separate-identity"):
            assert a_iso["controls"][c] == "passed", (c, a_iso)
    finally:
        srv.close()
        shutil.rmtree(base, ignore_errors=True)
