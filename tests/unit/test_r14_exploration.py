"""R14 exploration rules, as pure functions (``eidos.domain.exploration``, ``eidos.domain.turns``, context-policy/3):

* #2 a selection rule names its scorer and its selection data up front, and only a receipt that ran that scorer on that
  data (and read no attested held-out artifact) is selection evidence;
* #3 the reproduction criterion: a same-recipe, other-seed job reproduces the incumbent only when it is scored under the
  rule and is no worse than the incumbent by more than the tolerance;
* #6 a round counts toward "rounds without improvement" only once it is evaluated;
* #4, #5 the hold-back: a token envelope per decision, and the exploration deadline a turn must be over by.

Hand-built jobs and notes, no network. The builders are those of ``test_exploration_policy``."""

from __future__ import annotations

import json

import pytest
from test_exploration_policy import (
    BUDGETS,
    COMPLETION,
    DATA,
    LOWER_RULE,
    RULE,
    SCORER,
    challengers,
    exploration_view,
    job,
    line_with,
    lock,
    make_view,
    run_state,
    scored,
    text_of,
    train_chain,
)

from eidos.domain.exploration import (
    DEFAULT_POLICY,
    RuleError,
    compact,
    exploration_deadline_unix,
    parse_rule,
    pending_outputs,
    policy,
    ref_matches,
    reproduced,
    reproduction,
    rounds,
    rule_from_notes,
    selection_evidence,
)
from eidos.domain.exploration import (
    POLICY_ID as EXPLORATION_POLICY_ID,
)
from eidos.domain.turns import completion_state, turn_boundary
from eidos.investigator.context import POLICY_ID, pack

SCORE_SPLIT = {"split": "validation"}


def scorer_job(jid, accuracy, *, component="eidos.project.score@1", config=None, inputs=None, **kw):
    """A job that scores whatever it reads (``inputs`` default: the predictions of a candidate ``a``)."""
    return job(jid, component=component, config={**SCORE_SPLIT, **(config or {})},
               inputs={"predictions": "art_a_pred"} if inputs is None else inputs, outputs={},
               metrics={"accuracy": accuracy}, **kw)


def candidate_with(scoring):
    """Candidate ``a`` (a training job, its inference job) followed by the given scoring jobs."""
    return [*train_chain("a")[:2], *scoring]


# -- R14 #2: the rule names its scorer and its data ----------------------------------------------------------------------


def test_policy_id_is_v2_and_the_default_policy_has_a_reproduction_tolerance():
    assert EXPLORATION_POLICY_ID == "exploration-policy/2"
    assert DEFAULT_POLICY["reproduction_tolerance"] == 0.02
    assert policy({"exploration": {"reproduction_tolerance": 0.1}})["reproduction_tolerance"] == 0.1


def test_parse_rule_refuses_a_rule_without_a_scorer():
    with pytest.raises(RuleError, match="scorer"):
        parse_rule({"metric": "accuracy", "data": DATA})


@pytest.mark.parametrize("scorer", ["", "   ", None, 3, ["eidos.project.score"], {"component": "x"}])
def test_parse_rule_refuses_a_scorer_that_is_not_a_component_ref(scorer):
    with pytest.raises(RuleError, match="scorer"):
        parse_rule({"metric": "accuracy", "scorer": scorer, "data": DATA})


def test_parse_rule_refuses_a_rule_without_data():
    with pytest.raises(RuleError, match="data"):
        parse_rule({"metric": "accuracy", "scorer": SCORER})


@pytest.mark.parametrize("data", [None, {}, {"inputs": {}}, {"config": {}}, {"inputs": {}, "config": {}},
                                  "validation", ["config"], 3])
def test_parse_rule_refuses_data_that_names_neither_inputs_nor_config(data):
    with pytest.raises(RuleError, match="data"):
        parse_rule({"metric": "accuracy", "scorer": SCORER, "data": data})


def test_parse_rule_refuses_data_with_keys_other_than_inputs_and_config():
    with pytest.raises(RuleError, match="data"):
        parse_rule({"metric": "accuracy", "scorer": SCORER, "data": {"config": {"split": "validation"}, "labels": "x"}})


@pytest.mark.parametrize("data", [{"inputs": {"labels": 3}}, {"inputs": {"labels": ""}}, {"inputs": ["art_val"]},
                                  {"config": "validation"}, {"config": ["split"]}])
def test_parse_rule_refuses_malformed_data(data):
    with pytest.raises(RuleError, match="data"):
        parse_rule({"metric": "accuracy", "scorer": SCORER, "data": data})


def test_parse_rule_accepts_data_with_inputs_only_config_only_or_both():
    inputs, config = {"labels": "art_val"}, {"split": "validation"}
    for data in ({"inputs": inputs}, {"config": config}, {"inputs": inputs, "config": config}):
        assert parse_rule({"metric": "accuracy", "scorer": SCORER, "data": data})["data"] == data


