"""Earlier releases stay installable so pinned references resolve (R16 finding 3, ADR 0024).

``components/<id>/`` holds the current release; ``components/<id>@<release>/`` holds an earlier release, byte for byte as
it shipped. A fresh installation therefore has ``eidos.search.recipe-sweep@1.0.0`` as well as ``@1.1.0``. These author
tests pin the 1.0.0 bundle's bytes, check that both releases install from the built-in directories, and run each release
through the standalone toolkit, where both share a module name. The lab path (publish, then run by the pinned ref in a
fresh project) is ``tests/integration/test_pinned_component_release.py``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from conftest import COMPONENTS, ROOT, run_component
from eidos.cli.main import builtin_component_dirs, bundle_dir
from eidos_wire import load_manifest

SWEEP = "eidos.search.recipe-sweep"
PINNED = f"{SWEEP}@1.0.0"
# sha256 of the source bundle (``eidos.cli.main.bundle_dir``) of eidos.search.recipe-sweep as released at 1.0.0 (main
# 7e6641d): the bytes a project that pinned 1.0.0 published. A change to that directory is a different release.
PINNED_BUNDLE_SHA256 = "8cf11f80f69a054e8500a26ef3bc26456bbc990ea024531481b84a9110c13623"
FIT = ("import json, sys\na = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
       "json.dump({'validation_accuracy': 1.0 - abs(float(a['--lr']) - 0.1)}, open(a['--out'] + '/receipt.json', 'w'))\n")


def grid_config(seeds=None):
    config = {"command": {"argv": ["python", "fit.py", "--out", "{out}", "--lr", "{param.lr}"]},
              "space": {"mode": "grid", "parameters": {"lr": {"values": [0.5, 0.1]}}},
              "objective": {"metric": "validation_accuracy"}, "budget": {"max_trials": 10}}
    if seeds is not None:
        config["seed_policy"] = {"seeds": seeds}
    return config


SOURCE = {"source": ({"files": {"fit.py": FIT}}, "application/json")}


def test_the_pinned_release_bundle_is_byte_identical_to_what_shipped():
    assert hashlib.sha256(bundle_dir(COMPONENTS / PINNED)).hexdigest() == PINNED_BUNDLE_SHA256


def test_a_fresh_installation_ships_both_sweep_releases():
    refs = {}
    for directory in builtin_component_dirs():
        manifest = load_manifest(directory / "eidos-component.json")
        if manifest.id == SWEEP:
            refs[directory.name] = manifest.ref
    assert refs == {SWEEP: f"{SWEEP}@1.1.0", PINNED: PINNED}


def test_every_kept_release_is_older_than_the_current_release_of_its_component():
    kept = [p for p in COMPONENTS.glob("*@*/eidos-component.json")]
    assert kept, "the 1.0.0 sweep is kept"

    def version(text):
        return tuple(int(part) for part in text.split("-")[0].split("."))

    for path in kept:
        old = load_manifest(path)
        assert path.parent.name == old.ref
        current = load_manifest(COMPONENTS / old.id / "eidos-component.json")
        assert version(old.release) < version(current.release), (old.ref, current.ref)


@pytest.mark.parametrize("name, release", [(PINNED, "1.0.0"), (SWEEP, "1.1.0")])
def test_each_release_runs_its_own_code_under_the_lab_runner(tmp_path, name, release):
    manifest = json.loads((COMPONENTS / name / "eidos-component.json").read_text())
    assert manifest["release"] == release
    res = run_component(tmp_path, name, manifest["entry"], inputs=SOURCE, config=grid_config())
    assert res.ok, res.manifest.error
    assert res.receipt["best_recipe_id"] == "r0001-c924a5f4"
    # Each release's own module ran: only 1.1.0 knows typed templates, so 1.0.0 still asks for a command sweep's objective.
    template = run_component(tmp_path, name, manifest["entry"], inputs=SOURCE, config={"template": {"name": "quick"}},
                             label="template")
    assert not template.ok
    message = template.manifest.error["message"]
    assert ("objective.metric is required" in message) == (release == "1.0.0"), message


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory):
    sys.path.insert(0, str(ROOT))
    try:
        from study.toolkit.build_toolkit import build_toolkit
    finally:
        sys.path.remove(str(ROOT))
    return build_toolkit(tmp_path_factory.mktemp("kit"))


def run_toolkit(toolkit, cwd, *args):
    return subprocess.run([sys.executable, "-I", str(toolkit / "run_component.py"), *args],
                          capture_output=True, text=True, timeout=240, cwd=str(cwd), env={"PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize("name", [PINNED, SWEEP])
def test_the_toolkit_lists_and_runs_each_release_with_its_own_module(toolkit, tmp_path, name):
    listed = run_toolkit(toolkit, tmp_path, "--list")
    assert listed.returncode == 0, listed.stderr
    assert name in listed.stdout.split()
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"files": {"fit.py": FIT}}))
    args = {"inputs": {"source": {"file": str(source), "media_type": "application/json"}}, "config": grid_config()}
    proc = run_toolkit(toolkit, tmp_path, name, "--args", json.dumps(args), "--out", str(tmp_path / "out"))
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    manifest = json.loads((tmp_path / "out" / "staging" / "eidos-output-manifest.json").read_text())
    assert manifest["receipt"]["best_recipe_id"] == "r0001-c924a5f4"
    # the release named, not whichever shares its module name on the path: only 1.1.0 knows typed templates
    args["config"] = {"template": {"name": "quick"}}
    run_toolkit(toolkit, tmp_path, name, "--args", json.dumps(args), "--out", str(tmp_path / "template"))
    manifest = json.loads((tmp_path / "template" / "staging" / "eidos-output-manifest.json").read_text())
    assert manifest["status"] == "failed"
    message = manifest["error"]["message"]
    assert ("objective.metric is required" in message) == (name == PINNED), message


def test_baseline_errors_1_0_0_bundle_is_byte_identical_to_what_shipped():
    assert hashlib.sha256(bundle_dir(COMPONENTS / "eidos.diagnose.baseline-errors@1.0.0")).hexdigest() == (
        "82548802d9c50619dd4ce7e0b8418673044f4809fab17272230c8916773a281c")


def test_a_fresh_installation_ships_both_baseline_errors_releases():
    refs = {load_manifest(d / "eidos-component.json").ref for d in builtin_component_dirs()
            if d.name.startswith("eidos.diagnose.baseline-errors")}
    assert refs == {"eidos.diagnose.baseline-errors@1.0.0", "eidos.diagnose.baseline-errors@1.1.0"}
