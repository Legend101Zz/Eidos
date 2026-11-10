"""Expand an "improves under a resource budget" request into an evidence-plan draft.

The draft names arms, per-seed run pipelines, a seed policy and the analysis unit. It never carries a
success bar: the minimum useful effect, guardrails and decision standard come from the pinned charter
rule named by ``success_rule_id`` when the server compiles the plan. A request that tries to supply
one is refused.
"""

from __future__ import annotations

from typing import Any

from eidos_worker.errors import ContractError

# The server rejects a draft containing any of these keys at any depth; so do we, on input and output.
FORBIDDEN_KEYS = (
    "min_effect",
    "threshold",
    "success_rule",
    "success_rules",
    "decision_standard",
    "guardrails",
    "max_regression",
    "metric_direction",
)
TRAIN = "eidos.pytorch.train"
EVALUATE = "eidos.pytorch.evaluate"
REPORT = "eidos.task.paired-classification-report@1.0.0"
ROLES = ("baseline", "candidate")


def find_forbidden(obj: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                found.append(f"{path}.{k}" if path else k)
            found.extend(find_forbidden(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(find_forbidden(v, f"{path}[{i}]"))
    return found


def _seeds(request: dict[str, Any]):
    spec = request.get("seeds")
    if isinstance(spec, list) and spec and all(isinstance(s, int) for s in spec):
        return list(spec)
    if isinstance(spec, dict) and isinstance(spec.get("count"), int) and spec["count"] > 0:
        start = int(spec.get("start", 0))
        return [start + i for i in range(spec["count"])]
    return None


def _arm_runs(
    arm_id: str, role: str, request: dict[str, Any], recipe: dict[str, Any], seeds, evaluation: dict[str, Any]
):
    parent = request.get("parent")
    runs = []
    for seed in seeds or []:
        prefix = f"{arm_id}.s{seed}"
        train_cfg = {**(request.get("common_train") or {}), **(recipe.get("train") or {}), "seed": seed}
        jobs: list[dict[str, Any]] = []
        if parent:
            # Branch from the parent checkpoint; the baseline is the no-change continuation.
            train_cfg.setdefault("branch", {"stream_policy": "paired", "intervention": {"kind": "none"}})
            jobs.append(
                {
                    "id": f"{prefix}.train",
                    "component": TRAIN,
                    "inputs": {"parent_checkpoint": parent["checkpoint_ref"]},
                    "config": train_cfg,
                }
            )
        else:
            jobs.append({"id": f"{prefix}.train", "component": TRAIN, "inputs": {}, "config": train_cfg})
        last = f"{prefix}.train"
        for i, post in enumerate(recipe.get("post") or []):
            job_id = f"{prefix}.post{i}"
            jobs.append(
                {
                    "id": job_id,
                    "component": post["component"],
                    "inputs": {"checkpoint": f"{last}.checkpoint"},
                    "config": post.get("config", {}),
                }
            )
            last = job_id
        jobs.append(
            {
                "id": f"{prefix}.evaluate",
                "component": EVALUATE,
                "inputs": {"checkpoint": f"{last}.checkpoint"},
                "config": {
                    **(request.get("common_evaluate") or {}),
                    "evaluation": {**evaluation, "cluster": str(seed)},
                },
            }
        )
        runs.append({"seed": seed, "cluster": str(seed), "jobs": jobs})
    return runs


def _seed_key(seed: Any) -> str:
    if isinstance(seed, bool) or not isinstance(seed, (int, str)) or str(seed).strip() == "":
        raise ContractError(f"classification.predictions: seed {seed!r} must be an integer or a non-empty string")
    return str(seed).strip()


def _classification_arms(request: dict[str, Any]):
    """Ordinary accuracy from existing held-out predictions (ADR 0018).

    ``classification = {labels: <artifact id>, predictions: {baseline: {seed: <artifact id>},
    candidate: {seed: <artifact id>}}, classes?, id_field?}``. Every training seed must have predictions
    in both arms: each seed is one cluster and the assessor pairs examples within it. Each arm gets one
    paired-classification-report job per seed, run on the same three artifacts with its own role.
    """
    spec = request["classification"]
    if not isinstance(spec, dict):
        raise ContractError("request.classification must be an object")
    labels = spec.get("labels")
    if not isinstance(labels, str) or not labels:
        raise ContractError("classification.labels must be the labels artifact id")
    preds = spec.get("predictions") or {}
    per_role: dict[str, dict[str, str]] = {}
    for role in ROLES:
        by_seed = preds.get(role)
        if not isinstance(by_seed, dict) or not by_seed:
            raise ContractError(
                f"classification.predictions.{role} must map each training seed to a predictions artifact id"
            )
        per_role[role] = {}
        for seed, art in by_seed.items():
            if not isinstance(art, str) or not art:
                raise ContractError(f"classification.predictions.{role}[{seed!r}] must be an artifact id")
            per_role[role][_seed_key(seed)] = art
    if set(per_role["baseline"]) != set(per_role["candidate"]):
        raise ContractError(
            "classification.predictions: baseline seeds "
            f"{sorted(per_role['baseline'])} != candidate seeds {sorted(per_role['candidate'])}; "
            "paired seed clusters need the same training seeds in both arms"
        )
    seeds = sorted(per_role["baseline"], key=lambda s: (not s.isdigit(), len(s), s))
    extra = {k: spec[k] for k in ("classes", "id_field") if k in spec}
    arms = []
    for role in ROLES:
        runs = []
        for seed in seeds:
            runs.append({
                "seed": int(seed) if seed.isdigit() else seed,
                "cluster": seed,
                "jobs": [{
                    "id": f"{role}.s{seed}.report",
                    "component": REPORT,
                    "inputs": {"baseline": per_role["baseline"][seed], "candidate": per_role["candidate"][seed],
                               "labels": labels},
                    "config": {"role": role, "cluster": seed, **extra},
                }],
            })
        recipe = request.get(role) or {}
        arms.append({"id": role, "role": role,
                     "description": recipe.get("description", f"{role} recipe (existing held-out predictions)"),
                     "runs": runs})
    return arms, [int(s) if s.isdigit() else s for s in seeds], labels


def _classification_draft(request: dict[str, Any], claim: dict[str, Any], obligations: list[str]):
    arms, seeds, labels = _classification_arms(request)
    several = len(seeds) > 1
    alternatives = list(request.get("alternatives_unresolved") or [
        "gain explained by extra computation rather than the candidate's mechanism",
        "gain specific to the evaluation population",
    ])
    if not several:
        alternatives.append("gain specific to these trained models: one training seed per arm, so training-seed "
                            "variance is not estimated")
    draft: dict[str, Any] = {
        "success_rule_id": request["success_rule_id"],
        "assessor": request.get("assessor"),
        "arms": arms,
        "analysis_unit": "example",
        # Several training seeds: examples are clustered by seed so the interval reflects seed variance.
        # One seed: there is no seed variance to estimate; examples are the independent units (disclosed).
        "cluster_by": "seed" if several else None,
        "seed_policy": {
            "kind": "independent-training-seeds",
            "seeds": seeds,
            "shared_across_arms": True,
            "note": ("each seed is one trained model per arm; examples within a seed are clustered" if several
                     else "one trained model per arm; the interval reflects held-out example sampling only"),
        },
        "splits": {"evaluation": {"labels": labels, "population": "held-out examples"}, "tuning": None},
        "tuning": request.get("tuning", {}),
        "required_isolation": request.get("required_isolation"),
        "resource_reservation": {"matched_budget": request.get("budget") or {}, "arms": len(arms),
                                 "seeds": len(seeds)},
        "obligations": obligations,
        "alternatives_unresolved": alternatives,
        "disclosures": {
            "claim_statement": claim.get("statement"),
            "evidence_path": "existing held-out predictions -> eidos.task.paired-classification-report -> "
                             "eidos.assessors.paired-ci (metric accuracy)",
            "predictions_predate_plan": "the predictions were produced before this plan was frozen; the plan pins "
                                        "their artifacts, and the charter rule was fixed before them",
            "seed_variance": "clustered by training seed" if several else "not estimated (one seed per arm)",
            "template": "eidos.templates.improves-under-budget",
        },
    }
    return draft, seeds


def run(inputs, config, *, reader, writer, context):
    request = reader.json(inputs["request"])
    if not isinstance(request, dict):
        raise ContractError("request must be a JSON object")
    bad = find_forbidden(request)
    if bad:
        raise ContractError(
            f"request must not define success bars; remove {bad} (the charter rule owns them)"
        )
    claim = request.get("claim") or {}
    if claim.get("kind") != "improves":
        raise ContractError("this template applies only to claims of kind 'improves'")
    if not request.get("success_rule_id"):
        raise ContractError("request.success_rule_id (a rule in the pinned charter) is required")
    if "classification" in request:
        obligations = [] if request.get("required_isolation") else ["required_isolation"]
        if not request.get("assessor"):
            obligations.append("assessor")
        draft, seeds = _classification_draft(request, claim, obligations)
        leaks = find_forbidden(draft)
        if leaks:
            raise ContractError(f"internal error: draft contains forbidden keys {leaks}")
        writer.stage_json("draft", draft, schema="evidence-plan/1")
        return {
            "arms": [a["id"] for a in draft["arms"]],
            "seeds": seeds,
            "jobs_planned": sum(len(r["jobs"]) for a in draft["arms"] for r in a["runs"]),
            "open_obligations": obligations,
            "metrics": {"arms": len(draft["arms"]), "seeds": len(seeds)},
        }
    seeds = _seeds(request)
    evaluation = dict(request.get("evaluation") or {})
    baseline = dict(request.get("baseline") or {})
    candidate = dict(request.get("candidate") or {})
    if not candidate:
        raise ContractError("request.candidate (the intervention under test) is required")
    obligations: list[str] = []
    if not request.get("required_isolation"):
        obligations.append("required_isolation")  # never default confirmation to the same OS user
    if seeds is None:
        obligations.append("seed_policy")
    if not evaluation:
        obligations.append("evaluation split and size")
    if not request.get("assessor"):
        obligations.append("assessor")
    budget = request.get("budget") or {}
    arms = [
        {
            "id": "baseline",
            "role": "baseline",
            "description": baseline.get("description", "reference recipe"),
            "runs": _arm_runs("baseline", "baseline", request, baseline, seeds, evaluation),
        },
        {
            "id": "candidate",
            "role": "candidate",
            "description": candidate.get("description", "candidate recipe"),
            "runs": _arm_runs("candidate", "candidate", request, candidate, seeds, evaluation),
        },
    ]
    draft: dict[str, Any] = {
        "success_rule_id": request["success_rule_id"],
        "assessor": request.get("assessor"),
        "arms": arms,
        "analysis_unit": "pair",
        "cluster_by": "seed",
        "seed_policy": (
            {
                "kind": "independent-training-seeds",
                "seeds": seeds,
                "shared_across_arms": True,
                "stream_policy": "paired",
                "note": "each seed is one independent model; pairs within a seed are clustered",
            }
            if seeds is not None
            else None
        ),
        "splits": {"evaluation": evaluation or None, "tuning": None},
        "tuning": request.get("tuning", {}),
        "required_isolation": request.get("required_isolation"),
        "resource_reservation": {"matched_budget": budget, "arms": len(arms), "seeds": len(seeds or [])},
        "obligations": obligations,
        "alternatives_unresolved": list(
            request.get("alternatives_unresolved")
            or [
                "gain explained by extra computation rather than the candidate's mechanism",
                "gain specific to the evaluation population",
            ]
        ),
        "disclosures": {
            "claim_statement": claim.get("statement"),
            "budget_matching": "arms are planned with equal training budget",
            "template": "eidos.templates.improves-under-budget",
        },
    }
    leaks = find_forbidden(draft)
    if leaks:
        raise ContractError(f"internal error: draft contains forbidden keys {leaks}")
    writer.stage_json("draft", draft, schema="evidence-plan/1")
    return {
        "arms": [a["id"] for a in arms],
        "seeds": seeds,
        "jobs_planned": sum(len(r["jobs"]) for a in arms for r in a["runs"]),
        "open_obligations": obligations,
        "metrics": {"arms": len(arms), "seeds": len(seeds or [])},
    }
