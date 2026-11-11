"""The incumbent-and-challengers exploration policy (ADR 0023, R14): the pure rules in ``eidos.domain.exploration`` and
the context-policy/3 lines that present them. All pure: hand-built jobs, notes and views go in, decisions and prompts
come out. No network, no model calls.

Since R14 a selection rule names its ``scorer`` component and the selection ``data`` that scorer reads, and only a
receipt that ran that scorer on that data is selection evidence; the R14 rules themselves (provenance, reproduction,
evaluated rounds, hold-back) are in ``test_r14_exploration.py``."""

from __future__ import annotations

import json

import pytest

from eidos.domain.assessment import candidate_lock_job_id
from eidos.domain.exploration import (
    DEFAULT_POLICY,
    RuleError,
    allowance,
    at_ceiling,
    better,
    ceiling_of,
    compact,
    holdback_seconds,
    lineage,
    lock_versions,
    metric_value,
    parse_rule,
    policy,
    reproduced,
    reproduction_jobs,
    rounds,
    rule_from_notes,
    scorer,
    selection_evidence,
    state,
)
from eidos.investigator.context import POLICY_ID, POLICY_V2, pack

SCORER = "eidos.project.score"  # a ref with no release matches any release of the component
DATA = {"config": {"split": "validation"}}  # the selection data the scorer reads
SELECTION = {"scorer": SCORER, "data": DATA}
RULE = {"metric": "accuracy", "split": "validation", "direction": "higher_is_better", **SELECTION}
LOWER_RULE = {"metric": "mse", "split": "validation", "direction": "lower_is_better", **SELECTION}


# -- builders ------------------------------------------------------------------------------------------------------------


def job(jid, *, component="eidos.project.command@1", purpose="open-search", status="succeeded", config=None,
        inputs=None, outputs=None, metrics=None):
    """A job as the investigation view shows it. It produces ``art_<id>`` as its ``model`` unless told otherwise."""
    outs = {"model": f"art_{jid}"} if outputs is None else outputs
    return {"id": jid, "component": component, "environment": "lab", "purpose": purpose, "status": status,
            "config": {"seed": 1, "lr": 0.1} if config is None else config,
            "inputs": {"source": "art_x"} if inputs is None else inputs,
            "outputs": [{"slot": slot, "artifact_id": art, "role": "output", "schema": "x/1"} for slot, art in outs.items()],
            "error": None, "receipt": {"metrics": metrics} if metrics is not None else None}


def scored(jid, accuracy=0.8, *, metrics=None, config=None, **kw):
    """A job that is its own scorer: the rule's scorer component, run on the rule's selection data (split validation),
    reporting ``accuracy`` (or ``metrics``). Its own receipt is the selection evidence for its recipe."""
    return job(jid, component="eidos.project.score@1",
               config={"seed": 1, "lr": 0.1, "split": "validation", **(config or {})},
               metrics={"accuracy": accuracy} if metrics is None else metrics, **kw)


def train_chain(prefix, *, seed=1, accuracy=0.8, lr=0.1):
    """A train job -> an inference job reading its model -> a score job reading the predictions (the only one with
    metrics: the scorer, on the selection data)."""
    train = job(f"{prefix}_train", config={"seed": seed, "lr": lr})
    infer = job(f"{prefix}_infer", component="eidos.project.infer@1", inputs={"model": f"art_{prefix}_train"},
                outputs={"predictions": f"art_{prefix}_pred"})
    score = job(f"{prefix}_score", component="eidos.project.score@1", purpose="measurement-development",
                config={"split": "validation"}, inputs={"predictions": f"art_{prefix}_pred"}, outputs={},
                metrics={"classification-report/1:accuracy": accuracy})
    return [train, infer, score]


def lock(note_id, job_id, selection=None, **extra):
    body = {"job_id": job_id}
    if selection is not None:
        body["selection"] = selection
    return {"id": note_id, "kind": "candidate_lock", "text": json.dumps(body), **extra}


def rule_note(note_id, rule):
    return {"id": note_id, "kind": "selection_rule", "text": json.dumps(rule)}


def run_state(*, notes=None, jobs=None, round_log=None, note_turns=None, **over):
    kw = {"notes": [lock("note_1", "job_a", RULE)] if notes is None else notes,
          "jobs": [scored("job_a", 0.8)] if jobs is None else jobs,
          "round_log": [] if round_log is None else round_log,
          "note_turns": {"note_1": 2} if note_turns is None else note_turns,
          "lock_job": candidate_lock_job_id}
    kw.update(over)
    return state(**kw)


def challengers(n):
    """``n`` scored open-search challenger jobs (different learning rates) and the round log with one per turn, from turn 3."""
    jobs = [scored(f"job_c{i}", 0.7, config={"lr": 0.2 + i / 10}) for i in range(n)]
    log = [{"turn": 3 + i, "jobs": [f"job_c{i}"]} for i in range(n)]
    return jobs, log


COMPLETION = {"deadline_unix": 1_000_000.0, "remaining_seconds": 1000.0, "conclude_threshold_seconds": 180.0,
              "conclude_now": False}
BUDGETS = {"model_tokens": {"limit_amount": 1_000_000, "used": 100_000, "reserved": 0}}


# -- 1. parse_rule -------------------------------------------------------------------------------------------------------


def test_parse_rule_accepts_a_json_string():
    assert parse_rule(json.dumps(RULE)) == RULE


def test_parse_rule_accepts_a_dict():
    assert parse_rule(dict(RULE)) == RULE


def test_parse_rule_defaults_split_to_validation_and_direction_to_higher_is_better():
    assert parse_rule({"metric": "accuracy", **SELECTION}) == RULE


def test_parse_rule_strips_the_metric_name():
    assert parse_rule({"metric": "  accuracy ", **SELECTION})["metric"] == "accuracy"


def test_parse_rule_accepts_training_cv_and_lower_is_better():
    rule = parse_rule({"metric": "mse", "split": "training-cv", "direction": "lower_is_better", **SELECTION})
    assert (rule["split"], rule["direction"]) == ("training-cv", "lower_is_better")


def test_parse_rule_carries_a_numeric_ceiling_as_a_float_and_a_round_limit():
    rule = parse_rule({"metric": "accuracy", "ceiling": 1, "max_rounds": 4, **SELECTION})
    assert rule["ceiling"] == 1.0 and isinstance(rule["ceiling"], float)
    assert rule["max_rounds"] == 4


