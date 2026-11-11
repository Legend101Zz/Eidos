"""context-policy/2 (ADR 0020): pinned facts, known failures, redacted error views, wall time; plus policy/1 and the
wall-time arithmetic in ``eidos.domain.turns``. All pure: a hand-built view goes in, a packed prompt comes out."""

from __future__ import annotations

import json

import pytest

from eidos.domain.environments import capability_summary
from eidos.domain.turns import (
    CONCLUDE_FRACTION,
    MAX_CONCLUDE_SECONDS,
    MIN_CONCLUDE_SECONDS,
    MIN_TURN_SECONDS,
    completion_state,
    conclude_threshold,
    turn_seconds,
)
from eidos.investigator import context
from eidos.investigator.context import (
    FAILURE_GROUPS,
    MAX_CATALOG,
    MAX_JOBS,
    POLICY_V1,
    STDERR_TAIL_CHARS,
    TOOL_GUIDE,
    pack,
)

NO_MODULE = "ModuleNotFoundError: No module named 'numpy'"
SUCCESS_RULES = [{"id": "rule-acc", "metric": "accuracy", "min_delta": 0.05, "marker": "SUCCESS-RULE-MARKER"}]


def make_job(i, *, status="succeeded", purpose="open-search", component="c@1", environment="lab", error=None,
             receipt=None, inputs=None, config=None, outputs=None):
    return {"id": f"job_{i:04x}", "component": component, "environment": environment, "status": status,
            "purpose": purpose, "inputs": inputs or {}, "config": config or {}, "receipt": receipt, "error": error,
            "outputs": outputs or []}


def failed_job(i, message=NO_MODULE, **kw):
    kw.setdefault("error", {"category": "execution", "message": message})
    return make_job(i, status="failed", **kw)


def make_view(jobs=(), **over):
    view = {"investigation": {"id": "inv1", "status": "active", "body": {"question": "Does X improve Y?"}},
            "horizon": 10, "jobs": list(jobs), "budgets": {}, "environments": ["lab"], "environment_details": {},
            "charter": {"study_id": "s1", "exploration_only": False, "population": "p", "comparator": "c",
                        "metrics": {}, "success_rules": SUCCESS_RULES, "resource_limits": {}}}
    view.update(over)
    return view


def make_catalog(n, summary_chars=60):
    return [{"ref": f"comp-{i:02d}@1.0.0", "implements": "thing/1", "kind": "component", "summary": "s" * summary_chars}
            for i in range(n)]


def job_record(text, job_id):
    """The JSON line packed for a job (job lines are the only lines starting with a ``component`` key)."""
    for line in text.splitlines():
        if line.startswith('{"component"'):
            rec = json.loads(line)
            if rec["id"] == job_id:
                return rec
    raise AssertionError(f"no job line for {job_id}")


def line_starting(text, prefix):
    matches = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert len(matches) == 1, f"expected one line starting {prefix!r}, got {matches}"
    return matches[0]


def pinned_notes_lock():
    return [{"id": "note_lock1", "kind": "candidate_lock", "text": "LOCK-TEXT: lr=0.01, seed policy 1..5"}]


# -- the policy ids ------------------------------------------------------------------------------------------------------


def test_default_policy_is_v2():
    out = pack(make_view(), [])
    assert out["policy"] == "context-policy/2"
    assert "## Pinned facts" in out["text"]


def test_source_manifest_digest_does_not_depend_on_the_policy():
    view = make_view([make_job(1)])
    assert pack(view, [])["source_manifest_digest"] == pack(view, [], policy=POLICY_V1)["source_manifest_digest"]


# -- the failing job's error view ---------------------------------------------------------------------------------------


def error_job_text(**error):
    base = {"category": "execution", "type": "RuntimeError", "message": "the command failed"}
    out = pack(make_view([failed_job(1, error={**base, **error})]), [])
    return out["text"], job_record(out["text"], "job_0001")["error"]


def test_error_view_has_category_exception_and_message():
    stderr = "Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\nValueError: bad shape\n"
    _, err = error_job_text(stderr_tail=stderr)
    assert err["category"] == "execution"
    assert err["exception"] == "ValueError"
    assert err["message"] == "the command failed"


