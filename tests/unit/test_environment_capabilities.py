"""Environment capability records and admission preflight (ADR 0020, addendum R10): pure domain rules, no I/O."""

from __future__ import annotations

import sys

import pytest
from packaging.requirements import Requirement

from eidos.domain import environments as envs
from eidos.domain.environments import (
    CAPABILITIES_SCHEMA,
    MAX_INCLUDE_DEPTH,
    MAX_REPORTED,
    assess,
    capability_summary,
    describe_problems,
    import_name,
    normalize_name,
    parse_requirements,
    unsatisfied,
)

# A marker environment that no real interpreter reports, so a verdict that follows it can only have come from the record.
RECORD_MARKERS = {
    "implementation_name": "cpython", "implementation_version": "2.7.18", "os_name": "plan9-os",
    "platform_machine": "m68k", "platform_python_implementation": "CPython", "platform_release": "0.1",
    "platform_system": "Plan9", "platform_version": "#0", "python_full_version": "2.7.18", "python_version": "2.7",
    "sys_platform": "plan9",
}


def make_env(modules=None, *, installed=None, status="ok", import_names=None, marker_environment=RECORD_MARKERS, **caps):
    """An environment record as the lab stores it after a probe."""
    record = {"schema": CAPABILITIES_SCHEMA, "status": status, "interpreter_digest": "sha256:" + "ab" * 32,
              "python_version": "3.12.1", "modules": modules or {}, "import_names": import_names or {}, **caps}
    if marker_environment is not None:
        record["marker_environment"] = dict(marker_environment)
    return {"installed": installed or {}, "capabilities": record}


def provided(version="2.1", distribution="numpy"):
    return {"provided": True, "distribution": distribution, "version": version}


NUMPY_OK = {"numpy": provided("2.1")}
NUMPY_ENV = {"numpy": "2.1", "pytest": "8.3"}


def inconclusive_of(requirements, env):
    return [(i["requirement"], i["inconclusive"]) for i in assess(requirements, env)["inconclusive"]]


def test_the_schema_is_environment_capabilities_2():
    assert CAPABILITIES_SCHEMA == "environment-capabilities/2"
    assert make_env()["capabilities"]["schema"] == "environment-capabilities/2"


# -- unsatisfied: what is refused ---------------------------------------------------------------------------------------


def test_refuses_a_key_module_whose_distribution_is_absent_and_names_the_requirement():
    env = make_env({"sklearn": {"provided": False}}, installed=NUMPY_ENV)
    problems = unsatisfied({"comp@1": ["scikit-learn>=1.0"]}, env)
    assert len(problems) == 1
    p = problems[0]
    assert p["problem"] == "not-installed"
    assert p["owner"] == "comp@1"
    assert p["package"] == "scikit-learn"
    assert p["requirement"] == "scikit-learn>=1.0"


def test_there_is_no_import_measured_problem_any_more():
    # A module record that says nothing is provided cannot refuse a distribution the inventory shows installed.
    env = make_env({"numpy": {"provided": False}}, installed=NUMPY_ENV)
    assert assess({"c": ["numpy>=2.0"]}, env) == {"problems": [], "inconclusive": []}


def test_refuses_a_version_mismatch_and_reports_what_was_found():
    problems = unsatisfied({"comp@1": ["numpy>=3.0"]}, make_env(NUMPY_OK, installed=NUMPY_ENV))
    assert [(p["problem"], p["package"], p["found"]) for p in problems] == [("version-mismatch", "numpy", "2.1")]


def test_the_version_comes_from_the_installed_inventory_not_from_the_module_record():
    env = make_env({"numpy": provided("2.1")}, installed={"numpy": "1.26.4"})
    problems = unsatisfied({"c": ["numpy==2.0.0"]}, env)
    assert [(p["problem"], p["found"]) for p in problems] == [("version-mismatch", "1.26.4")]


@pytest.mark.parametrize(("installed", "refused"), [("1.26.4", True), ("2.1", False), ("2.0.0", False), ("3.0.0", True),
                                                    ("3.0.0rc1", True), ("2.9.9", False), ("1.99", True)])
def test_a_compound_constraint_uses_the_whole_specifier(installed, refused):
    problems = unsatisfied({"c": ["numpy>=2,<3"]}, make_env(installed={"numpy": installed, "pytest": "8.3"}))
    assert [(p["problem"], p["found"]) for p in problems] == ([("version-mismatch", installed)] if refused else [])


