"""Regenerate the assessor's fixtures and its control suite (author-written, not independent).

Run from the repository root with the lab interpreter:

    /workspace/Eidos/.venv/bin/python components/build_assessor_fixtures.py

The frozen plan body is produced by the lab's own ``compile_plan`` from a charter, so fixtures carry a
realistic plan. Literal controls are generated once with fixed seeds and embedded; the stochastic
controls embed only a generator spec and are expanded per repetition by the qualification runner.
"""

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "components" / "eidos.qualification-runner"))

from qualification_runner.generators import paired_rows  # noqa: E402

from eidos.domain.charter import charter_digest, validate_charter  # noqa: E402
from eidos.domain.evidence import compile_plan  # noqa: E402
from eidos.domain.qualification import validate_control_suite  # noqa: E402

ASSESSOR = ROOT / "components" / "eidos.assessors.paired-ci"
REF = "eidos.assessors.paired-ci@1.2.0"


def plan_body():
    charter = validate_charter(
        {
            "study_id": "order-study",
            "question": "Does silencing the recorder repair paired accuracy?",
            "metrics": {
                "both_correct_rate": {
                    "identity": "paired-report/1:both-correct-rate",
                    "direction": "increase",
                    "denominator": "pairs",
                },
                "reference_accuracy": {
                    "identity": "paired-report/1:reference-accuracy",
                    "direction": "increase",
                    "denominator": "pairs",
                },
            },
            "population": {"generator": "switch-generator/1 opposite-order pairs"},
            "comparator": {"arm_role": "baseline"},
            "success_rules": [
                {
                    "id": "early-repair",
                    "metric": "both_correct_rate",
                    "min_effect": 0.05,
                    "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
                    "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                    "guardrails": [{"metric": "reference_accuracy", "max_regression": 0.05}],
                }
            ],
        }
    )
    draft = {
        "success_rule_id": "early-repair",
        "assessor": {"ref": REF},
        "arms": [{"id": "baseline", "role": "baseline"}, {"id": "candidate", "role": "candidate"}],
        "analysis_unit": "pair",
        "cluster_by": "seed",
        "seed_policy": {"kind": "independent-training-seeds", "seeds": [41, 42, 43, 44]},
        "required_isolation": {"profile": "dev:same-user", "controls": []},
    }
    return compile_plan(
        draft=draft,
        claim={"kind": "improves", "scope": {"builder": "OrderedSwitchNet"}},
        claim_ref={"id": "claim:order-edit", "revision": 1},
        charter=charter,
        charter_ref={"id": "charter:order-study", "revision": 1, "digest": charter_digest(charter)},
        template_ref={"ref": "eidos.templates.improves-under-budget@1.0.0"},
    )


def accuracy_plan(seeds=(11, 12, 13), cluster_by="seed"):
    """A frozen plan for an ordinary multiclass accuracy rule (ADR 0018), compiled by the lab."""
    charter = validate_charter(
        {
            "study_id": "accuracy-study",
            "question": "Does the candidate recipe raise held-out multiclass accuracy by at least 0.02?",
            "metrics": {
                "accuracy": {
                    "identity": "classification-report/1:accuracy",
                    "direction": "increase",
                    "denominator": "held-out examples",
                },
            },
            "population": {"split": "held-out examples"},
            "comparator": {"arm_role": "baseline"},
            "success_rules": [
                {
                    "id": "practical-accuracy-gain",
                    "metric": "accuracy",
                    "min_effect": 0.02,
                    "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
                    "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
                    "guardrails": [],
                }
            ],
        }
    )
    draft = {
        "success_rule_id": "practical-accuracy-gain",
        "assessor": {"ref": REF},
        "arms": [{"id": "baseline", "role": "baseline"}, {"id": "candidate", "role": "candidate"}],
        "analysis_unit": "example",
        "cluster_by": cluster_by,
        "seed_policy": {"kind": "independent-training-seeds", "seeds": list(seeds)},
        "required_isolation": {"profile": "dev:same-user", "controls": []},
    }
    return compile_plan(
        draft=draft,
        claim={"kind": "improves", "scope": {"task": "multiclass classification"}},
        claim_ref={"id": "claim:accuracy", "revision": 1},
        charter=charter,
        charter_ref={"id": "charter:accuracy-study", "revision": 1, "digest": charter_digest(charter)},
        template_ref={"ref": "eidos.templates.improves-under-budget@1.2.0"},
    )


