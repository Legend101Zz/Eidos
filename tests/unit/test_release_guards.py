"""H2 (R17 #4): the lab's invocation guard for the pinned release ``eidos.search.recipe-sweep@1.0.0``.

``components/eidos.search.recipe-sweep@1.0.0/`` stays byte for byte as it shipped (its bundle digest is pinned), so the
stricter held-out file-name rule of 1.1.0 cannot be added to its code. ``eidos.domain.release_guards`` carries that rule
and the lab applies it at resolution. Author tests of pure functions: no database, no models, no network. The lab path
(the refusal at ``run_component``) is ``tests/integration/test_pinned_component_release.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from eidos.domain import release_guards
from eidos.domain.release_guards import guarded, names_heldout_file, problems, sweep_problems

ROOT = Path(__file__).resolve().parents[2]
SWEEP = "eidos.search.recipe-sweep"
PINNED = f"{SWEEP}@1.0.0"
CURRENT = f"{SWEEP}@1.1.0"

# Names 1.0.0 let through (a held-out word not delimited by non-alphanumerics) that 1.1.0 refuses.
LEAKS_IN_1_0_0 = ["test1.csv", "testfeatures.csv", "x_test2.npy", "holdout3.npz"]
HELDOUT_NAMES = [*LEAKS_IN_1_0_0, "TestData.csv", "heldout.csv"]
# Checked against the rule: no word begins with test/holdout/heldout and no held-out split name sits at a boundary.
ORDINARY_NAMES = ["train.csv", "fit.py", "latest.csv", "contest.csv", "attestation.json"]


def load_sweep(directory: str, alias: str):
    """The sweep module of one component directory, loaded under its own module name (both releases are named
    ``recipe_sweep``, so a plain import could only ever find one of them)."""
    path = ROOT / "components" / directory / "recipe_sweep.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sweep_1_1_0():
    return load_sweep(SWEEP, "recipe_sweep_release_1_1_0")


@pytest.fixture(scope="module")
def sweep_1_0_0():
    return load_sweep(f"{SWEEP}@1.0.0", "recipe_sweep_release_1_0_0")


# -- which releases are guarded --------------------------------------------------------------------------------------
def test_only_the_release_shipped_before_the_fix_is_guarded():
    assert guarded(PINNED) is True


@pytest.mark.parametrize("ref", [CURRENT, f"{SWEEP}@1.2.0", f"{SWEEP}@0.9.0", SWEEP, f"{SWEEP}@", "eidos.project.command@1.0.0",
                                 "eidos.search.recipe-sweep-extra@1.0.0", "", "@1.0.0"])
def test_other_releases_and_other_components_are_not_guarded(ref):
    assert guarded(ref) is False


# -- the file-name rule -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", HELDOUT_NAMES)
def test_a_word_beginning_with_a_held_out_prefix_names_a_held_out_file(name):
    assert names_heldout_file(name) is True


@pytest.mark.parametrize("name", ["Test.csv", "my-test-data.csv", "test_set.csv", "TestSet.csv", "hold_out.csv",
                                  "held-out.csv", "HoldOut.csv", "testing.csv", "data/test1.csv", "labels_holdout.npz"])
def test_the_split_names_and_prefixed_words_in_other_spellings_are_refused_too(name):
    assert names_heldout_file(name) is True


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_an_ordinary_file_name_is_not_taken_for_a_held_out_one(name):
    """``latest``, ``contest`` and ``attestation`` contain the letters of ``test`` inside a word, not at its start."""
    assert names_heldout_file(name) is False


@pytest.mark.parametrize("name", ["validation.csv", "val_features.npz", "protest.csv", "fastest-run.json", "data.csv"])
def test_more_ordinary_names_pass(name):
    assert names_heldout_file(name) is False


def test_a_non_string_name_is_judged_by_its_text():
    assert names_heldout_file(None) is False
    assert names_heldout_file(1234) is False


# -- the invocation guard: command.copy ------------------------------------------------------------------------------
def test_a_copy_list_naming_held_out_files_is_refused_entry_by_entry():
    out = problems(PINNED, {"command": {"copy": ["train.csv", "test1.csv", "testfeatures.csv"]}}, None)
    assert len(out) == 2
    assert "'test1.csv'" in out[0] and "'testfeatures.csv'" in out[1]
    assert all("held-out split" in line and "command.copy" in line for line in out)


def test_a_copy_list_of_ordinary_files_is_accepted():
    assert problems(PINNED, {"command": {"copy": ["train.csv", "fit.py", "latest.csv"]}}, None) == []


@pytest.mark.parametrize("config", [None, {}, {"command": None}, {"command": {}}, {"command": {"argv": ["python", "fit.py"]}},
                                    {"command": "python fit.py"}, {"command": {"copy": "test1.csv"}},
                                    {"command": {"copy": None}}, {"command": {"copy": [1, None, ["test1.csv"]]}}])
def test_a_missing_or_malformed_copy_list_is_not_the_guards_to_judge(config):
    """Shape errors are the component contract's to report; the guard only judges what it can read as a name."""
    assert problems(PINNED, config, None) == []