def test_parse_rule_drops_an_empty_half_of_the_data_and_strips_the_scorer():
    rule = parse_rule({"metric": "accuracy", "scorer": f"  {SCORER}@2 ", "data": {"inputs": {"labels": "art_val"},
                                                                               "config": {}}})
    assert rule["scorer"] == f"{SCORER}@2"
    assert rule["data"] == {"inputs": {"labels": "art_val"}}


def test_parse_rule_carries_reproduce_within_as_a_float_and_omits_it_otherwise():
    assert parse_rule({**RULE, "reproduce_within": 1})["reproduce_within"] == 1.0
    assert parse_rule({**RULE, "reproduce_within": 0})["reproduce_within"] == 0.0
    assert "reproduce_within" not in parse_rule(RULE)


@pytest.mark.parametrize("within", [-0.01, True, "0.02", [0.02], float("nan"), float("inf")])
def test_parse_rule_refuses_a_bad_reproduce_within(within):
    with pytest.raises(RuleError, match="reproduce_within"):
        parse_rule({**RULE, "reproduce_within": within})


def test_a_rule_note_and_a_lock_selection_without_a_scorer_record_no_rule():
    no_scorer = {k: v for k, v in RULE.items() if k != "scorer"}
    assert rule_from_notes([{"id": "n1", "kind": "selection_rule", "text": json.dumps(no_scorer)},
                            lock("n2", "job_a", no_scorer)]) is None


def test_selection_evidence_takes_a_receipt_that_ran_the_scorer_on_the_selection_data():
    jobs = candidate_with([scorer_job("a_score", 0.8)])
    assert selection_evidence(jobs, "a_train", RULE) == {"metric": "accuracy", "split": "validation", "value": 0.8,
                                                         "scored_by": "a_score"}


def test_selection_evidence_ignores_a_receipt_from_another_component():
    jobs = candidate_with([scorer_job("a_other", 0.99, component="eidos.project.other@1")])
    assert selection_evidence(jobs, "a_train", RULE) is None
    # the candidate's own receipt does not count either, however good (a training component is not the scorer)
    jobs[0]["receipt"] = {"metrics": {"accuracy": 0.99}}
    assert selection_evidence(jobs, "a_train", RULE) is None


def test_selection_evidence_skips_another_component_and_takes_the_next_matching_scorer():
    jobs = candidate_with([scorer_job("a_other", 0.99, component="eidos.project.other@1"), scorer_job("a_score", 0.8)])
    assert selection_evidence(jobs, "a_train", RULE)["scored_by"] == "a_score"


def test_selection_evidence_a_scorer_that_only_shares_a_name_prefix_is_another_component():
    jobs = candidate_with([scorer_job("a_score", 0.9, component=f"{SCORER}r@1")])
    assert selection_evidence(jobs, "a_train", RULE) is None


@pytest.mark.parametrize("config", [{"split": "test"}, {"split": "training-cv"}, {"split": None}, {"folds": 5}])
def test_selection_evidence_ignores_a_job_run_with_different_data_config(config):
    scorer = job("a_score", component="eidos.project.score@1", config=config, inputs={"predictions": "art_a_pred"},
                 outputs={}, metrics={"accuracy": 0.99})
    assert selection_evidence(candidate_with([scorer]), "a_train", RULE) is None


def test_selection_evidence_ignores_a_job_without_a_config_object():
    scorer = scorer_job("a_score", 0.99)
    for cfg in (None, [("split", "validation")], "split=validation"):
        scorer["config"] = cfg
        assert selection_evidence(candidate_with([scorer]), "a_train", RULE) is None


def test_selection_evidence_needs_every_data_config_key():
    rule = {**RULE, "data": {"config": {"split": "validation", "fold": 2}}}
    only_split = scorer_job("a_split", 0.9)
    both = scorer_job("a_both", 0.7, config={"fold": 2})
    wrong = scorer_job("a_wrong", 0.95, config={"fold": 3})
    assert selection_evidence(candidate_with([only_split, wrong, both]), "a_train", rule)["scored_by"] == "a_both"


def test_selection_evidence_ignores_a_job_with_different_data_inputs():
    rule = {**RULE, "data": {"inputs": {"labels": "art_val"}}}
    other_labels = scorer_job("a_other", 0.99, inputs={"predictions": "art_a_pred", "labels": "art_test"})
    no_labels = scorer_job("a_none", 0.98)
    assert selection_evidence(candidate_with([other_labels, no_labels]), "a_train", rule) is None
    good = scorer_job("a_good", 0.8, inputs={"predictions": "art_a_pred", "labels": "art_val"})
    assert selection_evidence(candidate_with([other_labels, no_labels, good]), "a_train", rule)["scored_by"] == "a_good"