def test_error_view_exception_falls_back_to_the_recorded_type():
    _, err = error_job_text(type="pkg.errors.Whatever", stderr_tail="process killed")
    assert err["exception"] == "Whatever"


def test_error_view_reports_the_innermost_exception_of_a_traceback():
    stderr = "KeyError: 'a'\n\nDuring handling of the above exception, another exception occurred:\n\nOSError: disk full\n"
    _, err = error_job_text(stderr_tail=stderr)
    assert err["exception"] == "OSError"


def test_error_view_stderr_tail_is_bounded_and_keeps_the_end():
    stderr = "x" * 5000 + "\nValueError: THE-REAL-REASON\n"
    _, err = error_job_text(stderr_tail=stderr)
    tail = err["stderr_tail"]
    assert len(tail) <= STDERR_TAIL_CHARS
    assert tail.endswith("ValueError: THE-REAL-REASON\n")
    assert err["exception"] == "ValueError"


def test_error_view_uses_the_traceback_tail_when_there_is_no_stderr_tail():
    _, err = error_job_text(traceback_tail="line1\nZeroDivisionError: division by zero")
    assert err["stderr_tail"].endswith("ZeroDivisionError: division by zero")
    assert err["exception"] == "ZeroDivisionError"


def test_error_view_has_no_stderr_tail_key_when_nothing_was_captured():
    _, err = error_job_text()
    assert "stderr_tail" not in err


def test_error_view_message_is_cut_to_1200_characters():
    _, err = error_job_text(message="m" * 5000)
    assert err["message"] == "m" * 1200


def test_error_view_redacts_a_secret_in_stderr_and_message():
    text, err = error_job_text(stderr_tail="loading config\nAPI_KEY=abcdef\nValueError: bad\n",
                               message="failed with password: hunter2xyz")
    assert "abcdef" not in text and "hunter2xyz" not in text
    assert "API_KEY=[redacted]" in err["stderr_tail"]
    assert "password: [redacted]" in err["message"]


def test_error_view_redacts_provider_style_keys():
    text, _ = error_job_text(stderr_tail="used sk-abcdefghijklmnopqrstuvwx and ghp_abcdefghijklmnopqrst1234 and "
                                         "Bearer abcdefghijklmnopqrstuvwx\n")
    assert "sk-abcdef" not in text and "ghp_abcdef" not in text and "Bearer abcdef" not in text
    assert "abcdefghijklmnopqrstuvwx" not in text


def test_error_view_redacts_secrets_held_in_prefixed_environment_variable_names():
    # Real-world forms: OPENAI_API_KEY, GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY. The pattern's leading word boundary does not
    # fire after an underscore, so these name-prefixed variables are not redacted.
    stderr = "OPENAI_API_KEY=prefixsecret1\nGITHUB_TOKEN=prefixsecret2\nAWS_SECRET_ACCESS_KEY=prefixsecret3\n"
    text, _ = error_job_text(stderr_tail=stderr)
    for leaked in ("prefixsecret1", "prefixsecret2", "prefixsecret3"):
        assert leaked not in text


def test_error_view_redacts_the_token_of_an_authorization_bearer_header():
    # "Authorization: Bearer <token>": the redaction consumes the word "Bearer" as the value and leaves the token.
    text, _ = error_job_text(stderr_tail="HTTP 401 for header Authorization: Bearer tok_1234567890abcdef\n")
    assert "tok_1234567890abcdef" not in text


def test_error_view_of_a_non_dict_error_is_passed_through():
    out = pack(make_view([failed_job(1, error="plain failure")]), [])
    assert job_record(out["text"], "job_0001")["error"] == "plain failure"


# -- known failures ------------------------------------------------------------------------------------------------------


def test_known_failure_line_summarises_repeated_failures():
    out = pack(make_view([failed_job(i) for i in range(3)]), [])
    line = line_starting(out["text"], "- c@1 failed")
    assert "failed 3×" in line
    assert "lab" in line
    assert "ModuleNotFoundError" in line
    assert "numpy" in line
    assert "(last: job_0002)" in line
    assert "Known failures so far" in out["text"]


