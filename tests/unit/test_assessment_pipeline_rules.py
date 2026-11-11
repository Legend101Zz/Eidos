"""Pure rules of assessment by default (ADR 0021): request checks, lock inspection, provenance, and the wiring of the
``assess_candidate`` tool, the charter's ``held_out`` section and selection-data refusal into the domain rules.

Author tests of pure functions: no database, no models, no network.
"""

from __future__ import annotations

import pytest

from eidos.domain import assessment as A
from eidos.domain import turns
from eidos.domain.charter import validate_charter
from eidos.domain.errors import Invalid
from eidos.domain.evidence import check_freezable


def charter_with(identity: str | None, **over):
    metrics = {"accuracy": {"direction": "increase", "denominator": "examples",
                            **({"identity": identity} if identity is not None else {})}}
    body = {"metrics": metrics, "success_rules": [{"id": "practical-accuracy-gain", "metric": "accuracy"},
                                                  {"id": "second-rule", "metric": "accuracy"}]}
    body.update(over)
    return body


def codes(reasons):
    return [r["code"] for r in reasons]


# -- metric_path ---------------------------------------------------------------------------------------------------
def test_an_exploration_only_charter_has_no_assessment_path():
    rule, path, reasons = A.metric_path({"exploration_only": True, "success_rules": []}, "practical-accuracy-gain")
    assert (rule, path) == (None, None)
    assert codes(reasons) == ["exploration-only-charter"]
    assert "prospective success rules" in reasons[0]["message"]


def test_an_unknown_rule_is_refused_and_the_available_ones_are_listed():
    rule, path, reasons = A.metric_path(charter_with("classification-report/1:accuracy"), "nope")
    assert (rule, path) == (None, None)
    assert codes(reasons) == ["rule-not-found"]
    assert reasons[0]["details"] == {"available": ["practical-accuracy-gain", "second-rule"]}
    assert "'nope'" in reasons[0]["message"]


def test_a_charter_without_rules_has_no_rule_to_find():
    _, _, reasons = A.metric_path({"metrics": {}}, "any")
    assert codes(reasons) == ["rule-not-found"] and reasons[0]["details"] == {"available": []}


@pytest.mark.parametrize("identity", ["custom/1:f1", "classification-report/1:macro-f1", "", None])
def test_a_metric_identity_without_a_path_is_refused_with_the_supported_ones_listed(identity):
    rule, path, reasons = A.metric_path(charter_with(identity), "practical-accuracy-gain")
    assert rule["id"] == "practical-accuracy-gain" and path is None
    assert codes(reasons) == ["no-assessment-path"]
    assert reasons[0]["details"]["supported"] == sorted(A.METRIC_PATHS)


def test_a_rule_whose_metric_is_not_defined_has_no_assessment_path():
    body = charter_with("classification-report/1:accuracy")
    body["success_rules"][0]["metric"] = "undefined"
    rule, path, reasons = A.metric_path(body, "practical-accuracy-gain")
    assert rule is body["success_rules"][0] and path is None and codes(reasons) == ["no-assessment-path"]


@pytest.mark.parametrize(("identity", "expected"), [
    ("classification-report/1:accuracy", "classification"),
    ("paired-report/1:both-correct-rate", "train-evaluate"),
    ("paired-report/1:reference-accuracy", "train-evaluate"),
])
def test_classification_and_paired_identities_select_their_paths(identity, expected):
    body = charter_with(identity)
    rule, path, reasons = A.metric_path(body, "second-rule")
    assert path == expected and reasons == []
    assert rule is body["success_rules"][1]
    assert A.METRIC_PATHS[identity] == expected


def test_the_path_names_are_the_published_constants():
    assert (A.PATH_CLASSIFICATION, A.PATH_TRAIN_EVALUATE) == ("classification", "train-evaluate")
    assert set(A.METRIC_PATHS.values()) == {"classification", "train-evaluate"}