def test_selection_evidence_data_inputs_and_config_must_both_match():
    rule = {**RULE, "data": {"inputs": {"labels": "art_val"}, "config": {"split": "validation"}}}
    right_inputs_wrong_config = scorer_job("a_x", 0.99, config={"split": "test"},
                                           inputs={"predictions": "art_a_pred", "labels": "art_val"})
    right_config_wrong_inputs = scorer_job("a_y", 0.98, inputs={"predictions": "art_a_pred", "labels": "art_test"})
    both = scorer_job("a_z", 0.8, inputs={"predictions": "art_a_pred", "labels": "art_val"})
    assert selection_evidence(candidate_with([right_inputs_wrong_config, right_config_wrong_inputs]),
                              "a_train", rule) is None
    assert selection_evidence(candidate_with([right_inputs_wrong_config, both]), "a_train", rule)["scored_by"] == "a_z"


def test_selection_evidence_ignores_a_job_that_reads_a_held_out_artifact():
    scorer = scorer_job("a_score", 0.99, inputs={"predictions": "art_a_pred", "labels": "art_heldout"})
    jobs = candidate_with([scorer])
    assert selection_evidence(jobs, "a_train", RULE, {"art_heldout"}) is None
    assert selection_evidence(jobs, "a_train", RULE)["scored_by"] == "a_score"  # nothing attested held out
    assert selection_evidence(jobs, "a_train", RULE, held_out={"art_other"})["scored_by"] == "a_score"


def test_selection_evidence_skips_a_held_out_scorer_for_the_next_one():
    leaked = scorer_job("a_leak", 0.99, inputs={"predictions": "art_a_pred", "labels": "art_heldout"})
    jobs = candidate_with([leaked, scorer_job("a_score", 0.8)])
    ev = selection_evidence(jobs, "a_train", RULE, {"art_heldout"})
    assert (ev["scored_by"], ev["value"]) == ("a_score", 0.8)


def test_selection_evidence_a_held_out_artifact_is_never_evidence_even_if_the_rule_names_it():
    rule = {**RULE, "data": {"inputs": {"labels": "art_heldout"}}}
    scorer = scorer_job("a_score", 0.8, inputs={"predictions": "art_a_pred", "labels": "art_heldout"})
    assert selection_evidence(candidate_with([scorer]), "a_train", rule)["scored_by"] == "a_score"
    assert selection_evidence(candidate_with([scorer]), "a_train", rule, {"art_heldout"}) is None


def test_selection_evidence_a_ref_without_a_release_matches_any_release():
    for component in (f"{SCORER}@1", f"{SCORER}@2", f"{SCORER}@2.1.0"):
        jobs = candidate_with([scorer_job("a_score", 0.8, component=component)])
        assert selection_evidence(jobs, "a_train", RULE)["value"] == 0.8


def test_selection_evidence_a_ref_with_a_release_matches_only_that_release():
    rule = {**RULE, "scorer": f"{SCORER}@2"}
    assert selection_evidence(candidate_with([scorer_job("a_score", 0.8, component=f"{SCORER}@2")]), "a_train",
                              rule)["value"] == 0.8
    for component in (f"{SCORER}@1", f"{SCORER}@20", SCORER):
        assert selection_evidence(candidate_with([scorer_job("a_score", 0.8, component=component)]), "a_train",
                                  rule) is None


@pytest.mark.parametrize("ref, wanted, expected", [
    (f"{SCORER}@1", SCORER, True), (SCORER, SCORER, True), (f"{SCORER}@1", f"{SCORER}@1", True),
    (f"{SCORER}@2", f"{SCORER}@1", False), (SCORER, f"{SCORER}@1", False), (f"{SCORER}r@1", SCORER, False),
    ("other.score@1", SCORER, False), (None, SCORER, False), ("", SCORER, False), (3, SCORER, False)])
def test_ref_matches(ref, wanted, expected):
    assert ref_matches(ref, wanted) is expected


def test_state_takes_no_evidence_from_a_receipt_that_does_not_match_the_rule():
    wrong_data = scored("job_a", 0.99, config={"split": "test"})
    assert run_state(jobs=[wrong_data])["incumbent"]["evidence"] is None
    assert run_state(jobs=[scored("job_a", 0.8)])["incumbent"]["evidence"]["value"] == 0.8


def test_state_held_out_artifacts_are_never_evidence():
    jobs = [scored("job_a", 0.99, inputs={"labels": "art_heldout"})]
    assert run_state(jobs=jobs)["incumbent"]["evidence"]["value"] == 0.99
    assert run_state(jobs=jobs, held_out={"art_heldout"})["incumbent"]["evidence"] is None


# -- R14 #3: the reproduction criterion ----------------------------------------------------------------------------------


def attempt_jobs(attempt_accuracy, *, incumbent_accuracy=0.8, attempt_seed=2):
    """The incumbent chain ``a`` and a same-recipe re-run ``r`` under another seed, each scored by its own scorer job."""
    return [*train_chain("a", seed=1, accuracy=incumbent_accuracy), *train_chain("r", seed=attempt_seed,
                                                                                   accuracy=attempt_accuracy)]