# -- the invocation guard: the source bundle -------------------------------------------------------------------------
def test_a_source_bundle_with_held_out_named_data_is_refused():
    out = problems(PINNED, {}, ["fit.py", "data/train.csv", "testfeatures.csv", "labels/x_test2.npy"])
    assert len(out) == 2
    assert "'testfeatures.csv'" in out[0] and "'labels/x_test2.npy'" in out[1]
    assert all("source bundle file" in line for line in out)


def test_code_files_may_carry_a_held_out_name_but_data_under_such_a_name_may_not():
    """Source suffixes (.py .sh .md .toml) are exempt, as in 1.1.0: a script named test_model.py is code, not data."""
    assert problems(PINNED, {}, ["fit.py", "test_model.py", "run_tests.sh", "testing-notes.md", "holdout.toml"]) == []
    out = problems(PINNED, {}, ["tests/test1.csv"])
    assert len(out) == 1 and "'tests/test1.csv'" in out[0]


def test_a_directory_with_a_held_out_name_taints_the_data_inside_it_but_not_code():
    assert problems(PINNED, {}, ["tests/fit.py", "heldout/runner.sh"]) == []
    assert len(problems(PINNED, {}, ["tests/train.csv"])) == 1
    assert len(problems(PINNED, {}, ["holdout/features.npz"])) == 1


def test_a_source_bundle_of_ordinary_files_is_accepted():
    assert problems(PINNED, {}, ["fit.py", "data/train.csv", "latest.csv", "contest.csv", "attestation.json"]) == []


def test_backslash_paths_are_read_as_directories():
    assert len(problems(PINNED, {}, ["tests\\train.csv"])) == 1
    assert problems(PINNED, {}, ["tests\\fit.py"]) == []


def test_copy_and_source_problems_are_collected_together_copy_first():
    out = problems(PINNED, {"command": {"copy": ["test1.csv"]}}, ["testfeatures.csv"])
    assert len(out) == 2
    assert out[0].startswith("command.copy entry") and out[1].startswith("source bundle file")


def test_no_source_files_and_no_config_leaves_nothing_to_refuse():
    assert problems(PINNED, None, None) == []
    assert problems(PINNED, {}, []) == []
    assert sweep_problems(None, None) == []


# -- a release that is not guarded -----------------------------------------------------------------------------------
@pytest.mark.parametrize("ref", [CURRENT, "eidos.project.command@1.0.0", SWEEP])
def test_a_release_that_is_not_guarded_has_no_problems_whatever_it_names(ref):
    """1.1.0 refuses these itself at run time; the lab adds nothing for it."""
    config = {"command": {"copy": ["test1.csv", "testfeatures.csv"]}}
    assert problems(ref, config, ["test1.csv", "x_test2.npy", "holdout3.npz"]) == []


def test_the_registry_lists_exactly_the_pinned_sweep_release():
    assert release_guards.GUARDED_RELEASES == {SWEEP: ("1.0.0",)}


# -- parity with the components ---------------------------------------------------------------------------------------
PARITY_NAMES = [*HELDOUT_NAMES, *ORDINARY_NAMES, "Test.csv", "my-test-data.csv", "test_set.csv", "TestSet.csv",
                "hold_out.csv", "held-out.csv", "HoldOut.csv", "testing.csv", "data/test1.csv", "labels_holdout.npz",
                "validation.csv", "val_features.npz", "protest.csv", "fastest-run.json", "data.csv", "test.csv",
                "test", "heldout", "x.csv", "A1.csv", "", "XTest2.npy", "ytest.npy", "tests", "TESTFEATURES.CSV"]


def test_the_lab_rule_is_recipe_sweep_1_1_0s_own_rule(sweep_1_1_0):
    for name in PARITY_NAMES:
        assert names_heldout_file(name) == sweep_1_1_0._names_heldout_file(name), name


def test_the_lab_refuses_every_name_that_1_1_0_refuses_and_in_particular_what_1_0_0_missed(sweep_1_0_0, sweep_1_1_0):
    for name in LEAKS_IN_1_0_0:
        assert sweep_1_0_0._names_heldout(name) is False, f"1.0.0 now catches {name}: the guard may no longer be needed"
        assert sweep_1_1_0._names_heldout_file(name) is True, name
        assert names_heldout_file(name) is True, name


def test_what_1_0_0_already_refused_the_lab_refuses_too(sweep_1_0_0):
    """The guard widens 1.0.0's rule; it never admits a name 1.0.0 would have refused."""
    for name in [*PARITY_NAMES, "test.csv", "test_data.csv", "heldout.csv", "held_out.npz", "labels.test.csv"]:
        if sweep_1_0_0._names_heldout(name):
            assert names_heldout_file(name) is True, name


def test_the_source_bundle_suffix_exemption_is_the_components(sweep_1_1_0):
    assert release_guards.SOURCE_CODE_SUFFIXES == sweep_1_1_0.SOURCE_CODE_SUFFIXES
