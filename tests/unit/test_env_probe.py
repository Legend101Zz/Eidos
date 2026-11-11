"""Probing an interpreter for its capability record (ADR 0020, addendum R10), without running any of its code.

The real-interpreter tests probe the interpreter running the tests and compare with what ``importlib.metadata`` says in this
process. The R10 tests build a fake project environment (a venv with hand-written distributions) under ``tmp_path``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from packaging.markers import default_environment

from eidos.domain.environments import (
    CAPABILITIES_SCHEMA,
    KEY_MODULES,
    assess,
    capability_summary,
    normalize_name,
    unsatisfied,
)
from eidos.runtime import env_probe
from eidos.runtime.env_probe import (
    MAX_INCONCLUSIVE,
    declared_requirements,
    interpreter_digest,
    probe_environment,
    registration_key,
)

MISSING_PACKAGE = "definitely-not-installed-xyz"


@pytest.fixture(scope="module")
def probe():
    return probe_environment(sys.executable)


@pytest.fixture(scope="module")
def probe_with_missing_requirement():
    return probe_environment(sys.executable, requirements=[MISSING_PACKAGE])


def missing_module_record(caps):
    """What a module no installed distribution provides looks like: absent, or unknown if some mapping is unknown."""
    return {"provided": None if caps.get("unmapped") else False}


def test_probe_of_the_running_interpreter_succeeds(probe):
    caps = probe["capabilities"]
    assert caps["schema"] == CAPABILITIES_SCHEMA == "environment-capabilities/2"
    assert caps["status"] == "ok", caps.get("error")
    assert "error" not in caps
    assert caps["interpreter"] == sys.executable
    assert caps["python_version"] == probe["python_version"] == platform.python_version()


def test_probe_records_the_interpreter_digest(probe):
    digest = probe["capabilities"]["interpreter_digest"]
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert digest == interpreter_digest(sys.executable)
    assert digest == "sha256:" + hashlib.sha256(Path(os.path.realpath(sys.executable)).read_bytes()).hexdigest()


def test_probe_records_the_marker_environment_of_the_probed_interpreter(probe):
    assert probe["capabilities"]["marker_environment"] == default_environment()


def test_probe_returns_the_installed_inventory_the_interpreter_reports(probe):
    installed = probe["installed"]
    assert isinstance(installed, dict) and installed
    assert all(k == k.lower() and "_" not in k for k in installed)  # normalized distribution names
    assert "pytest" in installed
    for name, version in installed.items():
        assert importlib.metadata.version(name) == version
    reality = {normalize_name(d.metadata["Name"]) for d in importlib.metadata.distributions() if d.metadata["Name"]}
    assert {normalize_name(name) for name in installed} == reality
    assert probe["devices"]["cpu"] is True
    assert probe["platform"]


def test_probe_records_every_key_module_consistently_with_the_installed_distributions(probe):
    caps = probe["capabilities"]
    modules = caps["modules"]
    assert set(KEY_MODULES) <= set(modules)
    providers = importlib.metadata.packages_distributions()
    for name in KEY_MODULES:
        rec = modules[name]
        assert "importable" not in rec and "error" not in rec  # nothing was imported
        if name in providers:
            assert rec["provided"] is True
            assert normalize_name(rec["distribution"]) in {normalize_name(d) for d in providers[name]}
            assert rec["version"] == importlib.metadata.version(rec["distribution"])
        else:
            assert rec == missing_module_record(caps)


def test_probe_records_the_import_names_that_differ_from_the_distribution_name(probe):
    providers = importlib.metadata.packages_distributions()
    names = probe["capabilities"]["import_names"]
    for module, dist in names.items():
        assert not module.startswith("_")
        assert module != dist.replace("-", "_")
        if module in providers:
            assert dist in {normalize_name(d) for d in providers[module]}
    differing = [m for m, dists in providers.items()
                 if not m.startswith("_") and any(m != normalize_name(d).replace("-", "_") for d in dists)]
    assert bool(names) == bool(differing)


def test_lock_digest_is_stable_for_the_same_environment(probe):
    again = probe_environment(sys.executable)
    assert again["lock_digest"] == probe["lock_digest"]
    assert again["installed"] == probe["installed"]
    assert probe["lock_digest"].startswith("sha256:")
    assert len(probe["lock_digest"]) == len("sha256:") + 64
    assert probe["lock_digest"] == "sha256:" + hashlib.sha256(json.dumps(probe["installed"], sort_keys=True).encode()).hexdigest()


def test_probe_without_declared_requirements_omits_the_requirements_field(probe):
    assert "requirements" not in probe
    assert "inconclusive" not in probe["capabilities"]
    assert "distributions" not in probe["capabilities"]


# -- declared requirements are recorded; absent ones are not imported to find out --------------------------------------


def test_declared_requirement_for_a_missing_package_is_recorded_as_not_provided(probe_with_missing_requirement):
    out = probe_with_missing_requirement
    caps = out["capabilities"]
    assert caps["status"] == "ok", caps.get("error")
    assert out["requirements"] == [MISSING_PACKAGE]
    assert caps["modules"]["definitely_not_installed_xyz"] == missing_module_record(caps)
    assert normalize_name(MISSING_PACKAGE) not in {normalize_name(n) for n in out["installed"]}


def test_a_declared_requirement_the_environment_lacks_is_a_problem_by_assess(probe_with_missing_requirement):
    out = probe_with_missing_requirement
    result = assess({"project": out["requirements"]}, out)
    if out["capabilities"].get("unmapped") or out["capabilities"].get("import_names_truncated"):
        assert result["problems"] == []
        assert [i["inconclusive"] for i in result["inconclusive"]] == ["unknown-import-mapping"]
    else:
        assert [(p["package"], p["problem"]) for p in result["problems"]] == [(MISSING_PACKAGE, "not-installed")]
        assert result["inconclusive"] == []


def test_declared_requirements_are_probed_alongside_the_key_modules(probe_with_missing_requirement):
    modules = probe_with_missing_requirement["capabilities"]["modules"]
    assert set(KEY_MODULES) <= set(modules)
    assert len(modules) == len(KEY_MODULES) + 1


def test_a_declared_requirement_is_probed_under_its_import_name():
    out = probe_environment(sys.executable, requirements=["scikit-learn>=1.0"])
    assert out["requirements"] == ["scikit-learn>=1.0"]
    modules = out["capabilities"]["modules"]
    assert "sklearn" in modules and "scikit_learn" not in modules


def test_a_declared_requirement_that_is_installed_is_recorded_with_its_distribution_and_version(probe):
    out = probe_environment(sys.executable, requirements=["pytest>=1", "PyTest"])
    module = out["capabilities"]["modules"]["pytest"]
    assert module == {"provided": True, "distribution": "pytest", "version": importlib.metadata.version("pytest")}
    assert out["requirements"] == ["PyTest", "pytest>=1"]
    assert assess({"c": out["requirements"]}, out) == {"problems": [], "inconclusive": []}


def test_requirements_come_from_the_project_root_and_the_caller_merged(tmp_path):
    (tmp_path / "requirements.txt").write_text("definitely-not-installed-abc>=1.0\n")
    out = probe_environment(sys.executable, project_root=tmp_path, requirements=[MISSING_PACKAGE])
    assert out["requirements"] == sorted(["definitely-not-installed-abc>=1.0", MISSING_PACKAGE])
    modules = out["capabilities"]["modules"]
    expected = missing_module_record(out["capabilities"])
    assert modules["definitely_not_installed_abc"] == expected
    assert modules["definitely_not_installed_xyz"] == expected


def test_what_the_probe_could_not_decide_about_the_declaration_is_recorded(tmp_path):
    (tmp_path / "requirements.txt").write_text("-e .\nfoo @ https://example.invalid/foo.whl\n-r missing.txt\npytest\n")
    out = probe_environment(sys.executable, project_root=tmp_path)
    undecided = {u["requirement"]: u["inconclusive"] for u in out["capabilities"]["inconclusive"]}
    assert undecided == {"-e .": "not-a-parsed-requirement", "foo @ https://example.invalid/foo.whl": "url-requirement",
                         "-r missing.txt": "not-a-parsed-requirement"}
    assert capability_summary(out)["inconclusive_requirements"] == out["capabilities"]["inconclusive"]
    assert unsatisfied({"project": out["requirements"]}, out) == []


def test_the_undecided_requirements_recorded_are_bounded(tmp_path):
    lines = [f"pkg{i} @ https://example.invalid/pkg{i}.whl" for i in range(MAX_INCONCLUSIVE + 5)]
    (tmp_path / "requirements.txt").write_text("\n".join(lines) + "\n")
    out = probe_environment(sys.executable, project_root=tmp_path)
    assert len(out["requirements"]) == MAX_INCONCLUSIVE + 5
    assert len(out["capabilities"]["inconclusive"]) == MAX_INCONCLUSIVE


# -- failures --------------------------------------------------------------------------------------------------------------


def test_probe_of_a_missing_interpreter_fails_without_raising():
    out = probe_environment("/nonexistent/python")
    caps = out["capabilities"]
    assert caps["status"] == "failed"
    assert caps["error"]
    assert "FileNotFoundError" in caps["error"] or "OSError" in caps["error"]
    assert caps["modules"] == {}
    assert out["installed"] == {}
    assert "lock_digest" not in out
    assert unsatisfied({"c": ["numpy"]}, out) == []


def executable(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_probe_of_an_interpreter_that_produces_no_report_fails_without_raising(tmp_path):
    fake = executable(tmp_path / "fake-python", "echo 'no report here' >&2\nexit 3\n")
    out = probe_environment(fake)
    caps = out["capabilities"]
    assert caps["status"] == "failed"
    assert "no report" in caps["error"] and "exit 3" in caps["error"]
    assert "no report here" in caps["error"]
    assert caps["interpreter_digest"].startswith("sha256:")  # what could be measured is kept


def test_probe_of_an_interpreter_that_reports_and_then_fails_is_failed(tmp_path):
    fake = executable(tmp_path / "fake-python", """echo '{"python_version": "3.12.0", "platform": "x", "installed": {}, "top_levels": {},"""
                      """ "requires": {}, "marker_environment": {}}'\nexit 5\n""")
    out = probe_environment(fake)
    assert out["capabilities"]["status"] == "failed"
    assert "exited with 5" in out["capabilities"]["error"]
    assert out["installed"] == {}


