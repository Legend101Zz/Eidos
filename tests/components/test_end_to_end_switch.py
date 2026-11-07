"""Script-level reproduction of the design's switch numbers entirely through components.

OrderedSwitchNet, 45 steps, seed 41, evaluated on 4000 pairs drawn with data seed 76102:
both-correct 0.2625 unedited and 0.844 after zeroing the recorder weight (handoff results; exact on
CPU with torch 2.15, one thread, deterministic algorithms). Training, edit, evaluation and scoring
are each a separate component run; the assessor then applies a frozen plan to three seeds.
"""

import csv
import io
import json

import pytest

from conftest import (
    ADAPTER,
    BEHAVIOR,
    COMPONENTS,
    EVALUATE,
    SCORER,
    TRAIN,
    WEIGHT_EDIT,
    ckpt_input,
    train_config,
)

EVAL = {"adapter": ADAPTER, "evaluation": {"split": "test", "data_seed": 76102, "pairs": 4000}}
EDIT = {
    "edits": [{"entry": "readout.weight", "index": [0, -1], "op": "set", "value": 0.0}],
    "reason": "silence recorder",
}


def score(lab_run, predictions):
    return lab_run(*SCORER, inputs={"predictions": (predictions.path("predictions"), "text/csv")}, config={})


def test_design_switch_numbers_through_components(torch_run, lab_run):
    arms = {"baseline": [], "candidate": []}
    for seed in (41, 42, 43):
        parent = torch_run(*TRAIN, config=train_config("OrderedSwitchNet", 45, seed))
        assert parent.ok, parent.manifest.error
        edited = torch_run(*WEIGHT_EDIT, inputs=ckpt_input(parent), config=EDIT)
        for arm, ckpt in (("baseline", parent), ("candidate", edited)):
            rows = torch_run(*EVALUATE, inputs=ckpt_input(ckpt), config=EVAL)
            assert rows.ok, rows.manifest.error
            arms[arm].append(rows)
            if seed == 41:
                report = score(lab_run, rows).json("report")["overall"]
                if arm == "baseline":
                    assert report["pairs"] == 4000 and report["both_correct_rate"] == pytest.approx(0.2625)
                    assert report["reference_accuracy"] == pytest.approx(0.984)
                else:
                    assert report["both_correct_rate"] == pytest.approx(0.844)
                    assert report["reference_accuracy"] == pytest.approx(0.919)
    # the same rows through the assessor under a frozen plan (fixture plan from the lab's compile_plan)
    plan = json.loads((COMPONENTS / "eidos.assessors.paired-ci/fixtures/planted-effect.json").read_text())[
        "inputs"
    ]["evidence"]["json"]["plan"]
    evidence = {
        "plan": plan,
        "arms": {
            arm: {
                "role": arm,
                "rows": [r for res in results for r in csv.DictReader(io.StringIO(res.text("predictions")))],
            }
            for arm, results in arms.items()
        },
    }
    verdict = lab_run(
        "eidos.assessors.paired-ci", "paired_ci.assess:run", inputs={"evidence": evidence}, config={}
    )
    evaluation = verdict.json("evaluation")
    assert evaluation["clusters"] == ["41", "42", "43"] and evaluation["units_by_role"] == {
        "baseline": 12000,
        "candidate": 12000,
    }
    assert evaluation["effect"]["estimate"] > 0.4  # both-correct improves a lot...
    guard = evaluation["guardrails"][0]
    assert (
        guard["metric"] == "reference_accuracy" and guard["status"] == "violated"
    )  # ...but plain accuracy regressed
    assert verdict.receipt["outcome"] in ("protocol-failed", "inconclusive")


def test_wrong_builder_is_caught_on_the_switch_checkpoint(torch_run):
    parent = torch_run(*TRAIN, config=train_config("OrderedSwitchNet", 45, 41))
    res = torch_run(*BEHAVIOR, inputs=ckpt_input(parent), config={"adapter": ADAPTER, "builder": "SwitchNet"})
    assert res.receipt["behavior_match"] is False and res.receipt["strict_load_ok"] is True