def test_reproduction_an_unscored_same_recipe_other_seed_job_does_not_reproduce():
    jobs = [*train_chain("a"), job("r_train", config={"seed": 2, "lr": 0.1})]
    out = reproduction(jobs, "a_train", RULE)
    assert out["reproduced_by"] is None
    assert out["attempts"] == [{"job_id": "r_train", "status": "unscored"}]
    assert reproduced(jobs, "a_train", RULE) is None


def test_reproduction_an_attempt_scored_by_the_wrong_scorer_is_unscored():
    jobs = [*train_chain("a"), *train_chain("r", seed=2)]
    jobs[-1]["component"] = "eidos.project.other@1"  # r_score is not the rule's scorer
    out = reproduction(jobs, "a_train", RULE)
    assert (out["reproduced_by"], [a["status"] for a in out["attempts"]]) == (None, ["unscored"])


def test_reproduction_an_attempt_scored_within_the_tolerance_reproduces():
    out = reproduction(attempt_jobs(0.79), "a_train", RULE)
    assert out["reproduced_by"] == "r_train"
    assert out["attempts"] == [{"job_id": "r_train", "value": 0.79, "scored_by": "r_score", "status": "reproduced"}]
    assert out["incumbent_value"] == 0.8
    assert out["criterion"] == {"metric": "accuracy", "split": "validation", "direction": "higher_is_better",
                                "within": 0.02}
    assert reproduced(attempt_jobs(0.79), "a_train", RULE) == "r_train"


def test_reproduction_an_attempt_that_does_better_than_the_incumbent_reproduces():
    assert reproduced(attempt_jobs(0.9), "a_train", RULE) == "r_train"


def test_reproduction_an_attempt_scored_outside_the_tolerance_is_not_reproduced():
    jobs = attempt_jobs(0.7)
    out = reproduction(jobs, "a_train", RULE)
    assert out["reproduced_by"] is None
    assert out["attempts"] == [{"job_id": "r_train", "value": 0.7, "scored_by": "r_score", "status": "not-reproduced"}]
    assert reproduced(jobs, "a_train", RULE) is None


def test_reproduction_a_shortfall_exactly_at_the_tolerance_still_reproduces():
    rule = {**RULE, "reproduce_within": 0.25}
    assert reproduced(attempt_jobs(0.5, incumbent_accuracy=0.75), "a_train", rule) == "r_train"
    assert reproduced(attempt_jobs(0.49, incumbent_accuracy=0.75), "a_train", rule) is None


def test_reproduction_the_rules_reproduce_within_overrides_the_policy_tolerance():
    jobs = attempt_jobs(0.7)  # 0.1 short of the incumbent
    loose_rule, tight_rule = {**RULE, "reproduce_within": 0.15}, {**RULE, "reproduce_within": 0.01}
    lenient_policy = policy({"exploration": {"reproduction_tolerance": 0.5}})
    assert reproduced(jobs, "a_train", RULE, lenient_policy) == "r_train"  # the policy alone is the tolerance
    assert reproduced(jobs, "a_train", loose_rule) == "r_train"  # the default policy is stricter than the rule
    out = reproduction(jobs, "a_train", tight_rule, lenient_policy)  # the rule's own 0.01 beats the policy's 0.5
    assert out["reproduced_by"] is None and out["criterion"]["within"] == 0.01
    assert reproduction(jobs, "a_train", loose_rule, policy(None))["criterion"]["within"] == 0.15


def test_reproduction_without_a_rule_tolerance_the_policys_reproduction_tolerance_is_used():
    jobs = attempt_jobs(0.7)
    assert reproduction(jobs, "a_train", RULE)["criterion"]["within"] == 0.02
    assert reproduction(jobs, "a_train", RULE, {"reproduction_tolerance": 0.2})["criterion"]["within"] == 0.2
    assert reproduced(jobs, "a_train", RULE, {"reproduction_tolerance": 0.2}) == "r_train"


def test_reproduction_a_zero_tolerance_asks_for_no_worse():
    rule = {**RULE, "reproduce_within": 0}
    assert reproduced(attempt_jobs(0.8), "a_train", rule) == "r_train"
    assert reproduced(attempt_jobs(0.799), "a_train", rule) is None


def lower_jobs(attempt_mse, incumbent_mse=0.10):
    return [scored("job_a", metrics={"mse": incumbent_mse}), scored("job_r", metrics={"mse": attempt_mse},
                                                                    config={"seed": 2})]


def test_reproduction_lower_is_better_within_the_tolerance_above_the_incumbent_reproduces():
    out = reproduction(lower_jobs(0.11), "job_a", LOWER_RULE)
    assert out["reproduced_by"] == "job_r" and out["attempts"][0]["status"] == "reproduced"
    assert out["criterion"] == {"metric": "mse", "split": "validation", "direction": "lower_is_better", "within": 0.02}
    assert out["incumbent_value"] == 0.10


