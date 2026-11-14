"""Seeded, rule-based PMLB candidate selection; no hand-picked task list."""

import argparse
import csv
import io
import random
import re
from collections import Counter, defaultdict

from . import intake
from .common import sha, write_json

EXCLUDED = ("breast", "wdbc", "wpbc", "diabetes", "digits", "optdigits", "pendigits", "mnist", "iris",
            "wine", "house", "cal_housing", "california", "boston", "fri_c", "friedman", "linnerud",
            "covtype", "olivetti", "20news", "lfw", "kddcup", "rcv1")
RULES = {"excluded_name_substrings": list(EXCLUDED), "rows": [1600, 150000], "features": [3, 200],
         "classification_classes": [2, 10], "min_class_fraction": 0.05,
         "imbalanced_min_fraction": 0.02, "max_imbalanced": 4, "max_per_family": 2,
         "max_formula_tasks": 4, "row_bands": [[1600, 4999], [5000, 19999], [20000, 150000]],
         "balance": "alternate task types; round-robin row bands; seeded shuffle within cells"}


def family(name):
    name = name.lower()
    for prefix in ("gametes", "feynman", "strogatz", "mfeat"):
        if name.startswith(prefix + "_"):
            return prefix
    name = re.sub(r"\d+", "", name)
    return re.sub(r"(?:[_-](?:v|variant|small|large|easy|hard|test|train|binary|multi))*[_-]*$", "", name)


def row_band(n):
    return 0 if n < 5000 else (1 if n < 20000 else 2)


def select(summary_bytes, seed=20261114, count=60):
    rows = list(csv.DictReader(io.StringIO(summary_bytes.decode("utf-8")), delimiter="\t"))
    eligible, rejected = [], []
    for row in rows:
        key = row.get("dataset", row.get("name", ""))
        kind = row.get("task", row.get("task_type", "")).lower()
        n = int(float(row.get("n_instances", row.get("n_rows", 0))))
        d = int(float(row.get("n_features", 0)))
        k = int(float(row.get("n_classes", 0)))
        minority = row.get("minority_class_size")
        fraction = float(minority) / n if minority is not None and n else None
        reason = next((f"name:{s}" for s in EXCLUDED if s in key.lower()), None)
        if not reason and (kind not in {"classification", "regression"} or not 1600 <= n <= 150000
                           or not 3 <= d <= 200):
            reason = "task/size/features"
        if not reason and kind == "classification" and (not 2 <= k <= 10 or (fraction is not None and fraction < 0.02)):
            reason = "classes/minority"
        if reason:
            rejected.append({"key": key, "reason": reason})
        else:
            eligible.append(dict(source="pmlb", key=key, task_type=kind, n_rows=n, n_features=d,
                                 n_classes=k, minority_fraction=fraction if kind == "classification" else None,
                                 imbalanced=kind == "classification" and fraction is not None and fraction < 0.05,
                                 family=family(key), row_band=row_band(n), summary=row))
    # Do not interpret a missing class-count field as zero or guess it from PMLB's imbalance index.
    # Compute a strict cardinality upper bound before doing any random drawing.
    family_counts = Counter(c["family"] for c in eligible)
    maximum = sum(min(n, RULES["max_per_family"]) for n in family_counts.values())
    if maximum < count:
        raise ValueError(f"summary permits at most {maximum} candidates after size/name/class/family rules "
                         f"even before minority-class screening; requested {count}")
    missing = [c["key"] for c in eligible if c["task_type"] == "classification" and c["minority_fraction"] is None]
    if missing:
        raise ValueError("summary lacks minority_class_size for eligible classification tasks; "
                         "cannot verify the required 5%/2% smallest-class rule from its imbalance index")
    rng = random.Random(f"{seed}:{sha(summary_bytes)}")
    cells = defaultdict(list)
    for card in sorted(eligible, key=lambda c: c["key"]):
        cells[card["task_type"], card["row_band"]].append(card)
    for cell in cells.values():
        rng.shuffle(cell)
    chosen, families = [], Counter()
    imbalanced, formulas = 0, 0
    while len(chosen) < count:
        progress = False
        for kind in ("classification", "regression"):
            for band in range(3):
                cell = cells[kind, band]
                while cell:
                    candidate = cell.pop()
                    formula = candidate["family"] in {"feynman", "strogatz"}
                    if (families[candidate["family"]] >= 2 or (candidate["imbalanced"] and imbalanced >= 4)
                            or (formula and formulas >= 4)):
                        rejected.append({"key": candidate["key"], "reason": "family/imbalance/formula cap"})
                        continue
                    candidate["card_id"] = f"R{len(chosen) + 1:03d}"
                    chosen.append(candidate)
                    families[candidate["family"]] += 1
                    imbalanced += candidate["imbalanced"]
                    formulas += formula
                    progress = True
                    break
                if len(chosen) == count:
                    break
            if len(chosen) == count:
                break
        if not progress:
            raise ValueError(f"only {len(chosen)} candidates satisfy all rules; requested {count}")
    # Interleave types in catalog order, keeping the rule-based identities stable.
    classification = [c for c in chosen if c["task_type"] == "classification"]
    regression = [c for c in chosen if c["task_type"] == "regression"]
    chosen = []
    while classification or regression:
        for group in (classification, regression):
            if group:
                chosen.append(group.pop(0))
    for i, card in enumerate(chosen, 1):
        card["card_id"] = f"R{i:03d}"
    return dict(schema_version="eidos-card-catalog/0.1", seed=seed, summary_sha256=sha(summary_bytes),
                rules=RULES, cards=chosen, exclusions=rejected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--allow-root", action="store_true")
    args = parser.parse_args()
    result = select(intake.summary(args.cache, allow_root=args.allow_root), args.seed, args.count)
    write_json(args.out, result)
    print(f"selected {len(result['cards'])} cards; summary sha256={result['summary_sha256']}")


if __name__ == "__main__":
    main()
