"""HTML report rendered from a fake read-only client (fixture-only data, no lab)."""

from __future__ import annotations

import re

import pytest

from eidos.client import LabError
from eidos.reporting.html import render_report, write_report

XSS = "<script>alert('x')</script>"
IMG = '"><img src=x onerror=alert(1)>'


class ReadOnlyFake:
    """Has no .command(): any attempt to change state fails the test with AttributeError."""

    def __init__(self, fail: set[str] | None = None):
        self.fail = fail or set()
        self.seen: list[str] = []

    def health(self):
        return {"status": "ok", "api": "eidos-api/1"}

    def query(self, name, params=None, *, project=None):
        params = params or {}
        self.seen.append(name)
        if name in self.fail:
            raise LabError(403, {"code": "FORBIDDEN", "message": "no"})
        return getattr(self, "q_" + name)(params)

    # -- data ---------------------------------------------------------------------------------
    def q_get_investigation(self, p):
        return {"kind": "investigation", "id": p["investigation_id"], "revision": 4, "head_revision": 4,
                "status": "active", "created_by": "alice", "created_at": "2026-01-01T00:00:00Z",
                "body": {"question": "Does X help? " + XSS, "environment": "default", "model_artifact_id": "art_m",
                         "charter_ref": {"id": "charter:s1", "revision": 2, "digest": "sha256:aa"},
                         "investigator": {"runtime": "fixture", "model": None},
                         "steering": [{"by": "alice", "instruction": "try lr " + IMG}]},
                "turns": [{"id": "turn_1", "seq": 1, "status": "completed", "horizon": 9, "stop_reason": "waiting"},
                          {"id": "turn_2", "seq": 2, "status": "completed", "horizon": 20, "stop_reason": "budget-exhausted"}],
                "jobs": [{"id": "job_a", "status": "succeeded"}, {"id": "job_b", "status": "failed"},
                         {"id": "job_c", "status": "succeeded"}],
                "budgets": {"exploration": {"compute_seconds": {"limit_amount": 100.0, "used": 60.0, "reserved": 10.0,
                                                                "uncertain": 15.0}},
                            "confirmation": {"trials": {"limit_amount": 4.0, "used": 1.0, "reserved": 0.0,
                                                        "uncertain": 0.0}}}}

    def q_get_turn(self, p):
        return {"id": p["turn_id"], "result": {"stop_reason": "waiting", "summary": "summary " + XSS},
                "decisions": [
                    {"slot": 0, "accepted": True, "body": {"rationale": "because " + XSS, "calls": [
                        {"tool": "run_component"}, {"tool": "record_note"}], "usage": {
                            "input_tokens": 10, "output_tokens": 5, "fixture": True}, "prompt_revision": "p1"}},
                    {"slot": 0, "accepted": False, "body": {"reason": "stale-revision", "proposal": {}}}]}

    def q_get_job(self, p):
        jobs = {
            "job_a": {"id": "job_a", "status": "succeeded", "purpose": "open-search", "trial_id": "trial_a",
                      "lock": {"component": {"ref": "train@1.0.0"}, "seed": 7, "isolation_profile": "dev:same-user"},
                      "terminal": {"receipt": {"metrics": {"loss": 0.5, "acc": 0.7}, "steps": 100, "device": "cpu"}}},
            "job_b": {"id": "job_b", "status": "failed", "purpose": "open-search", "trial_id": "trial_b",
                      "lock": {"component": {"ref": "train@1.0.0"}},
                      "terminal": {"error": {"code": "OOM", "message": "out of memory " + XSS}}},
            "job_c": {"id": "job_c", "status": "succeeded", "purpose": "confirmation", "trial_id": "trial_c",
                      "lock": {"component": {"ref": "train@1.0.0"}},
                      "terminal": {"receipt": {"metrics": {"loss": 0.4, "acc": 0.75}}}}}
        return jobs[p["job_id"]]

    def q_list_records(self, p):
        k = p["kind"]
        if k == "branch":
            return [{"id": "branch:trial_b", "body": {"trial_id": "trial_b", "restore_class": "exact",
                                                      "stream_policy": "paired", "independence_unit": "traj1",
                                                      "intervention": {"kind": "change-learning"},
                                                      "parent": {"trial_id": "trial_a", "checkpoint_id": "art_ck",
                                                                 "step": 50}}},
                    {"id": "branch:trial_c", "body": {"trial_id": "trial_c", "restore_class": "warm-start",
                                                      "stream_policy": "independent", "independence_unit": "traj1",
                                                      "intervention": {"kind": "none"},
                                                      "parent": {"trial_id": "trial_a", "checkpoint_id": "art_ck",
                                                                 "step": 50}}}]
        if k == "claim":
            return [{"id": f"claim_{i}", "status": "assessed", "body": {
                "statement": f"claim {i} {XSS}", "kind": "improves", "investigation_id": "inv1",
                "scope": {"model": "m"}}} for i in (1, 2, 3)] + [
                {"id": "claim_other", "status": "proposed", "body": {"statement": "other inv", "kind": "improves",
                                                                      "investigation_id": "inv-zzz", "scope": {"m": 1}}},
                {"id": "claim_4", "status": "proposed", "body": {"statement": "unassessed", "kind": "decodable",
                                                                  "investigation_id": "inv1", "scope": {"m": 1}}}]
        if k == "evidence_plan":
            return [{"id": "plan_1", "status": "assessed", "revision": 3, "digest": "sha256:pp", "body": {
                "investigation_id": "inv1", "claim_ref": {"id": "claim_1"}, "template": {"ref": "tmpl@1"},
                "assessor": {"ref": "assess@1"}, "arms": [{"id": "treat"}, {"id": "control"}]}}]
        if k == "export":
            return [{"id": "export_1", "status": "active", "body": {"checkpoint_artifact_id": "art_ck", "builder": "b:f",
                                                                       "behavior_check": {"job_id": "job_c"}, "claims": ["claim_1"]}}]
        if k == "withdrawal":
            return [{"id": "withdrawal:r:1", "body": {"release_ref": "r@1", "severity": "scientific",
                                                      "defect": "bad metric " + XSS, "affected": ["assess_2"]}}]
        if k == "component_release":
            return [{"id": "train@1.0.0", "status": "withdrawn", "digest": "sha256:cc", "labels": {"kind": "train"}}]
        if k == "investigation":
            return [{"id": "inv1"}]
        return []

    def q_get_record(self, p):
        return {"id": p["id"], "status": "superseded", "body": {
            "question": "Charter question", "exploration_only": False, "known_exposures": ["dev set seen " + XSS],
            "success_rules": [{"id": "r1", "metric": "acc", "min_effect": 0.02,
                               "decision_standard": {"kind": "paired-difference-ci"}}]}}

    def q_record_history(self, p):
        return [{"revision": 1, "created_by": "alice", "created_at": "t1",
                 "body": {"committed_before_exploration": True}},
                {"revision": 2, "created_by": "bob", "created_at": "t2", "body": {
                    "exposure_informed_lineage": True, "amendment": {
                        "rationale": "lower threshold after peeking " + XSS, "exposure_informed": True,
                        "changes_success_rule": True, "changes": {"success_rules": {}}}}}]

    def q_get_claim(self, p):
        a = {
            "claim_1": [{"id": "as_1", "outcome": "protocol-passed", "conformance": "conformant", "health": "affected",
                         "plan_id": "plan_1"}],
            "claim_2": [{"id": "as_2", "outcome": "protocol-failed", "conformance": "conformant", "health": "intact",
                         "plan_id": "plan_2"}],
            "claim_3": [{"id": "as_3", "outcome": "inconclusive", "conformance": "incomplete", "health": "intact",
                         "plan_id": "plan_3"}],
            "claim_4": []}[p["claim_id"]]
        return {"id": p["claim_id"], "assessments": a, "support": {"status": "unsupported-pending-reassessment"
                                                                    if p["claim_id"] == "claim_1" else "supported"}}

    def q_get_assessment(self, p):
        return {"id": p["assessment_id"], "body": {"findings": [{"msg": "finding " + XSS}],
                                                    "evaluation": {"estimate": 0.03}}}

    def q_list_events(self, p):
        if p.get("cursor"):
            return {"events": [], "next_cursor": p["cursor"], "horizon": 42, "more": False}
        return {"events": [{"seq": i, "type": "job.finished", "subject": f"job_{i}", "created_at": "t"}
                           for i in range(1, 6)], "next_cursor": 5, "horizon": 42, "more": False}

    def q_budgets(self, p):
        return {"scope": "project:p", "budgets": {"trials": {"limit_amount": 10, "used": 2, "reserved": 0,
                                                             "uncertain": 0}}}

    def q_project_summary(self, p):
        return {"horizon": 42}