def test_known_failures_cover_jobs_outside_the_job_window():
    jobs = [failed_job(i) for i in range(3)] + [make_job(100 + i) for i in range(40)]
    out = pack(make_view(jobs), [])
    assert len(jobs) > MAX_JOBS
    assert not {"job_0000", "job_0001", "job_0002"} & set(out["selections"]["jobs"])
    line = line_starting(out["text"], "- c@1 failed")
    assert "failed 3×" in line and "lab" in line and "ModuleNotFoundError" in line and "numpy" in line


def test_known_failures_are_grouped_by_component_environment_exception_and_detail():
    jobs = [failed_job(1), failed_job(2, environment="gpu"), failed_job(3, component="other@2"),
            failed_job(4, message="ModuleNotFoundError: No module named 'torch'"), failed_job(5)]
    out = pack(make_view(jobs), [])
    lines = [ln for ln in out["text"].splitlines() if " failed " in ln and ln.startswith("- ")]
    assert len(lines) == 4
    assert sum("failed 2×" in ln for ln in lines) == 1
    assert any("in environment gpu" in ln for ln in lines)
    assert any("other@2 failed" in ln for ln in lines)
    assert any("torch" in ln for ln in lines)


def test_known_failures_list_the_most_repeated_first():
    jobs = [failed_job(1, component="rare@1"), failed_job(2, component="common@1"), failed_job(3, component="common@1"),
            failed_job(4, component="common@1")]
    lines = [ln for ln in pack(make_view(jobs), [])["text"].splitlines() if ln.startswith("- ") and " failed " in ln]
    assert lines[0].startswith("- common@1 failed 3")
    assert lines[1].startswith("- rare@1 failed 1")


def test_succeeded_jobs_are_not_known_failures():
    out = pack(make_view([make_job(1), make_job(2, error=None)]), [])
    assert "Known failures so far" not in out["text"]
    assert out["omissions"]["known_failures"] == []


def test_known_failures_keep_only_the_top_groups_and_list_the_rest_as_omitted():
    jobs = [failed_job(i, component=f"comp{i:02d}@1", message=f"ValueError: reason {i}") for i in range(FAILURE_GROUPS + 3)]
    out = pack(make_view(jobs), [])
    shown = [ln for ln in out["text"].splitlines() if ln.startswith("- comp") and " failed " in ln]
    assert len(shown) == FAILURE_GROUPS
    assert len(out["omissions"]["known_failures"]) == 3
    assert all(o.count("|") == 2 for o in out["omissions"]["known_failures"])
    assert out["truncations"]["context_truncated"] is True


def test_known_failure_detail_is_redacted():
    out = pack(make_view([failed_job(1, message="RuntimeError: login failed token=zzsecretvalue")]), [])
    assert "zzsecretvalue" not in out["text"]
    assert "token=[redacted]" in line_starting(out["text"], "- c@1 failed")


def test_admission_refusals_appear_as_known_failures():
    refusals = [{"component": "c@1", "environment": "lab", "problems": [{"package": "torch"}, {"package": "numpy"}]},
                {"component": "c@1", "environment": "lab", "problems": [{"package": "numpy"}]}]
    out = pack(make_view(admission_refusals=refusals), [])
    line = line_starting(out["text"], "- c@1 refused")
    assert "refused 2× before launch in environment lab" in line
    assert line.endswith("missing or incompatible numpy, torch")


# -- pinned facts survive truncation -------------------------------------------------------------------------------------


def make_heavy_view():
    """40 bulky jobs (baseline and three failures first, so all fall outside the job window), a lock, a rule, an
    environment capability record and a large catalog."""
    env = {"python_version": "3.12.1", "installed": {"numpy": "2.1"},
           "capabilities": {"schema": "environment-capabilities/2", "status": "ok", "python_version": "3.12.1",
                            "interpreter_digest": "sha256:" + "cd" * 32,
                            "modules": {"numpy": {"provided": True, "version": "2.1"},
                                        "sklearn": {"provided": False}}}}
    bulky = {"k" + str(n): "v" * 40 for n in range(30)}
    jobs = [make_job(0, purpose="reproduction", status="succeeded", receipt={"metrics": {"accuracy": 0.61}})]
    jobs += [failed_job(i) for i in range(1, 4)]
    jobs += [make_job(100 + i, config=bulky, purpose="open-search") for i in range(36)]
    return make_view(jobs, environment_details={"lab": capability_summary(env)}, pinned_notes=pinned_notes_lock())