@pytest.mark.parametrize("raw", [{}, {"metric": ""}, {"metric": "   "}, {"metric": 3}, {"split": "validation"}])
def test_parse_rule_rejects_a_missing_or_blank_metric(raw):
    with pytest.raises(RuleError, match="metric"):
        parse_rule(raw)


@pytest.mark.parametrize("split", ["test", "held-out", "heldout", "", None])
def test_parse_rule_rejects_a_held_out_or_unknown_split(split):
    with pytest.raises(RuleError, match="held-out"):
        parse_rule({"metric": "accuracy", "split": split, **SELECTION})


@pytest.mark.parametrize("direction", ["up", "higher", "maximize", "", None])
def test_parse_rule_rejects_a_bad_direction(direction):
    with pytest.raises(RuleError, match="direction"):
        parse_rule({"metric": "accuracy", "direction": direction, **SELECTION})


@pytest.mark.parametrize("ceiling", ["high", "1.0", True, False, [1], float("nan"), float("inf")])
def test_parse_rule_rejects_a_non_numeric_ceiling(ceiling):
    with pytest.raises(RuleError, match="ceiling"):
        parse_rule({"metric": "accuracy", "ceiling": ceiling, **SELECTION})


@pytest.mark.parametrize("max_rounds", [-1, True, False, 2.5, "3", [3]])
def test_parse_rule_rejects_a_negative_or_non_integer_max_rounds(max_rounds):
    with pytest.raises(RuleError, match="max_rounds"):
        parse_rule({"metric": "accuracy", "max_rounds": max_rounds, **SELECTION})


def test_parse_rule_accepts_zero_max_rounds():
    assert parse_rule({"metric": "accuracy", "max_rounds": 0, **SELECTION})["max_rounds"] == 0


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "3", ["accuracy"], None])
def test_parse_rule_rejects_something_that_is_not_a_json_object(raw):
    with pytest.raises(RuleError):
        parse_rule(raw)


# -- 2. rule_from_notes --------------------------------------------------------------------------------------------------


def test_rule_from_notes_returns_the_rule_and_the_note_that_recorded_it():
    assert rule_from_notes([rule_note("note_r", RULE)]) == (RULE, "note_r")


def test_rule_from_notes_first_selection_rule_note_wins():
    second = {**RULE, "metric": "f1"}
    assert rule_from_notes([rule_note("note_1", RULE), rule_note("note_2", second)]) == (RULE, "note_1")


def test_rule_from_notes_counts_a_lock_selection_when_it_comes_first():
    other = {**RULE, "metric": "f1"}
    assert rule_from_notes([lock("note_1", "job_a", RULE), rule_note("note_2", other)]) == (RULE, "note_1")


def test_rule_from_notes_a_selection_rule_note_before_a_lock_selection_wins():
    other = {**RULE, "metric": "f1"}
    assert rule_from_notes([rule_note("note_1", RULE), lock("note_2", "job_a", other)]) == (RULE, "note_1")


def test_rule_from_notes_skips_an_invalid_rule_note():
    bad = {**RULE, "split": "test"}
    assert rule_from_notes([rule_note("note_1", bad), rule_note("note_2", RULE)]) == (RULE, "note_2")


def test_rule_from_notes_skips_a_rule_without_a_scorer_or_selection_data():
    no_scorer = {k: v for k, v in RULE.items() if k != "scorer"}
    no_data = {k: v for k, v in RULE.items() if k != "data"}
    notes = [rule_note("note_1", no_scorer), lock("note_2", "job_a", no_data), rule_note("note_3", RULE)]
    assert rule_from_notes(notes) == (RULE, "note_3")
    assert rule_from_notes(notes[:2]) is None


def test_rule_from_notes_skips_an_invalid_lock_selection():
    bad = {**RULE, "direction": "sideways"}
    assert rule_from_notes([lock("note_1", "job_a", bad), lock("note_2", "job_b", RULE)]) == (RULE, "note_2")


def test_rule_from_notes_ignores_a_lock_without_a_selection_and_other_kinds():
    notes = [lock("note_1", "job_a"), {"id": "note_2", "kind": "observation", "text": json.dumps(RULE)}]
    assert rule_from_notes(notes) is None


def test_rule_from_notes_is_none_without_notes():
    assert rule_from_notes([]) is None


# -- 3. metric_value -----------------------------------------------------------------------------------------------------


def test_metric_value_exact_key():
    assert metric_value({"accuracy": 0.8, "loss": 0.3}, "accuracy") == 0.8


def test_metric_value_matches_a_namespaced_key_by_its_last_segment():
    assert metric_value({"classification-report/1:accuracy": 0.9}, "accuracy") == 0.9


def test_metric_value_matches_a_plain_key_against_a_namespaced_name():
    assert metric_value({"accuracy": 0.9}, "classification-report/1:accuracy") == 0.9


def test_metric_value_matches_a_dotted_key():
    assert metric_value({"validation.accuracy": 0.7}, "accuracy") == 0.7


def test_metric_value_exact_key_wins_over_other_suffix_matches():
    assert metric_value({"accuracy": 0.5, "a:accuracy": 0.6, "b:accuracy": 0.7}, "accuracy") == 0.5


def test_metric_value_is_none_when_two_keys_share_the_last_segment():
    assert metric_value({"report-a:accuracy": 0.1, "report-b/accuracy": 0.2}, "accuracy") is None


def test_metric_value_ignores_non_numeric_candidates_when_matching_by_suffix():
    assert metric_value({"a:accuracy": "bad", "b:accuracy": 0.5}, "accuracy") == 0.5


@pytest.mark.parametrize("bad", ["high", True, False, None, [0.9], float("nan")])
def test_metric_value_is_none_for_a_non_numeric_value(bad):
    assert metric_value({"accuracy": bad}, "accuracy") is None


@pytest.mark.parametrize("metrics", [None, [], "accuracy", 0.9, {}])
def test_metric_value_is_none_without_a_metrics_mapping(metrics):
    assert metric_value(metrics, "accuracy") is None


def test_metric_value_is_none_for_a_missing_metric():
    assert metric_value({"loss": 0.3}, "accuracy") is None


# -- 4. lineage and scorer -----------------------------------------------------------------------------------------------


def test_lineage_follows_consumers_of_outputs_in_job_order():
    jobs = [*train_chain("a"), job("job_other", inputs={"source": "art_z"})]
    assert [j["id"] for j in lineage(jobs, "a_train")] == ["a_infer", "a_score"]