def test_numpy_two_to_three_is_refused_at_1_26_and_satisfied_at_2_1():
    refused = unsatisfied({"c": ["numpy>=2,<3"]}, make_env(installed={"numpy": "1.26.4"}))
    assert [(p["package"], p["found"]) for p in refused] == [("numpy", "1.26.4")]
    assert unsatisfied({"c": ["numpy>=2,<3"]}, make_env(installed={"numpy": "2.1.0"})) == []


def test_an_installed_prerelease_counts_as_the_version_it_is():
    # A range with no pre-release bound would, by default, exclude a pre-release; here it is the installed one.
    assert unsatisfied({"c": ["numpy>=1.0"]}, make_env(installed={"numpy": "2.0.0rc1"})) == []
    problems = unsatisfied({"c": ["numpy>=2.0"]}, make_env(installed={"numpy": "2.0.0rc1"}))
    assert [p["problem"] for p in problems] == ["version-mismatch"]


def test_an_unreadable_installed_version_is_inconclusive_not_refused():
    env = make_env(installed={"numpy": "not a version", "pytest": "8.3"})
    result = assess({"c": ["numpy>=1.0"]}, env)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"]) for i in result["inconclusive"]] == [("numpy", "version-unreadable")]


def test_refuses_a_package_absent_from_a_non_empty_inventory():
    problems = unsatisfied({"c": ["pandas>=2.0"]}, make_env(installed=NUMPY_ENV))
    assert [(p["problem"], p["package"]) for p in problems] == [("not-installed", "pandas")]


def test_an_empty_inventory_is_not_evidence_of_absence():
    assert assess({"c": ["pandas>=2.0"]}, make_env(installed={})) == {"problems": [], "inconclusive": []}


def test_inventory_lookup_normalizes_distribution_names():
    env = make_env(installed={"typing_extensions": "4.12.0"})
    assert unsatisfied({"c": ["Typing.Extensions>=4.0"]}, env) == []
    assert unsatisfied({"c": ["typing-extensions>=5"]}, env)[0]["found"] == "4.12.0"


def test_unsatisfied_is_the_refusable_part_of_assess():
    env = make_env(installed=NUMPY_ENV)
    reqs = {"c": ["pandas", "x @ https://example.invalid/x.whl", "numpy"]}
    assert unsatisfied(reqs, env) == assess(reqs, env)["problems"]
    assert [p["package"] for p in unsatisfied(reqs, env)] == ["pandas"]


# -- what is never refused ----------------------------------------------------------------------------------------------


def test_missing_capabilities_are_never_refused():
    env = {"installed": {"numpy": "1.0"}}
    assert unsatisfied({"c": ["numpy>=9.0", "torch"]}, env) == []
    assert unsatisfied({"c": ["numpy>=9.0"]}, {"installed": {}, "capabilities": None}) == []
    assert assess({"c": ["numpy>=9.0"]}, env) == {"problems": [], "inconclusive": []}


def test_a_failed_probe_is_never_refused_even_with_modules_recorded():
    env = make_env({"numpy": {"provided": False}}, installed={"x": "1"}, status="failed",
                   error="probe stopped after 3 of 5 modules (exit -11)")
    assert assess({"c": ["numpy>=1.0", "pandas"]}, env) == {"problems": [], "inconclusive": []}


def test_an_old_schema_record_counts_as_unprobed_and_is_never_refused():
    env = make_env(installed={"numpy": "1.26.4", "pytest": "8.3"})
    env["capabilities"]["schema"] = "environment-capabilities/1"
    env["capabilities"]["modules"] = {"numpy": {"importable": False, "error": "ImportError"}}
    assert assess({"c": ["numpy>=2", "pandas"]}, env) == {"problems": [], "inconclusive": []}
    assert capability_summary(env)["probed"] is False


def test_a_record_with_no_schema_counts_as_unprobed():
    env = make_env(installed=NUMPY_ENV)
    del env["capabilities"]["schema"]
    assert unsatisfied({"c": ["pandas", "numpy>=3"]}, env) == []


def test_lab_provided_eidos_packages_are_not_refused():
    # Not in the inventory, and the inventory is non-empty: only the lab prefix exempts them.
    env = make_env(installed={"numpy": "2.1"})
    assert assess({"c": ["eidos-wire>=1.0", "eidos-sdk", "eidos.runtime==0.1"]}, env) == {"problems": [], "inconclusive": []}


def test_accepts_a_satisfied_requirement():
    env = make_env({**NUMPY_OK, "sklearn": provided("1.5.0", "scikit-learn")},
                   installed={"numpy": "2.1", "scikit-learn": "1.5.0"})
    assert assess({"a": ["numpy>=2.0", "scikit-learn"], "b": ["numpy==2.1"]}, env) == {"problems": [], "inconclusive": []}