def assert_pinned_facts(text):
    assert "- Baseline: job job_0000" in text and '"accuracy": 0.61' in text
    assert "SUCCESS-RULE-MARKER" in text and "Charter success rule(s)" in text
    env_line = line_starting(text, "- Environment lab:")
    assert "python 3.12.1" in env_line and "numpy 2.1" in env_line and "sklearn MISSING" in env_line
    assert "Current lock (note_lock1, 1 recorded): LOCK-TEXT: lr=0.01" in text
    assert "failed 3×" in text and "ModuleNotFoundError" in text
    assert "You are the Eidos investigator" in text and "## Tools" in text


def test_pinned_facts_survive_truncation():
    catalog = make_catalog(40, summary_chars=3000)
    char_limit = 9000
    out = pack(make_heavy_view(), catalog, char_limit=char_limit)
    assert_pinned_facts(out["text"])
    assert out["truncations"]["context_truncated"] is True
    assert out["chars"] == len(out["text"])
    assert out["chars"] <= char_limit


def test_truncation_lists_the_dropped_catalog_entries_and_jobs():
    catalog = make_catalog(40, summary_chars=3000)
    view = make_heavy_view()
    out = pack(view, catalog, char_limit=9000)
    assert out["selections"]["catalog"] == []
    assert set(out["omissions"]["catalog"]) == {c["ref"] for c in catalog}
    assert "## Component catalog" not in out["text"]
    all_jobs = {j["id"] for j in view["jobs"]}
    dropped = set(out["omissions"]["jobs"])
    assert dropped and dropped <= all_jobs
    assert dropped.isdisjoint(out["selections"]["jobs"])
    assert {"job_0000", "job_0001"} <= dropped  # outside the window
    assert dropped | set(out["selections"]["jobs"]) == all_jobs


def test_without_a_tight_budget_nothing_is_marked_truncated():
    out = pack(make_view([make_job(1), make_job(2)]), make_catalog(3))
    assert out["truncations"]["context_truncated"] is False
    assert out["selections"]["jobs"] == ["job_0001", "job_0002"]
    assert out["selections"]["catalog"] == [c["ref"] for c in make_catalog(3)]
    assert out["omissions"] == {"jobs": [], "claims": [], "catalog": [], "known_failures": [], "sections": []}


def test_catalog_beyond_the_cap_is_omitted_even_with_room():
    out = pack(make_view(), make_catalog(MAX_CATALOG + 5))
    assert len(out["selections"]["catalog"]) == MAX_CATALOG
    assert out["omissions"]["catalog"] == [c["ref"] for c in make_catalog(MAX_CATALOG + 5)[MAX_CATALOG:]]
    assert "(more components exist" in out["text"]


def test_the_catalog_is_dropped_first_and_jobs_survive():
    jobs = [make_job(i, config={"a": "b" * 20}) for i in range(5)]
    view = make_view(jobs)
    catalog = make_catalog(20, summary_chars=300)
    # The room a pack has once the pinned facts are in is char_limit minus them and a 400-character margin; measure the
    # packed size without a catalog and leave only a little more than that.
    without_catalog = pack(view, [], char_limit=10**9)
    out = pack(view, catalog, char_limit=without_catalog["chars"] + 400 + 100)
    assert out["selections"]["jobs"] == [j["id"] for j in jobs]
    assert out["omissions"]["jobs"] == []
    assert out["selections"]["catalog"] == []
    assert set(out["omissions"]["catalog"]) == {c["ref"] for c in catalog}
    assert out["truncations"]["context_truncated"] is True
    assert "## Jobs (5 total" in out["text"] and "## Component catalog" not in out["text"]


def test_a_roomier_budget_keeps_the_newest_jobs_when_not_all_fit():
    jobs = [make_job(i, config={"a": "b" * 20}) for i in range(10)]
    view = make_view(jobs)
    full = pack(view, [], char_limit=10**9)
    one_job = len(json.dumps(job_record(full["text"], "job_0009"), sort_keys=True)) + 1
    out = pack(view, [], char_limit=full["chars"] + 400 - 3 * one_job)
    kept = out["selections"]["jobs"]
    assert 0 < len(kept) < 10
    assert kept == [j["id"] for j in jobs[-len(kept):]]  # the newest ones, in order
    assert set(out["omissions"]["jobs"]) == {j["id"] for j in jobs[: 10 - len(kept)]}