def test_lineage_excludes_unrelated_jobs():
    unrelated = [*train_chain("b", accuracy=0.99), job("job_other", inputs={"source": "art_z"})]
    ids = [j["id"] for j in lineage([*train_chain("a"), *unrelated], "a_train")]
    assert ids == ["a_infer", "a_score"]
    assert not {"b_train", "b_infer", "b_score", "job_other"} & set(ids)


def test_lineage_excludes_the_job_itself():
    assert "a_train" not in [j["id"] for j in lineage(train_chain("a"), "a_train")]


def test_lineage_ignores_failed_jobs_and_what_only_they_could_have_fed():
    train, infer, score = train_chain("a")
    infer["status"] = "failed"
    assert lineage([train, infer, score], "a_train") == []


def test_lineage_of_an_unknown_job_is_empty():
    assert lineage(train_chain("a"), "job_missing") == []


def test_scorer_is_the_first_job_of_the_lineage_with_metrics():
    assert scorer(train_chain("a"), "a_train")["id"] == "a_score"


def test_scorer_ignores_a_failed_scoring_job():
    jobs = train_chain("a")
    jobs[2]["status"] = "failed"
    assert scorer(jobs, "a_train") is None


def test_scorer_ignores_jobs_outside_the_lineage_even_with_metrics():
    jobs = [job("job_a"), job("job_x", inputs={"source": "art_z"}, metrics={"accuracy": 0.99})]
    assert scorer(jobs, "job_a") is None


def test_scorer_with_a_metric_skips_a_job_that_does_not_report_it():
    jobs = train_chain("a")
    jobs.append(job("a_other_score", component="eidos.project.score@1", inputs={"predictions": "art_a_pred"},
                    metrics={"mse": 0.2}))
    assert scorer(jobs, "a_train", "mse")["id"] == "a_other_score"
    assert scorer(jobs, "a_train", "accuracy")["id"] == "a_score"


# -- 5. selection_evidence -----------------------------------------------------------------------------------------------


def test_selection_evidence_reads_the_recorded_receipt_of_the_scorer():
    ev = selection_evidence(train_chain("a", accuracy=0.8125), "a_train", RULE)
    assert ev == {"metric": "accuracy", "split": "validation", "value": 0.8125, "scored_by": "a_score"}


def test_selection_evidence_value_is_never_taken_from_note_text():
    # a lock that claims 0.99 in its text; the evidence is the scorer's receipt (0.8)
    note = {"id": "note_1", "kind": "candidate_lock",
            "text": json.dumps({"job_id": "a_train", "selection": RULE, "accuracy": 0.99,
                                "evidence": {"value": 0.99, "scored_by": "a_score"}})}
    st = run_state(notes=[note], jobs=train_chain("a", accuracy=0.8))
    assert st["incumbent"]["evidence"]["value"] == 0.8


def test_selection_evidence_ignores_a_scorer_outside_the_lineage():
    # a matching scorer on the right data, but it never read this candidate's outputs
    jobs = [*train_chain("a", accuracy=0.8), scored("job_x", 0.99, inputs={"source": "art_z"})]
    ev = selection_evidence(jobs, "a_train", RULE)
    assert (ev["value"], ev["scored_by"]) == (0.8, "a_score")


def test_selection_evidence_takes_the_first_matching_scorer_of_the_lineage_not_a_later_better_one():
    jobs = train_chain("a", accuracy=0.7)
    jobs.append(job("a_score2", component="eidos.project.score@1", config={"split": "validation"},
                    inputs={"predictions": "art_a_pred"}, metrics={"accuracy": 0.9}))
    ev = selection_evidence(jobs, "a_train", RULE)
    assert (ev["scored_by"], ev["value"]) == ("a_score", 0.7)


def test_selection_evidence_has_no_way_to_nominate_a_scorer():
    # R14 #2: the scorer is the rule's, fixed before any result; the model can no longer pick one by id
    jobs = train_chain("a", accuracy=0.7)
    with pytest.raises(TypeError):
        selection_evidence(jobs, "a_train", RULE, scored_by="a_score")  # type: ignore[call-arg]


def test_selection_evidence_falls_back_to_the_jobs_own_receipt():
    ev = selection_evidence([scored("job_a", 0.85)], "job_a", RULE)
    assert ev == {"metric": "accuracy", "split": "validation", "value": 0.85, "scored_by": "job_a"}


def test_selection_evidence_prefers_a_scorer_over_the_jobs_own_receipt():
    jobs = train_chain("a", accuracy=0.8)
    jobs[0]["receipt"] = {"metrics": {"accuracy": 0.99}}  # e.g. a training-set number, from the training component
    assert selection_evidence(jobs, "a_train", RULE)["value"] == 0.8


def test_selection_evidence_prefers_the_lineage_even_when_the_job_is_a_scorer_itself():
    jobs = [scored("a_train", 0.99), job("a_inf", component="eidos.project.infer@1", inputs={"model": "art_a_train"},
                                          outputs={"predictions": "art_a_pred"}),
            scored("a_score", 0.8, inputs={"predictions": "art_a_pred"}, outputs={})]
    assert selection_evidence(jobs, "a_train", RULE)["scored_by"] == "a_score"


def test_selection_evidence_is_none_when_the_metric_is_absent():
    assert selection_evidence([scored("job_a", metrics={"loss": 0.3})], "job_a", RULE) is None


def test_selection_evidence_is_none_for_an_unknown_job_or_a_failed_one():
    assert selection_evidence([scored("job_a", 0.8)], "job_missing", RULE) is None
    assert selection_evidence([scored("job_a", 0.8, status="failed")], "job_a", RULE) is None


def test_selection_evidence_carries_the_rules_split():
    rule = {**RULE, "split": "training-cv"}
    assert selection_evidence([scored("job_a", 0.8)], "job_a", rule)["split"] == "training-cv"


def test_selection_evidence_is_none_for_a_receipt_that_did_not_run_the_rules_scorer():
    # the same number on a command job's receipt (R14 #2: only the rule's scorer, on its data, is evidence)
    assert selection_evidence([job("job_a", metrics={"accuracy": 0.85})], "job_a", RULE) is None


# -- 6. better and the ceiling -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("challenger, incumbent, expected", [(0.9, 0.8, True), (0.7, 0.8, False), (0.8, 0.8, False)])
def test_better_higher_is_better_is_strict(challenger, incumbent, expected):
    assert better(challenger, incumbent, "higher_is_better") is expected