def test_unversioned_requirement_for_an_installed_distribution_is_satisfied():
    assert unsatisfied({"c": ["numpy"]}, make_env(NUMPY_OK, installed=NUMPY_ENV)) == []


def test_unparseable_requirements_are_inconclusive_and_never_refused():
    env = make_env(NUMPY_OK, installed=NUMPY_ENV)
    result = assess({"c": ["not a requirement!!"]}, env)
    assert result["problems"] == []
    assert [(i["owner"], i["requirement"], i["inconclusive"]) for i in result["inconclusive"]] == [
        ("c", "not a requirement!!", "not-a-parsed-requirement")]


def test_same_owner_and_package_is_reported_once_but_other_owners_are_separate():
    env = make_env(installed={"pytest": "8.3"})
    problems = unsatisfied({"a": ["numpy>=1", "numpy<3"], "b": ["numpy"]}, env)
    assert sorted((p["owner"], p["package"]) for p in problems) == [("a", "numpy"), ("b", "numpy")]


def test_same_owner_and_package_version_mismatches_are_reported_once():
    problems = unsatisfied({"a": ["numpy>=3", "numpy<2"]}, make_env(installed=NUMPY_ENV))
    assert [(p["owner"], p["package"], p["requirement"]) for p in problems] == [("a", "numpy", "numpy>=3")]


def test_reports_every_problem_across_owners():
    env = make_env({**NUMPY_OK, "torch": {"provided": False}}, installed=NUMPY_ENV)
    problems = unsatisfied({"a": ["torch", "numpy>=5"], "b": ["pandas"]}, env)
    assert {(p["owner"], p["package"], p["problem"]) for p in problems} == {
        ("a", "torch", "not-installed"), ("a", "numpy", "version-mismatch"), ("b", "pandas", "not-installed")}


# -- R10 finding 3: a distribution whose import name is not its own name -----------------------------------------------


def test_python_dateutil_is_satisfied_when_installed_and_never_refused():
    env = make_env({"dateutil": provided("2.9.0", "python-dateutil")}, installed={"python-dateutil": "2.9.0", "pytest": "8.3"},
                   import_names={"dateutil": "python-dateutil"})
    reqs = {"c": ["python-dateutil", "python-dateutil>=2.8", "python_dateutil==2.9.0", "Python.Dateutil<3"]}
    assert assess(reqs, env) == {"problems": [], "inconclusive": []}


def test_a_distribution_with_unknown_import_names_is_not_a_refusal():
    env = make_env({"numpy": {"provided": None}, "mystery": {"provided": None, "distribution": "mystery-pkg", "version": "1.0"}},
                   installed={"mystery-pkg": "1.0", "pytest": "8.3"}, unmapped=["mystery-pkg"])
    result = assess({"c": ["mystery-pkg>=1", "numpy", "pandas"]}, env)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"]) for i in result["inconclusive"]] == [
        ("numpy", "unknown-import-mapping"), ("pandas", "unknown-import-mapping")]


def test_an_unknown_mapping_does_not_shield_a_version_mismatch_of_an_installed_distribution():
    env = make_env(installed={"numpy": "1.26.4", "pytest": "8.3"}, unmapped=["mystery-pkg"])
    assert [p["problem"] for p in unsatisfied({"c": ["numpy>=2"]}, env)] == ["version-mismatch"]


def test_an_unknown_mapping_that_never_applied_does_not_inconclude_installed_requirements():
    env = make_env(installed={"numpy": "2.1", "pytest": "8.3"}, unmapped=["mystery-pkg"])
    assert assess({"c": ["numpy>=2"]}, env) == {"problems": [], "inconclusive": []}


def test_a_package_absent_from_the_inventory_whose_import_name_another_distribution_provides_is_inconclusive():
    env = make_env(installed={"scikit-learn-intelex": "2024.1", "pytest": "8.3"},
                   import_names={"sklearn": "scikit-learn-intelex"})
    result = assess({"c": ["scikit-learn>=1.0"]}, env)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"], i["found"]) for i in result["inconclusive"]] == [
        ("scikit-learn", "provided-by-another-distribution", "scikit-learn-intelex")]


def test_the_guessed_import_name_of_an_absent_distribution_follows_the_import_name_table():
    env = make_env(installed={"pytest": "8.3"}, import_names={"yaml": "ruamel-yaml", "PIL": "pillow-simd", "cv2": "opencv-contrib"})
    assert unsatisfied({"c": ["pyyaml", "pillow", "opencv-python"]}, env) == []
    assert [r for r, _ in inconclusive_of({"c": ["pyyaml", "pillow", "opencv-python"]}, env)] == ["pyyaml", "pillow", "opencv-python"]