def test_probe_that_times_out_fails_without_raising(tmp_path):
    fake = executable(tmp_path / "slow-python", "sleep 60\n")
    started = time.monotonic()
    out = probe_environment(fake, timeout=1)
    assert time.monotonic() - started < 15
    assert out["capabilities"]["status"] == "failed"
    assert "TimeoutExpired" in out["capabilities"]["error"]
    assert out["installed"] == {}


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_descendant_holding_the_output_open_cannot_extend_the_timeout(tmp_path):
    """R10 #2: the probe's own process group is killed at the timeout, so a grandchild that inherited stdout and outlives its
    parent (which is what would hold a pipe open and hang the registering process) is gone and the call returns."""
    pidfile = tmp_path / "child.pid"
    fake = executable(tmp_path / "fake-python", f"sleep 300 &\necho $! > {pidfile}\nsleep 300\n")
    child = None
    try:
        started = time.monotonic()
        out = probe_environment(fake, timeout=1)
        elapsed = time.monotonic() - started
        assert pidfile.exists(), "the fake interpreter never started its child"
        child = int(pidfile.read_text())
        assert out["capabilities"]["status"] == "failed"
        assert out["capabilities"]["error"]
        assert elapsed < 10, f"the probe took {elapsed:.1f}s with a 1s timeout"
        deadline = time.monotonic() + 5
        while pid_is_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_is_alive(child), "the descendant of the probed interpreter outlived the probe"
    finally:
        if child is None and pidfile.exists():
            child = int(pidfile.read_text())
        if child is not None and pid_is_alive(child):
            os.kill(child, 9)