@pytest.mark.parametrize("challenger, incumbent, expected", [(0.1, 0.2, True), (0.3, 0.2, False), (0.2, 0.2, False)])
def test_better_lower_is_better_is_strict(challenger, incumbent, expected):
    assert better(challenger, incumbent, "lower_is_better") is expected


def test_at_ceiling_perfect_accuracy_is_a_ceiling_for_higher_is_better():
    assert at_ceiling(1.0, RULE) is True
    assert at_ceiling(0.999, RULE) is False


def test_at_ceiling_zero_mse_is_a_ceiling_for_lower_is_better():
    assert at_ceiling(0.0, LOWER_RULE) is True
    assert at_ceiling(0.01, LOWER_RULE) is False


def test_at_ceiling_a_namespaced_metric_name_uses_its_last_segment():
    assert at_ceiling(1.0, {**RULE, "metric": "classification-report/1:accuracy"}) is True


def test_at_ceiling_a_declared_ceiling_overrides_the_default():
    assert at_ceiling(0.95, {**RULE, "ceiling": 0.95}) is True
    assert at_ceiling(0.99, {**RULE, "ceiling": 1.5}) is False
    assert ceiling_of({**RULE, "ceiling": 0.95}) == 0.95


def test_at_ceiling_a_declared_ceiling_gives_an_unknown_metric_a_ceiling():
    rule = {"metric": "custom_score", "split": "validation", "direction": "higher_is_better", "ceiling": 5.0}
    assert at_ceiling(5.0, rule) is True
    assert at_ceiling(4.9, rule) is False


def test_at_ceiling_an_unknown_metric_has_no_ceiling():
    rule = {"metric": "custom_score", "split": "validation", "direction": "higher_is_better"}
    assert ceiling_of(rule) is None
    assert at_ceiling(1.0, rule) is False


def test_at_ceiling_a_bounded_metric_in_the_wrong_direction_has_no_ceiling():
    assert at_ceiling(1.0, {**RULE, "direction": "lower_is_better"}) is False


def test_at_ceiling_is_false_without_a_value():
    assert at_ceiling(None, RULE) is False


# -- 7. lock_versions ----------------------------------------------------------------------------------------------------


def test_lock_versions_numbers_the_locks_that_name_a_job_oldest_first():
    versions = lock_versions([lock("note_1", "job_a"), lock("note_2", "job_b")], candidate_lock_job_id)
    assert [(v["version"], v["note_id"], v["job_id"]) for v in versions] == [(1, "note_1", "job_a"), (2, "note_2", "job_b")]


def test_lock_versions_relocking_the_same_job_is_not_a_new_version():
    versions = lock_versions([lock("note_1", "job_a"), lock("note_2", "job_a")], candidate_lock_job_id)
    assert [(v["version"], v["note_id"]) for v in versions] == [(1, "note_1")]


def test_lock_versions_going_back_to_an_earlier_job_is_a_new_version():
    notes = [lock("note_1", "job_a"), lock("note_2", "job_b"), lock("note_3", "job_a")]
    assert [v["job_id"] for v in lock_versions(notes, candidate_lock_job_id)] == ["job_a", "job_b", "job_a"]


def test_lock_versions_skips_a_lock_that_names_no_job_without_leaving_a_gap():
    notes = [lock("note_1", "job_a"), {"id": "note_2", "kind": "candidate_lock", "text": "lr=0.01, looks good"},
             lock("note_3", "job_b")]
    assert [(v["version"], v["job_id"]) for v in lock_versions(notes, candidate_lock_job_id)] == [(1, "job_a"), (2, "job_b")]


def test_lock_versions_takes_the_job_from_a_single_evidence_ref():
    note = {"id": "note_1", "kind": "candidate_lock", "text": "free text", "evidence_refs": ["job_z"]}
    assert [v["job_id"] for v in lock_versions([note], candidate_lock_job_id)] == ["job_z"]


def test_lock_versions_carries_recorded_incumbent_evidence_and_basis():
    recorded = {"version": 1, "evidence": {"metric": "accuracy", "split": "validation", "value": 0.8, "scored_by": "job_s"},
                "basis": "first-lock"}
    versions = lock_versions([lock("note_1", "job_a", incumbent=recorded), lock("note_2", "job_b")], candidate_lock_job_id)
    assert versions[0]["evidence"] == recorded["evidence"] and versions[0]["basis"] == "first-lock"
    assert versions[1]["evidence"] is None and versions[1]["basis"] is None


def test_lock_versions_ignores_other_note_kinds():
    notes = [{"id": "note_1", "kind": "observation", "text": json.dumps({"job_id": "job_a"})}]
    assert lock_versions(notes, candidate_lock_job_id) == []


# -- 8. reproduced, reproduction_jobs and rounds -------------------------------------------------------------------------


def test_reproduced_a_later_same_recipe_job_with_another_seed_counts():
    jobs = [scored("job_a", 0.8), scored("job_r", 0.8, config={"seed": 2})]
    assert reproduced(jobs, "job_a", RULE) == "job_r"


def test_reproduced_ignores_the_timeout_in_the_recipe():
    jobs = [scored("job_a", 0.8, config={"timeout_seconds": 60}),
            scored("job_r", 0.8, config={"seed": 2, "timeout_seconds": 600})]
    assert reproduced(jobs, "job_a", RULE) == "job_r"


def test_reproduced_needs_a_different_seed():
    assert reproduced([scored("job_a"), scored("job_r")], "job_a", RULE) is None


def test_reproduced_needs_the_same_recipe():
    assert reproduced([scored("job_a"), scored("job_r", config={"seed": 2, "lr": 0.5})], "job_a", RULE) is None


def test_reproduced_needs_the_same_inputs():
    other = scored("job_r", config={"seed": 2}, inputs={"source": "art_other"})
    assert reproduced([scored("job_a"), other], "job_a", RULE) is None


def test_reproduced_only_looks_at_jobs_after_the_incumbent():
    jobs = [scored("job_r", config={"seed": 2}), scored("job_a")]
    assert reproduced(jobs, "job_a", RULE) is None


def test_reproduced_needs_a_succeeded_job():
    jobs = [scored("job_a"), scored("job_r", config={"seed": 2}, status="failed")]
    assert reproduced(jobs, "job_a", RULE) is None


def test_reproduced_is_none_for_an_unknown_job():
    assert reproduced([scored("job_a")], "job_missing", RULE) is None


def test_reproduced_is_none_without_a_rule_even_for_a_same_recipe_rerun():
    # R14 #3: no prospective criterion, no reproduction
    jobs = [scored("job_a", 0.8), scored("job_r", 0.8, config={"seed": 2})]
    assert reproduced(jobs, "job_a", None) is None