def test_an_absent_package_with_no_other_provider_and_a_complete_mapping_is_refused():
    env = make_env(installed={"pytest": "8.3"}, import_names={"dateutil": "python-dateutil"})
    assert [p["package"] for p in unsatisfied({"c": ["scikit-learn", "pandas"]}, env)] == ["scikit-learn", "pandas"]


def test_a_truncated_import_name_table_makes_an_absent_package_inconclusive():
    env = make_env(installed={"pytest": "8.3"}, import_names_truncated=True)
    result = assess({"c": ["pandas"]}, env)
    assert result["problems"] == []
    assert [i["inconclusive"] for i in result["inconclusive"]] == ["unknown-import-mapping"]


def test_a_provider_elsewhere_does_not_shield_a_version_mismatch_of_the_installed_distribution():
    env = make_env(installed={"numpy": "1.26.4", "pytest": "8.3"}, import_names={"numpy": "numpy-compat"})
    assert [p["problem"] for p in unsatisfied({"c": ["numpy>=2"]}, env)] == ["version-mismatch"]


# -- R10 finding 4: markers, extras and requirements that cannot be decided -------------------------------------------


def test_a_marker_that_is_false_for_the_probed_interpreter_is_skipped():
    env = make_env(installed=NUMPY_ENV)
    assert assess({"c": ['colorama>=0.4; sys_platform == "win32"', 'pywin32; os_name == "nt"']}, env) == {
        "problems": [], "inconclusive": []}


def test_a_marker_that_is_true_for_the_probed_interpreter_applies():
    env = make_env(installed=NUMPY_ENV)
    problems = unsatisfied({"c": ['colorama>=0.4; sys_platform == "plan9"', 'numpy>=3; python_version < "3"']}, env)
    assert [(p["package"], p["problem"]) for p in problems] == [("colorama", "not-installed"), ("numpy", "version-mismatch")]


def test_the_records_marker_environment_decides_not_the_running_interpreter():
    env = make_env(installed=NUMPY_ENV)  # a plan9 / Python 2.7 record, whatever runs this test
    running = f'sys_platform == "{sys.platform}"'
    assert unsatisfied({"c": [f"colorama; {running}"]}, env) == []  # true here, false in the record: skipped
    assert [p["package"] for p in unsatisfied({"c": ['colorama; sys_platform == "plan9"']}, env)] == ["colorama"]
    version = f'python_version == "{sys.version_info.major}.{sys.version_info.minor}"'
    assert unsatisfied({"c": [f"colorama; {version}"]}, env) == []
    assert [p["package"] for p in unsatisfied({"c": ['colorama; python_version == "2.7"']}, env)] == ["colorama"]


def test_a_marker_with_extra_at_the_top_level_does_not_apply():
    assert unsatisfied({"c": ['colorama; extra == "win"']}, make_env(installed=NUMPY_ENV)) == []


def test_a_requirement_with_a_marker_is_inconclusive_when_the_record_has_no_marker_environment():
    env = make_env(installed=NUMPY_ENV, marker_environment=None)
    result = assess({"c": ['colorama; sys_platform == "win32"', "pandas"]}, env)
    assert [p["package"] for p in result["problems"]] == ["pandas"]  # an unmarked requirement is still decided
    assert [(i["package"], i["inconclusive"]) for i in result["inconclusive"]] == [("colorama", "marker-not-evaluated")]


REQUESTS_EXTRAS = ['PySocks!=1.5.7,>=1.5.6; extra == "socks"', 'chardet<6,>=3.0.2; extra == "use-chardet-on-py3"',
                   'colorama; extra == "win" and sys_platform == "win32"', 'idna>=3; python_version >= "1" or extra == "socks"']


def requests_env(installed=None, **caps):
    return make_env(installed={"requests": "2.32.0", **(installed or {})}, distributions={"requests": {"requires_dist": REQUESTS_EXTRAS}},
                    marker_environment={**RECORD_MARKERS, "python_version": "3.12"}, **caps)


def test_an_extra_is_satisfied_when_what_it_adds_is_installed():
    env = requests_env({"pysocks": "1.7.1"})
    assert assess({"c": ["requests[socks]>=2"]}, env) == {"problems": [], "inconclusive": []}


def test_an_extra_is_unsatisfied_when_what_it_adds_is_missing():
    problems = unsatisfied({"c": ["requests[socks]"]}, requests_env({"pytest": "8.3"}))
    assert [(p["problem"], p["package"], p["via_extra"], p["requirement"]) for p in problems] == [
        ("not-installed", "pysocks", "socks", "requests[socks]")]
    assert describe_problems(problems) == "pysocks (extra socks)"


