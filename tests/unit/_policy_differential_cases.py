"""Public fixed inputs ported from the independent review's main differential probes."""

import json
import random

from eidos.domain.assessment import candidate_lock_job_id

SUCCESS_RULES = [{"id": "rule-acc", "metric": "accuracy", "min_delta": 0.05}]


def job(
    i,
    status="succeeded",
    purpose="open-search",
    component="c@1",
    receipt=None,
    config=None,
    inputs=None,
    outputs=None,
    error=None,
):
    return {
        "id": f"job_{i:04x}",
        "component": component,
        "environment": "default",
        "status": status,
        "purpose": purpose,
        "inputs": inputs or {},
        "config": config or {},
        "receipt": receipt,
        "error": error,
        "outputs": outputs or [],
    }


def view(jobs, **over):
    v = {
        "investigation": {
            "id": "inv1",
            "status": "active",
            "body": {"question": "Does X improve Y?", "investigator": {}},
        },
        "horizon": 10,
        "jobs": list(jobs),
        "budgets": {},
        "environments": ["default"],
        "environment_details": {},
        "charter": {
            "study_id": "s1",
            "exploration_only": False,
            "population": "p",
            "comparator": "c",
            "metrics": {},
            "success_rules": SUCCESS_RULES,
            "resource_limits": {},
        },
    }
    v.update(over)
    return v


catalog = [
    {"ref": f"comp-{i:02d}@1.0.0", "implements": "thing/1", "kind": "component", "summary": "s" * 60}
    for i in range(12)
]
hyps = [{"hypothesis": "h1", "prediction": "p", "falsifier": "f"}]
diag_rec = {"top_hypothesis": "h1", "hypotheses": hyps, "metrics": {"n": 1}}
jobs = [
    job(0, purpose="reproduction", receipt={"metrics": {"accuracy": 0.61}}),
    job(
        1,
        component="eidos.diagnose.baseline-errors@1.0.0",
        receipt=diag_rec,
        outputs=[{"slot": "report", "artifact_id": "art_1", "role": "output", "schema": "report/1"}],
    ),
    job(
        2,
        component="eidos.search.recipe-sweep@1.1.0",
        receipt={"metrics": {"best_validation_accuracy": 0.7}},
        config={"template": {"a": 1}},
    ),
    job(
        3,
        status="failed",
        error={"category": "execution", "message": "ModuleNotFoundError: No module named 'numpy'"},
    ),
    job(4, purpose="hypothesis-test", receipt={"metrics": {"validation_accuracy": 0.66}}),
]
notes = [
    {"id": "note_lock1", "kind": "candidate_lock", "text": json.dumps({"job_id": "job_0004"})},
    {"id": "note_base", "kind": "baseline", "text": "baseline job_0000", "evidence_refs": ["job_0000"]},
]
expl = {
    "policy": "exploration-policy/2",
    "phase": "explore",
    "rounds": 1,
    "rounds_without_improvement": 1,
    "stop": None,
    "rule": {"metric": "validation_accuracy", "split": "validation", "direction": "higher_is_better"},
    "incumbent": {"job_id": "job_0004", "evidence": {"value": 0.66}},
    "pending_challengers": [],
    "allowance": {
        "seconds_left": 900,
        "tokens_left": 500000,
        "holdback_seconds": 120.0,
        "holdback_tokens": 1000,
    },
    "versions": [],
    "frozen": False,
    "patience_rounds": 3,
}
views = {
    "plain": view(jobs, pinned_notes=notes, exploration=expl),
    "no-expl": view(jobs[:2]),
    "empty": view([]),
    "finalization": view(
        jobs,
        pinned_notes=notes,
        exploration=expl,
        finalization={
            "locked_candidate": {"job_id": "job_0004", "status": "succeeded"},
            "turn_id": "t1",
            "reads": 0,
        },
    ),
    "tight": view(jobs * 8, pinned_notes=notes, exploration=expl),
}
SCORER = "eidos.project.score"
DATA = {"config": {"split": "validation"}}
RULE = {
    "metric": "accuracy",
    "split": "validation",
    "direction": "higher_is_better",
    "scorer": SCORER,
    "data": DATA,
}
LOWER = {
    "metric": "mse",
    "split": "validation",
    "direction": "lower_is_better",
    "scorer": SCORER,
    "data": DATA,
}