def test_reproduction_jobs_are_same_recipe_reruns_and_the_lineages_but_not_the_originals():
    original, rerun = train_chain("a", seed=1), train_chain("r", seed=2)
    challenger = job("job_c", config={"seed": 1, "lr": 0.5})
    jobs = [*original, *rerun, challenger]
    # reruns of the original's recipe, and what scores the rerun or the original itself; never the original
    assert reproduction_jobs(jobs, ["a_train"]) == {"r_train", "r_infer", "r_score", "a_infer", "a_score"}


def test_reproduction_jobs_cover_every_recipe_given():
    jobs = [job("job_a"), job("job_b", config={"seed": 1, "lr": 0.3}), job("job_a2", config={"seed": 2, "lr": 0.1}),
            job("job_b2", config={"seed": 2, "lr": 0.3}), job("job_c", config={"seed": 1, "lr": 0.9})]
    assert reproduction_jobs(jobs, ["job_a", "job_b"]) == {"job_a2", "job_b2"}


def test_rounds_is_empty_before_the_first_lock():
    jobs = [scored("job_b")]
    assert rounds([{"turn": 3, "jobs": ["job_b"]}], jobs, None, None, rule=RULE) == ([], 0, [])


def test_rounds_count_only_turns_after_the_first_locks_turn():
    jobs = [scored("job_b"), scored("job_c"), scored("job_d")]
    log = [{"turn": 1, "jobs": ["job_b"]}, {"turn": 2, "jobs": ["job_c"]}, {"turn": 3, "jobs": ["job_d"]}]
    assert rounds(log, jobs, 2, 2, rule=RULE) == ([3], 1, [])


@pytest.mark.parametrize("purpose, counted", [("open-search", True), ("hypothesis-test", True),
                                              ("measurement-development", True), ("reproduction", False),
                                              ("confirmation", False)])
def test_rounds_count_only_challenger_purposes(purpose, counted):
    turns, _, _ = rounds([{"turn": 3, "jobs": ["job_b"]}], [scored("job_b", purpose=purpose)], 2, 2, rule=RULE)
    assert (turns == [3]) is counted


def test_rounds_a_turn_counts_once_however_many_jobs_it_admitted():
    jobs = [scored("job_b"), scored("job_c")]
    turns, since, _ = rounds([{"turn": 3, "jobs": ["job_b", "job_c"]}, {"turn": 3, "jobs": ["job_c"]}], jobs, 2, 2,
                             rule=RULE)
    assert (turns, since) == ([3], 1)


def test_rounds_exclude_reproduction_work_and_a_turn_of_nothing_else_is_no_round():
    jobs = [scored("job_r"), scored("job_b")]
    log = [{"turn": 3, "jobs": ["job_r"]}, {"turn": 4, "jobs": ["job_r", "job_b"]}]
    assert rounds(log, jobs, 2, 2, excluded={"job_r"}, rule=RULE) == ([4], 1, [])


def test_rounds_without_improvement_count_rounds_after_the_latest_locks_turn():
    jobs = [scored(f"job_{i}") for i in range(5)]
    log = [{"turn": 3 + i, "jobs": [f"job_{i}"]} for i in range(5)]  # turns 3..7
    turns, since, pending = rounds(log, jobs, 2, 5, rule=RULE)  # the incumbent last changed in turn 5
    assert turns == [3, 4, 5, 6, 7]
    assert since == 2  # turns 6 and 7
    assert pending == []


def test_rounds_a_job_unknown_to_the_job_list_is_not_a_challenger():
    assert rounds([{"turn": 3, "jobs": ["job_missing"]}], [scored("job_b")], 2, 2, rule=RULE) == ([], 0, [])


# -- 9. allowance, hold-back and policy ----------------------------------------------------------------------------------


@pytest.mark.parametrize("wall, expected", [(3600, 540.0), (600, 120.0), (10_000, 900.0), (6000, 900.0), (800, 120.0)])
def test_holdback_is_the_fraction_of_the_wall_limit_clamped_between_the_minimum_and_maximum(wall, expected):
    assert holdback_seconds(DEFAULT_POLICY, wall) == expected


@pytest.mark.parametrize("wall", [None, 0])
def test_holdback_is_zero_without_a_wall_limit(wall):
    assert holdback_seconds(DEFAULT_POLICY, wall) == 0.0


def test_allowance_seconds_left_is_remaining_less_the_conclude_threshold_and_the_holdback():
    left = allowance(DEFAULT_POLICY, COMPLETION, 3600.0, None)
    assert left["holdback_seconds"] == 540.0
    assert left["seconds_left"] == 1000.0 - 180.0 - 540.0


def test_allowance_without_a_wall_limit_holds_nothing_back():
    left = allowance(DEFAULT_POLICY, COMPLETION, None, None)
    assert (left["holdback_seconds"], left["seconds_left"]) == (0.0, 820.0)


def test_allowance_tokens_left_is_limit_less_used_reserved_and_the_token_holdback():
    budgets = {"model_tokens": {"limit_amount": 1_000_000, "used": 100_000, "reserved": 50_000}}
    left = allowance(DEFAULT_POLICY, None, None, budgets)
    assert left["holdback_tokens"] == 150_000
    assert left["tokens_left"] == 1_000_000 - 100_000 - 50_000 - 150_000


def test_allowance_is_none_without_a_completion_and_without_a_token_limit():
    left = allowance(DEFAULT_POLICY, None, 3600.0, None)
    assert left == {"seconds_left": None, "tokens_left": None, "holdback_seconds": None, "holdback_tokens": None}


@pytest.mark.parametrize("budgets", [{}, {"model_tokens": {"limit_amount": None, "used": 5}}, {"model_tokens": None},
                                     {"wall_seconds": {"limit_amount": 10}}])
def test_allowance_tokens_are_none_without_a_token_limit(budgets):
    assert allowance(DEFAULT_POLICY, COMPLETION, 3600.0, budgets)["tokens_left"] is None


def test_allowance_can_go_negative_when_the_holdback_is_reached():
    thin = {**COMPLETION, "remaining_seconds": 500.0}
    assert allowance(DEFAULT_POLICY, thin, 3600.0, None)["seconds_left"] < 0


def test_policy_without_a_config_is_the_default_and_does_not_alias_it():
    pol = policy(None)
    assert pol == DEFAULT_POLICY and pol is not DEFAULT_POLICY
    pol["patience_rounds"] = 99
    assert DEFAULT_POLICY["patience_rounds"] == 3