def test_reproduction_lower_is_better_a_lower_value_reproduces_and_a_higher_one_is_not_reproduced():
    assert reproduced(lower_jobs(0.05), "job_a", LOWER_RULE) == "job_r"
    out = reproduction(lower_jobs(0.20), "job_a", LOWER_RULE)
    assert out["reproduced_by"] is None and out["attempts"][0]["status"] == "not-reproduced"


def test_reproduction_lower_is_better_is_not_judged_like_higher_is_better():
    # a value 0.1 *below* the incumbent would be "not reproduced" if the direction were ignored
    assert reproduced(lower_jobs(0.0, incumbent_mse=0.10), "job_a", LOWER_RULE) == "job_r"
    assert reproduced(lower_jobs(0.2, incumbent_mse=0.10), "job_a", {**LOWER_RULE, "reproduce_within": 0.2}) == "job_r"


def test_reproduction_an_unscored_incumbent_has_no_baseline_to_reproduce():
    jobs = [job("job_a"), scored("job_r", 0.8, config={"seed": 2})]
    jobs[0].update(component="eidos.project.score@1", config={"seed": 1, "lr": 0.1, "split": "validation"})
    out = reproduction(jobs, "job_a", RULE)
    assert out["incumbent_value"] is None and out["reproduced_by"] is None
    assert out["attempts"] == [{"job_id": "job_r", "status": "incumbent-unscored"}]


def test_reproduction_the_first_reproducing_attempt_wins_and_stops_the_scan():
    jobs = [scored("job_a", 0.8), scored("job_r1", 0.5, config={"seed": 2}), scored("job_r2", 0.79, config={"seed": 3}),
            scored("job_r3", 0.8, config={"seed": 4})]
    out = reproduction(jobs, "job_a", RULE)
    assert out["reproduced_by"] == "job_r2"
    assert [(a["job_id"], a["status"]) for a in out["attempts"]] == [("job_r1", "not-reproduced"),
                                                                      ("job_r2", "reproduced")]


def test_reproduction_lists_every_attempt_when_none_reproduces():
    jobs = [scored("job_a", 0.8), scored("job_r1", 0.5, config={"seed": 2}),
            scored("job_r2", metrics={"loss": 1.0}, config={"seed": 3})]
    out = reproduction(jobs, "job_a", RULE)
    assert [(a["job_id"], a["status"]) for a in out["attempts"]] == [("job_r1", "not-reproduced"),
                                                                      ("job_r2", "unscored")]


def test_reproduction_a_failed_or_earlier_or_same_seed_job_is_no_attempt():
    jobs = [scored("job_early", 0.8, config={"seed": 5}), scored("job_a", 0.8),
            scored("job_failed", 0.8, config={"seed": 2}, status="failed"), scored("job_same_seed", 0.8)]
    assert reproduction(jobs, "job_a", RULE)["attempts"] == []


def test_reproduction_is_none_without_a_rule_or_for_an_unknown_job():
    jobs = attempt_jobs(0.8)
    assert reproduction(jobs, "a_train", None) is None
    assert reproduction(jobs, "job_missing", RULE) is None


def test_reproduction_an_attempt_reading_held_out_data_is_unscored():
    jobs = [scored("job_a", 0.8), scored("job_r", 0.8, config={"seed": 2}, inputs={"source": "art_x", "labels": "art_h"})]
    # the held-out read changes the recipe's inputs: put the same on the incumbent so that job_r is an attempt
    jobs[0]["inputs"] = {"source": "art_x", "labels": "art_h"}
    out = reproduction(jobs, "job_a", RULE, held_out={"art_h"})
    assert out["incumbent_value"] is None
    assert out["attempts"] == [{"job_id": "job_r", "status": "unscored"}]


def test_state_the_incumbents_reproduced_by_follows_the_criterion():
    notes = [lock("note_1", "a_train", RULE)]
    within = run_state(notes=notes, jobs=attempt_jobs(0.79))["incumbent"]
    assert within["reproduced_by"] == "r_train"
    assert within["reproduction"]["attempts"][0]["status"] == "reproduced"
    outside = run_state(notes=notes, jobs=attempt_jobs(0.7))["incumbent"]
    assert outside["reproduced_by"] is None
    assert outside["reproduction"]["attempts"][0]["status"] == "not-reproduced"
    unscored = run_state(notes=notes, jobs=[*train_chain("a"), job("r_train", config={"seed": 2, "lr": 0.1})])["incumbent"]
    assert unscored["reproduced_by"] is None and unscored["reproduction"]["attempts"][0]["status"] == "unscored"


