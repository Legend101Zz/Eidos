"""Every shipped manifest parses, points at real files, and its fixtures pass the real protocol."""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import COMPONENTS, SDK_PATH
from eidos_wire import load_manifest
from eidos_worker.validate import validate_component

ALL = sorted(p.parent for p in COMPONENTS.glob("*/eidos-component.json"))
TORCH = {
    "eidos.pytorch.train",
    "eidos.pytorch.evaluate",
    "eidos.pytorch.weight-edit",
    "eidos.pytorch.behavior-check",
}
LAB = {"eidos.assessors.paired-ci", "eidos.qualification-runner"}
NUMPY = {"eidos.task.score-classification", "eidos.task.score-regression",
         "eidos.diagnose.baseline-errors"}  # need numpy (project env)


def test_ten_components_present():
    assert len(ALL) >= 10


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_manifest_is_valid_and_complete(path):
    m = load_manifest(path / "eidos-component.json")
    assert m.id == path.name
    assert (path / m.card).is_file()
    module = m.entry.split(":")[0].split(".")
    assert (path / Path(*module)).with_suffix(".py").is_file()
    for control in m.controls:
        assert (path / control).is_file(), control
    if m.kind != "training-extension":
        assert m.controls, "every runnable component ships fixtures"


@pytest.mark.parametrize("name", sorted(NUMPY))
def test_numpy_component_fixtures(name):
    import os
    import subprocess
    py = os.environ.get("EIDOS_NUMPY_PYTHON")
    if not py:
        pytest.skip("set EIDOS_NUMPY_PYTHON to an interpreter with numpy")
    r = subprocess.run([py, "-m", "eidos_worker.validate", str(COMPONENTS / name)], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": SDK_PATH}, timeout=240)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr


@pytest.mark.parametrize("path", [p for p in ALL if p.name not in TORCH | NUMPY], ids=lambda p: p.name)
def test_fixtures_pass_in_lab_environment(path):
    report = validate_component(path, timeout=240)
    assert report.manifest_problems == []
    assert report.ok, [(f.path, f.problems) for f in report.fixtures if not f.passed]
    assert all(f.passed for f in report.fixtures), "no fixture may be skipped"


@pytest.mark.parametrize("name", sorted(TORCH))
def test_fixtures_pass_in_project_environment(name, torch_py):
    import os

    env = dict(os.environ, PYTHONPATH=SDK_PATH)
    proc = subprocess.run(
        [torch_py, "-m", "eidos_worker.validate", str(COMPONENTS / name)],
        capture_output=True,
        text=True,
        env=env,
        timeout=280,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr[-1500:]
    assert "SKIP" not in proc.stdout and proc.stdout.strip().endswith("ok")


def test_group_ema_author_test(torch_py):
    proc = subprocess.run(
        [torch_py, str(COMPONENTS / "lab.learning.group-balanced-ema" / "fixtures" / "test_group_ema.py")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr[-1500:]
    assert proc.stdout.count("ok test_") == 4


def test_fixtures_are_labelled_author_tests():
    for path in ALL:
        for f in (path / "fixtures").glob("*.json"):
            assert json.loads(f.read_text()).get("author_test") is True, f
