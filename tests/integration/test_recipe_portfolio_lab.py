"""Published portfolio bundles execute through the real ledger and worker runner."""

import json

import pytest
import test_diagnose_and_template_sweep_lab as template_lab
from test_command_path import _publish
from test_diagnose_and_template_sweep_lab import ROOT, RUN, _bundle, _output, _run

from eidos.cli.main import bundle_dir
from eidos.domain.errors import ResolutionError

scene = template_lab.scene


@pytest.mark.pg
@pytest.mark.parametrize("kind", ["classification", "regression"])
def test_portfolio_table_oof_and_generated_source_are_real_job_outputs(scene, kind, tmp_path, study_arm4):
    name = "eidos.search.recipe-portfolio"
    scene["refs"][name] = _publish(scene["lab"], scene["pid"], *_bundle(name),
                                   bundle_bytes=bundle_dir(ROOT / "components" / name))["component_ref"]
    lab, pid = scene["lab"], scene["pid"]
    charter = lab.cmd(lab.admin, "commit_study_charter", {"charter": {
        "study_id": pid, "question": "visible portfolio export", "exploration_only": True,
    }}, project=pid)["result"]["charter_id"]
    inv = lab.cmd(lab.admin, "start_investigation", {"question": "visible portfolio export", "charter_id": charter},
                  project=pid)["result"]["investigation_id"]
    job = _run(scene, name, dict(mode="portfolio", task_type=kind, include=["linear", "neighbors"],
                                 scheme="cv-train", cv_folds=3, cv_repeats=1, budget=dict(max_seconds=60)), investigation=inv)
    assert job["status"] == "succeeded", job.get("terminal")
    table = _output(scene, job, "portfolio_table")
    oof = _output(scene, job, "oof")
    source = _output(scene, job, "promotion_source")
    assert table["coverage_complete"] and oof["recipes"]
    assert table["library_digest"] == source["library_digest"] == oof["library_digest"]
    assert table["selection"]["basis"] == source["basis"] == "one-se-choice"
    assert source["claim"] is None and source["exploratory"] is True
    assert source["files"]["project.py"] == (ROOT / "components" / name / "templates/project.py").read_text()
    assert json.loads(source["files"]["recipe.json"])["task_type"] == kind
    assert job["attempts"][-1]["receipt"]["metrics"]["round_evaluated"] == 1.
    from test_turn_recovery import ShimClient
    # Changing manifest dependencies changes the immutable bundle pin too. Exercise
    # the real study export so a stale pin cannot silently break treatment runs.
    reference = next(o["artifact_id"] for o in job["outputs"] if o["slot"] == "portfolio_table")
    entry, mapping = study_arm4.export_sweep_evidence(ShimClient(lab, lab.admin), pid, inv, tmp_path,
                                                     reference, scene["arts"])
    assert entry["sha256"] == mapping["sha256"]
    assert json.loads((tmp_path / entry["path"]).read_bytes()) == table


@pytest.mark.pg
def test_portfolio_mode_is_checked_at_acceptance(scene):
    name = "eidos.search.recipe-portfolio"
    scene["refs"][name] = _publish(scene["lab"], scene["pid"], *_bundle(name),
                                   bundle_bytes=bundle_dir(ROOT / "components" / name))["component_ref"]
    with pytest.raises(ResolutionError):
        scene["lab"].cmd(scene["lab"].admin, "run_component", {
            "component_ref": scene["refs"][name], **RUN, "config": {"mode": "confirm"},
            "inputs": dict(scene["arts"]),
        }, project=scene["pid"])


@pytest.mark.pg
def test_missing_portfolio_dependencies_refuse_before_validation_fixtures(lab):
    import sys

    from eidos.domain.errors import PreflightRefused
    from eidos.runtime.env_probe import probe_environment

    pid = template_lab.project(lab)
    # Probe the torch/numpy/sklearn-free lab interpreter, never the project environment.
    probed = probe_environment(sys.executable)
    lab.cmd(lab.admin, "register_environment", dict(name="missing-science", python_executable=sys.executable,
            project_root=str(ROOT), **probed), project=pid)
    digest, size = lab.store.put_bytes(bundle_dir(ROOT / "components/eidos.search.recipe-portfolio"))
    art = lab.cmd(lab.admin, "register_artifact", dict(digest=digest, size=size, schema="source-bundle/1"), project=pid)["result"]["artifact_id"]
    with pytest.raises(PreflightRefused, match="missing or incompatible"):
        lab.cmd(lab.admin, "validate_component", dict(bundle_artifact_id=art, environment="missing-science"), project=pid)
    assert not lab.q(lab.admin, "list_jobs", {}, project=pid)