def test_state_reproduction_uses_the_rules_tolerance_then_the_configured_policy():
    jobs = attempt_jobs(0.7)
    rule = {**RULE, "reproduce_within": 0.15}
    assert run_state(notes=[lock("note_1", "a_train", rule)], jobs=jobs)["incumbent"]["reproduced_by"] == "r_train"
    assert run_state(notes=[lock("note_1", "a_train", RULE)], jobs=jobs)["incumbent"]["reproduced_by"] is None
    cfg = {"exploration": {"reproduction_tolerance": 0.15}}
    st = run_state(notes=[lock("note_1", "a_train", RULE)], jobs=jobs, config=cfg)
    assert st["incumbent"]["reproduced_by"] == "r_train"
    assert st["incumbent"]["reproduction"]["criterion"]["within"] == 0.15


def test_state_without_a_rule_nothing_reproduces_the_incumbent():
    st = run_state(notes=[lock("note_1", "a_train")], jobs=attempt_jobs(0.8))
    assert st["rule"] is None
    assert st["incumbent"]["reproduction"] is None and st["incumbent"]["reproduced_by"] is None


def test_compact_keeps_the_reproduction_and_reproduced_by():
    st = run_state(notes=[lock("note_1", "a_train", RULE)], jobs=attempt_jobs(0.79))
    inc = compact(st)["incumbent"]
    assert inc["reproduced_by"] == "r_train" and inc["reproduction"] == st["incumbent"]["reproduction"]


# -- R14 #6: a round counts only once it is evaluated ---------------------------------------------------------------------


def one_round(challenger, rule=RULE):
    return rounds([{"turn": 3, "jobs": ["job_c"]}], [challenger], 2, 2, rule=rule)


@pytest.mark.parametrize("status", ["running", "queued", "pending"])
def test_rounds_a_round_whose_challenger_is_still_running_is_pending(status):
    assert one_round(scored("job_c", 0.7, status=status)) == ([3], 0, ["job_c"])


def test_rounds_a_round_whose_challenger_succeeded_but_is_unscored_is_pending():
    unscored = job("job_c", config={"seed": 1, "lr": 0.5})  # a training job: no receipt metrics at all
    assert one_round(unscored) == ([3], 0, ["job_c"])


def test_rounds_a_succeeded_challenger_scored_with_the_wrong_scorer_or_data_is_pending():
    assert one_round(job("job_c", metrics={"accuracy": 0.9})) == ([3], 0, ["job_c"])  # not the rule's scorer
    assert one_round(scored("job_c", 0.9, config={"split": "test"})) == ([3], 0, ["job_c"])  # not the rule's data


def test_rounds_a_succeeded_challenger_is_pending_without_a_rule_to_score_it_by():
    assert one_round(scored("job_c", 0.7), rule=None) == ([3], 0, ["job_c"])


def test_rounds_a_scored_challenger_is_an_evaluated_round():
    assert one_round(scored("job_c", 0.7)) == ([3], 1, [])


@pytest.mark.parametrize("status", ["failed", "cancelled", "pruned"])
def test_rounds_a_challenger_that_ended_without_a_result_is_evaluated(status):
    assert one_round(scored("job_c", 0.7, status=status)) == ([3], 1, [])
    assert one_round(job("job_c", status=status)) == ([3], 1, [])


def test_rounds_a_round_with_a_challenger_pending_and_one_done_is_pending_until_both_are():
    jobs = [scored("job_c0", 0.7), scored("job_c1", 0.7, status="running")]
    log = [{"turn": 3, "jobs": ["job_c0", "job_c1"]}]
    assert rounds(log, jobs, 2, 2, rule=RULE) == ([3], 0, ["job_c1"])
    jobs[1]["status"] = "failed"
    assert rounds(log, jobs, 2, 2, rule=RULE) == ([3], 1, [])


def test_rounds_scoring_a_pending_challenger_completes_its_round():
    c = job("job_c", config={"seed": 1, "lr": 0.5})
    log = [{"turn": 3, "jobs": ["job_c"]}]
    assert rounds(log, [c], 2, 2, rule=RULE) == ([3], 0, ["job_c"])
    score = scorer_job("job_c_score", 0.7, inputs={"predictions": "art_job_c"})
    c["outputs"] = [{"slot": "predictions", "artifact_id": "art_job_c", "role": "output", "schema": "x/1"}]
    assert rounds(log, [c, score], 2, 2, rule=RULE) == ([3], 1, [])


def unscored_challengers(n):
    jobs = [job(f"job_c{i}", config={"seed": 1, "lr": 0.2 + i / 10}) for i in range(n)]
    return jobs, [{"turn": 3 + i, "jobs": [f"job_c{i}"]} for i in range(n)]


def test_state_three_pending_rounds_do_not_trigger_ceiling_no_improvement():
    extra, log = unscored_challengers(3)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["phase"], st["stop"]) == ("explore", None)
    assert (st["rounds"], st["rounds_without_improvement"]) == (3, 0)
    assert st["pending_challengers"] == ["job_c0", "job_c1", "job_c2"]


