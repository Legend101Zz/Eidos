"""Probing an interpreter for its capability record (ADR 0020), using the interpreter running the tests."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from eidos.domain.environments import CAPABILITIES_SCHEMA, KEY_MODULES
from eidos.runtime.env_probe import declared_requirements, interpreter_digest, probe_environment

MISSING_PACKAGE = "definitely-not-installed-xyz"


@pytest.fixture(scope="module")
def probe():
    return probe_environment(sys.executable)


@pytest.fixture(scope="module")
def probe_with_missing_requirement():
    return probe_environment(sys.executable, requirements=[MISSING_PACKAGE])


def test_probe_of_the_running_interpreter_succeeds(probe):
    caps = probe["capabilities"]
    assert caps["schema"] == CAPABILITIES_SCHEMA
    assert caps["status"] == "ok", caps.get("error")
    assert "error" not in caps
    assert caps["interpreter"] == sys.executable
    assert caps["python_version"] == probe["python_version"]
    assert probe["python_version"].startswith(f"{sys.version_info.major}.{sys.version_info.minor}")


def test_probe_records_the_interpreter_digest(probe):
    digest = probe["capabilities"]["interpreter_digest"]
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert digest == interpreter_digest(sys.executable)


def test_probe_records_every_key_module_consistently_with_reality(probe):
    modules = probe["capabilities"]["modules"]
    assert set(KEY_MODULES) <= set(modules)
    for name in KEY_MODULES:
        rec = modules[name]
        assert rec["importable"] is (importlib.util.find_spec(name) is not None)
        if not rec["importable"]:
            assert rec["error"]  # the exception type, not a traceback
            assert "\n" not in rec["error"]


def test_probe_records_numpy_importability_matching_the_environment(probe):
    rec = probe["capabilities"]["modules"]["numpy"]
    if importlib.util.find_spec("numpy") is None:
        assert rec["importable"] is False
        assert rec["error"] == "ModuleNotFoundError"
    else:
        assert rec["importable"] is True


def test_probe_returns_the_installed_inventory_and_a_lock_digest(probe):
    installed = probe["installed"]
    assert isinstance(installed, dict) and installed
    assert all(k == k.lower() and "_" not in k for k in installed)  # normalized distribution names
    assert "pytest" in installed
    assert probe["lock_digest"].startswith("sha256:")
    assert len(probe["lock_digest"]) == len("sha256:") + 64
    assert probe["devices"]["cpu"] is True
    assert probe["platform"]


def test_lock_digest_is_stable_for_the_same_environment(probe):
    again = probe_environment(sys.executable)
    assert again["lock_digest"] == probe["lock_digest"]
    assert again["installed"] == probe["installed"]


def test_probe_without_declared_requirements_omits_the_requirements_field(probe):
    assert "requirements" not in probe


def test_declared_requirement_for_a_missing_package_is_recorded_and_not_importable(probe_with_missing_requirement):
    out = probe_with_missing_requirement
    assert out["capabilities"]["status"] == "ok", out["capabilities"].get("error")
    assert out["requirements"] == [MISSING_PACKAGE]
    rec = out["capabilities"]["modules"]["definitely_not_installed_xyz"]
    assert rec["importable"] is False
    assert rec["error"] == "ModuleNotFoundError"
    assert MISSING_PACKAGE.replace("-", "_") not in out["installed"]


def test_declared_requirements_are_probed_alongside_the_key_modules(probe_with_missing_requirement):
    modules = probe_with_missing_requirement["capabilities"]["modules"]
    assert set(KEY_MODULES) <= set(modules)
    assert len(modules) == len(KEY_MODULES) + 1


def test_a_declared_requirement_is_probed_under_its_import_name():
    out = probe_environment(sys.executable, requirements=["scikit-learn>=1.0"])
    assert out["requirements"] == ["scikit-learn>=1.0"]
    modules = out["capabilities"]["modules"]
    assert "sklearn" in modules and "scikit_learn" not in modules
    assert len(modules) == len(KEY_MODULES)  # sklearn is already a key module: probed once


def test_requirements_come_from_the_project_root_and_the_caller_merged(tmp_path):
    (tmp_path / "requirements.txt").write_text("definitely-not-installed-abc>=1.0\n")
    out = probe_environment(sys.executable, project_root=tmp_path, requirements=[MISSING_PACKAGE])
    assert out["requirements"] == sorted(["definitely-not-installed-abc>=1.0", MISSING_PACKAGE])
    modules = out["capabilities"]["modules"]
    assert modules["definitely_not_installed_abc"]["importable"] is False
    assert modules["definitely_not_installed_xyz"]["importable"] is False


def test_probe_of_a_missing_interpreter_fails_without_raising():
    out = probe_environment("/nonexistent/python")
    caps = out["capabilities"]
    assert caps["status"] == "failed"
    assert caps["error"]
    assert "FileNotFoundError" in caps["error"] or "OSError" in caps["error"]
    assert caps["modules"] == {}
    assert out["installed"] == {}
    assert "lock_digest" not in out


def test_probe_of_an_interpreter_that_produces_no_report_fails_without_raising(tmp_path):
    fake = tmp_path / "fake-python"
    fake.write_text("#!/bin/sh\necho 'no report here' >&2\nexit 3\n")
    fake.chmod(0o755)
    out = probe_environment(str(fake))
    caps = out["capabilities"]
    assert caps["status"] == "failed"
    assert "no report" in caps["error"] and "exit 3" in caps["error"]
    assert caps["interpreter_digest"].startswith("sha256:")  # what could be measured is kept


def test_probe_that_times_out_fails_without_raising(monkeypatch):
    import subprocess

    from eidos.runtime import env_probe

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(env_probe.subprocess, "run", timed_out)
    out = probe_environment(sys.executable, timeout=1)
    assert out["capabilities"]["status"] == "failed"
    assert "TimeoutExpired" in out["capabilities"]["error"]
    assert out["installed"] == {}


# -- declared_requirements -----------------------------------------------------------------------------------------------


def test_declared_requirements_reads_requirements_txt_fully(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# project\nnumpy==2.0.1\nrequests>=2.31\n-r base.txt\npandas ; python_version < '3.8'\nscikit-learn\n")
    assert declared_requirements(tmp_path) == ["numpy==2.0.1", "requests>=2.31", "scikit-learn"]


def test_declared_requirements_reads_lock_file_key_modules_only_without_versions(tmp_path):
    (tmp_path / "requirements.lock").write_text(
        "numpy==2.0.1\nurllib3==2.2.2\ntorch==2.3.1\ncertifi==2024.6.2\nscikit-learn==1.5.0\n")
    assert declared_requirements(tmp_path) == ["numpy", "scikit-learn", "torch"]


def test_declared_requirements_combines_both_files_sorted_and_deduplicated(tmp_path):
    (tmp_path / "requirements.txt").write_text("numpy>=2.0\nrequests\nscipy\n")
    (tmp_path / "requirements.lock").write_text("scipy==1.13.1\nidna==3.7\ntorch==2.3.1\n")
    out = declared_requirements(str(tmp_path))
    assert out == sorted(out)
    assert out == ["numpy>=2.0", "requests", "scipy", "torch"]
    assert "idna" not in out


def test_declared_requirements_without_files_or_root_is_empty(tmp_path):
    assert declared_requirements(tmp_path) == []
    assert declared_requirements(None) == []
    assert declared_requirements("") == []


def test_declared_requirements_ignores_a_directory_named_like_a_requirements_file(tmp_path):
    (tmp_path / "requirements.txt").mkdir()
    assert declared_requirements(tmp_path) == []


def test_declared_requirements_tolerates_undecodable_bytes(tmp_path):
    (tmp_path / "requirements.txt").write_bytes(b"numpy>=1.0\n\xff\xfe\nrequests\n")
    assert declared_requirements(tmp_path) == ["numpy>=1.0", "requests"]
