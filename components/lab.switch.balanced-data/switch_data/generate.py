"""Synthetic switch-order sequences with an explicit requested-versus-realized exception count.

Each example is a sequence of background events with two switches (A=0, B=1) at distinct positions;
the target is whether A comes before B. A ``recorder`` bit equals the target except for an
"exception" subset where it is flipped. Two ways to choose exceptions are offered, because they
realize different things:

* ``rounded-per-batch`` - every batch of ``batch_size`` examples gets exactly ``round(batch_size *
  rate)`` exceptions (Python rounding: ``round(0.5) == 0``). A requested rate that is not a multiple of
  1/batch_size silently becomes a different one (0.5% of 100 realizes 0%).
* ``bernoulli`` - each example is an exception independently with probability ``rate`` (realized count
  varies; it is not rounded per batch).

The receipt records requested and realized counts and rates so a consumer can see which happened.
"""

from __future__ import annotations

import random

from eidos_worker.errors import ContractError

SEQUENCE_LENGTH = 12
MODES = ("rounded-per-batch", "bernoulli")


def _example(rng: random.Random, index: int):
    events = [rng.randrange(2, 6) for _ in range(SEQUENCE_LENGTH)]
    pos_a = rng.randrange(SEQUENCE_LENGTH)
    pos_b = (pos_a + rng.randrange(1, SEQUENCE_LENGTH)) % SEQUENCE_LENGTH
    events[pos_a], events[pos_b] = 0, 1
    return {"id": index, "events": events, "pos_a": pos_a, "pos_b": pos_b, "target": int(pos_a < pos_b)}


def run(inputs, config, *, writer, context):
    n = int(config.get("examples", 1000))
    batch_size = int(config.get("batch_size", 100))
    rate = float(config.get("exception_rate", 0.02))
    mode = config.get("mode", "rounded-per-batch")
    seed = int(config.get("seed", context.seed if context.seed is not None else 0))
    if mode not in MODES:
        raise ContractError(f"mode must be one of {MODES}")
    if n < 1 or batch_size < 1 or not 0.0 <= rate <= 1.0:
        raise ContractError("examples and batch_size must be positive and exception_rate in [0, 1]")
    rng = random.Random(seed)
    rows, per_batch = [], []
    for start in range(0, n, batch_size):
        size = min(batch_size, n - start)
        batch = [_example(rng, start + i) for i in range(size)]
        if mode == "rounded-per-batch":
            chosen = set(rng.sample(range(size), round(size * rate)))
        else:
            chosen = {i for i in range(size) if rng.random() < rate}
        for i, row in enumerate(batch):
            row["exception"] = int(i in chosen)
            row["recorder"] = row["target"] ^ row["exception"]
            row["batch"] = start // batch_size
        per_batch.append(len(chosen))
        rows.extend(batch)
    realized = sum(per_batch)
    writer.stage_rows("dataset", rows, format="jsonl", schema="switch-sequences/1")
    return {
        "mode": mode,
        "seed": seed,
        "examples": n,
        "batch_size": batch_size,
        "requested_exception_rate": rate,
        "requested_exceptions": n * rate,
        "requested_exceptions_per_full_batch": batch_size * rate,
        "realized_exceptions": realized,
        "realized_exception_rate": realized / n,
        "realized_exceptions_per_batch": {
            "min": min(per_batch),
            "max": max(per_batch),
            "batches": len(per_batch),
        },
        "group_counts": {"agree": n - realized, "exception": realized},
        "metrics": {"realized_exception_rate": realized / n, "requested_exception_rate": rate},
    }