def test_a_descendant_that_outlives_a_successful_probe_is_also_stopped(tmp_path):
    """The group is killed on every exit, not only at the timeout: nothing the interpreter left behind survives the probe."""
    pidfile = tmp_path / "child.pid"
    real = f"{sys.executable!r}"
    fake = executable(tmp_path / "fake-python", f"sleep 300 &\necho $! > {pidfile}\nexec {real} \"$@\"\n")
    child = None
    try:
        out = probe_environment(fake, timeout=30)
        child = int(pidfile.read_text())
        assert out["capabilities"]["status"] == "ok", out["capabilities"].get("error")
        deadline = time.monotonic() + 5
        while pid_is_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_is_alive(child)
    finally:
        if child is not None and pid_is_alive(child):
            os.kill(child, 9)


def test_the_probe_runs_the_interpreter_isolated_without_site_processing(monkeypatch, tmp_path):
    seen = []
    real = env_probe._run_bounded

    def spy(argv, **kwargs):
        seen.append((argv, kwargs))
        return real(argv, **kwargs)

    monkeypatch.setattr(env_probe, "_run_bounded", spy)
    probe_environment(sys.executable)
    ((argv, kwargs),) = seen
    assert argv[:4] == [sys.executable, "-I", "-S", "-c"]
    assert not {"PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME"} & set(kwargs["env"])
    assert not any(k.startswith("EIDOS_") for k in kwargs["env"])
    assert Path(kwargs["cwd"]) != Path.cwd()


# -- a fake project environment ---------------------------------------------------------------------------------------------


def make_venv(tmp_path: Path, name: str = "venv") -> tuple[Path, Path]:
    """An empty virtual environment (no pip) under ``tmp_path``: its interpreter and its site-packages directory."""
    venv = tmp_path / name
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    (site,) = venv.glob("lib/python*/site-packages")
    return venv / "bin" / "python", site


