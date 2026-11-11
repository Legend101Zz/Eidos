"""Environment capability records and admission preflight (ADR 0020): pure domain rules, no I/O."""

from __future__ import annotations

from eidos.domain import environments as envs
from eidos.domain.environments import (
    MAX_REPORTED,
    capability_summary,
    describe_problems,
    import_name,
    normalize_name,
    parse_requirements,
    unsatisfied,
)


def make_env(modules=None, *, installed=None, status="ok", **caps):
    """An environment record as the lab stores it after a probe."""
    return {
        "installed": installed or {},
        "capabilities": {"schema": "environment-capabilities/1", "status": status,
                         "interpreter_digest": "sha256:" + "ab" * 32, "python_version": "3.12.1",
                         "modules": modules or {}, **caps},
    }


NUMPY_OK = {"numpy": {"importable": True, "version": "2.1"}}


# -- unsatisfied: what is refused ---------------------------------------------------------------------------------------


def test_refuses_key_module_that_is_not_importable_and_names_the_error():
    env = make_env({"sklearn": {"importable": False, "error": "ModuleNotFoundError"}})
    problems = unsatisfied({"comp@1": ["scikit-learn>=1.0"]}, env)
    assert len(problems) == 1
    p = problems[0]
    assert p["problem"] == "not-importable"
    assert p["owner"] == "comp@1"
    assert p["package"] == "scikit-learn"
    assert p["requirement"] == "scikit-learn>=1.0"
    assert p["error"] == "ModuleNotFoundError"


def test_refuses_a_version_mismatch_and_reports_what_was_found():
    problems = unsatisfied({"comp@1": ["numpy>=3.0"]}, make_env(NUMPY_OK))
    assert [(p["problem"], p["package"], p["found"]) for p in problems] == [("version-mismatch", "numpy", "2.1")]


def test_version_mismatch_uses_the_installed_inventory_when_the_probe_has_no_version():
    env = make_env({"numpy": {"importable": True, "version": None}}, installed={"numpy": "1.26.4"})
    problems = unsatisfied({"c": ["numpy==2.0.0"]}, env)
    assert [(p["problem"], p["found"]) for p in problems] == [("version-mismatch", "1.26.4")]


def test_refuses_a_package_absent_from_a_non_empty_inventory():
    env = make_env(installed={"numpy": "2.1", "pytest": "8.3"})
    problems = unsatisfied({"c": ["pandas>=2.0"]}, env)
    assert [(p["problem"], p["package"]) for p in problems] == [("not-installed", "pandas")]


def test_an_empty_inventory_is_not_evidence_of_absence():
    assert unsatisfied({"c": ["pandas>=2.0"]}, make_env(installed={})) == []


def test_inventory_lookup_normalizes_distribution_names():
    env = make_env(installed={"typing_extensions": "4.12.0"})
    assert unsatisfied({"c": ["Typing.Extensions>=4.0"]}, env) == []


# -- unsatisfied: what is never refused ---------------------------------------------------------------------------------


def test_missing_capabilities_are_never_refused():
    env = {"installed": {"numpy": "1.0"}}
    assert unsatisfied({"c": ["numpy>=9.0", "torch"]}, env) == []
    assert unsatisfied({"c": ["numpy>=9.0"]}, {"installed": {}, "capabilities": None}) == []


def test_a_failed_probe_is_never_refused_even_with_bad_modules_recorded():
    env = make_env({"numpy": {"importable": False, "error": "ImportError"}}, installed={"x": "1"}, status="failed",
                   error="probe stopped after 3 of 5 modules (exit -11)")
    assert unsatisfied({"c": ["numpy>=1.0", "pandas"]}, env) == []


def test_lab_provided_eidos_packages_are_not_refused():
    # Not importable, not installed, and the inventory is non-empty: only the lab prefix exempts them.
    env = make_env({"eidos_wire": {"importable": False, "error": "ModuleNotFoundError"}}, installed={"numpy": "2.1"})
    assert unsatisfied({"c": ["eidos-wire>=1.0", "eidos-sdk", "eidos.runtime==0.1"]}, env) == []


def test_accepts_a_satisfied_requirement():
    env = make_env({**NUMPY_OK, "sklearn": {"importable": True, "version": "1.5.0"}}, installed={"numpy": "2.1"})
    assert unsatisfied({"a": ["numpy>=2.0", "scikit-learn"], "b": ["numpy==2.1"]}, env) == []


def test_unversioned_requirement_for_an_importable_module_is_satisfied():
    assert unsatisfied({"c": ["numpy"]}, make_env(NUMPY_OK)) == []


def test_unparseable_requirements_are_ignored():
    assert unsatisfied({"c": ["not a requirement!!", "numpy[extra]>=1"]}, make_env(NUMPY_OK, installed={"z": "1"})) == []


def test_same_owner_and_package_is_reported_once_but_other_owners_are_separate():
    env = make_env({"numpy": {"importable": False, "error": "ImportError"}})
    problems = unsatisfied({"a": ["numpy>=1", "numpy<3"], "b": ["numpy"]}, env)
    assert sorted((p["owner"], p["package"]) for p in problems) == [("a", "numpy"), ("b", "numpy")]


def test_reports_every_problem_across_owners():
    env = make_env({**NUMPY_OK, "torch": {"importable": False, "error": "OSError"}}, installed={"numpy": "2.1"})
    problems = unsatisfied({"a": ["torch", "numpy>=5"], "b": ["pandas"]}, env)
    assert {(p["owner"], p["package"], p["problem"]) for p in problems} == {
        ("a", "torch", "not-importable"), ("a", "numpy", "version-mismatch"), ("b", "pandas", "not-installed")}