# -- check_seed_pairing --------------------------------------------------------------------------------------------
def test_paired_seeds_are_returned_per_arm_without_problems():
    arms, problems = A.check_seed_pairing({"11": "job_b11", "12": "job_b12"}, {"12": "job_c12", "11": "job_c11"})
    assert problems == []
    assert arms == {"baseline": {"11": "job_b11", "12": "job_b12"}, "candidate": {"11": "job_c11", "12": "job_c12"}}


def test_integer_and_padded_text_keys_are_normalized_to_strings():
    arms, problems = A.check_seed_pairing({11: "a", 12: "b"}, {"11": "c", " 12 ": "d"})
    assert problems == []
    assert arms["baseline"] == {"11": "a", "12": "b"} and arms["candidate"] == {"11": "c", "12": "d"}


def test_unpaired_seeds_are_one_seed_pairing_problem_listing_both_sides():
    arms, problems = A.check_seed_pairing({"10": "a", "9": "b", "11": "c"}, {"9": "d", "12": "e"})
    assert codes(problems) == ["seed-pairing"]
    assert problems[0]["details"] == {"baseline": ["9", "10", "11"], "candidate": ["9", "12"]}  # numeric order
    assert set(arms) == {"baseline", "candidate"}


@pytest.mark.parametrize("bad", [1.5, True, None, "", "   ", (1, 2)])
def test_a_seed_that_is_not_an_integer_or_a_text_is_refused(bad):
    _, problems = A.check_seed_pairing({bad: "a"}, {"1": "b"})
    assert "seed-pairing" in codes(problems)
    assert any(repr(bad) in p["message"] and p["message"].startswith("baseline") for p in problems)


def test_a_seed_written_two_ways_is_a_duplicate():
    arms, problems = A.check_seed_pairing({"1": "a"}, {1: "b", " 1": "c"})
    assert codes(problems) == ["seed-pairing"] and "appears twice" in problems[0]["message"]
    assert arms["candidate"] == {"1": "b"}


@pytest.mark.parametrize("empty", [{}, None, [], "11"])
def test_an_empty_or_malformed_arm_is_a_seed_pairing_problem(empty):
    arms, problems = A.check_seed_pairing(empty, {"11": "c"})
    assert codes(problems) == ["seed-pairing"] and problems[0]["message"].startswith("baseline must map")
    assert "baseline" not in arms
    arms, problems = A.check_seed_pairing({"11": "b"}, empty)
    assert codes(problems) == ["seed-pairing"] and problems[0]["message"].startswith("candidate must map")


def test_both_arms_empty_names_both_roles():
    arms, problems = A.check_seed_pairing({}, {})
    assert arms == {} and [p["message"].split(" ")[0] for p in problems] == ["baseline", "candidate"]


def test_seed_helpers():
    assert A.seed_key(7) == "7" and A.seed_key(" 7 ") == "7" and A.seed_key("s-a") == "s-a"
    assert all(A.seed_key(x) is None for x in (True, 1.0, None, "", "  ", [1]))
    assert sorted(["10", "9", "11", "b", "a"], key=A.seed_sort_key) == ["9", "10", "11", "a", "b"]
    assert A.seed_value("12") == 12 and A.seed_value("abc") == "abc"


# -- fresh_initialization_problem ----------------------------------------------------------------------------------
def test_a_lock_that_restored_the_parent_checkpoint_port_is_not_fresh():
    problem = A.fresh_initialization_problem({"inputs": {"parent_checkpoint": {"digest": "sha256:aa"}}})
    assert problem == "it restored a checkpoint through input 'parent_checkpoint'"


@pytest.mark.parametrize("schema", ["eidos-checkpoint/1", "model-file/1"])
def test_any_input_with_a_checkpoint_schema_is_not_fresh(schema):
    problem = A.fresh_initialization_problem({"inputs": {"weights": {"schema": schema}}})
    assert problem == "it restored a checkpoint through input 'weights'"


def test_a_branched_trial_is_not_fresh():
    problem = A.fresh_initialization_problem({"config": {"branch": {"stream_policy": "paired"}}})
    assert problem == "it branched from a parent trial"


def test_the_checkpoint_problem_is_reported_before_the_branch_problem():
    lock = {"inputs": {"parent_checkpoint": {}}, "config": {"branch": {"x": 1}}}
    assert "restored a checkpoint" in A.fresh_initialization_problem(lock)