def test_jobs_beyond_the_window_are_omitted_even_with_room():
    jobs = [make_job(i) for i in range(MAX_JOBS + 4)]
    out = pack(make_view(jobs), [])
    assert out["selections"]["jobs"] == [j["id"] for j in jobs[-MAX_JOBS:]]
    assert out["omissions"]["jobs"] == [j["id"] for j in jobs[:4]]


# -- individual pinned facts ---------------------------------------------------------------------------------------------


def test_baseline_fact_is_the_reproduction_job_with_its_metrics():
    out = pack(make_view([make_job(1, purpose="reproduction", receipt={"metrics": {"accuracy": 0.5}}), make_job(2)]), [])
    line = line_starting(out["text"], "- Baseline:")
    assert "job job_0001" in line and "c@1" in line and "environment lab" in line and '"accuracy": 0.5' in line


def test_baseline_fact_says_so_when_there_is_no_baseline():
    out = pack(make_view([make_job(1), failed_job(2, purpose="reproduction")]), [])
    assert "- Baseline: none recorded yet (reproduce it first)." in out["text"]


def test_baseline_note_pointing_at_a_job_wins_and_is_found_outside_the_window():
    jobs = [make_job(0, receipt={"metrics": {"accuracy": 0.7}})] + [make_job(10 + i) for i in range(MAX_JOBS + 5)]
    notes = [{"id": "note_b", "kind": "baseline", "text": "see job_0000", "evidence_refs": []}]
    out = pack(make_view(jobs, pinned_notes=notes), [])
    assert "job_0000" not in out["selections"]["jobs"]
    assert "- Baseline: job job_0000" in out["text"]


def test_baseline_fact_includes_the_job_that_scored_it():
    base = make_job(1, purpose="reproduction", receipt={"metrics": {"loss": 1.0}},
                    outputs=[{"slot": "preds", "artifact_id": "art_preds", "role": "output", "schema": "p/1"}])
    scorer = make_job(2, inputs={"predictions": "art_preds"}, receipt={"metrics": {"accuracy": 0.8}})
    line = line_starting(pack(make_view([base, scorer]), [])["text"], "- Baseline:")
    assert "scored by job_0002" in line and '"accuracy": 0.8' in line


def test_lock_fact_uses_the_latest_lock_note():
    notes = [{"id": "n1", "kind": "lock", "text": "old lock"}, {"id": "n2", "kind": "candidate_lock", "text": "new lock"},
             {"id": "n3", "kind": "observation", "text": "unrelated"}]
    out = pack(make_view(pinned_notes=notes), [])
    assert "- Current lock (n2, 2 recorded): new lock" in out["text"]
    assert "unrelated" not in out["text"]


def test_lock_fact_redacts_secrets_and_is_bounded():
    notes = [{"id": "n1", "kind": "candidate_lock", "text": "token=lockleak " + "w" * 2000}]
    text = pack(make_view(pinned_notes=notes), [])["text"]
    assert "lockleak" not in text
    assert len(line_starting(text, "- Current lock")) < 700


def test_latest_frozen_evidence_plan_is_pinned():
    plans = [{"plan_id": "plan_old", "status": "draft", "claim": "c0"},
             {"plan_id": "plan_f", "status": "frozen", "claim": "c1", "assessment": None}]
    text = pack(make_view(evidence_plans=plans), [])["text"]
    assert "- Latest frozen evidence plan: plan_f (frozen, claim c1" in text
    assert "Latest frozen evidence plan: plan_old" not in text


def test_charter_success_rules_are_pinned_but_not_duplicated_in_the_charter_block():
    text = pack(make_view(), [])["text"]
    assert text.count("SUCCESS-RULE-MARKER") == 1
    assert "## Charter (fixed; read-only)" in text


def test_missing_charter_is_stated():
    text = pack(make_view(charter=None), [])["text"]
    assert "## Charter: NONE committed" in text
    assert "Charter success rule(s)" not in text


