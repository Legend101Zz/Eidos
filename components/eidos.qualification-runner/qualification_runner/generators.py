"""Seeded generators for stochastic controls (standard library only).

A control input value may contain ``"$generate": {"name": "paired-rows/1", "params": {...}}``; the
runner replaces that key by the generator's output, seeded by ``params.seed`` and the repetition index,
so repetition ``k`` of a stochastic control is reproducible and different from repetition ``k + 1``.
"""

from __future__ import annotations

import random
from typing import Any


def _clip(x: float) -> float:
    return min(1.0, max(0.0, x))


def paired_rows(params: dict[str, Any], repetition: int) -> dict[str, Any]:
    """Paired prediction rows for a baseline and a candidate arm.

    Both-correct happens with probability ``reference_rate * p`` where ``p`` is chosen so that the
    arm's expected both-correct rate is ``baseline_rate`` / ``candidate_rate``; each cluster (a
    training seed) gets its own random shift of ``p`` with standard deviation ``cluster_sd``.
    """
    rng = random.Random(f"{params.get('seed', 0)}:{repetition}")
    clusters, pairs = int(params.get("clusters", 6)), int(params.get("pairs", 80))
    arms = {
        "baseline": (
            str(params.get("baseline_arm", "baseline")),
            params["baseline_rate"],
            params.get("baseline_reference_rate", 0.97),
        ),
        "candidate": (
            str(params.get("candidate_arm", "candidate")),
            params["candidate_rate"],
            params.get("candidate_reference_rate", 0.97),
        ),
    }
    out: dict[str, Any] = {arm_id: {"role": role, "rows": []} for role, (arm_id, _, _) in arms.items()}
    sd = float(params.get("cluster_sd", 0.03))
    for c in range(clusters):
        shift = {role: rng.gauss(0.0, sd) for role in arms}
        for i in range(pairs):
            for role, (arm_id, rate, ref_rate) in arms.items():
                p = _clip(rate / ref_rate + shift[role])
                ref_ok = rng.random() < ref_rate
                var_ok = rng.random() < (p if ref_ok else 0.5)
                out[arm_id]["rows"].append(
                    {
                        "pair_id": f"p{c}-{i}",
                        "cluster": str(c),
                        "reference_logit": 1.5 if ref_ok else -1.5,
                        "variant_logit": -1.5 if var_ok else 1.5,
                        "reference_label": 1,
                        "variant_label": 0,
                    }
                )
    return {"arms": out}


GENERATORS = {"paired-rows/1": paired_rows}