@pytest.mark.parametrize("lock", [
    {},
    {"inputs": None, "config": None},
    {"inputs": {"data": {"schema": "dataset/1", "digest": "sha256:bb"}, "empty": None}, "config": {"seed": 1}},
    {"config": {"branch": None}},
    {"config": {"branch": {}}},
])
def test_a_clean_lock_is_a_fresh_initialization(lock):
    assert A.fresh_initialization_problem(lock) is None


# -- job_seed ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("lock", "expected"), [
    ({"seed": 11}, "11"),
    ({"seed": "11"}, "11"),
    ({"seed": 0}, "0"),
    ({"config": {"seed": 12}}, "12"),
    ({"seed": 5, "config": {"seed": 6}}, "5"),  # the lock's own seed wins
    ({"seed": None, "config": {"seed": 6}}, "6"),
    ({}, None),
    ({"config": None}, None),
    ({"config": {"seed": True}}, None),
    ({"config": {"seed": 1.5}}, None),
])
def test_job_seed_prefers_the_lock_seed_then_the_config_seed(lock, expected):
    assert A.job_seed(lock) == expected


# -- recipe_identity -----------------------------------------------------------------------------------------------
def _lock(seed, **over):
    lock = {"component": {"ref": "eidos.pytorch.train@1.0.0"},
            "inputs": {"data": {"digest": "sha256:aa", "schema": "dataset/1"}, "init": None},
            "config": {"lr": 0.1, "steps": 45, "seed": seed}, "seed": seed}
    lock.update(over)
    return lock


def test_recipe_identity_is_component_input_digests_and_configuration_without_the_seed():
    assert A.recipe_identity(_lock(11)) == {"component": "eidos.pytorch.train@1.0.0",
                                            "inputs": {"data": "sha256:aa", "init": None},
                                            "config": {"lr": 0.1, "steps": 45}}


def test_recipe_identity_ignores_the_seed_but_not_the_recipe():
    assert A.recipe_identity(_lock(11)) == A.recipe_identity(_lock(12))
    assert A.recipe_identity(_lock(11)) != A.recipe_identity(_lock(11, config={"lr": 0.2, "steps": 45, "seed": 11}))
    assert A.recipe_identity(_lock(11)) != A.recipe_identity(
        _lock(11, inputs={"data": {"digest": "sha256:cc"}, "init": None}))
    assert A.recipe_identity(_lock(11)) != A.recipe_identity(_lock(11, component={"ref": "eidos.pytorch.train@2.0.0"}))


def test_recipe_identity_of_a_bare_lock():
    assert A.recipe_identity({}) == {"component": None, "inputs": {}, "config": {}}


# -- check_provenance ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("declared", [None, "held-out", ["held-out"], {}, {"split": "validation"}, {"split": None},
                                      {"note": "held-out"}])
def test_missing_or_invalid_provenance_is_required(declared):
    recorded, reasons = A.check_provenance(declared, charter={"held_out": {"labels": ["sha256:x"]}},
                                           labels_digest="sha256:x")
    assert recorded is None and codes(reasons) == ["provenance-required"]


def test_selection_data_never_becomes_confirmation_even_when_attested():
    charter = {"held_out": {"labels": ["sha256:x"], "splits": ["test"]}}
    recorded, reasons = A.check_provenance({"split": "selection"}, charter=charter, labels_digest="sha256:x",
                                           split="test")
    assert recorded is None and codes(reasons) == ["selection-data"]
    assert "exploratory" in reasons[0]["message"] and "unfulfilled obligation" in reasons[0]["message"]


@pytest.mark.parametrize("charter", [{}, {"held_out": None}, {"held_out": {}}, {"held_out": {"labels": ["sha256:y"]}},
                                     {"held_out": {"splits": ["train"]}}])
def test_held_out_data_an_investigator_declares_but_the_charter_does_not_attest_is_refused(charter):
    recorded, reasons = A.check_provenance({"split": "held-out"}, charter=charter, labels_digest="sha256:x",
                                           split="test")
    assert recorded is None and codes(reasons) == ["held-out-unattested"]
    assert "labels digest sha256:x" in reasons[0]["message"]