def test_an_extra_is_unsatisfied_when_what_it_adds_is_installed_at_the_wrong_version():
    problems = unsatisfied({"c": ["requests[socks]"]}, requests_env({"pysocks": "1.5.7"}))
    assert [(p["problem"], p["package"], p["via_extra"], p["found"]) for p in problems] == [
        ("version-mismatch", "pysocks", "socks", "1.5.7")]


def test_extras_are_compared_without_regard_to_case_or_separators():
    env = requests_env()
    assert [p["package"] for p in unsatisfied({"c": ["requests[SOCKS]"]}, env)] == ["pysocks"]
    assert [p["package"] for p in unsatisfied({"c": ["requests[Use_Chardet_On_Py3]"]}, env)] == ["chardet"]


def test_each_requested_extra_is_checked_and_an_unrequested_one_is_not():
    both = "requests[socks,use-chardet-on-py3]"
    only_socks = requests_env({"pysocks": "1.7.1"})
    assert unsatisfied({"c": ["requests[socks]"]}, only_socks) == []
    assert [p["package"] for p in unsatisfied({"c": [both]}, only_socks)] == ["chardet"]
    assert [p["package"] for p in unsatisfied({"c": [both]}, requests_env({"chardet": "5.2.0"}))] == ["pysocks"]
    assert unsatisfied({"c": [both]}, requests_env({"pysocks": "1.7.1", "chardet": "5.2.0"})) == []


def test_an_extras_requirement_whose_marker_is_false_for_the_record_is_not_needed():
    assert unsatisfied({"c": ["requests[win]"]}, requests_env()) == []


def test_what_the_base_distribution_already_requires_is_not_attributed_to_the_extra():
    # idna is required with or without the extra: this check does not read the base requirements, so it is not refused here.
    assert unsatisfied({"c": ["requests[socks]"]}, requests_env({"pysocks": "1.7.1"})) == []


def test_an_extra_whose_requirement_is_provided_by_another_distribution_is_inconclusive():
    env = requests_env({"pytest": "8.3"}, import_names={"pysocks": "pysocks-compat"})
    result = assess({"c": ["requests[socks]"]}, env)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"], i["found"], i["via_extra"]) for i in result["inconclusive"]] == [
        ("pysocks", "provided-by-another-distribution", "pysocks-compat", "socks")]


def test_extras_are_inconclusive_when_the_record_did_not_keep_the_distributions_requirements():
    env = make_env(installed={"requests": "2.32.0"})
    result = assess({"c": ["requests[socks]"]}, env)
    assert result["problems"] == []
    assert [(i["package"], i["inconclusive"]) for i in result["inconclusive"]] == [("requests", "extras-not-recorded")]


def test_extras_are_inconclusive_when_a_recorded_requirement_cannot_be_read():
    env = make_env(installed={"requests": "2.32.0"}, distributions={"requests": {"requires_dist": ["not valid!!; extra == 'x'"]}})
    result = assess({"c": ["requests[x]"]}, env)
    assert result["problems"] == []
    assert [i["inconclusive"] for i in result["inconclusive"]] == ["extras-not-recorded"]


def test_extras_of_an_absent_distribution_are_not_checked_the_distribution_is_what_is_missing():
    problems = unsatisfied({"c": ["flask[async]"]}, requests_env())
    assert [(p["problem"], p["package"]) for p in problems] == [("not-installed", "flask")]


@pytest.mark.parametrize("line", [
    "foo @ https://example.com/foo.whl",
    "foo @ git+https://example.com/foo.git@main",
    "numpy @ https://example.com/numpy.whl",  # installed, but the URL is a different artifact: not decided
    "-r missing.txt",
    "-e .",
    "-e git+https://example.com/x.git#egg=x",
    "--constraint c.txt",
    "-c constraints.txt",
    "git+https://example.com/x.git",
    "https://example.com/bare.whl",
    "./local/pkg",
])
def test_what_cannot_be_decided_is_inconclusive_and_never_refused(line):
    env = make_env(NUMPY_OK, installed=NUMPY_ENV)
    result = assess({"c": [line]}, env)
    assert result["problems"] == []
    assert [i["requirement"] for i in result["inconclusive"]] == [line]
    assert unsatisfied({"c": [line]}, env) == []


def test_a_url_requirement_with_a_false_marker_is_skipped_but_with_a_true_marker_inconclusive():
    env = make_env(installed=NUMPY_ENV)
    assert assess({"c": ['foo @ https://example.com/foo.whl ; sys_platform == "win32"']}, env) == {
        "problems": [], "inconclusive": []}
    assert [i["inconclusive"] for i in assess({"c": ['foo @ https://example.com/foo.whl ; sys_platform == "plan9"']},
                                              env)["inconclusive"]] == ["url-requirement"]