def test_policy_overrides_known_keys_from_the_exploration_config():
    pol = policy({"exploration": {"patience_rounds": 5, "max_rounds": 8, "holdback_fraction": 0.3}})
    assert (pol["patience_rounds"], pol["max_rounds"], pol["holdback_fraction"]) == (5, 8, 0.3)
    assert pol["min_holdback_seconds"] == DEFAULT_POLICY["min_holdback_seconds"]


def test_policy_ignores_unknown_keys():
    pol = policy({"exploration": {"patience_rounds": 2, "bogus": 1}, "other": {"patience_rounds": 9}})
    assert pol["patience_rounds"] == 2 and "bogus" not in pol


@pytest.mark.parametrize("config", [{}, {"exploration": None}, {"exploration": {}}])
def test_policy_with_no_exploration_settings_is_the_default(config):
    assert policy(config) == DEFAULT_POLICY


# -- 10. state and compact -----------------------------------------------------------------------------------------------


def test_state_is_baseline_with_no_lock():
    st = run_state(notes=[], note_turns={})
    assert (st["phase"], st["stop"], st["incumbent"], st["versions"], st["rounds"]) == ("baseline", None, None, [], 0)


def test_state_baseline_still_reports_a_recorded_rule_and_the_allowance():
    st = run_state(notes=[rule_note("note_r", RULE)], note_turns={}, completion=COMPLETION, wall_seconds=3600.0)
    assert st["phase"] == "baseline"
    assert (st["rule"], st["rule_note"]) == (RULE, "note_r")
    assert st["allowance"]["seconds_left"] == 280.0


def test_state_explores_with_a_lock_and_an_allowance_left():
    st = run_state(completion=COMPLETION, wall_seconds=3600.0, budgets=BUDGETS)
    assert (st["phase"], st["stop"]) == ("explore", None)
    assert st["incumbent"]["version"] == 1 and st["incumbent"]["job_id"] == "job_a"
    assert st["rule"] == RULE and st["rule_note"] == "note_1"
    assert st["allowance"]["seconds_left"] == 280.0
    assert st["allowance"]["tokens_left"] == 1_000_000 - 100_000 - 150_000


def test_state_takes_the_incumbents_evidence_from_the_recorded_receipt():
    st = run_state()
    assert st["incumbent"]["evidence"] == {"metric": "accuracy", "split": "validation", "value": 0.8, "scored_by": "job_a"}


def test_state_without_a_rule_has_no_evidence_and_so_no_ceiling_stop():
    st = run_state(notes=[lock("note_1", "job_a")], jobs=[scored("job_a", 1.0)])
    assert st["rule"] is None and st["incumbent"]["evidence"] is None
    assert (st["phase"], st["stop"]) == ("explore", None)


def test_state_uses_recorded_incumbent_evidence_instead_of_recomputing_it():
    recorded = {"metric": "accuracy", "split": "validation", "value": 0.6, "scored_by": "job_s"}
    st = run_state(notes=[lock("note_1", "job_a", RULE, incumbent={"version": 1, "evidence": recorded, "basis": "x"})])
    assert st["incumbent"]["evidence"] == recorded
    assert st["incumbent"]["basis"] == "x"


def test_state_finalizes_with_selection_frozen():
    st = run_state(frozen=True)
    assert (st["phase"], st["stop"], st["frozen"]) == ("finalize", "selection-frozen", True)


def test_state_frozen_outranks_the_other_stops():
    st = run_state(jobs=[scored("job_a", 1.0)], frozen=True)
    assert st["stop"] == "selection-frozen"


def test_state_finalizes_at_a_perfect_score():
    st = run_state(jobs=[scored("job_a", 1.0)])
    assert (st["phase"], st["stop"]) == ("finalize", "ceiling-perfect-score")


def test_state_finalizes_at_a_declared_ceiling():
    rule = {**RULE, "ceiling": 0.8}
    st = run_state(notes=[lock("note_1", "job_a", rule)])
    assert st["stop"] == "ceiling-perfect-score"


def test_state_finalizes_with_no_improvement_in_the_patience_rounds():
    extra, log = challengers(3)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["phase"], st["stop"]) == ("finalize", "ceiling-no-improvement")
    assert (st["rounds"], st["rounds_without_improvement"], st["patience_rounds"]) == (3, 3, 3)


def test_state_keeps_exploring_just_short_of_the_patience_rounds():
    extra, log = challengers(2)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["phase"], st["stop"], st["rounds_without_improvement"]) == ("explore", None, 2)


def test_state_patience_comes_from_the_exploration_config():
    extra, log = challengers(1)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   config={"exploration": {"patience_rounds": 1}})
    assert (st["stop"], st["patience_rounds"]) == ("ceiling-no-improvement", 1)


def test_state_zero_patience_never_stops_on_no_improvement():
    extra, log = challengers(4)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   config={"exploration": {"patience_rounds": 0}})
    assert (st["phase"], st["stop"]) == ("explore", None)


def test_state_finalizes_at_the_rules_search_limit():
    extra, log = challengers(2)
    rule = {**RULE, "max_rounds": 2}
    st = run_state(notes=[lock("note_1", "job_a", rule)], jobs=[scored("job_a", 0.8), *extra], round_log=log)
    assert (st["phase"], st["stop"], st["max_rounds"]) == ("finalize", "search-limit", 2)


def test_state_finalizes_at_the_search_limit_from_the_policy_config():
    extra, log = challengers(2)
    st = run_state(jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   config={"exploration": {"max_rounds": 2}})
    assert st["stop"] == "search-limit"


def test_state_search_limit_of_zero_stops_exploration_at_once():
    st = run_state(notes=[lock("note_1", "job_a", {**RULE, "max_rounds": 0})])
    assert st["stop"] == "search-limit"


def test_state_finalizes_when_the_wall_holdback_is_reached():
    thin = {**COMPLETION, "remaining_seconds": 700.0}  # 700 - 180 - 540 < 0
    st = run_state(completion=thin, wall_seconds=3600.0)
    assert (st["phase"], st["stop"]) == ("finalize", "wall-holdback")
    assert st["allowance"]["seconds_left"] <= 0


def test_state_finalizes_when_seconds_left_is_exactly_zero():
    edge = {**COMPLETION, "remaining_seconds": 720.0}  # 720 - 180 - 540 == 0
    assert run_state(completion=edge, wall_seconds=3600.0)["stop"] == "wall-holdback"