def add_dist(site: Path, name: str, version: str, *, top_level=None, record=None, requires=(), extras=()) -> Path:
    """A hand-written installed distribution: metadata only, plus what it says it provides (top_level.txt and/or RECORD).
    The files a RECORD lists are created if absent (``importlib.metadata`` leaves out listed files that do not exist)."""
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata += [f"Provides-Extra: {e}" for e in extras] + [f"Requires-Dist: {r}" for r in requires]
    (dist_info / "METADATA").write_text("\n".join(metadata) + "\n")
    if top_level is not None:
        (dist_info / "top_level.txt").write_text("".join(f"{t}\n" for t in top_level))
    if record is not None:
        (dist_info / "RECORD").write_text("".join(f"{path},,\n" for path in record))
        for path in record:
            target = site / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    return dist_info


def writes_marker(marker: Path) -> str:
    return f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"


def test_probing_a_project_environment_runs_none_of_its_code(tmp_path):
    """R10 #2: a .pth import line, sitecustomize and an installed package's __init__ all write a marker if they run. They run
    when the interpreter starts normally or the package is imported (the control), and not when it is probed."""
    markers = tmp_path / "markers"
    markers.mkdir()
    pth, custom, imported = markers / "pth", markers / "sitecustomize", markers / "import"
    python, site = make_venv(tmp_path)
    (site / "hook.pth").write_text(f"import pathlib; pathlib.Path({str(pth)!r}).write_text('ran')\n")
    (site / "sitecustomize.py").write_text(writes_marker(custom))
    (site / "foo_pkg").mkdir()
    (site / "foo_pkg" / "__init__.py").write_text(writes_marker(imported))
    add_dist(site, "foo-pkg", "1.0", record=["foo_pkg/__init__.py", "foo_pkg-1.0.dist-info/METADATA", "foo_pkg-1.0.dist-info/RECORD"])

    # PYTHONPATH puts sitecustomize ahead of any one the interpreter's own installation ships (Homebrew and Debian do).
    control = subprocess.run([str(python), "-c", "import foo_pkg"], capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(site)})
    assert control.returncode == 0, control.stderr
    assert sorted(p.name for p in markers.iterdir()) == ["import", "pth", "sitecustomize"], "the hooks are not live in this venv"
    for marker in markers.iterdir():
        marker.unlink()

    out = probe_environment(str(python), requirements=["foo-pkg"])

    assert sorted(markers.iterdir()) == [], "the probe ran code from the probed environment"
    caps = out["capabilities"]
    assert caps["status"] == "ok", caps.get("error")
    assert out["installed"] == {"foo-pkg": "1.0"}
    assert out["requirements"] == ["foo-pkg"]
    assert caps["modules"]["foo_pkg"] == {"provided": True, "distribution": "foo-pkg", "version": "1.0"}
    assert caps["interpreter"] == str(python)
    assert unsatisfied({"c": ["foo-pkg>=1"]}, out) == []


