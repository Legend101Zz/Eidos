"""Score labeled binary prediction pairs, overall and by group (stdlib only).

Input rows (csv or jsonl): ``pair_id, reference_logit, variant_logit, reference_label,
variant_label`` and the grouping column named by ``config["group_by"]`` (default ``stratum``).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from eidos_worker.errors import ContractError

REQUIRED = ("pair_id", "reference_logit", "variant_logit", "reference_label", "variant_label")


def _score(row, key, pair_id):
    try:
        value = float(row[key])
    except (TypeError, ValueError) as exc:
        raise ContractError(f"pair {pair_id}: {key} is not a number: {row[key]!r}") from exc
    if not math.isfinite(value):
        raise ContractError(f"pair {pair_id}: non-finite score in {key}")
    return value


def _label(row, key, pair_id):
    raw = row[key]
    text = str(raw).strip()
    if text not in ("0", "1", "0.0", "1.0"):
        raise ContractError(f"pair {pair_id}: {key} must be binary (0 or 1), got {raw!r}")
    return int(float(text))


def _rates(counts):
    n = counts["pairs"]
    return {
        "pairs": n,
        "reference_correct": counts["reference_correct"],
        "both_correct": counts["both_correct"],
        "reference_accuracy": counts["reference_correct"] / n,
        "both_correct_rate": counts["both_correct"] / n,
    }


def run(inputs, config, *, reader, writer, context):
    group_by = config.get("group_by", "stratum")
    seen = set()
    total = Counter()
    groups = defaultdict(Counter)
    rows = 0
    for row in reader.rows(inputs["predictions"]):
        rows += 1
        missing = [k for k in REQUIRED + (group_by,) if k not in row]
        if missing:
            raise ContractError(f"row {rows}: missing columns {missing}")
        pair_id = str(row["pair_id"])
        if pair_id in seen:
            raise ContractError(f"duplicate pair id {pair_id}")
        seen.add(pair_id)
        reference = _score(row, "reference_logit", pair_id)
        variant = _score(row, "variant_logit", pair_id)
        first = _label(row, "reference_label", pair_id)
        second = _label(row, "variant_label", pair_id)
        group = row[group_by]
        if group is None or str(group) == "":
            raise ContractError(f"pair {pair_id}: empty value in grouping column {group_by!r}")
        reference_ok = (reference > 0) == bool(first)
        variant_ok = (variant > 0) == bool(second)
        for counter in (total, groups[str(group)]):
            counter.update(
                pairs=1, reference_correct=int(reference_ok), both_correct=int(reference_ok and variant_ok)
            )
    if not total:
        raise ContractError("supply at least one pair")
    report = {
        "group_by": group_by,
        "overall": _rates(total),
        "by_group": {name: _rates(c) for name, c in sorted(groups.items())},
    }
    writer.stage_json("report", report, schema="grouped-pair-counts/1")
    context.record("input_rows", rows)
    context.record("unique_pairs", len(seen))
    context.record("group_counts", {name: c["pairs"] for name, c in sorted(groups.items())})
    context.record("rejected_rows", 0)
    context.record(
        "metrics",
        {
            "both_correct_rate": report["overall"]["both_correct_rate"],
            "reference_accuracy": report["overall"]["reference_accuracy"],
            "pairs": rows,
        },
    )