def test_state_finalizes_when_the_token_holdback_is_reached():
    spent = {"model_tokens": {"limit_amount": 1_000_000, "used": 900_000, "reserved": 0}}
    st = run_state(budgets=spent)
    assert (st["phase"], st["stop"]) == ("finalize", "token-holdback")
    assert st["allowance"]["tokens_left"] < 0


def test_state_with_no_completion_and_no_token_limit_has_no_allowance_stop():
    st = run_state()
    assert st["allowance"]["seconds_left"] is None and st["allowance"]["tokens_left"] is None
    assert st["phase"] == "explore"


def test_state_a_later_relock_resets_rounds_without_improvement():
    extra, log = challengers(3)  # challenger rounds in turns 3, 4 and 5
    notes = [lock("note_1", "job_a", RULE), lock("note_2", "job_c1")]  # re-locked in turn 4
    st = run_state(notes=notes, jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   note_turns={"note_1": 2, "note_2": 4})
    assert (st["rounds"], st["rounds_without_improvement"]) == (3, 1)
    assert (st["phase"], st["stop"]) == ("explore", None)
    assert [v["job_id"] for v in st["versions"]] == ["job_a", "job_c1"]
    assert st["incumbent"]["version"] == 2


def test_state_rounds_count_from_the_first_lock_even_after_a_relock():
    extra, log = challengers(3)
    log = [{"turn": 1, "jobs": ["job_c0"]}, *log]  # a round before any lock is not a challenger round
    notes = [lock("note_1", "job_a", RULE), lock("note_2", "job_c1")]
    st = run_state(notes=notes, jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   note_turns={"note_1": 2, "note_2": 4})
    assert st["rounds"] == 3


def test_state_relocking_the_same_job_does_not_reset_the_count():
    extra, log = challengers(3)
    notes = [lock("note_1", "job_a", RULE), lock("note_2", "job_a")]
    st = run_state(notes=notes, jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   note_turns={"note_1": 2, "note_2": 5})
    assert (st["rounds_without_improvement"], st["stop"]) == (3, "ceiling-no-improvement")


def test_state_a_same_recipe_reproduction_is_not_a_challenger_round_and_marks_the_incumbent_reproduced():
    # a_train is scored by a_score; r_train re-runs a_train's recipe under another seed and is scored by r_score
    jobs = [*train_chain("a", seed=1, accuracy=0.8), *train_chain("r", seed=2, accuracy=0.8)]
    assert jobs[3]["purpose"] == "open-search"  # excluded by recipe, not by purpose
    log = [{"turn": 3, "jobs": ["r_train"]}, {"turn": 4, "jobs": ["r_infer"]}, {"turn": 5, "jobs": ["r_score"]}]
    st = run_state(notes=[lock("note_1", "a_train", RULE)], jobs=jobs, round_log=log)
    assert st["rounds"] == 0
    assert st["incumbent"]["reproduced_by"] == "r_train"
    assert st["phase"] == "explore"


def test_state_the_baselines_reruns_are_not_challenger_rounds():
    jobs = [job("job_base", config={"seed": 1, "lr": 0.9}), job("job_base2", config={"seed": 2, "lr": 0.9}),
            scored("job_a", 0.8)]
    log = [{"turn": 3, "jobs": ["job_base2"]}]
    assert run_state(jobs=jobs, round_log=log, baseline_job="job_base")["rounds"] == 0
    assert run_state(jobs=jobs, round_log=log)["rounds"] == 1


def test_state_reports_the_policy_id_and_frozen_flag():
    st = run_state()
    assert st["policy"] == "exploration-policy/2" and st["frozen"] is False


def test_compact_keeps_phase_stop_incumbent_rule_allowance_and_the_version_count():
    extra, log = challengers(1)
    notes = [lock("note_1", "job_a", RULE), lock("note_2", "job_c0")]
    st = run_state(notes=notes, jobs=[scored("job_a", 0.8), *extra], round_log=log,
                   note_turns={"note_1": 2, "note_2": 3}, completion=COMPLETION, wall_seconds=3600.0)
    out = compact(st)
    assert (out["phase"], out["stop"], out["rule"]) == ("explore", None, RULE)
    assert out["versions"] == 2 and out["rounds"] == 1 and out["allowance"] == st["allowance"]
    assert out["incumbent"]["job_id"] == "job_c0" and out["incumbent"]["version"] == 2
    assert out["incumbent"]["note_id"] == "note_2"
    assert set(out["incumbent"]) == {"version", "job_id", "note_id", "evidence", "basis", "reproduced_by",
                                     "reproduction"}
    assert out["pending_challengers"] == []


def test_compact_of_a_baseline_state_has_no_incumbent():
    out = compact(run_state(notes=[], note_turns={}))
    assert (out["phase"], out["incumbent"], out["versions"], out["rule"]) == ("baseline", None, 0, None)


def test_compact_of_a_finalize_state_keeps_the_stop_reason_and_limits():
    out = compact(run_state(jobs=[scored("job_a", 1.0)]))
    assert (out["phase"], out["stop"]) == ("finalize", "ceiling-perfect-score")
    assert out["patience_rounds"] == 3 and out["max_rounds"] is None


def test_compact_is_json_serialisable():
    json.dumps(compact(run_state(completion=COMPLETION, wall_seconds=3600.0, budgets=BUDGETS)))


# -- 11. the context lines -----------------------------------------------------------------------------------------------


def make_view(jobs=(), **over):
    view = {"investigation": {"id": "inv1", "status": "active", "body": {"question": "Does X improve Y?"}},
            "horizon": 10, "jobs": list(jobs), "budgets": {}, "environments": ["lab"], "environment_details": {},
            "charter": {"study_id": "s1", "exploration_only": False, "population": "p", "comparator": "c",
                        "metrics": {}, "success_rules": [], "resource_limits": {}}}
    view.update(over)
    return view


def exploration_view(**state_over):
    """A view whose ``exploration`` is the compact state of a locked incumbent (job_a, accuracy 0.8)."""
    jobs = state_over.pop("jobs", None) or [scored("job_a", 0.8)]
    return make_view(jobs, exploration=compact(run_state(jobs=jobs, **state_over)))


def text_of(view, **kw):
    return pack(view, [], **kw)["text"]


def line_with(text, fragment):
    matches = [ln for ln in text.splitlines() if fragment in ln]
    assert matches, f"no line contains {fragment!r}"
    return matches[0]