def test_probing_does_not_run_code_in_the_project_root_or_from_the_parent_environment(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    shadows = tmp_path / "shadows"
    shadows.mkdir()
    marker = tmp_path / "ran"
    for name in ("json", "platform", "importlib", "sitecustomize", "usercustomize"):
        for where in (root, shadows):
            (where / f"{name}.py").write_text(writes_marker(marker))
    (root / "requirements.txt").write_text("numpy\n")
    monkeypatch.setenv("PYTHONPATH", str(shadows))
    monkeypatch.setenv("PYTHONSTARTUP", str(shadows / "json.py"))
    monkeypatch.chdir(root)
    out = probe_environment(sys.executable, project_root=root)
    assert out["capabilities"]["status"] == "ok", out["capabilities"].get("error")
    assert not marker.exists()


def test_probe_of_a_project_environment_sees_only_that_environment(tmp_path):
    python, site = make_venv(tmp_path)
    out = probe_environment(str(python))
    assert out["capabilities"]["status"] == "ok", out["capabilities"].get("error")
    assert out["installed"] == {}  # not the packages of the interpreter running the tests
    assert {n: m["provided"] for n, m in out["capabilities"]["modules"].items()} == {m: False for m in KEY_MODULES}
    add_dist(site, "foo-pkg", "1.0", top_level=["foo_pkg"])
    after = probe_environment(str(python))
    assert after["installed"] == {"foo-pkg": "1.0"}
    assert "pytest" not in after["installed"]
    assert after["lock_digest"] != out["lock_digest"]  # a change of inventory is a change of digest
    assert after["capabilities"]["interpreter_digest"] == out["capabilities"]["interpreter_digest"]


def test_a_pth_path_line_adds_a_directory_but_its_import_line_is_not_run(tmp_path):
    python, site = make_venv(tmp_path)
    src, rel, marker = tmp_path / "src", site / "extra-src", tmp_path / "ran"
    src.mkdir()
    rel.mkdir()
    add_dist(src, "editable-pkg", "0.1", top_level=["editable_pkg"])
    add_dist(rel, "relative-pkg", "0.2", top_level=["relative_pkg"])
    (site / "legacy.pth").write_text(f"# a comment\nextra-src\n{src}\nimport pathlib; pathlib.Path({str(marker)!r}).touch()\nno-such-dir\n")
    out = probe_environment(str(python))
    assert out["installed"] == {"editable-pkg": "0.1", "relative-pkg": "0.2"}
    assert not marker.exists()


def test_the_earlier_path_shadows_a_later_duplicate_distribution(tmp_path):
    python, site = make_venv(tmp_path)
    later = tmp_path / "later"
    later.mkdir()
    add_dist(site, "dup-pkg", "1.0", top_level=["dup_pkg"])
    add_dist(later, "dup-pkg", "2.0", top_level=["dup_pkg"])
    add_dist(later, "only-later", "0.1", top_level=["only_later"])
    (site / "later.pth").write_text(f"{later}\n")
    assert probe_environment(str(python))["installed"] == {"dup-pkg": "1.0", "only-later": "0.1"}


def test_a_broken_environment_entry_does_not_fail_the_probe(tmp_path):
    python, site = make_venv(tmp_path)
    (site / "binary.pth").write_bytes(b"\xff\xfe\x00 not text\n")
    (site / "nameless-1.0.dist-info").mkdir()
    (site / "nameless-1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\n")
    add_dist(site, "fine-pkg", "1.0", top_level=["fine_pkg"])
    out = probe_environment(str(python))
    assert out["capabilities"]["status"] == "ok", out["capabilities"].get("error")
    assert out["installed"] == {"fine-pkg": "1.0"}


# -- R10 #3: what a distribution provides comes from its own metadata ----------------------------------------------------------


def test_python_dateutil_is_satisfied_by_a_real_probe_and_never_refused(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "python-dateutil", "2.9.0", top_level=["dateutil", "_private", "not-an-identifier"])
    reqs = ["python-dateutil", "python-dateutil>=2.8", "python_dateutil==2.9.0"]
    out = probe_environment(str(python), requirements=reqs)
    caps = out["capabilities"]
    assert caps["status"] == "ok", caps.get("error")
    assert out["installed"] == {"python-dateutil": "2.9.0"}
    assert caps["modules"]["dateutil"] == {"provided": True, "distribution": "python-dateutil", "version": "2.9.0"}
    assert "python_dateutil" not in caps["modules"]  # the distribution name is not an import name
    assert caps["import_names"] == {"dateutil": "python-dateutil"}  # only public, valid names
    assert "unmapped" not in caps and "inconclusive" not in caps
    assert assess({"c": out["requirements"]}, out) == {"problems": [], "inconclusive": []}
    assert unsatisfied({"c": ["dateutil", "python-dateutil<2.9"]}, out)[-1]["found"] == "2.9.0"  # a real mismatch still shows


def test_a_distribution_whose_import_names_are_unknown_is_unknown_not_absent_and_not_a_refusal(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "mystery-pkg", "1.0")  # no top_level.txt and no RECORD
    out = probe_environment(str(python), requirements=["mystery-pkg>=1", "pandas"])
    caps = out["capabilities"]
    assert caps["status"] == "ok", caps.get("error")
    assert caps["unmapped"] == ["mystery-pkg"]
    assert caps["modules"]["mystery_pkg"] == {"provided": None, "distribution": "mystery-pkg", "version": "1.0"}
    assert all(caps["modules"][m] == {"provided": None} for m in KEY_MODULES)  # nothing can be called absent
    summary = capability_summary(out)
    assert summary["modules"]["mystery_pkg"] == "unknown"
    assert all(summary["modules"][m] == "unknown" for m in KEY_MODULES)
    result = assess({"c": ["mystery-pkg>=1", "numpy", "pandas"]}, out)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"]) for i in result["inconclusive"]] == [
        ("numpy", "unknown-import-mapping"), ("pandas", "unknown-import-mapping")]
    assert [u["requirement"] for u in caps["inconclusive"]] == ["pandas"]


def test_import_names_come_from_record_when_top_level_txt_is_absent(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "foo-pkg", "1.0", record=[
        "foo_pkg/__init__.py", "foo_pkg/sub/mod.py", "foo_pkg/__pycache__/mod.cpython-312.pyc", "single_module.py",
        "ext.cpython-312-darwin.so", "foo_pkg-1.0.dist-info/METADATA", "foo_pkg-1.0.data/scripts/tool", "../../../bin/foo-cli",
        "_private.py", "README.md"])
    out = probe_environment(str(python), requirements=["foo-pkg"])
    caps = out["capabilities"]
    assert "unmapped" not in caps
    assert caps["import_names"] == {"ext": "foo-pkg", "single_module": "foo-pkg"}  # foo_pkg is the distribution's own name
    assert caps["modules"]["foo_pkg"]["provided"] is True
    assert unsatisfied({"c": ["foo-pkg"]}, out) == []