def test_probed_environment_line_shows_versions_and_missing_modules():
    env = {"capabilities": {"schema": "environment-capabilities/2", "status": "ok", "python_version": "3.12.1", "interpreter_digest": "sha256:" + "ef" * 32,
                            "modules": {"numpy": {"provided": True, "version": "2.1"}, "torch": {"provided": False}}}}
    text = pack(make_view(environment_details={"lab": capability_summary(env)}), [])["text"]
    line = line_starting(text, "- Environment lab:")
    assert line == "- Environment lab: python 3.12.1, interpreter sha256:efefefefefef, numpy 2.1, torch MISSING"


def test_unprobed_environment_line_says_so():
    details = {"lab": capability_summary({"python_version": "3.10.2"})}
    text = pack(make_view(environment_details=details), [])["text"]
    assert "- Environment lab: capabilities not probed (python 3.10.2)" in text


def test_environment_names_are_listed_when_there_are_no_capability_details():
    text = pack(make_view(environments=["lab", "gpu"], environment_details={}), [])["text"]
    assert "- Registered environments (names for `environment`): lab, gpu" in text


def test_overlong_question_is_cut_and_recorded_as_omitted():
    view = make_view()
    view["investigation"]["body"]["question"] = "q" * (context.QUESTION_CHARS + 123)
    out = pack(view, [], char_limit=10**7)
    assert out["omissions"]["sections"] == ["question: last 123 characters"]
    assert "q" * context.QUESTION_CHARS in out["text"] and "q" * (context.QUESTION_CHARS + 1) not in out["text"]
    assert out["truncations"]["context_truncated"] is True


# -- wall time -----------------------------------------------------------------------------------------------------------


def test_no_wall_fact_without_a_completion():
    text = pack(make_view(), [])["text"]
    assert "Wall time" not in text and "CONCLUDE NOW" not in text


def test_wall_fact_before_the_reserve_shows_remaining_time():
    completion = completion_state(deadline_unix=2000.0, now=1000.0, threshold=100.0)
    text = pack(make_view(completion=completion), [])["text"]
    assert "CONCLUDE NOW" not in text
    line = line_starting(text, "- Wall time:")
    assert "1000 s remain" in line and "from 100 s left" in line


def test_wall_fact_in_the_reserve_says_conclude_now():
    completion = completion_state(deadline_unix=2000.0, now=1950.0, threshold=100.0)
    assert completion["conclude_now"] is True
    text = pack(make_view(completion=completion), [])["text"]
    assert "CONCLUDE NOW" in text and "50 s of wall time remain" in text
    assert "- Wall time:" not in text


def test_wall_fact_is_pinned_under_truncation():
    completion = completion_state(deadline_unix=2000.0, now=1950.0, threshold=100.0)
    out = pack(make_heavy_view() | {"completion": completion}, make_catalog(40, 3000), char_limit=9000)
    assert "CONCLUDE NOW" in out["text"]
    assert_pinned_facts(out["text"])


# -- policy/1 ------------------------------------------------------------------------------------------------------------


def test_policy_v1_is_still_available_and_has_no_pinned_facts():
    view = make_view([failed_job(1), make_job(2, purpose="reproduction")], pinned_notes=pinned_notes_lock(),
                     completion=completion_state(2000.0, 1000.0, 100.0))
    out = pack(view, make_catalog(3), policy="context-policy/1")
    assert out["policy"] == "context-policy/1"
    assert "Pinned facts" not in out["text"]
    assert "Known failures so far" not in out["text"]
    assert "Wall time" not in out["text"]
    assert "LOCK-TEXT" not in out["text"]
    assert out["selections"] == {"jobs": ["job_0001", "job_0002"], "claims": [],
                                 "catalog": [c["ref"] for c in make_catalog(3)]}
    assert set(out["omissions"]) == {"jobs", "catalog"}
    assert out["truncations"] == {"context_truncated": False}
    assert "## Charter (fixed success rules; read-only)" in out["text"]


def test_policy_v1_error_view_has_no_exception_or_stderr_tail():
    job = failed_job(1, error={"category": "execution", "message": "boom", "stderr_tail": "ValueError: x"})
    err = job_record(pack(make_view([job]), [], policy=POLICY_V1)["text"], "job_0001")["error"]
    assert err == {"category": "execution", "message": "boom"}


