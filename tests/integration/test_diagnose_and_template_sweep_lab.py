"""ADR 0024 through the lab: ``eidos.diagnose.baseline-errors`` and a ``eidos.search.recipe-sweep@1.1.0`` template run as
ordinary jobs (published bundles, the real runner, a registered numpy environment), and the sweep's config contract
refuses a config that is neither a template nor a command sweep at acceptance.

Author tests. They need an interpreter with numpy (``EIDOS_NUMPY_PYTHON``; CI's integration job provides one) and skip
without it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_command_path import _publish, _register_lab_env, project
from test_turn_recovery import ShimClient

from eidos.cli.main import bundle_dir
from eidos.runtime.runner import AttemptRunner

ROOT = Path(__file__).resolve().parents[2]
NUMPY_PY = os.environ.get("EIDOS_NUMPY_PYTHON", "")
pytestmark = [pytest.mark.pg, pytest.mark.skipif(not (NUMPY_PY and Path(NUMPY_PY).exists()),
                                reason="set EIDOS_NUMPY_PYTHON to an interpreter with numpy")]
RUN = {"environment": "numpy", "isolation_profile": "dev:same-user"}

# Image-as-vector data with a planted per-example additive offset in validation only.
GENERATE = """
import io, sys, numpy as np
rng = np.random.default_rng(5)
protos = rng.normal(size=(4, 64))
protos = np.stack([np.convolve(p, np.ones(3) / 3, "same") for p in protos])
def gen(n):
    y = rng.integers(0, 4, n)
    return protos[y] + rng.normal(size=(n, 64)), y