def test_a_package_absent_from_the_inventory_whose_guessed_import_name_another_distribution_provides_is_inconclusive(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "foo-compat", "3.0", top_level=["foo_pkg"])
    out = probe_environment(str(python), requirements=["foo-pkg>=1"])
    caps = out["capabilities"]
    assert out["installed"] == {"foo-compat": "3.0"}
    assert caps["modules"]["foo_pkg"] == {"provided": True, "distribution": "foo-compat", "version": "3.0"}
    assert caps["import_names"] == {"foo_pkg": "foo-compat"}
    result = assess({"c": ["foo-pkg>=1"]}, out)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"], i["found"]) for i in result["inconclusive"]] == [
        ("foo-pkg", "provided-by-another-distribution", "foo-compat")]
    assert caps["inconclusive"] == [{"requirement": "foo-pkg>=1", "inconclusive": "provided-by-another-distribution"}]
    assert [p["package"] for p in unsatisfied({"c": ["bar-pkg"]}, out)] == ["bar-pkg"]  # nothing provides bar_pkg: refused


def test_the_probe_bounds_how_many_import_names_it_records(tmp_path, monkeypatch):
    monkeypatch.setattr(env_probe, "MAX_IMPORT_NAMES", 3)
    python, site = make_venv(tmp_path)
    for i in range(6):
        add_dist(site, f"dist-{i}", "1.0", top_level=[f"mod_{i}"])
    out = probe_environment(str(python), requirements=["absent-pkg"])
    caps = out["capabilities"]
    assert len(caps["import_names"]) == 3 and caps["import_names_truncated"] is True
    result = assess({"c": ["absent-pkg"]}, out)  # a truncated table cannot show absence
    assert result["problems"] == []
    assert [i["inconclusive"] for i in result["inconclusive"]] == ["unknown-import-mapping"]


# -- R10 #4: extras read from the distribution's own metadata ---------------------------------------------------------------------


def test_extras_are_checked_against_the_requirements_recorded_from_the_distribution(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "foo-pkg", "1.0", top_level=["foo_pkg"], extras=["fast"], requires=["bar-lib>=2; extra == 'fast'", "baz"])
    add_dist(site, "other-pkg", "1.0", top_level=["other_pkg"], extras=["x"], requires=["qux; extra == 'x'"])
    wanted = ["foo-pkg[fast]", "other-pkg"]
    out = probe_environment(str(python), requirements=wanted)
    # Only what asked for extras is kept, and only the requirements that belong to an extra.
    assert out["capabilities"]["distributions"] == {"foo-pkg": {"requires_dist": ["bar-lib>=2; extra == 'fast'"]}}
    problems = unsatisfied({"c": ["foo-pkg[fast]"]}, out)
    assert [(p["problem"], p["package"], p["via_extra"]) for p in problems] == [("not-installed", "bar-lib", "fast")]

    bar = add_dist(site, "bar-lib", "1.0", top_level=["bar_lib"])
    problems = unsatisfied({"c": ["foo-pkg[fast]"]}, probe_environment(str(python), requirements=wanted))
    assert [(p["problem"], p["package"], p["found"]) for p in problems] == [("version-mismatch", "bar-lib", "1.0")]

    shutil.rmtree(bar)
    add_dist(site, "bar-lib", "2.5", top_level=["bar_lib"])
    assert unsatisfied({"c": ["foo-pkg[fast]"]}, probe_environment(str(python), requirements=wanted)) == []


def test_extras_of_a_distribution_the_probe_was_not_asked_about_are_not_recorded_so_they_are_inconclusive(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "foo-pkg", "1.0", top_level=["foo_pkg"], extras=["fast"], requires=["bar-lib; extra == 'fast'"])
    out = probe_environment(str(python), requirements=["foo-pkg"])
    assert "distributions" not in out["capabilities"]
    result = assess({"c": ["foo-pkg[fast]"]}, out)
    assert result["problems"] == []
    assert [i["inconclusive"] for i in result["inconclusive"]] == ["extras-not-recorded"]


def test_a_marker_in_a_declared_requirement_is_decided_by_the_probed_interpreter(tmp_path):
    python, site = make_venv(tmp_path)
    add_dist(site, "foo-pkg", "1.0", top_level=["foo_pkg"])
    reqs = [f'bar-pkg; python_version == "{sys.version_info.major}.{sys.version_info.minor}"', 'baz-pkg; python_version < "3"']
    out = probe_environment(str(python), requirements=reqs)
    assert [p["package"] for p in unsatisfied({"c": reqs}, out)] == ["bar-pkg"]