def test_state_three_running_rounds_do_not_trigger_ceiling_no_improvement():
    extra, log = challengers(3)
    for j in extra:
        j["status"] = "running"
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["phase"], st["stop"], st["rounds_without_improvement"]) == ("explore", None, 0)
    assert st["pending_challengers"] == ["job_c0", "job_c1", "job_c2"]


def test_state_once_the_pending_rounds_are_scored_they_trigger_ceiling_no_improvement():
    extra, log = unscored_challengers(3)
    scores = [scorer_job(f"job_c{i}_score", 0.7, inputs={"predictions": f"art_job_c{i}"}) for i in range(3)]
    for j in extra:
        j["outputs"] = [{"slot": "predictions", "artifact_id": f"art_{j['id']}", "role": "output", "schema": "x/1"}]
    st = run_state(jobs=[scored("job_a", 0.8), *extra, *scores], round_log=log)
    assert (st["phase"], st["stop"]) == ("finalize", "ceiling-no-improvement")
    assert (st["rounds"], st["rounds_without_improvement"], st["pending_challengers"]) == (3, 3, [])


def test_state_failed_challengers_are_evaluated_rounds_without_improvement():
    extra, log = challengers(3)
    for j in extra:
        j["status"] = "failed"
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["stop"], st["rounds_without_improvement"], st["pending_challengers"]) == ("ceiling-no-improvement", 3, [])


def test_state_two_evaluated_rounds_and_a_pending_one_keep_exploring():
    extra, log = challengers(3)
    extra[2]["status"] = "running"
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["stop"], st["rounds"], st["rounds_without_improvement"]) == (None, 3, 2)
    assert st["pending_challengers"] == ["job_c2"]


def test_pending_outputs_are_the_artifacts_of_pending_challengers_and_of_what_read_them():
    c = job("job_c")  # produces art_job_c
    infer = job("job_inf", component="eidos.project.infer@1", inputs={"model": "art_job_c"},
                outputs={"predictions": "art_job_pred"})
    other = job("job_other")
    assert pending_outputs([c, infer, other], ["job_c"]) == ["art_job_c", "art_job_pred"]
    assert pending_outputs([c, infer, other], []) == []


def test_compact_keeps_pending_challengers_and_pending_outputs_when_there_are_any():
    extra, log = unscored_challengers(1)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert compact(st)["pending_challengers"] == ["job_c0"]
    assert "pending_outputs" not in compact(st)
    assert compact({**st, "pending_outputs": ["art_job_c0"]})["pending_outputs"] == ["art_job_c0"]
    assert compact(run_state())["pending_challengers"] == []


# -- R14 #4: the token hold-back and the exploration deadline ---------------------------------------------------------------

# BUDGETS: limit 1_000_000, used 100_000, hold-back 15%: 750_000 model tokens left for exploration.


def test_state_stops_with_token_holdback_when_one_decision_no_longer_fits():
    st = run_state(budgets=BUDGETS, decision_tokens=800_000)
    assert st["allowance"]["tokens_left"] == 750_000
    assert (st["phase"], st["stop"]) == ("finalize", "token-holdback")


@pytest.mark.parametrize("decision_tokens", [750_000, 749_999, 100_000, 1, 0, None])
def test_state_keeps_exploring_when_a_decision_fits_in_the_tokens_left(decision_tokens):
    st = run_state(budgets=BUDGETS, decision_tokens=decision_tokens)
    assert (st["phase"], st["stop"]) == ("explore", None)


def test_state_token_holdback_is_the_first_decision_that_does_not_fit():
    assert run_state(budgets=BUDGETS, decision_tokens=750_001)["stop"] == "token-holdback"
    assert run_state(budgets=BUDGETS, decision_tokens=750_000)["stop"] is None


def test_state_without_a_token_limit_a_decision_envelope_stops_nothing():
    assert run_state(decision_tokens=10**9)["stop"] is None


def test_state_a_decision_envelope_does_not_touch_the_stop_reasons_it_cannot_outrank():
    thin = {**COMPLETION, "remaining_seconds": 700.0}
    assert run_state(completion=thin, wall_seconds=3600.0, budgets=BUDGETS, decision_tokens=800_000)["stop"] == "wall-holdback"
    assert run_state(frozen=True, budgets=BUDGETS, decision_tokens=800_000)["stop"] == "selection-frozen"
    assert run_state(jobs=[scored("job_a", 1.0)], budgets=BUDGETS, decision_tokens=800_000)["stop"] == "ceiling-perfect-score"


def test_state_the_holdback_with_no_decision_envelope_is_the_exhausted_allowance():
    spent = {"model_tokens": {"limit_amount": 1_000_000, "used": 900_000, "reserved": 0}}
    assert run_state(budgets=spent)["stop"] == "token-holdback"
    assert run_state(budgets=spent, decision_tokens=1)["stop"] == "token-holdback"