@pytest.fixture
def page():
    return render_report(ReadOnlyFake(), "proj", "inv1")


def test_key_sections_present(page):
    for needle in ("Question:", "Charter", "Revision and amendment history", "Success rules", "Turns and decisions",
                   "stop reason: <b>budget-exhausted</b>", "Jobs", "Checkpoint lineage", "Claims and assessments",
                   "Evidence plans", "Budgets", "Exports", "Withdrawals", "Provenance"):
        assert needle in page, needle
    assert "<svg" in page and page.count("<svg") >= 4  # budgets, lineage, metric plots
    assert "exposure-informed" in page and "changes success rule" in page
    assert "fixture investigator: not real LLM behaviour" in page and "fixture provider" in page
    assert "unapplied: stale-revision" in page
    assert "OOM" in page and "loss=0.5" in page and "seed=7" in page and "steps=100" in page
    assert "trial_a" in page and "independence unit" in page and "not independent replications" in page
    assert "uncertain (possible spend)" in page
    assert "eidos-api/1" in page and "cursor 5 at horizon 42" in page and "cannot change scientific" in page
    assert "other inv" not in page  # claims of other investigations are not mixed in


def test_protocol_outcome_and_health_side_by_side_and_negatives_kept(page):
    # claim_1: protocol passed but evidence affected - both visible in the same card
    card = re.search(r'<div class="card">.*?assessment <code>as_1</code>', page, re.S).group(0)
    assert "protocol-passed" in card and "Evidence health" in card and ">affected<" in card
    neg = page.split("Negative, failed and inconclusive results")[1].split("</table>")[0]
    assert "protocol-failed" in neg and "inconclusive" in neg and "unassessed" in neg
    assert "protocol-passed" not in neg
    assert "unsupported-pending-reassessment" in page