def test_inconclusive_requirements_are_reported_once_per_owner_and_package():
    env = make_env(installed=NUMPY_ENV)
    lines = ["foo @ https://example.com/a.whl", "foo @ https://example.com/b.whl", "-e .", "-e ."]
    assert [i["requirement"] for i in assess({"c": lines, "d": lines[:1]}, env)["inconclusive"]] == [
        "foo @ https://example.com/a.whl", "-e .", "foo @ https://example.com/a.whl"]


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


def test_parse_requirements_drops_comments_and_source_options_and_keeps_the_rest_as_written():
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
    assert parse_requirements(text) == [
        "-r other.txt", "-e .", "pkg-url @ https://example.invalid/pkg.whl", "https://example.invalid/bare.whl",
        'colorama>=0.4; sys_platform == "win32"', "numpy>=1.0", "requests==2.31.0", "pandas"]


def test_parse_requirements_does_not_skip_marker_lines():
    assert parse_requirements('foo>=1; python_version < "3.9"\nbar; sys_platform == "win32"\n') == [
        'foo>=1; python_version < "3.9"', 'bar; sys_platform == "win32"']


def test_parse_requirements_does_not_treat_a_url_fragment_as_a_comment():
    line = "pkg @ https://example.invalid/pkg.whl#sha256=abc123"
    assert parse_requirements(line + "  # why") == [line]


def test_parse_requirements_strips_line_continuation_backslash():
    assert parse_requirements("numpy>=1.0 \\\n") == ["numpy>=1.0"]


def test_parse_requirements_joins_a_requirement_continued_over_lines():
    assert [Requirement(r) for r in parse_requirements("numpy \\\n    >=1.0 \\\n    ,<3\n")] == [Requirement("numpy>=1.0,<3")]
    out = parse_requirements("numpy \\\n    >=1.0\nrequests\n")
    assert [Requirement(r) for r in out] == [Requirement("numpy>=1.0"), Requirement("requests")]


def test_parse_requirements_joins_continuations_in_a_file_with_windows_line_endings():
    out = parse_requirements("numpy \\\r\n  >=1\r\nrequests\r\n")
    assert [Requirement(r) for r in out] == [Requirement("numpy>=1"), Requirement("requests")]


def test_parse_requirements_drops_hash_options_including_continued_ones():
    text = "numpy==2.0.1 \\\n    --hash=sha256:aaaa \\\n    --hash=sha256:bbbb\nrequests==2.31.0 --hash=sha256:cccc\n"
    assert parse_requirements(text) == ["numpy==2.0.1", "requests==2.31.0"]


@pytest.mark.parametrize("option", [
    "--index-url https://example.invalid/simple", "-i https://example.invalid/simple", "--index-url=https://example.invalid/simple",
    "--extra-index-url https://example.invalid/simple", "--no-index", "-f ./wheels", "--find-links ./wheels",
    "--trusted-host example.invalid", "--no-binary :all:", "--only-binary :all:", "--prefer-binary", "--require-hashes", "--pre",
])
def test_parse_requirements_drops_options_that_say_where_packages_come_from(option):
    assert parse_requirements(f"{option}\nnumpy\n") == ["numpy"]


@pytest.mark.parametrize("line", ["-c constraints.txt", "--constraint constraints.txt", "-e .", "--editable ./pkg", "-r missing.txt",
                                  "--requirement missing.txt", "--no-such-option"])
def test_parse_requirements_keeps_what_it_cannot_resolve_for_assess_to_report(line):
    assert parse_requirements(f"numpy\n{line}\n") == ["numpy", line]


def test_parse_requirements_keeps_an_include_when_no_reader_is_given():
    assert parse_requirements("-r base.txt\nnumpy\n") == ["-r base.txt", "numpy"]


def test_parse_requirements_keeps_an_include_the_reader_cannot_read():
    assert parse_requirements("-r base.txt\nnumpy\n", include=lambda path: None) == ["-r base.txt", "numpy"]


def test_parse_requirements_follows_includes_in_place_by_path_relative_to_the_root():
    files = {"base.txt": "scipy\n# done\n", "requirements.txt": "unused"}
    asked = []

    def read(path):
        asked.append(path)
        return files.get(path)

    text = "numpy\n-r base.txt\n--requirement base.txt\nrequests\n"
    assert parse_requirements(text, include=read) == ["numpy", "scipy", "scipy", "requests"]
    assert asked == ["base.txt", "base.txt"]