def test_exploration_deadline_is_the_deadline_less_the_conclusion_reserve_and_the_holdback():
    st = run_state(completion=COMPLETION, wall_seconds=3600.0)
    assert st["phase"] == "explore" and st["allowance"]["holdback_seconds"] == 540.0
    assert exploration_deadline_unix(st, COMPLETION) == 1_000_000.0 - 180.0 - 540.0


def test_exploration_deadline_without_a_wall_limit_holds_nothing_back():
    st = run_state(completion=COMPLETION)
    assert exploration_deadline_unix(st, COMPLETION) == 1_000_000.0 - 180.0


def test_exploration_deadline_is_none_when_the_turn_is_not_exploratory():
    finalize = run_state(frozen=True, completion=COMPLETION, wall_seconds=3600.0)
    baseline = run_state(notes=[], note_turns={}, completion=COMPLETION, wall_seconds=3600.0)
    assert (finalize["phase"], baseline["phase"]) == ("finalize", "baseline")
    assert exploration_deadline_unix(finalize, COMPLETION) is None
    assert exploration_deadline_unix(baseline, COMPLETION) is None


def test_exploration_deadline_is_none_without_a_completion():
    assert exploration_deadline_unix(run_state(), None) is None
    assert exploration_deadline_unix(run_state(completion=COMPLETION, wall_seconds=3600.0), {}) is None


def test_turn_boundary_of_an_exploratory_turn_is_the_exploration_deadline():
    completion = {**completion_state(1000.0, 500.0, 60.0), "exploration_deadline_unix": 800.0}
    assert completion["conclude_now"] is False
    assert turn_boundary(completion) == 800.0


def test_turn_boundary_is_the_conclusion_reserve_without_an_exploration_deadline():
    base = completion_state(1000.0, 500.0, 60.0)
    assert turn_boundary(base) == 940.0
    assert turn_boundary({**base, "exploration_deadline_unix": None}) == 940.0


def test_turn_boundary_never_passes_the_conclusion_reserve():
    completion = {**completion_state(1000.0, 500.0, 60.0), "exploration_deadline_unix": 990.0}
    assert turn_boundary(completion) == 940.0


def test_turn_boundary_of_the_concluding_turn_is_the_wall_deadline_whatever_the_exploration_deadline():
    completion = {**completion_state(1000.0, 950.0, 60.0), "exploration_deadline_unix": 800.0}
    assert completion["conclude_now"] is True
    assert turn_boundary(completion) == 1000.0


# -- R14 #3 in the context: the incumbent line ----------------------------------------------------------------------------


def incumbent_line(view):
    return line_with(text_of(view, policy=POLICY_ID), "Incumbent v1")


def test_context_an_unreproduced_incumbent_states_the_reproduction_tolerance():
    line = incumbent_line(exploration_view())
    assert "not reproduced yet" in line
    assert "a reproduction must score within 0.02 of it under the rule" in line


def test_context_the_reproduction_tolerance_is_the_rules_when_it_declares_one():
    notes = [lock("note_1", "job_a", {**RULE, "reproduce_within": 0.05})]
    assert "within 0.05 of it" in incumbent_line(exploration_view(notes=notes))


def test_context_the_reproduction_tolerance_is_the_configured_policys():
    view = exploration_view(config={"exploration": {"reproduction_tolerance": 0.1}})
    assert "within 0.1 of it" in incumbent_line(view)


def test_context_a_reproduced_incumbent_names_its_reproduction_not_the_tolerance():
    jobs = [scored("job_a", 0.8), scored("job_r", 0.8, config={"seed": 2})]
    line = incumbent_line(exploration_view(jobs=jobs))
    assert "reproduced by job_r" in line and "must score within" not in line and "not reproduced" not in line


def test_context_an_incumbent_reproduced_only_by_a_poor_attempt_still_states_the_tolerance():
    jobs = [scored("job_a", 0.8), scored("job_r", 0.5, config={"seed": 2})]
    line = incumbent_line(exploration_view(jobs=jobs))
    assert "not reproduced yet" in line and "within 0.02" in line


def test_context_without_a_rule_there_is_no_tolerance_to_state():
    line = incumbent_line(exploration_view(notes=[lock("note_1", "job_a")]))
    assert "not reproduced yet" in line and "within" not in line


def test_context_an_incumbent_without_evidence_is_told_to_score_it_with_the_rules_scorer():
    line = incumbent_line(exploration_view(jobs=[scored("job_a", 0.8, config={"split": "test"})]))
    assert "no recorded selection evidence" in line and "scorer" in line
    assert "no recorded selection evidence" not in incumbent_line(exploration_view())


def test_context_without_a_rule_it_says_to_record_the_scorer_and_the_data_before_locking():
    view = make_view(exploration=compact(run_state(notes=[], jobs=[], note_turns={})))
    line = line_with(pack(view, [], policy=POLICY_ID)["text"], "Selection rule")
    assert "none recorded" in line and '"scorer"' in line and '"data"' in line and "before you lock" in line
