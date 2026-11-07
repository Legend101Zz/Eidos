# Provenance: copied from the Eidos design handoff archive (eidos-claude-handoff), worked example
# "pytorch-lab-worked-example" (pair_report.py). It is the researcher's own existing code and has no Eidos
# imports; the Eidos adapter lives in eidos_adapter.py.
"""Summarize labeled binary prediction pairs. Uses only the standard library."""
import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


def read_pairs(path):
    with Path(path).open(newline="") as stream:
        yield from csv.DictReader(stream)


def summarize(rows):
    totals = Counter()
    groups = defaultdict(Counter)
    seen = set()
    for row in rows:
        pair_id = str(row["pair_id"])
        if pair_id in seen:
            raise ValueError(f"Duplicate pair ID {pair_id}")
        seen.add(pair_id)
        original, variant = map(float, (row["reference_logit"], row["variant_logit"]))
        if not (math.isfinite(original) and math.isfinite(variant)):
            raise ValueError(f"Nonfinite score in pair {pair_id}")
        first, second = map(int, (row["reference_label"], row["variant_label"]))
        if first not in (0, 1) or second not in (0, 1):
            raise ValueError(f"Invalid binary label in pair {pair_id}")
        correct = (original > 0) == first
        other_correct = (variant > 0) == second
        for counter in (totals, groups[row["stratum"]]):
            counter.update(pairs=1, original_correct=int(correct),
                           both_correct=int(correct and other_correct))

    def report(counts):
        n = counts["pairs"]
        return {"pairs": n, "reference_accuracy": counts["original_correct"] / n,
                "both_correct_rate": counts["both_correct"] / n}

    if not totals:
        raise ValueError("Supply at least one pair")
    return {"overall": report(totals), "by_stratum": {k: report(v) for k, v in sorted(groups.items())}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predictions", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(read_pairs(args.predictions)), indent=2))
