"""Training extensions bind as published releases: pinned by digest in the run lock, bundle mounted
by the launcher, hook phases/writers validated before admission."""

from __future__ import annotations

import sys

import pytest

from eidos.domain.errors import IncompatibleHook, Invalid

pytestmark = pytest.mark.pg


def _setup(lab):
    from test_command_path import _publish, project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}}, project=pid)
    _publish(lab, pid, {"id": "t.trainer", "release": "1.0.0", "implements": "trainer/1", "kind": "artifact-op",
                        "entry": "m:run", "scientific_effects": ["change-weights"]}, {"m.py": "def run(i, c): return {}\n"})
    for name, writes in (("ema", ["param.grad", "state:ema/1"]), ("clip", ["param.grad"])):
        _publish(lab, pid, {"id": f"t.{name}", "release": "1.0.0", "implements": f"{name}-rule/1",
                            "kind": "training-extension", "entry": f"{name}:create", "phases": ["before-optimizer-step"],
                            "scientific_effects": ["change-learning"], "writes": writes},
                 {f"{name}.py": "def create(cfg, ctx): return object()\n"})
    return pid


def test_published_extension_is_pinned_and_mounted(lab):
    pid = _setup(lab)
    r = lab.cmd(lab.admin, "plan_trial", {"component_ref": "t.trainer@1.0.0", "environment": "lab",
                                          "config": {"extensions": [{"component_ref": "t.ema@1.0.0", "config": {"decay": 0.9}}]}},
                project=pid)["result"]
    lock = r["lock"]
    assert lock["extensions"][0]["ref"] == "t.ema@1.0.0" and lock["extensions"][0]["pinned"]
    assert lock["config"]["extensions"][0]["spec"] == "ema:create"
    assert lock["config"]["extensions"][0]["phases"] == ["before-optimizer-step"]
    assert "ext-0" in lock["extra_bundles"]


def test_conflicting_extension_writers_fail_resolution(lab):
    pid = _setup(lab)
    with pytest.raises(IncompatibleHook):
        lab.cmd(lab.admin, "plan_trial", {"component_ref": "t.trainer@1.0.0", "environment": "lab", "config": {
            "extensions": [{"component_ref": "t.ema@1.0.0"}, {"component_ref": "t.clip@1.0.0"}]}}, project=pid)


def test_retired_extension_cannot_bind_new_runs(lab):
    pid = _setup(lab)
    lab.cmd(lab.admin, "retire_component", {"component_ref": "t.ema@1.0.0"}, project=pid)
    with pytest.raises(Invalid):
        lab.cmd(lab.admin, "plan_trial", {"component_ref": "t.trainer@1.0.0", "environment": "lab",
                                          "config": {"extensions": [{"component_ref": "t.ema@1.0.0"}]}}, project=pid)