def test_parse_requirements_resolves_nested_includes_relative_to_the_including_file():
    files = {
        "reqs/base.txt": "-r extra/more.txt\n-r ../top.txt\nbase-pkg\n",
        "reqs/extra/more.txt": "more-pkg\n-r ./sibling.txt\n",
        "reqs/extra/sibling.txt": "sibling-pkg\n",
        "top.txt": "top-pkg\n",
    }
    asked = []

    def read(path):
        asked.append(path)
        return files.get(path)

    out = parse_requirements("-r reqs/base.txt\nroot-pkg\n", include=read)
    assert out == ["more-pkg", "sibling-pkg", "top-pkg", "base-pkg", "root-pkg"]
    assert asked == ["reqs/base.txt", "reqs/extra/more.txt", "reqs/extra/sibling.txt", "top.txt"]


def test_parse_requirements_does_not_loop_on_a_cycle():
    files = {"requirements.txt": "-r b.txt\nnumpy\n", "b.txt": "-r requirements.txt\nscipy\n"}
    out = parse_requirements(files["requirements.txt"], include=files.get)
    assert out == ["-r requirements.txt", "scipy", "numpy"]  # the closing include is kept, unresolved


def test_parse_requirements_does_not_loop_on_a_file_that_includes_itself():
    assert parse_requirements("-r requirements.txt\nnumpy\n", include=lambda path: "-r requirements.txt\nnumpy\n") == [
        "-r requirements.txt", "numpy"]


def test_parse_requirements_follows_a_file_included_twice_without_a_cycle():
    files = {"a.txt": "-r b.txt\n-r c.txt\n", "b.txt": "-r d.txt\nb-pkg\n", "c.txt": "-r d.txt\nc-pkg\n", "d.txt": "d-pkg\n"}
    assert parse_requirements(files["a.txt"], include=files.get, path="a.txt") == ["d-pkg", "b-pkg", "d-pkg", "c-pkg"]


def test_parse_requirements_stops_following_includes_at_a_bounded_depth():
    def read(path):
        n = int(path.removeprefix("f").removesuffix(".txt"))
        return f"-r f{n + 1}.txt\npkg{n}\n"

    out = parse_requirements("-r f1.txt\npkg0\n", include=read)
    assert out[-1] == "pkg0"
    unresolved = [line for line in out if line.startswith("-r ")]
    assert len(unresolved) == 1  # the chain ended at the bound, not by running out of files
    assert len([line for line in out if line.startswith("pkg")]) == MAX_INCLUDE_DEPTH + 1


@pytest.mark.parametrize("target", ["../outside.txt", "sub/../../outside.txt", "/etc/outside.txt", "../../x/y.txt", ".."])
def test_parse_requirements_never_asks_for_a_path_outside_the_root(target):
    asked = []

    def read(path):
        asked.append(path)
        return "secret-pkg\n"

    out = parse_requirements(f"numpy\n-r {target}\n", include=read)
    assert out == ["numpy", f"-r {target}"]
    assert asked == []


def test_parse_requirements_applies_the_escape_check_to_nested_includes_too():
    files = {"sub/inner.txt": "-r ../../outside.txt\ninner-pkg\n"}
    asked = []

    def read(path):
        asked.append(path)
        return files.get(path, "secret-pkg\n")

    out = parse_requirements("-r sub/inner.txt\n", include=read)
    assert out == ["-r ../../outside.txt", "inner-pkg"]
    assert asked == ["sub/inner.txt"]


def test_parse_requirements_a_path_that_leaves_and_returns_stays_inside_the_root():
    asked = []

    def read(path):
        asked.append(path)
        return "base-pkg\n"

    assert parse_requirements("-r sub/../base.txt\n", include=read) == ["base-pkg"]
    assert asked == ["base.txt"]


def test_parse_requirements_key_only_keeps_key_modules_without_versions():
    text = "numpy==2.5.4\nrequests==2.31.0\nscikit-learn>=1.2\nTorch==2.3.0\nurllib3==2.2.0\nscipy\nJobLib\n"
    assert parse_requirements(text, key_only=True) == ["numpy", "scikit-learn", "torch", "scipy", "joblib"]


def test_parse_requirements_key_only_keeps_markers_urls_and_what_it_cannot_read_and_drops_comments_and_other_packages():
    text = "# numpy==1\nnumpy==2.0; python_version < '3'\nscipy @ file:///x.whl\n-r numpy\nnot a requirement!!\ntorch==2.3\nrequests==1\n"
    assert parse_requirements(text, key_only=True) == [
        'numpy; python_version < "3"', "scipy @ file:///x.whl", "-r numpy", "not a requirement!!", "torch"]


