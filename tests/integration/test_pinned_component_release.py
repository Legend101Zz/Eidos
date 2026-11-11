"""A fresh project that installs the built-in components can still run ``eidos.search.recipe-sweep@1.0.0`` (R16 finding 3).

Release 1.1.0 added typed templates; ``components/eidos.search.recipe-sweep@1.0.0/`` keeps the 1.0.0 release, byte for
byte, beside it. Every built-in directory whose manifest is the sweep is published into a new project as the CLI installs
it (``bundle_dir``), and a job pinned to ``@1.0.0`` then resolves to 1.0.0's own contract and code and runs through the
real runner. The pinned bundle's bytes are checked in ``tests/components/test_pinned_releases.py``.

Author tests (PostgreSQL).
"""

from __future__ import annotations

import json
import sys

import pytest
from test_command_path import _publish, _register_lab_env, project
from test_turn_recovery import ShimClient

from eidos.cli.main import builtin_component_dirs, bundle_dir
from eidos.domain.errors import ResolutionError
from eidos.runtime.runner import AttemptRunner

pytestmark = pytest.mark.pg
SWEEP = "eidos.search.recipe-sweep"
RUN = {"environment": "lab", "isolation_profile": "dev:same-user"}
FIT_PY = ("import json, sys\n"
          "a = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
          "json.dump({'validation_accuracy': 1.0 - abs(float(a['--lr']) - 0.1)}, open(a['--out'] + '/receipt.json', 'w'))\n")
CONFIG = {"command": {"argv": [sys.executable, "fit.py", "--out", "{out}", "--lr", "{param.lr}"]},
          "space": {"mode": "grid", "parameters": {"lr": {"values": [0.5, 0.1, 0.01]}}},
          "objective": {"metric": "validation_accuracy", "direction": "maximize"}, "budget": {"max_trials": 10}}


@pytest.fixture()
def fresh(lab, tmp_path):
    pid = project(lab)
    _register_lab_env(lab, pid)
    refs = []
    for directory in builtin_component_dirs():
        files = json.loads(bundle_dir(directory))["files"]
        manifest = json.loads(files.pop("eidos-component.json"))
        if manifest["id"] == SWEEP:
            refs.append(_publish(lab, pid, manifest, files)["component_ref"])
    digest, size = lab.store.put_bytes(json.dumps({"files": {"fit.py": FIT_PY}}).encode())
    source = lab.cmd(lab.admin, "register_artifact", {"digest": digest, "size": size, "schema": "source-bundle/1",
                                                      "media_type": "application/vnd.eidos.source-bundle+json"},
                     project=pid)["result"]["artifact_id"]
    launcher = lab.principal(f"svc-launcher-{pid}", ["launcher"], [pid])
    root = tmp_path / "labroot"
    runner = AttemptRunner(ShimClient(lab, launcher), lab.store, lab_root=root, launcher_id=launcher.id, api_port=1,
                           credential_path=str(root / "config" / "secrets" / "launcher.token"))
    return {"lab": lab, "pid": pid, "refs": refs, "source": source, "runner": runner}


def _submit(fresh, ref: str, config: dict) -> str:
    lab = fresh["lab"]
    return lab.cmd(lab.admin, "run_component", {"component_ref": ref, **RUN, "config": config,
                                                "inputs": {"source": fresh["source"]}},
                   project=fresh["pid"])["result"]["job_id"]


def test_both_sweep_releases_install_into_a_fresh_project(fresh):
    assert sorted(fresh["refs"]) == [f"{SWEEP}@1.0.0", f"{SWEEP}@1.1.0"]


@pytest.mark.parametrize("release", ["1.0.0", "1.1.0"])
def test_a_job_pinned_to_either_release_runs_that_release(fresh, release):
    lab, pid = fresh["lab"], fresh["pid"]
    job_id = _submit(fresh, f"{SWEEP}@{release}", CONFIG)
    fresh["runner"].run_job(job_id, pid, activity_attempt=1)
    job = lab.q(lab.admin, "get_job", {"job_id": job_id}, project=pid)
    assert job["status"] == "succeeded", job.get("terminal")
    assert job["lock"]["component"]["ref"] == f"{SWEEP}@{release}"
    best = next(o for o in job["outputs"] if o["slot"] == "best_recipe")
    assert json.load(open(lab.store.path_for(best["digest"])))["recipe"]["parameters"] == {"lr": 0.1}


def test_the_pinned_ref_keeps_its_own_contract(fresh):
    """1.0.0 predates typed templates: its config contract still requires a command sweep, while 1.1.0 accepts a
    template. The pinned reference is not silently upgraded."""
    template = {"template": {"name": "quick"}}
    with pytest.raises(ResolutionError):
        _submit(fresh, f"{SWEEP}@1.0.0", template)
    assert _submit(fresh, f"{SWEEP}@1.1.0", template)
