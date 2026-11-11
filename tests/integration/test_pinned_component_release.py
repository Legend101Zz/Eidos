"""A fresh project that installs the built-in components can still run ``eidos.search.recipe-sweep@1.0.0`` (R16 finding 3).

Release 1.1.0 added typed templates; ``components/eidos.search.recipe-sweep@1.0.0/`` keeps the 1.0.0 release, byte for
byte, beside it. Every built-in directory whose manifest is the sweep is published into a new project as the CLI installs
it (``bundle_dir``), and a job pinned to ``@1.0.0`` then resolves to 1.0.0's own contract and code and runs through the
real runner. The pinned bundle's bytes are checked in ``tests/components/test_pinned_releases.py``.

H2 (R17 #4): the pinned 1.0.0 predates 1.1.0's stricter held-out file-name rule and its bytes cannot change, so the lab
refuses, at resolution, a 1.0.0 job whose ``command.copy`` or source bundle names a data file after the held-out split
(``domain.release_guards``). 1.1.0 jobs are not subject to the lab guard: that component refuses such names itself.

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


# -- the lab's guard for the pinned release (H2, R17 #4) -------------------------------------------------------------
PINNED = f"{SWEEP}@1.0.0"
CURRENT = f"{SWEEP}@1.1.0"
GUARD = "release-guard/1"
TABLE = "a,b\n1,2\n"


def _config(**command) -> dict:
    return {**CONFIG, "command": {**CONFIG["command"], **command}}


def _bundle(fresh, files: dict[str, str]) -> str:
    """Register a source-bundle/1 artifact (``{"files": {path: text}}``) and return its id."""
    lab = fresh["lab"]
    digest, size = lab.store.put_bytes(json.dumps({"files": files}).encode())
    return lab.cmd(lab.admin, "register_artifact", {"digest": digest, "size": size, "schema": "source-bundle/1",
                                                    "media_type": "application/vnd.eidos.source-bundle+json"},
                   project=fresh["pid"])["result"]["artifact_id"]


def _run(fresh, ref: str, config: dict, inputs: dict | None = None) -> dict:
    lab = fresh["lab"]
    return lab.cmd(lab.admin, "run_component", {"component_ref": ref, **RUN, "config": config, "inputs": inputs or {}},
                   project=fresh["pid"])["result"]


def _job_ids(fresh) -> set[str]:
    """Every job of the project (publishing a component admits its own qualification jobs, so the set is not empty)."""
    return {j["id"] for j in fresh["lab"].q(fresh["lab"].admin, "list_jobs", {}, project=fresh["pid"])}


def _guard_refusal(fresh, ref: str, config: dict, inputs: dict | None = None):
    """The ResolutionError a refused invocation raises; asserts that no job was admitted by the attempt."""
    before = _job_ids(fresh)
    with pytest.raises(ResolutionError) as refused:
        _run(fresh, ref, config, inputs)
    assert _job_ids(fresh) == before, "nothing is admitted"
    return refused.value


@pytest.mark.parametrize("name", ["test1.csv", "testfeatures.csv", "x_test2.npy", "holdout3.npz"])
def test_the_lab_refuses_a_pinned_1_0_0_copy_list_that_names_held_out_data(fresh, name):
    """1.0.0 checks held-out names only at word boundaries, so these passed its own check; the lab refuses them."""
    refused = _guard_refusal(fresh, PINNED, _config(copy=["train.csv", name]))
    assert refused.details["guard"] == GUARD
    assert refused.details["consumer"] == PINNED
    assert len(refused.details["problems"]) == 1 and repr(name) in refused.details["problems"][0]
    assert "held-out" in refused.message and PINNED in refused.message
    assert refused.next_action


def test_the_lab_guard_also_applies_beside_a_source_bundle(fresh):
    """``command.copy`` is refused whether or not a source bundle is bound: the listing itself is the refusal."""
    refused = _guard_refusal(fresh, PINNED, _config(copy=["test1.csv"]), {"source": fresh["source"]})
    assert refused.details["guard"] == GUARD and "'test1.csv'" in refused.details["problems"][0]


def test_the_lab_refuses_a_pinned_1_0_0_source_bundle_with_held_out_named_data(fresh):
    source = _bundle(fresh, {"fit.py": FIT_PY, "testfeatures.csv": TABLE, "data/train.csv": TABLE})
    refused = _guard_refusal(fresh, PINNED, CONFIG, {"source": source})
    assert refused.details["guard"] == GUARD and refused.details["consumer"] == PINNED
    assert len(refused.details["problems"]) == 1
    assert "source bundle file 'testfeatures.csv'" in refused.details["problems"][0]


def test_the_lab_names_every_problem_in_a_pinned_1_0_0_invocation(fresh):
    source = _bundle(fresh, {"fit.py": FIT_PY, "tests/test1.csv": TABLE, "x_test2.npy": TABLE})
    refused = _guard_refusal(fresh, PINNED, _config(copy=["holdout3.npz"]), {"source": source})
    problems = refused.details["problems"]
    assert len(problems) == 3, problems
    assert [p.split(" ")[0] for p in problems] == ["command.copy", "source", "source"]


def test_the_lab_guard_leaves_a_pinned_1_0_0_job_with_ordinary_names_alone(fresh):
    lab, pid = fresh["lab"], fresh["pid"]
    source = _bundle(fresh, {"fit.py": FIT_PY, "data/train.csv": TABLE, "latest.csv": TABLE, "contest.csv": TABLE,
                             "attestation.json": "{}", "test_model.py": "# code may carry a held-out name\n"})
    before = _job_ids(fresh)
    admitted = _run(fresh, PINNED, _config(copy=["train.csv", "latest.csv"]), {"source": source})
    assert admitted["component"] == PINNED
    assert _job_ids(fresh) == before | {admitted["job_id"]}
    assert lab.q(lab.admin, "get_job", {"job_id": admitted["job_id"]}, project=pid)["lock"]["component"]["ref"] == PINNED


def test_the_lab_guard_leaves_a_pinned_1_0_0_copy_list_of_ordinary_files_alone(fresh):
    admitted = _run(fresh, PINNED, _config(copy=["train.csv", "fit.py"]))
    assert admitted["component"] == PINNED and admitted["job_id"]


def test_a_pinned_1_0_0_job_that_the_guard_accepts_still_runs(fresh):
    """The guard is a refusal at the boundary only: a sound pinned job (here with plain data and a test-named script in
    its bundle) runs through the real runner as before."""
    lab, pid = fresh["lab"], fresh["pid"]
    source = _bundle(fresh, {"fit.py": FIT_PY, "train.csv": TABLE, "test_model.py": "# code\n"})
    job_id = _run(fresh, PINNED, CONFIG, {"source": source})["job_id"]
    fresh["runner"].run_job(job_id, pid, activity_attempt=1)
    job = lab.q(lab.admin, "get_job", {"job_id": job_id}, project=pid)
    assert job["status"] == "succeeded", job.get("terminal")


def test_a_source_bundle_that_does_not_parse_is_not_refused_by_the_guard(fresh):
    """The guard reads names, not bytes: a bundle it cannot read has no names for it to refuse (the component's own
    run reports the unusable bundle)."""
    lab = fresh["lab"]
    digest, size = lab.store.put_bytes(b"not json")
    broken = lab.cmd(lab.admin, "register_artifact", {"digest": digest, "size": size, "schema": "source-bundle/1",
                                                      "media_type": "application/vnd.eidos.source-bundle+json"},
                     project=fresh["pid"])["result"]["artifact_id"]
    assert _run(fresh, PINNED, CONFIG, {"source": broken})["component"] == PINNED


@pytest.mark.parametrize("files", [{"fit.py": FIT_PY, "testfeatures.csv": TABLE}, {"fit.py": FIT_PY, "tests/test1.csv": TABLE}])
def test_a_1_1_0_job_is_not_subject_to_the_lab_guard(fresh, files):
    """1.1.0 refuses such names itself, when it runs; the lab adds no refusal for it at resolution."""
    before = _job_ids(fresh)
    admitted = _run(fresh, CURRENT, CONFIG, {"source": _bundle(fresh, files)})
    assert admitted["component"] == CURRENT and admitted["job_id"]
    assert _job_ids(fresh) == before | {admitted["job_id"]}


def test_a_1_1_0_copy_list_naming_held_out_data_is_admitted_then_refused_by_the_component(fresh):
    lab, pid = fresh["lab"], fresh["pid"]
    job_id = _run(fresh, CURRENT, _config(copy=["test1.csv"]))["job_id"]
    fresh["runner"].run_job(job_id, pid, activity_attempt=1)
    job = lab.q(lab.admin, "get_job", {"job_id": job_id}, project=pid)
    assert job["status"] == "failed", job.get("terminal")
    assert "held-out" in json.dumps(job.get("terminal"))