def test_parse_requirements_key_only_applies_to_included_files():
    files = {"base.txt": "numpy==2.0\nurllib3==2.2\n"}
    assert parse_requirements("-r base.txt\ntorch==2.3\nidna==3\n", key_only=True, include=files.get) == ["numpy", "torch"]


def test_parse_requirements_pinned_line_yields_the_line_itself_when_not_key_only():
    assert parse_requirements("numpy==2.5.4") == ["numpy==2.5.4"]


def test_key_distributions_use_distribution_names():
    assert envs.key_distributions() == {"numpy", "torch", "scikit-learn", "joblib", "scipy"}


def test_what_parse_requirements_keeps_is_what_assess_decides_on():
    """A requirements file end to end: a pin that is unmet is refused; everything it cannot read is only reported."""
    text = "\n".join(["numpy>=3", "-r missing.txt", "-e .", "foo @ https://example.invalid/foo.whl", 'colorama; sys_platform == "win32"',
                      "--no-such-option"])
    env = make_env(NUMPY_OK, installed=NUMPY_ENV)
    result = assess({"project": parse_requirements(text)}, env)
    assert [p["package"] for p in result["problems"]] == ["numpy"]
    assert sorted(i["requirement"] for i in result["inconclusive"]) == sorted(
        ["-r missing.txt", "-e .", "foo @ https://example.invalid/foo.whl", "--no-such-option"])


# -- describe_problems ---------------------------------------------------------------------------------------------------


def test_describe_problems_names_missing_packages_with_their_detail():
    problems = [
        {"owner": "c", "package": "pandas", "requirement": "pandas", "problem": "not-installed"},
        {"owner": "c", "package": "pysocks", "requirement": "requests[socks]", "problem": "not-installed", "via_extra": "socks"},
        {"owner": "c", "package": "numpy", "requirement": "numpy>=3", "problem": "version-mismatch", "found": "2.1"},
    ]
    assert describe_problems(problems) == "pandas, pysocks (extra socks), numpy>=3 (found 2.1)"


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
    env = make_env({"numpy": provided("2.1"),
                    "joblib": {"provided": True, "distribution": "joblib", "version": None},
                    "sklearn": {"provided": False},
                    "torch": {"provided": None}})
    s = capability_summary(env)
    assert s["probed"] is True
    assert s["python_version"] == "3.12.1"
    assert s["interpreter_digest"] == "sha256:" + "ab" * 32
    assert s["modules"] == {"joblib": "present", "numpy": "2.1", "sklearn": None, "torch": "unknown"}
    assert list(s["modules"]) == ["joblib", "numpy", "sklearn", "torch"]  # sorted
    assert "inconclusive_requirements" not in s


def test_capability_summary_shows_an_unknown_mapping_as_unknown_not_absent():
    env = make_env({"numpy": {"provided": None}, "mystery_pkg": {"provided": None, "distribution": "mystery-pkg", "version": "1.0"}})
    assert capability_summary(env)["modules"] == {"mystery_pkg": "unknown", "numpy": "unknown"}


def test_capability_summary_carries_the_requirements_the_probe_could_not_decide():
    undecided = [{"requirement": "-e .", "inconclusive": "not-a-parsed-requirement"}]
    assert capability_summary(make_env(NUMPY_OK, inconclusive=undecided))["inconclusive_requirements"] == undecided


def test_capability_summary_falls_back_to_the_environment_python_version():
    env = make_env(NUMPY_OK)
    del env["capabilities"]["python_version"]
    env["python_version"] = "3.11.9"
    assert capability_summary(env)["python_version"] == "3.11.9"


def test_capability_summary_for_an_unprobed_environment():
    assert capability_summary({"python_version": "3.10.0"}) == {"probed": False, "python_version": "3.10.0"}
    assert capability_summary({"capabilities": None}) == {"probed": False, "python_version": None}


def test_capability_summary_for_an_old_schema_record_is_unprobed():
    env = make_env(NUMPY_OK)
    env["capabilities"]["schema"] = "environment-capabilities/1"
    env["python_version"] = "3.12.1"
    assert capability_summary(env) == {"probed": False, "python_version": "3.12.1"}


def test_capability_summary_for_a_failed_probe_carries_a_bounded_error():
    env = make_env(status="failed", error="X" * 1000)
    env["python_version"] = "3.9.0"
    s = capability_summary(env)
    assert s["probed"] is False
    assert s["python_version"] == "3.9.0"
    assert s["probe_error"] == "X" * 300
    assert "modules" not in s