def job(
    jid,
    component="eidos.project.command@1",
    purpose="open-search",
    status="succeeded",
    config=None,
    inputs=None,
    outputs=None,
    metrics=None,
):
    outs = {"model": f"art_{jid}"} if outputs is None else outputs
    return {
        "id": jid,
        "component": component,
        "environment": "lab",
        "purpose": purpose,
        "status": status,
        "config": {"seed": 1, "lr": 0.1} if config is None else config,
        "inputs": {"source": "art_x"} if inputs is None else inputs,
        "outputs": [
            {"slot": s, "artifact_id": a, "role": "output", "schema": "x/1"} for s, a in outs.items()
        ],
        "error": None,
        "receipt": {"metrics": metrics} if metrics is not None else None,
    }


def state_cases():
    rng = random.Random(1234)
    res = []
    for _n in range(400):
        rule = rng.choice([RULE, LOWER])
        metric = rule["metric"]
        jobs = []
        ids = []
        for i in range(rng.randint(2, 10)):
            jid = f"j{i}"
            ids.append(jid)
            kind = rng.choice(["scored", "train", "infer", "fail", "other", "portfolio", "diag", "unscored"])
            purpose = rng.choice(
                ["open-search", "hypothesis-test", "measurement-development", "reproduction", "confirmation"]
            )
            status = rng.choice(["succeeded"] * 4 + ["failed", "running", "pruned"])
            v = round(rng.random(), 2)
            if kind == "scored":
                jobs.append(
                    job(
                        jid,
                        component="eidos.project.score@1",
                        purpose=purpose,
                        status=status,
                        config={"seed": rng.randint(1, 3), "split": "validation"},
                        metrics={metric: v},
                    )
                )
            elif kind == "train":
                jobs.append(
                    job(jid, purpose=purpose, status=status, config={"seed": rng.randint(1, 3), "lr": 0.1})
                )
            elif kind == "infer" and i:
                src = rng.choice(ids[:-1])
                jobs.append(
                    job(
                        jid,
                        component="eidos.project.infer@1",
                        purpose=purpose,
                        status=status,
                        inputs={"model": f"art_{src}"},
                        outputs={"predictions": f"art_{jid}_p"},
                    )
                )
            elif kind == "fail":
                jobs.append(job(jid, purpose=purpose, status="failed"))
            elif kind == "portfolio":
                jobs.append(
                    job(
                        jid,
                        component="eidos.search.recipe-portfolio@1.0.0",
                        purpose=purpose,
                        status=status,
                        metrics={"round_evaluated": 1.0, "validation_chosen": v},
                    )
                )
            elif kind == "diag":
                jobs.append(
                    job(
                        jid,
                        component="eidos.diagnose.baseline-errors@1.1.0",
                        purpose=purpose,
                        status=status,
                        metrics={"n": 1},
                    )
                )
            else:
                jobs.append(job(jid, purpose=purpose, status=status, metrics={metric: v}))
        notes = []
        note_turns = {}
        if rng.random() < 0.8:
            notes.append({"id": "n_rule", "kind": "selection_rule", "text": json.dumps(rule)})
            note_turns["n_rule"] = 1
        for k in range(rng.randint(0, 3)):
            nid = f"n_lock{k}"
            target = rng.choice(ids)
            notes.append({"id": nid, "kind": "candidate_lock", "text": json.dumps({"job_id": target})})
            note_turns[nid] = rng.randint(1, 4)
        round_log = [
            {"turn": t, "jobs": rng.sample(ids, rng.randint(0, min(3, len(ids))))}
            for t in range(1, rng.randint(2, 9))
        ]
        cfg = rng.choice(
            [
                None,
                {},
                {"exploration": {"patience_rounds": 2}},
                {"exploration": {"max_rounds": 2, "patience_rounds": 5}},
            ]
        )
        kw = dict(
            notes=notes,
            jobs=jobs,
            round_log=round_log,
            note_turns=note_turns,
            lock_job=candidate_lock_job_id,
            completion=None,
            wall_seconds=rng.choice([None, 900.0, 40.0]),
            budgets=None,
            frozen=rng.random() < 0.1,
            config=cfg,
            baseline_job=rng.choice([None, "j0"]),
            held_out=None,
            decision_tokens=None,
        )
        res.append(kw)
    return res


def context_cases():
    return {f"{name}/{limit}": (v, catalog, limit) for name, v in views.items() for limit in (200000, 9000)}
