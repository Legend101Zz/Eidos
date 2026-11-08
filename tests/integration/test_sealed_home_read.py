"""Sealed lab jobs cannot read the user's home directory (found on a researcher Mac, 2026-11-08).

Before the fix the runner's sealed spec denied only the lab root and a fixed list of credential
directories, so a job under ``macos:seatbelt-sealed`` could read any other file in ``$HOME``
(for example ``~/.claude.json`` or a planted file), although the lab-free ``run_contained`` path
already denied the whole home. Paths a job needs (project root, interpreter prefixes) stay readable
even when they live inside the home directory."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidos.runtime.launcher import launch, run_probes, sealed_deny_read, seatbelt_available
from eidos.runtime.runner import AttemptRunner

mac_sealed = pytest.mark.skipif(not seatbelt_available(), reason="needs macOS sandbox-exec")


def _job_spec(tmp_path: Path, project: Path, profile: str = "macos:seatbelt-sealed"):
    lab = tmp_path / "lab"
    lab.mkdir(exist_ok=True)
    runner = AttemptRunner(SimpleNamespace(), None, lab_root=lab, launcher_id="L", api_port=1,
                           credential_path=str(lab / "secrets" / "t"))
    st = {"lock": {"environment": {"python_executable": sys.executable, "project_root": str(project)},
                   "component": {"kind": "artifact-op", "id": "t.probe", "release": "1.0.0", "entry": "m:f"},
                   "isolation_profile": profile, "config": {}, "limits": {}},
          "inputs": {}}
    ctx = {"job_id": "j1", "attempt_id": "j1_a1", "epoch": 1, "attempt_dir": str(lab / "jobs" / "j1" / "j1_a1")}
    return runner._prepare(st, ctx), lab


def test_sealed_job_spec_denies_the_home_directory(tmp_path):
    spec, lab = _job_spec(tmp_path, tmp_path / "project")
    assert str(Path.home()) in spec.deny_read and str(lab) in spec.deny_read
    assert spec.deny_read == sealed_deny_read(str(lab))
    same_user, _ = _job_spec(tmp_path, tmp_path / "project", profile="dev:same-user")
    assert same_user.deny_read == []


READ = r"""
import json, os, sys
cfg = json.loads(sys.argv[1]); r = {}
def ok(fn):
    try:
        fn(); return True
    except OSError:
        return False
r["planted"] = ok(lambda: open(cfg["planted"]).read())
r["home_listing"] = ok(lambda: os.listdir(cfg["home"]))
r["project_file"] = ok(lambda: open(cfg["project_file"]).read())
print(json.dumps(r))
"""


@mac_sealed
@pytest.mark.parametrize("project_in_home", [False, True])
def test_sealed_job_cannot_read_home_but_reads_its_project(tmp_path, project_in_home):
    home = Path.home()
    planted = home / f".eidos-planted-{os.getpid()}"
    project = (home / f".eidos-test-project-{os.getpid()}") if project_in_home else tmp_path / "project"
    project.mkdir()
    (project / "data.txt").write_text("project data")
    planted.write_text("secret")
    try:
        spec, _ = _job_spec(tmp_path, project)
        spec.argv = [sys.executable, "-I", "-c", READ, json.dumps(
            {"planted": str(planted), "home": str(home), "project_file": str(project / "data.txt")})]
        h = launch(spec)
        deadline = time.monotonic() + 60
        while h.exit_code() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        out = (Path(spec.attempt_dir) / "stdout.log").read_text().strip().splitlines()
        assert h.exit_code() == 0 and out, (Path(spec.attempt_dir) / "stderr.log").read_text()[-1500:]
        assert json.loads(out[-1]) == {"planted": False, "home_listing": False, "project_file": True}
    finally:
        planted.unlink(missing_ok=True)
        if project_in_home:
            (project / "data.txt").unlink(missing_ok=True)
            project.rmdir()


@mac_sealed
def test_credential_probe_covers_the_home_directory(tmp_path):
    spec, lab = _job_spec(tmp_path, tmp_path / "project")
    (tmp_path / "project").mkdir(exist_ok=True)
    (lab / "secrets").mkdir()
    cred = lab / "secrets" / "t"
    cred.write_text("c")
    canary = lab / "canary"
    canary.write_text("l")
    kw = dict(label_path=str(canary), credential_path=str(cred), host_write_path=str(lab / "hw"), api_port=1)
    assert run_probes(spec, **kw)["controls"]["no-credential-read"] == "passed"
    # the same probe without the home directory denied (the pre-fix configuration) must fail it
    spec.deny_read = [d for d in spec.deny_read if d != str(Path.home())]
    spec.attempt_dir = str(tmp_path / "pre-fix")
    assert run_probes(spec, **kw)["controls"]["no-credential-read"] == "failed"