def test_ledger_text_is_escaped_and_page_is_self_contained(page):
    assert "<script" not in page.lower()
    assert "<img" not in page.lower()
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert "&gt;&lt;img src=x onerror=alert(1)&gt;" in page
    assert "onerror=alert(1)>" not in page
    stripped = page.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert not re.search(r'(src|href)\s*=\s*["\']?(https?:)?//', stripped)
    assert "@import" not in page and "url(" not in page


def test_unreadable_views_are_reported_not_hidden():
    fake = ReadOnlyFake(fail={"get_claim", "get_turn"})
    page = render_report(fake, "proj", "inv1")
    assert "Data gaps" in page and "get_claim" in page and "FORBIDDEN" in page
    assert "turn detail unavailable" in page


def test_only_read_queries_are_issued():
    fake = ReadOnlyFake()
    render_report(fake, "proj", "inv1")
    assert set(fake.seen) <= {"get_investigation", "get_turn", "get_job", "list_records", "record_history",
                              "get_record", "get_claim", "get_assessment", "list_events", "budgets",
                              "project_summary"}


def test_write_report_returns_single_file(tmp_path):
    path = write_report(ReadOnlyFake(), "proj", "inv1", tmp_path / "out")
    assert path.parent == tmp_path / "out" and path.suffix == ".html"
    assert path.read_text().startswith("<!doctype html>")
    assert len(list((tmp_path / "out").iterdir())) == 1


def test_whole_project_report_when_no_investigation():
    page = render_report(ReadOnlyFake(), "proj", None)
    assert "Investigation <code>inv1</code>" in page


def test_needs_input_mark_and_turn_failure_are_shown_escaped():
    """ADR 0019: a failed turn's failure and the investigation's needs-input mark (with its code) are visible."""

    class Failed(ReadOnlyFake):
        def q_get_investigation(self, p):
            inv = super().q_get_investigation(p)
            inv["needs_input"] = {"code": "turn-failed", "since": "2026-11-11T04:07:13+00:00",
                                  "reason": "turn failed (non-retryable error): MODEL_CALL_FAILED " + XSS,
                                  "details": {"retryable": False}}
            return inv

        def q_get_turn(self, p):
            turn = super().q_get_turn(p)
            turn["result"] = {"stop_reason": "error", "failure": {
                "error_type": "MODEL_CALL_FAILED", "retryable": False, "message": "401 " + IMG}}
            return turn

    page = render_report(Failed(), "proj", "inv1")
    assert "Needs operator input</b> (turn-failed, since 2026-11-11T04:07:13+00:00)" in page
    assert "failed: MODEL_CALL_FAILED (not retryable): 401" in page
    assert "<script" not in page.lower() and "<img" not in page.lower()
    assert "Needs operator input" not in render_report(ReadOnlyFake(), "proj", "inv1")