def test_the_unattested_message_names_the_split_when_there_is_no_digest():
    _, reasons = A.check_provenance({"split": "held-out"}, charter={}, split="test")
    assert codes(reasons) == ["held-out-unattested"] and "split 'test'" in reasons[0]["message"]


def test_held_out_data_attested_by_labels_digest_returns_the_recorded_provenance():
    charter = {"held_out": {"labels": ["sha256:w", "sha256:x"]}}
    recorded, reasons = A.check_provenance({"split": "held-out"}, charter=charter, labels_digest="sha256:x")
    assert reasons == []
    assert recorded == {"split": "held-out", "attested_by": "charter", "labels_digest": "sha256:x"}


def test_held_out_data_attested_by_split_name_returns_the_recorded_provenance():
    charter = {"held_out": {"splits": ["test", "final"]}}
    recorded, reasons = A.check_provenance({"split": "held-out"}, charter=charter, split="final")
    assert reasons == []
    assert recorded == {"split": "held-out", "attested_by": "charter", "evaluation_split": "final"}


def test_either_attestation_suffices_and_both_are_recorded():
    charter = {"held_out": {"splits": ["test"]}}
    recorded, reasons = A.check_provenance({"split": "held-out"}, charter=charter, labels_digest="sha256:unlisted",
                                           split="test")
    assert reasons == []
    assert recorded == {"split": "held-out", "attested_by": "charter", "labels_digest": "sha256:unlisted",
                        "evaluation_split": "test"}


def test_the_declared_note_is_kept_but_bounded():
    charter = {"held_out": {"splits": ["test"]}}
    recorded, _ = A.check_provenance({"split": "held-out", "note": "x" * 600, "other": "dropped"}, charter=charter,
                                     split="test")
    assert recorded["note"] == "x" * 500 and "other" not in recorded
    recorded, _ = A.check_provenance({"split": "held-out", "note": ""}, charter=charter, split="test")
    assert "note" not in recorded


def test_a_digest_is_not_attested_by_a_split_entry_or_the_other_way_round():
    charter = {"held_out": {"labels": ["test"], "splits": ["sha256:x"]}}
    recorded, reasons = A.check_provenance({"split": "held-out"}, charter=charter, labels_digest="sha256:x", split="test")
    assert recorded is None and codes(reasons) == ["held-out-unattested"]


def test_reason_shape():
    assert A.reason("c", "m") == {"code": "c", "message": "m"}
    assert A.reason("c", "m", a=1) == {"code": "c", "message": "m", "details": {"a": 1}}


# -- the assess_candidate tool and conclusion claims in decisions --------------------------------------------------
def _call(tool="assess_candidate", **args):
    return {"tool": tool, "args": args}


def test_assess_candidate_is_an_effect_tool_that_requires_a_success_rule():
    assert turns.EFFECT_TOOLS["assess_candidate"] == "assess_candidate"
    assert turns.REQUIRED_ARGS["assess_candidate"] == ("success_rule_id",)


def test_a_decision_may_call_assess_candidate_with_a_success_rule():
    call = _call(success_rule_id="practical-accuracy-gain", candidate={"1": "job_1"})
    decision = turns.validate_decision({"calls": [call]}, max_calls=4)
    assert decision["calls"] == [call]
    assert "purpose" not in decision["calls"][0]["args"]  # not one of the tools that default a purpose


@pytest.mark.parametrize("args", [{}, {"success_rule_id": ""}, {"success_rule_id": None}, {"candidate": {"1": "j"}}])
def test_a_decision_calling_assess_candidate_without_a_success_rule_is_refused(args):
    with pytest.raises(Invalid, match="missing required args") as exc:
        turns.validate_decision({"calls": [_call(**args)]}, max_calls=4)
    assert exc.value.details == {"tool": "assess_candidate", "missing": ["success_rule_id"]}


