"""Lab-environment components: scorer, data generator, plan template (author tests)."""

import csv
import io
import json
import random
import sys

import pytest

from conftest import SCORER, SWITCH
from eidos.domain.charter import reject_threshold_overrides


def _import_summarize():
    sys.path.insert(0, str(SWITCH))
    try:
        import pair_report

        return pair_report.summarize
    finally:
        sys.path.remove(str(SWITCH))


def _rows(n, seed):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        rows.append(
            {
                "pair_id": i,
                "reference_logit": rng.gauss(0, 2),
                "variant_logit": rng.gauss(0, 2),
                "reference_label": rng.randrange(2),
                "variant_label": rng.randrange(2),
                "stratum": rng.choice(["adjacent", "2-3 apart", "4+ apart"]),
            }
        )
    return rows


def _csv(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def test_scorer_matches_the_handoff_reporter(lab_run):
    rows = _rows(500, 3)
    res = lab_run(*SCORER, inputs={"predictions": (_csv(rows).encode(), "text/csv")}, config={})
    assert res.ok
    expected = _import_summarize()(rows)
    report = res.json("report")
    assert report["overall"]["pairs"] == expected["overall"]["pairs"]
    assert report["overall"]["both_correct_rate"] == pytest.approx(expected["overall"]["both_correct_rate"])
    assert report["overall"]["reference_accuracy"] == pytest.approx(expected["overall"]["reference_accuracy"])
    for group, want in expected["by_stratum"].items():
        assert report["by_group"][group]["both_correct_rate"] == pytest.approx(want["both_correct_rate"])
    assert res.manifest.outputs[0].schema == "grouped-pair-counts/1"
    assert res.receipt["metrics"]["both_correct_rate"] == report["overall"]["both_correct_rate"]


def test_scorer_accepts_jsonl_and_rejects_invalid(lab_run):
    rows = _rows(10, 1)
    jsonl = "\n".join(json.dumps(r) for r in rows)
    assert lab_run(*SCORER, inputs={"predictions": (jsonl.encode(), "application/jsonl")}, config={}).ok
    dup = rows + [dict(rows[0])]
    bad = lab_run(*SCORER, inputs={"predictions": (_csv(dup).encode(), "text/csv")}, config={})
    assert bad.manifest.status == "failed" and bad.manifest.error["category"] == "contract"
    assert bad.slots() == ["log"]  # nothing is published from a failed run


def test_scorer_reused_unchanged_on_second_model_rows(lab_run):
    """Fixture rows in the image project's paired schema go through the same scorer."""
    rows = [
        {
            "pair_id": i,
            "reference_logit": 1.0,
            "variant_logit": 1.0 if i % 2 else -1.0,
            "reference_label": 1,
            "variant_label": 1,
            "stratum": "gain 0.7" if i % 2 else "gain 0.1",
            "cluster": "17",
        }
        for i in range(8)
    ]
    res = lab_run(*SCORER, inputs={"predictions": (_csv(rows).encode(), "text/csv")}, config={})
    assert res.json("report")["by_group"]["gain 0.7"]["both_correct_rate"] == 1.0
    assert res.json("report")["by_group"]["gain 0.1"]["both_correct_rate"] == 0.0


def test_balanced_data_requested_vs_realized(lab_run):
    run = lambda **cfg: lab_run("lab.switch.balanced-data", "switch_data.generate:run", config=cfg)  # noqa: E731
    rounded = run(examples=2000, batch_size=100, exception_rate=0.005, mode="rounded-per-batch", seed=2)
    assert rounded.receipt["requested_exceptions"] == 10.0 and rounded.receipt["realized_exceptions"] == 0
    bern = run(examples=20000, batch_size=100, exception_rate=0.005, mode="bernoulli", seed=2)
    assert bern.receipt["requested_exceptions"] == 100.0
    assert 60 <= bern.receipt["realized_exceptions"] <= 140 and bern.receipt["realized_exceptions"] != 100
    exact = run(examples=1000, batch_size=100, exception_rate=0.02, mode="rounded-per-batch", seed=2)
    assert (
        exact.receipt["realized_exceptions_per_batch"]["min"]
        == exact.receipt["realized_exceptions_per_batch"]["max"]
        == 2
    )
    rows = [json.loads(line) for line in exact.text("dataset").splitlines()]
    assert len(rows) == 1000 and sum(r["exception"] for r in rows) == 20
    assert all((r["recorder"] != r["target"]) == bool(r["exception"]) for r in rows)
    assert rows == [
        json.loads(line)
        for line in run(examples=1000, batch_size=100, exception_rate=0.02, mode="rounded-per-batch", seed=2)
        .text("dataset")
        .splitlines()
    ]


def _request(**over):
    req = {
        "claim": {"kind": "improves", "statement": "s", "scope": {"builder": "OrderedSwitchNet"}},
        "success_rule_id": "early-repair",
        "assessor": {"ref": "eidos.assessors.paired-ci@1.1.0"},
        "seeds": [1, 2, 3],
        "evaluation": {"split": "test", "data_seed": 9, "pairs": 100},
        "baseline": {},
        "candidate": {"post": [{"component": "eidos.pytorch.weight-edit", "config": {"edits": []}}]},
        "required_isolation": {"profile": "dev:same-user", "controls": []},
    }
    req.update(over)
    return req


def test_template_draft_compiles_against_a_charter(lab_run):
    from eidos.domain.charter import charter_digest, validate_charter
    from eidos.domain.evidence import compile_plan

    res = lab_run(
        "eidos.templates.improves-under-budget",
        "improves_template.expand:run",
        inputs={"request": _request()},
        config={},
    )
    draft = res.json("draft")
    reject_threshold_overrides("draft", draft)  # the server's own check
    assert draft["analysis_unit"] == "pair" and draft["cluster_by"] == "seed"
    assert [a["role"] for a in draft["arms"]] == ["baseline", "candidate"]
    assert draft["seed_policy"]["seeds"] == [1, 2, 3]
    charter = validate_charter(
        {
            "study_id": "s",
            "question": "q",
            "metrics": {"both_correct_rate": {"direction": "increase", "denominator": "pairs"}},
            "population": {"p": 1},
            "comparator": {"c": 1},
            "success_rules": [
                {
                    "id": "early-repair",
                    "metric": "both_correct_rate",
                    "min_effect": 0.05,
                    "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
                    "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                }
            ],
        }
    )
    plan = compile_plan(
        draft=draft,
        claim={"kind": "improves", "scope": {"x": 1}},
        claim_ref={"id": "c", "revision": 1},
        charter=charter,
        charter_ref={"id": "ch", "revision": 1, "digest": charter_digest(charter)},
        template_ref={"ref": "t"},
    )
    assert plan["success_rule"]["min_effect"] == 0.05 and plan["success_rule_source"] == "immutable-charter"
    assert plan["obligations"] == []


@pytest.mark.parametrize("key", ["min_effect", "threshold", "guardrails", "decision_standard"])
def test_template_refuses_success_bars_at_any_depth(lab_run, key):
    req = _request()
    req["candidate"]["post"][0]["config"][key] = 0.1
    res = lab_run(
        "eidos.templates.improves-under-budget",
        "improves_template.expand:run",
        inputs={"request": req},
        config={},
    )
    assert res.manifest.status == "failed" and res.manifest.error["category"] == "contract"


def test_template_leaves_open_obligations_instead_of_inventing(lab_run):
    res = lab_run(
        "eidos.templates.improves-under-budget",
        "improves_template.expand:run",
        inputs={"request": _request(seeds=None, evaluation=None, required_isolation=None)},
        config={},
    )
    draft = res.json("draft")
    assert draft["seed_policy"] is None and set(draft["obligations"]) == {
        "seed_policy",
        "evaluation split and size",
        "required_isolation",
    }