# -- names ---------------------------------------------------------------------------------------------------------------


def test_import_name_maps_distribution_to_module():
    assert import_name("scikit-learn") == "sklearn"
    assert import_name("Scikit_Learn") == "sklearn"
    assert import_name("pillow") == "PIL"
    assert import_name("PyYAML") == "yaml"
    assert import_name("opencv-python") == "cv2"
    assert import_name("my-package.name") == "my_package_name"
    assert import_name("numpy") == "numpy"


def test_normalize_name_collapses_separators_and_lowercases():
    assert normalize_name("Foo_Bar.baz--qux") == "foo-bar-baz-qux"


# -- parse_requirements --------------------------------------------------------------------------------------------------


def test_parse_requirements_skips_comments_options_urls_and_markers():
    text = "\n".join([
        "# a comment",
        "",
        "   ",
        "-r other.txt",
        "--index-url https://example.invalid/simple",
        "-e .",
        "pkg-url @ https://example.invalid/pkg.whl",
        "https://example.invalid/bare.whl",
        'colorama>=0.4; sys_platform == "win32"',
        "numpy>=1.0  # trailing comment",
        "requests==2.31.0",
        "pandas",
    ])
    assert parse_requirements(text) == ["numpy>=1.0", "requests==2.31.0", "pandas"]


def test_parse_requirements_strips_line_continuation_backslash():
    assert parse_requirements("numpy>=1.0 \\\n") == ["numpy>=1.0"]


def test_parse_requirements_key_only_keeps_key_modules_without_versions():
    text = "numpy==2.5.4\nrequests==2.31.0\nscikit-learn>=1.2\nTorch==2.3.0\nurllib3==2.2.0\nscipy\nJobLib\n"
    assert parse_requirements(text, key_only=True) == ["numpy", "scikit-learn", "torch", "scipy", "joblib"]


def test_parse_requirements_key_only_applies_the_same_skips():
    text = "# numpy==1\nnumpy==2.0; python_version < '3'\nscipy @ file:///x.whl\n-r numpy\ntorch==2.3\n"
    assert parse_requirements(text, key_only=True) == ["torch"]


def test_parse_requirements_pinned_line_yields_the_line_itself_when_not_key_only():
    assert parse_requirements("numpy==2.5.4") == ["numpy==2.5.4"]


def test_key_distributions_use_distribution_names():
    assert envs.key_distributions() == {"numpy", "torch", "scikit-learn", "joblib", "scipy"}


# -- describe_problems ---------------------------------------------------------------------------------------------------


def test_describe_problems_names_missing_packages_with_their_detail():
    problems = [
        {"owner": "c", "package": "pandas", "requirement": "pandas", "problem": "not-installed"},
        {"owner": "c", "package": "torch", "requirement": "torch", "problem": "not-importable", "error": "OSError"},
        {"owner": "c", "package": "numpy", "requirement": "numpy>=3", "problem": "version-mismatch", "found": "2.1"},
    ]
    assert describe_problems(problems) == "pandas, torch (OSError), numpy>=3 (found 2.1)"


def test_describe_problems_exactly_at_the_limit_has_no_suffix():
    problems = [{"package": f"p{i}", "requirement": f"p{i}", "problem": "not-installed"} for i in range(MAX_REPORTED)]
    text = describe_problems(problems)
    assert "more" not in text
    assert text.count(",") == MAX_REPORTED - 1


def test_describe_problems_truncates_past_the_limit_with_and_n_more():
    n = MAX_REPORTED + 7
    problems = [{"package": f"p{i}", "requirement": f"p{i}", "problem": "not-installed"} for i in range(n)]
    text = describe_problems(problems)
    listed, _, suffix = text.rpartition(" and ")
    assert suffix == "7 more"
    assert listed.split(", ") == [f"p{i}" for i in range(MAX_REPORTED)]


def test_describe_problems_empty_is_empty():
    assert describe_problems([]) == ""


# -- capability_summary --------------------------------------------------------------------------------------------------


def test_capability_summary_for_a_probed_environment_maps_modules():
    env = make_env({"numpy": {"importable": True, "version": "2.1"},
                    "joblib": {"importable": True},
                    "sklearn": {"importable": False, "error": "ModuleNotFoundError"}})
    s = capability_summary(env)
    assert s["probed"] is True
    assert s["python_version"] == "3.12.1"
    assert s["interpreter_digest"] == "sha256:" + "ab" * 32
    assert s["modules"] == {"joblib": "present", "numpy": "2.1", "sklearn": None}
    assert list(s["modules"]) == ["joblib", "numpy", "sklearn"]  # sorted


def test_capability_summary_falls_back_to_the_environment_python_version():
    env = make_env(NUMPY_OK)
    del env["capabilities"]["python_version"]
    env["python_version"] = "3.11.9"
    assert capability_summary(env)["python_version"] == "3.11.9"


def test_capability_summary_for_an_unprobed_environment():
    assert capability_summary({"python_version": "3.10.0"}) == {"probed": False, "python_version": "3.10.0"}
    assert capability_summary({"capabilities": None}) == {"probed": False, "python_version": None}


def test_capability_summary_for_a_failed_probe_carries_a_bounded_error():
    env = make_env(status="failed", error="X" * 1000)
    env["python_version"] = "3.9.0"
    s = capability_summary(env)
    assert s["probed"] is False
    assert s["python_version"] == "3.9.0"
    assert s["probe_error"] == "X" * 300
    assert "modules" not in s
