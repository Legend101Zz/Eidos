"""Fixture-shaped authorized views; no lab or model."""
import copy
import json

import pytest

from eidos.domain.promotion import suggested_lock
from eidos.investigator.autopilot import COMMAND, autopilot
from eidos.investigator.runtime import DecisionRequest, build_runtime


def request(prepared=False, kind="classification"):
    view = dict(investigation=dict(body=dict(task=dict(task_type=kind), inputs=dict(train="t", validation="v"),
        investigator=dict(context_policy="context-policy/4", exploration=dict(policy="exploration-policy/3",
                          project_command_ref=COMMAND)))), jobs=[], pinned_notes=[])
    if prepared:
        metric = "validation_accuracy" if kind == "classification" else "validation_rmse"
        stamp = dict(tool="prepare_candidate", proposed_by="decision", basis="one-se-choice")
        train = dict(component=COMMAND, status="succeeded", purpose="open-search", inputs=dict(source="s", train="t", validation="v"),
                     config=dict(argv=["python", "project.py", "train"], seed=11), outputs=[],
                     receipt=dict(metrics={metric: .8, metric + "_se": .02}), macro=stamp)
        view["jobs"] = [
            dict(train, id="job_train"),
            dict(train, id="job_check", config=dict(argv=["check"]), receipt=dict(metrics=dict(readiness_max_abs_diff=0))),
            dict(train, id="job_compare", config=dict(argv=["compare"]),
                 receipt=dict(metrics=dict(paired_delta=.2, paired_delta_lo=.1, paired_delta_hi=.3, paired_n=100))),
            dict(copy.deepcopy(train), id="job_second", purpose="reproduction", config=dict(argv=["python", "project.py", "train"], seed=23)),
        ]
    return DecisionRequest("turn", 0, "not a source of facts", [], {}, view=view)


def test_no_baseline_reproduces_supplied_project_without_authoring():
    decision = autopilot(request())
    (call,) = decision["calls"]
    assert call["tool"] == "run_component" and call["args"]["component_ref"] == COMMAND
    assert "source" not in call["args"]["inputs"]
    assert call["args"]["config"]["argv"] == ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", "{seed}"]


def test_after_baseline_batches_portfolio_and_prepare_with_default_budget():
    req = request()
    req.view.update(baseline_record=dict(job_id="b"), jobs=[dict(id="b", status="succeeded", outputs=[dict(slot="validation_predictions")])])
    calls = autopilot(req)["calls"]
    assert [c["tool"] for c in calls] == ["run_component", "prepare_candidate"]
    assert calls[0]["args"]["config"] == dict(mode="portfolio", task_type="classification", include="auto", scheme="auto")
    assert calls[1]["args"]["source"] == {"$ref": "call[0].outputs.promotion_source"}


@pytest.mark.parametrize("kind", ["classification", "regression"])
def test_supporting_candidate_copies_lock_then_concludes_with_positive_estimate(kind):
    req = request(True, kind)
    before = copy.deepcopy(req)
    decision = autopilot(req)
    assert req == before  # pure; no state stored in the request
    fact = suggested_lock(req.view)
    assert decision["calls"][0]["args"]["text"] == json.dumps(fact["lock"], sort_keys=True)
    req.view["pinned_notes"] = [dict(kind="candidate_lock", text=decision["calls"][0]["args"]["text"])]
    final = autopilot(req)
    assert final["calls"] == []
    assert final["control"]["claim"]["disposition"] == "candidate"
    assert final["control"]["claim"]["point_estimate"]["value"] == .2
    assert final["control"]["scope"]["answer"]["measurements"]
    assert "confirmed" not in json.dumps(final) and "proved" not in json.dumps(final)


@pytest.mark.parametrize("change", ["noise", "bar", "readiness", "second_seed", "nan"])
def test_prepared_without_support_never_locks(change):
    req = request(True)
    if change == "noise":
        req.view["jobs"][2]["receipt"]["metrics"]["paired_delta_lo"] = -.1
    elif change == "bar":
        req.view["investigation"]["body"]["task"]["practical_bar"] = .25
    elif change == "readiness":
        req.view["jobs"][1]["receipt"]["metrics"] = {}
    elif change == "second_seed":
        req.view["jobs"][-1]["receipt"]["metrics"]["validation_accuracy"] = .1
    else:
        req.view["jobs"][2]["receipt"]["metrics"]["paired_delta"] = float("nan")
    decision = autopilot(req)
    assert not decision["calls"] and decision["control"]["claim"]["disposition"] in ("null", "abstain")


@pytest.mark.parametrize("change", ["refused", "failed_receipt", "failed_job", "conclude_only", "wrong_context", "wrong_exploration", "missing_task", "missing_baseline_predictions"])
def test_failure_and_unsupported_requests_abstain(change):
    req = request()
    if change == "refused":
        req.turn_so_far = [dict(rejected=True, error=dict(message="refusal"))]
    elif change == "failed_receipt":
        req.turn_so_far = [dict(receipts=[dict(ok=False)])]
    elif change == "failed_job":
        req.view["jobs"] = [dict(status="failed")]
    elif change == "conclude_only":
        req.conclude_only = True
    elif change.startswith("wrong_"):
        cfg = req.view["investigation"]["body"]["investigator"]
        if change == "wrong_context":
            cfg["context_policy"] = "context-policy/3"
        else:
            cfg["exploration"]["policy"] = "exploration-policy/2"
    elif change == "missing_task":
        req.view["investigation"]["body"]["task"] = {}
    else:
        req.view.update(baseline_record=dict(job_id="b"), jobs=[dict(id="b", status="succeeded", outputs=[])])
    decision = autopilot(req)
    assert not decision["calls"] and decision["control"]["claim"]["disposition"] == "abstain"


def test_pending_work_waits_at_most_once():
    req = request()
    req.view["jobs"] = [dict(status="queued")]
    assert autopilot(req)["control"]["action"] == "wait_for_jobs"
    req.turn_so_far = [dict(calls=[], control=autopilot(req)["control"])]
    assert autopilot(req)["control"]["claim"]["disposition"] == "abstain"


def test_runtime_labels_all_results_fixture():
    result = build_runtime(dict(policy="autopilot-v2")).decide(request())
    assert result.usage["fixture"] and result.usage["usd"] == 0
    assert result.prompt_revision == "fixture:autopilot-v2"


def test_successful_lock_receipt_within_fixed_view_horizon_concludes_without_relocking():
    req = request(True)
    lock = autopilot(req)["calls"][0]
    req.turn_so_far = [dict(calls=[lock], receipts=[dict(ok=True, index=0, result=dict(note_id="note"))])]
    result = autopilot(req)
    assert result["calls"] == []
    assert result["control"]["claim"]["disposition"] == "candidate"


def test_structured_incumbent_takes_precedence_over_note_text():
    req = request(True)
    req.view["exploration"] = dict(incumbent=dict(job_id="job_train"))
    req.view["pinned_notes"] = [dict(kind="candidate_lock", text="not a source of facts")]
    assert autopilot(req)["control"]["claim"]["disposition"] == "candidate"
