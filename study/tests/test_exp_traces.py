import json
from pathlib import Path

import pytest

from study.exp.traces import METRICS, from_records, measure


def test_exp_traces_absent_and_non_arm4(tmp_path):
    assert measure(tmp_path) == dict.fromkeys(METRICS)
    assert from_records([], {"arm": 2}) == dict.fromkeys(METRICS)


def test_exp_traces_fabricated_intervals_and_calls():
    rows = [
        {"type": "turn", "data": {"decisions": [
            {"id": "d1", "accepted": True, "body": {"calls": [
                {"tool": "author_source"}, {"tool": "prepare_candidate"}]}},
            {"id": "d2", "accepted": False, "body": {"calls": [{"tool": "author_source"}]}}
        ]}},
        {"type": "note", "data": {"id": "n", "created_at": 15, "body": {"kind": "candidate_lock"}}},
        {"type": "ledger_event", "data": {"type": "model.usage", "body": {"usage": {"calls": 3}}}},
        {"type": "ledger_event", "data": {"type": "investigation.concluded", "created_at": 30}},
        *[{"type": "job", "data": {"id": str(i), "component": c, "started_at": a, "ended_at": b}}
          for i, (a, b, c) in enumerate([(10, 20, "other"), (15, 25, "eidos.search.recipe-portfolio@1"), (27, 29, "other")])],
    ]
    metrics = from_records(rows, {"arm": 4, "started_at": 10, "deadline_at": 50})
    assert metrics == dict(decisions=2, requests=3, time_to_first_lock_s=5, locks=1,
                          authoring_calls=1, prepare_candidate_calls=1, portfolio_jobs=1,
                          jobs=3, job_seconds=17, refused_decisions=1, unused_wall_s=20)


@pytest.mark.parametrize("name", ["N01-r1", "N01-r2", "N03-r1", "N04-r2"])
def test_exp_traces_committed_real_replays(tmp_path, name):
    fixture = json.loads((Path(__file__).parents[1] / "evals/fixtures" / (name + ".json")).read_text())
    # Reconstitute just the records this fixture actually preserves. No lab
    # paths or artifact blobs in run_record are followed.
    rows = ([{"type": "turn", "data": t} for t in fixture["turns"]]
            + [{"type": "ledger_event", "data": e} for e in fixture["receipts"]]
            + [{"type": "investigation", "data": fixture["final_state"]}])
    (tmp_path / "run_record.json").write_text(json.dumps(fixture["run_record"]))
    (tmp_path / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    metrics = measure(tmp_path)
    decisions = [d for t in fixture["turns"] for d in t["decisions"]]
    assert metrics["decisions"] == len({d["id"] for d in decisions})
    assert metrics["requests"] == sum(e["body"]["usage"]["calls"] for e in fixture["receipts"] if e["type"] == "model.usage")
    assert metrics["jobs"] == len(fixture["final_state"]["jobs"])
    assert metrics["job_seconds"] is None
    assert metrics["locks"] is None  # fixture receipts do not preserve note records


def test_exp_traces_missing_timestamps_are_unknown():
    metrics = from_records([{"type": "job", "data": {"id": "j", "component": "other"}}], {"arm": 4})
    assert metrics["jobs"] == 1
    assert metrics["job_seconds"] is None
    assert metrics["requests"] is None


def test_exp_traces_prepare_macro_expansion_is_one_call():
    stamp = dict(tool="prepare_candidate", proposed_by="decision1", basis="one-se-choice")
    rows = [{"type": "turn", "data": {"decisions": [{"id": "d", "accepted": True, "body": {
        "calls": [{"tool": "run_component", "macro": stamp} for _ in range(4)]}}]}}]
    assert from_records(rows, {"arm": 4})["prepare_candidate_calls"] == 1


def test_exp_traces_exported_job_attempts_and_response_messages():
    rows = [
        {"type": "job", "data": {"id": "j", "lock": {"component": {"ref": "eidos.search.recipe-portfolio@1"}},
                                 "attempts": [{"started_at": 1, "ended_at": 5}, {"started_at": 4, "ended_at": 7}]}},
        {"type": "model_response", "data": {"artifact_id": "a", "response": [
            {"kind": "request"}, {"kind": "response", "provider_response_id": "r1"},
            {"kind": "response", "provider_response_id": "r2"}]}},
    ]
    metrics = from_records(rows, {"arm": 4})
    assert metrics["portfolio_jobs"] == 1
    assert metrics["job_seconds"] == 6
    assert metrics["requests"] == 2


def test_exp_traces_first_of_multiple_locks():
    rows = [{"type": "note", "data": {"id": str(t), "created_at": t,
                                     "body": {"kind": "candidate_lock"}}} for t in (20, 12, 17)]
    assert from_records(rows, {"arm": 4, "started_at": 10})["time_to_first_lock_s"] == 2