def classification_rows(seed, seeds, examples, baseline_rate, candidate_rate, classes=("cat", "dog", "bird")):
    """Rows as eidos.task.paired-classification-report emits them, with per-arm correctness rates."""
    rng = random.Random(seed)
    arms = {"baseline": [], "candidate": []}
    for s in seeds:
        for i in range(examples):
            label = rng.choice(classes)
            for role, rate in (("baseline", baseline_rate), ("candidate", candidate_rate)):
                wrong = [c for c in classes if c != label]
                pred = label if rng.random() < rate else rng.choice(wrong)
                arms[role].append({"example_id": str(i), "cluster": str(s), "role": role, "label": label,
                                   "prediction": pred})
    return {"arms": {role: {"role": role, "rows": rows} for role, rows in arms.items()}}


def write(path, obj, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent, separators=None if indent else (",", ":")) + "\n")


def literal(plan, **params):
    return {"plan": plan, **paired_rows(dict(seed=1, **params), 0)}


def main():
    plan = plan_body()
    small = dict(clusters=3, pairs=12, cluster_sd=0.0)
    # 1.2.0's two-way (seed x example) interval with a t critical value on few seeds is more conservative than
    # 1.1.0's seed-only normal interval: the positive controls are sized so that the planted gain and the
    # reference-accuracy guardrail (margin 0.05) are both decidable (ADR 0018, F3b).
    planted = literal(plan, baseline_rate=0.2, candidate_rate=0.9, clusters=3, pairs=200, cluster_sd=0.0)
    null = literal(plan, baseline_rate=0.3, candidate_rate=0.3, **small)
    null["arms"]["candidate"]["rows"] = json.loads(json.dumps(null["arms"]["baseline"]["rows"]))
    refused = literal(plan, baseline_rate=0.2, candidate_rate=0.9, **small)  # refusals need no decidable effect
    dup = json.loads(json.dumps(refused))
    dup["arms"]["candidate"]["rows"].append(dict(dup["arms"]["candidate"]["rows"][0]))
    unknown = json.loads(json.dumps(refused))
    unknown["arms"]["ghost"] = unknown["arms"].pop("candidate")

    def fx(name, evidence, expect):
        write(
            ASSESSOR / "fixtures" / f"{name}.json",
            {
                "name": name,
                "author_test": True,
                "inputs": {"evidence": {"json": evidence}},
                "config": {},
                "expect": expect,
            },
            indent=1,
        )

    fx(
        "planted-effect",
        planted,
        {"status": "succeeded", "receipt": {"outcome": "protocol-passed", "units": 600, "clusters": 3}},
    )
    fx("null-effect", null, {"status": "succeeded", "receipt": {"outcome": "protocol-failed", "units": 36}})
    fx("duplicate-unit", dup, {"status": "failed", "error_category": "contract"})
    fx("unknown-arm", unknown, {"status": "failed", "error_category": "contract"})

    acc_plan = accuracy_plan()
    acc_planted = {"plan": acc_plan, **classification_rows(3, (11, 12, 13), 300, 0.60, 0.80)}
    acc_null = {"plan": acc_plan, **classification_rows(4, (11, 12, 13), 300, 0.70, 0.70)}
    acc_null["arms"]["candidate"]["rows"] = [
        {**r, "role": "candidate"} for r in json.loads(json.dumps(acc_null["arms"]["baseline"]["rows"]))
    ]
    acc_mismatch = json.loads(json.dumps(acc_planted))
    acc_mismatch["arms"]["candidate"]["rows"][0]["example_id"] = "not-a-labelled-id"
    one_seed_plan = accuracy_plan(seeds=(11,), cluster_by=None)
    acc_one_seed = {"plan": one_seed_plan, **classification_rows(5, (11,), 2000, 0.60, 0.70)}
    acc_noisy = {"plan": acc_plan, **classification_rows(6, (11, 12, 13), 10, 0.60, 0.70)}
    fx(
        "accuracy-planted",
        acc_planted,
        {"status": "succeeded", "receipt": {"outcome": "protocol-passed", "units": 900, "clusters": 3}},
    )
    fx("accuracy-null", acc_null, {"status": "succeeded", "receipt": {"outcome": "protocol-failed", "units": 900}})
    fx("accuracy-id-mismatch", acc_mismatch, {"status": "failed", "error_category": "contract"})
    fx(
        "accuracy-one-seed",
        acc_one_seed,
        {"status": "succeeded", "receipt": {"outcome": "protocol-passed", "units": 2000, "clusters": 1}},
    )

    big = dict(clusters=4, pairs=15, cluster_sd=0.02)
    guard = dict(clusters=6, pairs=60, cluster_sd=0.02, baseline_rate=0.2, candidate_rate=0.6,
                 candidate_reference_rate=0.75)
    one_cluster = literal(plan, baseline_rate=0.2, candidate_rate=0.9, clusters=1, pairs=40, cluster_sd=0.0)

    def control(id_, type_, evidence, expected, **extra):
        return {
            "id": id_,
            "type": type_,
            "deterministic": True,
            "repetitions": 1,
            "expected_outcome": expected,
            "input": {"inputs": {"evidence": evidence}, "config": {}},
            **extra,
        }

    def stochastic(id_, type_, **params):
        return {
            "id": id_,
            "type": type_,
            "deterministic": False,
            "repetitions": 40,
            "input": {
                "inputs": {
                    "evidence": {
                        "plan": plan,
                        "$generate": {
                            "name": "paired-rows/1",
                            "params": dict(seed=7, clusters=8, pairs=120, cluster_sd=0.03, **params),
                        },
                    }
                },
                "config": {},
            },
        }

    suite = {
        "target": {"kind": "assessor", "ref": REF},
        "scope": ["improves"],
        "origin": {
            "authored_by": "component author (same developer as the assessor): an author suite, not independent",
            "committed_before_run": True,
        },
        "controls": [
            control(
                "planted-large-effect",
                "planted",
                literal(plan, baseline_rate=0.2, candidate_rate=0.8, **big),
                "protocol-passed",
            ),
            control("null-identical-arms", "null", null, "protocol-failed"),
            control(
                "null-candidate-worse",
                "null",
                literal(plan, baseline_rate=0.5, candidate_rate=0.3, **big),
                "protocol-failed",
            ),
            control("null-guardrail-violated", "null", literal(plan, **guard), "protocol-failed"),
            control("null-single-cluster-inconclusive", "null", one_cluster, "inconclusive"),
            control("accuracy-planted-gain", "planted", acc_planted, "protocol-passed"),
            control("accuracy-null-identical-arms", "null", acc_null, "protocol-failed"),
            control("accuracy-null-small-noisy-inconclusive", "null", acc_noisy, "inconclusive"),
            stochastic("planted-stochastic-effect", "planted", baseline_rate=0.30, candidate_rate=0.55),
            stochastic("null-stochastic-no-effect", "null", baseline_rate=0.35, candidate_rate=0.35),
        ],
        "acceptance": {"min_power": 0.85, "max_false_positive": 0.10, "confidence": 0.95},
    }
    validate_control_suite(suite)
    write(ASSESSOR / "controls" / "suite.json", suite)


if __name__ == "__main__":
    main()