@pytest.mark.parametrize("action", ["conclude", "stop"])
@pytest.mark.parametrize("claim", [{"disposition": "certain"}, "confirmed", ["candidate"],
                                   {"point_estimate": 0.2}, {"evidence_refs": "art_1"}, {"obligations": {"a": 1}}])
def test_a_conclusion_with_a_malformed_claim_block_is_refused(action, claim):
    with pytest.raises(Invalid, match="control.claim"):
        turns.validate_decision({"control": {"action": action, "claim": claim}}, max_calls=4)


@pytest.mark.parametrize("claim", [None, {}, {"disposition": "candidate", "point_estimate": {"value": 0.1},
                                              "evidence_refs": ["art_1"], "obligations": ["held-out run"]}])
def test_a_conclusion_with_a_valid_or_absent_claim_block_is_accepted(claim):
    control = {"action": "conclude", "summary": "done", **({} if claim is None else {"claim": claim})}
    decision = turns.validate_decision({"control": control}, max_calls=4)
    assert decision["control"] == control


def test_a_malformed_claim_block_is_refused_alongside_valid_calls():
    proposal = {"calls": [_call(success_rule_id="r")], "control": {"action": "conclude", "claim": {"disposition": "sure"}}}
    with pytest.raises(Invalid):
        turns.validate_decision(proposal, max_calls=4)


# -- the charter's held_out section --------------------------------------------------------------------------------
def _charter_body(**over):
    body = {
        "study_id": "accuracy-study", "question": "Does the candidate raise held-out accuracy by 0.02?",
        "population": "held-out examples", "comparator": "reference recipe",
        "metrics": {"accuracy": {"direction": "increase", "denominator": "examples",
                                 "identity": "classification-report/1:accuracy"}},
        "success_rules": [{"id": "practical-accuracy-gain", "metric": "accuracy", "min_effect": 0.02,
                           "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                           "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95}}],
    }
    body.update(over)
    return body


@pytest.mark.parametrize("held_out", [
    {"labels": ["sha256:aa", "sha256:bb"], "splits": ["test", "final"]},
    {"labels": ["sha256:aa"]}, {"splits": ["test"]}, {"labels": [], "splits": []}, {},
])
def test_a_charter_may_attest_held_out_labels_and_splits(held_out):
    normalized = validate_charter(_charter_body(held_out=held_out))
    assert normalized["held_out"] == held_out


def test_a_charter_without_a_held_out_section_stays_valid_and_gets_none():
    assert "held_out" not in validate_charter(_charter_body())
    assert "held_out" not in validate_charter({"study_id": "s", "question": "q", "exploration_only": True})


@pytest.mark.parametrize("held_out", [
    {"labels": "sha256:aa"}, {"splits": "test"}, {"labels": {"sha256:aa": True}}, {"splits": 3},
    {"labels": [1]}, {"splits": [""]}, {"labels": [None]}, {"splits": [["test"]]},
    ["sha256:aa"], "sha256:aa", 3, True,
])
def test_a_held_out_section_that_is_not_string_lists_is_rejected(held_out):
    with pytest.raises(Invalid, match="invalid study charter") as exc:
        validate_charter(_charter_body(held_out=held_out))
    problems = exc.value.details["problems"]
    assert len(problems) == 1 and "held_out" in problems[0]


# -- selection data cannot be frozen -------------------------------------------------------------------------------
def _plan(**over):
    return {"obligations": [], "data_provenance": {"split": "held-out"}, **over}


def test_a_plan_over_selection_data_cannot_be_frozen_as_confirmation():
    plan = _plan(data_provenance={"split": "selection", "note": "used to pick the checkpoint"})
    with pytest.raises(Invalid, match="selection data") as exc:
        check_freezable(plan, template_approved=True, assessor_approved=True)
    assert exc.value.details["data_provenance"] == plan["data_provenance"]


def test_a_held_out_plan_or_one_without_provenance_is_freezable():
    assert check_freezable(_plan(), template_approved=True, assessor_approved=True) is None
    assert check_freezable({"obligations": []}, template_approved=True, assessor_approved=True) is None
    assert check_freezable(_plan(data_provenance=None), template_approved=True, assessor_approved=True) is None