def test_policy_v1_truncates_hard_and_appends_the_tool_guide():
    out = pack(make_heavy_view(), make_catalog(40, 3000), char_limit=9000, policy=POLICY_V1)
    assert out["truncations"] == {"context_truncated": True}
    assert "[context truncated by context-policy/1]" in out["text"]
    assert out["text"].endswith(TOOL_GUIDE)


# -- turns: conclude_threshold, completion_state, turn_seconds -----------------------------------------------------------


def test_conclude_threshold_is_a_fraction_of_the_wall_between_the_bounds():
    assert conclude_threshold(1000) == pytest.approx(1000 * CONCLUDE_FRACTION) == 100.0
    assert isinstance(conclude_threshold(1000), float)


def test_conclude_threshold_floors_and_caps():
    assert conclude_threshold(100) == MIN_CONCLUDE_SECONDS
    assert conclude_threshold(0) == MIN_CONCLUDE_SECONDS
    assert conclude_threshold(600) == MIN_CONCLUDE_SECONDS  # exactly at the floor
    assert conclude_threshold(6000) == MAX_CONCLUDE_SECONDS
    assert conclude_threshold(10**6) == MAX_CONCLUDE_SECONDS


def test_conclude_threshold_uses_the_configured_value_unchanged_by_the_bounds():
    assert conclude_threshold(1000, 5) == 5.0  # below the minimum: the operator asked for it
    assert conclude_threshold(1000, 5000) == 5000.0  # above the maximum
    assert conclude_threshold(1000, 0) == 0.0
    assert conclude_threshold(1000, -30) == 0.0  # never negative


def test_completion_state_reports_remaining_time_and_threshold():
    c = completion_state(deadline_unix=1000.0, now=400.25, threshold=100)
    assert c == {"deadline_unix": 1000.0, "remaining_seconds": 599.75, "conclude_threshold_seconds": 100.0,
                 "conclude_now": False}


def test_completion_state_conclude_now_boundary():
    assert completion_state(1000.0, 899.0, 100.0)["conclude_now"] is False  # 101 s left
    assert completion_state(1000.0, 899.999, 100.0)["conclude_now"] is False
    assert completion_state(1000.0, 900.0, 100.0)["conclude_now"] is True  # exactly at the threshold
    assert completion_state(1000.0, 900.001, 100.0)["conclude_now"] is True
    assert completion_state(1000.0, 1000.0, 0.0)["conclude_now"] is True


def test_completion_state_after_the_deadline_has_negative_remaining_and_concludes():
    c = completion_state(1000.0, 1010.0, 100.0)
    assert c["remaining_seconds"] == -10.0 and c["conclude_now"] is True


def test_turn_seconds_stops_a_non_concluding_turn_at_the_reserve():
    c = completion_state(deadline_unix=1000.0, now=0.0, threshold=100.0)
    assert turn_seconds(configured=2000, completion=c) == 900  # 1000 left minus the 100 s reserve
    assert turn_seconds(configured=300, completion=c) == 300  # the configured limit when it is tighter


def test_turn_seconds_floors_a_non_concluding_turn_at_the_minimum():
    c = completion_state(deadline_unix=1000.0, now=895.0, threshold=100.0)  # 105 left, 5 usable
    assert c["conclude_now"] is False
    assert turn_seconds(configured=600, completion=c) == MIN_TURN_SECONDS
    assert turn_seconds(configured=3, completion=completion_state(1000.0, 0.0, 100.0)) == MIN_TURN_SECONDS


def test_turn_seconds_truncates_fractional_usable_time():
    c = completion_state(deadline_unix=1000.0, now=0.5, threshold=100.0)  # 999.5 left, 899.5 usable
    assert turn_seconds(configured=2000, completion=c) == 899


def test_concluding_turn_may_use_all_that_is_left():
    c = completion_state(deadline_unix=1000.0, now=950.0, threshold=100.0)  # 50 s left, in the reserve
    assert c["conclude_now"] is True
    assert turn_seconds(configured=600, completion=c) == 50
    assert turn_seconds(configured=20, completion=c) == 20


def test_concluding_turn_is_floored_even_past_the_deadline():
    assert turn_seconds(600, completion_state(1000.0, 995.0, 100.0)) == MIN_TURN_SECONDS
    assert turn_seconds(600, completion_state(1000.0, 1500.0, 100.0)) == MIN_TURN_SECONDS