xt, yt = gen(240)
xv, yv = gen(120)
xv = xv + rng.uniform(1.0, 3.0, size=(120, 1))
np.savez(sys.argv[1] + "/train.npz", x=xt, y=yt, ids=np.arange(240))
np.savez(sys.argv[1] + "/validation.npz", x=xv, y=yv, ids=np.arange(1000, 1120))
"""


def _bundle(name: str) -> tuple[dict, dict]:
    """A built-in component as the CLI installs it (binary fixtures base64), split into manifest and files."""
    files = json.loads(bundle_dir(ROOT / "components" / name))["files"]
    return json.loads(files.pop("eidos-component.json")), files


@pytest.fixture()
def scene(lab, tmp_path):
    pid = project(lab)
    _register_lab_env(lab, pid)  # publication's (synthetic) validation job names the lab environment
    lab.cmd(lab.admin, "register_environment", {"name": "numpy", "python_executable": NUMPY_PY,
                                                "project_root": str(tmp_path), "python_version": "3.12", "installed": {}},
            project=pid)
    refs = {name: _publish(lab, pid, *_bundle(name), bundle_bytes=bundle_dir(ROOT / "components" / name))["component_ref"]
            for name in ("eidos.diagnose.baseline-errors", "eidos.search.recipe-sweep")}
    subprocess.run([NUMPY_PY, "-c", GENERATE, str(tmp_path)], check=True, timeout=120)
    arts = {}
    for split in ("train", "validation"):
        digest, size = lab.store.put_bytes((tmp_path / f"{split}.npz").read_bytes())
        arts[split] = lab.cmd(lab.admin, "register_artifact", {"digest": digest, "size": size, "schema": "task-array/1",
                                                               "media_type": "application/x-npz"},
                              project=pid)["result"]["artifact_id"]
    launcher = lab.principal(f"svc-launcher-{pid}", ["launcher"], [pid])
    root = tmp_path / "labroot"
    runner = AttemptRunner(ShimClient(lab, launcher), lab.store, lab_root=root, launcher_id=launcher.id, api_port=1,
                           credential_path=str(root / "config" / "secrets" / "launcher.token"))
    return {"lab": lab, "pid": pid, "refs": refs, "arts": arts, "runner": runner}


def _run(scene, name: str, config: dict, *, investigation: str | None = None) -> dict:
    lab, pid = scene["lab"], scene["pid"]
    job_id = lab.cmd(lab.admin, "run_component", {"component_ref": scene["refs"][name], **RUN, "config": config,
                                                  "inputs": dict(scene["arts"]),
                                                  **({"investigation_id": investigation} if investigation else {})}, project=pid)["result"]["job_id"]
    scene["runner"].run_job(job_id, pid, activity_attempt=1)
    return lab.q(lab.admin, "get_job", {"job_id": job_id}, project=pid)


def _output(scene, job: dict, slot: str):
    out = next(o for o in job["outputs"] if o["slot"] == slot)
    return json.load(open(scene["lab"].store.path_for(out["digest"])))


def test_the_diagnosis_runs_as_a_lab_job_and_ranks_the_planted_offset_first(scene):
    job = _run(scene, "eidos.diagnose.baseline-errors", {"seed": 3})
    assert job["status"] == "succeeded", job.get("terminal")
    report = _output(scene, job, "report")
    assert report["schema"] == "baseline-diagnosis/1" and report["exploratory"] is True and report["claim"] is None
    assert report["hypotheses"][0]["cause"] == "per-example-additive-offset"
    assert set(report["provenance"]["inputs"]) == {"train", "validation"}


def test_a_template_sweep_runs_as_a_lab_job_in_one_call(scene):
    job = _run(scene, "eidos.search.recipe-sweep", {"template": {
        "name": "quick", "task_type": "classification", "families": ["linear", "knn"], "seeds": [7]}})
    assert job["status"] == "succeeded", job.get("terminal")
    table, best = _output(scene, job, "evidence_table"), _output(scene, job, "best_recipe")
    assert table["counts"]["evaluated"] == 8 and table["counts"]["usable"] == 8
    assert table["counts"]["finalists_cross_validated"] == 3
    assert best["status"] == "found"
    assert best["recipe"]["parameters"]["preprocess"] in ("per_example_center", "per_example_standardize")


def test_a_config_that_is_neither_a_template_nor_a_command_sweep_is_refused_at_acceptance(scene):
    from eidos.domain.errors import ResolutionError

    lab, pid = scene["lab"], scene["pid"]
    with pytest.raises(ResolutionError) as refused:
        lab.cmd(lab.admin, "run_component", {"component_ref": scene["refs"]["eidos.search.recipe-sweep"], **RUN,
                                             "config": {"stopping": {"rule": "none"}}, "inputs": dict(scene["arts"])},
                project=pid)
    problems = refused.value.details["problems"]
    assert any("matches none of its alternatives" in problem for problem in problems), problems



def test_a_real_sweep_publication_is_exportable_only_as_visible_study_evidence(scene, tmp_path, study_arm4):

    lab, pid = scene["lab"], scene["pid"]
    charter = lab.cmd(lab.admin, "commit_study_charter", {"charter": {
        "study_id": "visible-sweep", "question": "Which recipe is promising?", "exploration_only": True,
    }}, project=pid)["result"]["charter_id"]
    inv = lab.cmd(lab.admin, "start_investigation", {
        "question": "Compare recipes on visible validation.", "charter_id": charter,
    }, project=pid)["result"]["investigation_id"]
    job = _run(scene, "eidos.search.recipe-sweep", {"template": {
        "name": "quick", "task_type": "classification", "families": ["linear"], "seeds": [7],
    }}, investigation=inv)
    assert job["status"] == "succeeded", job.get("terminal")
    publication = next(row for row in job["outputs"] if row["slot"] == "evidence_table")
    client = ShimClient(lab, lab.admin)
    entry, mapping = study_arm4.export_sweep_evidence(client, pid, inv, tmp_path, publication["artifact_id"], scene["arts"])
    assert entry["sha256"] == publication["digest"] == mapping["sha256"]
    assert (tmp_path / entry["path"]).read_bytes() == Path(lab.store.path_for(entry["sha256"])).read_bytes()
