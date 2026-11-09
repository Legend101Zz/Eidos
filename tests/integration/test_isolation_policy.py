"""Candidate code defaults to the strongest available isolation profile; a weaker profile needs an
explicit, recorded waiver (review item: investigator-authored code defaulted to same-user)."""

from __future__ import annotations

import sys

import pytest

from eidos.domain.errors import Invalid

pytestmark = pytest.mark.pg


def _setup(lab):
    from test_command_path import _publish, project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}}, project=pid)
    ref = _publish(lab, pid, {"id": "t.op", "release": "1.0.0", "implements": "op/1", "kind": "artifact-op",
                              "entry": "m:run", "scientific_effects": ["observe"]}, {"m.py": "x=1\n"})["component_ref"]
    return pid, ref


def test_default_is_strongest_and_weaker_needs_recorded_waiver(lab):
    lab.svc.settings["sealed_available"] = True
    try:
        pid, ref = _setup(lab)
        inv = lab.principal(f"inv-{pid}", ["investigator"], [pid])
        lock = lab.cmd(inv, "plan_trial", {"component_ref": ref, "environment": "lab"}, project=pid)["result"]["lock"]
        assert lock["isolation_profile"] == "linux:bwrap-sealed" and not lock["isolation_choice"]["explicit"]
        with pytest.raises(Invalid):
            lab.cmd(inv, "plan_trial", {"component_ref": ref, "environment": "lab", "isolation_profile": "dev:same-user"},
                    project=pid)
        lock = lab.cmd(inv, "plan_trial", {"component_ref": ref, "environment": "lab", "isolation_profile": "dev:same-user",
                                           "isolation_waiver": "debugging an import error interactively"},
                       project=pid)["result"]["lock"]
        choice = lock["isolation_choice"]
        assert choice["profile"] == "dev:same-user" and choice["waiver"] and choice["requested_by"] == inv.id
        assert choice["strongest_available"] == "linux:bwrap-sealed"
    finally:
        lab.svc.settings.pop("sealed_available", None)


def test_platform_without_sealed_profile_records_that_same_user_is_the_strongest(lab):
    pid, ref = _setup(lab)
    lock = lab.cmd(lab.admin, "plan_trial", {"component_ref": ref, "environment": "lab"}, project=pid)["result"]["lock"]
    assert lock["isolation_profile"] == "dev:same-user" and lock["isolation_choice"]["note"]