def test_context_default_policy_is_v3_and_v2_is_selectable():
    assert pack(make_view(), [])["policy"] == POLICY_ID == "context-policy/3"
    assert pack(make_view(), [], policy=POLICY_V2)["policy"] == POLICY_V2 == "context-policy/2"


def test_context_explore_lists_the_rule_the_incumbent_and_says_keep_improving():
    text = text_of(exploration_view(), policy=POLICY_ID)
    assert json.dumps(RULE, sort_keys=True) in line_with(text, "Selection rule")
    inc = line_with(text, "Incumbent v1")
    assert "job job_a" in inc and "accuracy 0.8" in inc and "not reproduced yet" in inc
    assert "keep improving" in line_with(text, "Exploration:")
    assert "STOPPED" not in text


def test_context_explore_lists_the_allowance_left():
    text = text_of(exploration_view(completion=COMPLETION, wall_seconds=3600.0, budgets=BUDGETS), policy=POLICY_ID)
    line = line_with(text, "Allowance left")
    assert "280 s" in line and "750000 model tokens" in line


def test_context_explore_says_a_reproduced_incumbent_is_reproduced():
    jobs = [scored("job_a", 0.8), scored("job_r", 0.8, config={"seed": 2})]
    text = text_of(exploration_view(jobs=jobs), policy=POLICY_ID)
    assert "reproduced by job_r" in line_with(text, "Incumbent v1")


def test_context_finalize_says_stopped_and_why():
    text = text_of(exploration_view(jobs=[scored("job_a", 1.0)]), policy=POLICY_ID)
    line = line_with(text, "Exploration:")
    assert "STOPPED" in line and "perfect score" in line
    assert "keep improving" not in text


def test_context_finalize_explains_a_frozen_selection():
    text = text_of(exploration_view(frozen=True), policy=POLICY_ID)
    assert "STOPPED" in text and "selection is frozen" in text


def test_context_finalize_explains_the_search_limit_with_its_number():
    jobs = [scored("job_a", 0.8)]
    view = make_view(jobs, exploration=compact(run_state(notes=[lock("note_1", "job_a", {**RULE, "max_rounds": 0})], jobs=jobs)))
    assert "search limit of 0 rounds" in text_of(view, policy=POLICY_ID)


def test_context_baseline_phase_says_there_is_no_incumbent_yet_and_no_rule_recorded():
    view = make_view(exploration=compact(run_state(notes=[], jobs=[], note_turns={})))
    text = text_of(view, policy=POLICY_ID)
    assert "no incumbent yet" in line_with(text, "Exploration:")
    assert "none recorded" in line_with(text, "Selection rule")
    assert "Incumbent v" not in text


def test_context_without_an_exploration_state_has_no_exploration_lines():
    text = text_of(make_view(), policy=POLICY_ID)
    assert "Selection rule" not in text and "Exploration:" not in text


def test_context_v2_replays_without_the_exploration_lines():
    view = exploration_view()
    text = text_of(view, policy=POLICY_V2)
    assert "Selection rule" not in text and "Incumbent v1" not in text and "Exploration:" not in text
    assert "Selection rule" in text_of(view, policy=POLICY_ID)


def baseline_record(scored=True):
    return {"job_id": "job_base", "component": "eidos.project.command@1", "environment": "lab",
            "metrics": {"accuracy": 0.7}, "scored_by": {"job_id": "job_bscore", "metrics": {"accuracy": 0.71}} if scored else None}


def test_context_names_the_recorded_baseline_without_a_reproduction_job_or_a_baseline_note():
    jobs = [job("job_x", purpose="open-search", metrics={"accuracy": 0.8})]  # nothing with purpose "reproduction"
    text = text_of(make_view(jobs, baseline_record=baseline_record()), policy=POLICY_ID)
    line = line_with(text, "Baseline (recorded)")
    assert "job job_base" in line and "environment lab" in line
    assert "scored by job_bscore" in line and '"accuracy": 0.71' in line
    assert "Baseline: none recorded yet" not in text


def test_context_recorded_baseline_that_is_not_scored_yet_says_so():
    text = text_of(make_view(baseline_record=baseline_record(scored=False)), policy=POLICY_ID)
    assert "not scored yet" in line_with(text, "Baseline (recorded)")
    assert "Baseline: none recorded yet" not in text


def test_context_recorded_baseline_outranks_a_recent_reproduction_job():
    jobs = [job("job_repro", purpose="reproduction", metrics={"accuracy": 0.5})]
    text = text_of(make_view(jobs, baseline_record=baseline_record()), policy=POLICY_ID)
    assert "Baseline (recorded): job job_base" in text
    assert "- Baseline: job job_repro" not in text


def test_context_an_explicit_baseline_note_overrides_the_recorded_baseline():
    jobs = [job("job_noted", purpose="open-search", metrics={"accuracy": 0.6})]
    note = {"id": "note_b", "kind": "baseline", "text": "the baseline is job_noted", "evidence_refs": ["job_noted"]}
    text = text_of(make_view(jobs, baseline_record=baseline_record(), pinned_notes=[note]), policy=POLICY_ID)
    assert "- Baseline: job job_noted" in text
    assert "Baseline (recorded)" not in text


def test_context_a_baseline_note_naming_no_job_does_not_hide_the_recorded_baseline():
    note = {"id": "note_b", "kind": "baseline", "text": "baseline accuracy is about 0.7", "evidence_refs": []}
    text = text_of(make_view([job("job_x")], baseline_record=baseline_record(), pinned_notes=[note]), policy=POLICY_ID)
    assert "Baseline (recorded): job job_base" in text
    assert "Baseline: none recorded yet" not in text


def test_context_with_no_baseline_record_still_says_none_recorded_yet():
    assert "- Baseline: none recorded yet" in text_of(make_view([job("job_x")]), policy=POLICY_ID)


def test_context_v2_does_not_use_the_baseline_record():
    text = text_of(make_view([job("job_x")], baseline_record=baseline_record()), policy=POLICY_V2)
    assert "Baseline (recorded)" not in text
    assert "- Baseline: none recorded yet" in text


def test_scoring_the_incumbent_after_the_lock_is_not_a_challenger_round():
    jobs = [job("job_inc"), job("job_inf", component="eidos.project.infer@1", purpose="measurement-development",
                                inputs={"model": "art_job_inc"}, outputs={"pred": "art_pred"})]
    excluded = reproduction_jobs(jobs, ["job_inc"])
    assert "job_inf" in excluded
    assert rounds([{"turn": 3, "jobs": ["job_inf"]}], jobs, 2, 2, excluded, RULE) == ([], 0, [])