# -- declared_requirements -------------------------------------------------------------------------------------------------


def test_declared_requirements_reads_requirements_txt_fully_and_keeps_what_it_cannot_resolve(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# project\nnumpy==2.0.1\nrequests>=2.31\n-r base.txt\npandas ; python_version < '3.8'\nscikit-learn\n")
    assert declared_requirements(tmp_path) == [
        "-r base.txt", "numpy==2.0.1", "pandas ; python_version < '3.8'", "requests>=2.31", "scikit-learn"]


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
    out = declared_requirements(tmp_path)
    assert {"numpy>=1.0", "requests"} <= set(out)
    assert len(out) == 3  # the undecodable line is kept as a line the check cannot read


def test_declared_requirements_follows_an_include_inside_the_project(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements.txt").write_text("-r requirements/base.txt\n--requirement requirements/dev.txt\nroot-pkg\n")
    (tmp_path / "requirements" / "base.txt").write_text("base-pkg>=1\n-r ../shared.txt\n")
    (tmp_path / "requirements" / "dev.txt").write_text("dev-pkg\n-r base.txt\n")
    (tmp_path / "shared.txt").write_text("shared-pkg\n")
    assert declared_requirements(tmp_path) == ["base-pkg>=1", "dev-pkg", "root-pkg", "shared-pkg"]


def test_declared_requirements_follows_an_include_from_a_lock_file_keeping_key_modules_only(tmp_path):
    (tmp_path / "requirements.lock").write_text("-r pins.txt\ntorch==2.3.1\n")
    (tmp_path / "pins.txt").write_text("numpy==2.0.1\nurllib3==2.2.2\n")
    assert declared_requirements(tmp_path) == ["numpy", "torch"]


def test_declared_requirements_follows_a_symlink_that_stays_inside_the_project(tmp_path):
    (tmp_path / "real.txt").write_text("real-pkg\n")
    (tmp_path / "alias.txt").symlink_to("real.txt")
    (tmp_path / "requirements.txt").write_text("-r alias.txt\n")
    assert declared_requirements(tmp_path) == ["real-pkg"]


def test_declared_requirements_does_not_loop_on_include_cycles(tmp_path):
    (tmp_path / "requirements.txt").write_text("-r a.txt\nroot-pkg\n")
    (tmp_path / "a.txt").write_text("-r requirements.txt\na-pkg\n")
    assert declared_requirements(tmp_path) == ["-r requirements.txt", "a-pkg", "root-pkg"]


@pytest.mark.parametrize("how", ["parent-path", "absolute-path", "symlink-to-file", "symlink-to-directory", "nested-parent-path"])
def test_declared_requirements_does_not_read_an_include_outside_the_project(tmp_path, how):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "outside.txt").write_text("secret-pkg\n")
    (outside / "more.txt").write_text("secret-pkg\n")
    (project / "sub").mkdir()
    include = {
        "parent-path": "../outside.txt",
        "absolute-path": str(tmp_path / "outside.txt"),
        "symlink-to-file": "link.txt",
        "symlink-to-directory": "linkdir/more.txt",
        "nested-parent-path": "sub/../../outside.txt",
    }[how]
    if how == "symlink-to-file":
        (project / "link.txt").symlink_to(tmp_path / "outside.txt")
    if how == "symlink-to-directory":
        (project / "linkdir").symlink_to(outside, target_is_directory=True)
    (project / "requirements.txt").write_text(f"-r {include}\nroot-pkg\n")
    out = declared_requirements(project)
    assert "secret-pkg" not in out
    assert out == sorted([f"-r {include}", "root-pkg"])


def test_a_declared_include_outside_the_project_is_only_reported_never_refused(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside.txt").write_text("secret-pkg\n")
    (project / "requirements.txt").write_text("-r ../outside.txt\n")
    out = probe_environment(sys.executable, project_root=project)
    assert out["requirements"] == ["-r ../outside.txt"]
    assert out["capabilities"]["inconclusive"] == [{"requirement": "-r ../outside.txt", "inconclusive": "not-a-parsed-requirement"}]
    assert unsatisfied({"project": out["requirements"]}, out) == []


# -- registration_key (R10 #8) ---------------------------------------------------------------------------------------------------


def registration_payload():
    """What the CLI registers, with a lock digest that is the same in every variant below."""
    return {
        "name": "default", "python_executable": "/work/venv/bin/python", "project_root": "/work/project", "adapter": None,
        "python_version": "3.12.1", "platform": "Linux-x86_64", "devices": {"cpu": True},
        "installed": {"numpy": "2.1.0", "pytest": "8.3.0"}, "lock_digest": "sha256:" + "11" * 32,
        "requirements": ["numpy>=2", "pytest"],
        "capabilities": {"schema": CAPABILITIES_SCHEMA, "status": "ok", "interpreter": "/work/venv/bin/python",
                         "interpreter_digest": "sha256:" + "22" * 32, "python_version": "3.12.1",
                         "marker_environment": {"sys_platform": "linux", "python_version": "3.12"},
                         "modules": {"numpy": {"provided": True, "distribution": "numpy", "version": "2.1.0"},
                                     "torch": {"provided": False}},
                         "import_names": {"dateutil": "python-dateutil"}},
    }


def with_change(change):
    payload = registration_payload()
    change(payload)
    return payload


CHANGES = {
    "interpreter digest": lambda p: p["capabilities"].__setitem__("interpreter_digest", "sha256:" + "33" * 32),
    "a module record": lambda p: p["capabilities"]["modules"].__setitem__("torch", {"provided": True, "distribution": "torch", "version": "2.3"}),
    "a module made unknown": lambda p: p["capabilities"]["modules"].__setitem__("torch", {"provided": None}),
    "the import names": lambda p: p["capabilities"]["import_names"].__setitem__("yaml", "pyyaml"),
    "the marker environment": lambda p: p["capabilities"]["marker_environment"].__setitem__("sys_platform", "darwin"),
    "the capability status": lambda p: p["capabilities"].__setitem__("status", "failed"),
    "an inconclusive requirement": lambda p: p["capabilities"].__setitem__("inconclusive", [{"requirement": "-e .", "inconclusive": "x"}]),
    "an installed version": lambda p: p["installed"].__setitem__("numpy", "2.1.1"),
    "an installed distribution": lambda p: p["installed"].__setitem__("scipy", "1.14.0"),
    "a removed distribution": lambda p: p["installed"].pop("pytest"),
    "a declared requirement": lambda p: p["requirements"].append("scipy"),
    "a changed declared requirement": lambda p: p["requirements"].__setitem__(0, "numpy>=2.1"),
    "no declared requirements": lambda p: p.pop("requirements"),
    "the interpreter path": lambda p: p.__setitem__("python_executable", "/work/other/bin/python"),
    "the project root": lambda p: p.__setitem__("project_root", "/work/elsewhere"),
}


@pytest.mark.parametrize("what", sorted(CHANGES))
def test_registration_key_differs_when_any_part_of_the_payload_differs_even_if_the_lock_digest_does_not(what):
    base, changed = registration_payload(), with_change(CHANGES[what])
    assert changed["lock_digest"] == base["lock_digest"]  # the old key was a function of this alone
    assert registration_key("env:p:default", changed) != registration_key("env:p:default", base)


def test_registration_key_differs_for_the_interpreter_digest_alone():
    base = registration_payload()
    other = copy.deepcopy(base)
    other["capabilities"]["interpreter_digest"] = "sha256:" + "44" * 32
    assert other != base and registration_key("k", other) != registration_key("k", base)


def test_registration_key_is_stable_for_an_equal_payload_regardless_of_key_order():
    base = registration_payload()

    def reordered(value):
        if isinstance(value, dict):
            return {k: reordered(value[k]) for k in sorted(value, reverse=True)}
        return value

    assert list(reordered(base)) != list(base)
    assert registration_key("env:p:default", reordered(base)) == registration_key("env:p:default", base)
    assert registration_key("env:p:default", copy.deepcopy(base)) == registration_key("env:p:default", base)
    assert registration_key("env:p:default", base) == registration_key("env:p:default", base)


def test_registration_key_carries_the_prefix_and_a_sha256_of_the_payload():
    key = registration_key("env:p:default", registration_payload())
    prefix, _, digest = key.rpartition(":")
    assert prefix == "env:p:default"
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert registration_key("env:q:default", registration_payload()) != key
    assert registration_key("env:p:default", registration_payload()).startswith("env:p:default:")


def test_registration_key_follows_a_real_probe(tmp_path):
    python, site = make_venv(tmp_path)
    payload = {"name": "default", "python_executable": str(python), **probe_environment(str(python), requirements=["foo-pkg"])}
    first = registration_key("env:p:default", payload)
    assert registration_key("env:p:default", {**payload, **probe_environment(str(python), requirements=["foo-pkg"])}) == first
    add_dist(site, "foo-pkg", "1.0", top_level=["foo_pkg"])
    changed = {"name": "default", "python_executable": str(python), **probe_environment(str(python), requirements=["foo-pkg"])}
    assert registration_key("env:p:default", changed) != first
