"""Two-stage, seeded candidate ordering and real-table eligibility checks."""

import argparse
import csv
import io
import random
import re
from collections import Counter, defaultdict

import numpy as np

from . import intake
from .common import sha, write_json

EXCLUDED = ("breast", "wdbc", "wpbc", "diabetes", "digits", "optdigits", "pendigits", "mnist", "iris",
            "wine", "house", "cal_housing", "california", "boston", "fri_c", "friedman", "fried", "linnerud",
            "covtype", "olivetti", "20news", "lfw", "kddcup", "rcv1")
RULES = {"excluded_name_substrings": list(EXCLUDED), "rows": [1600, 150000], "features": [3, 200],
         "classification_classes": [2, 10], "min_class_fraction": 0.05,
         "imbalanced_min_fraction": 0.02, "max_imbalanced": 4, "max_per_family": 2,
         "max_formula_tasks": 4, "row_bands": [[1600, 4999], [5000, 19999], [20000, 150000]],
         "balance": "alternate types and row bands; OpenML regression follows PMLB and only restores parity",
         "stage2": ["minority fraction", "missing values", "nonconstant features", "train size", "content duplicates"]}


SYNTHETIC = ("feynman_", "strogatz_", "bng", "2dplanes", "ring", "twonorm", "waveform", "mv",
             "fried", "gametes", "parity", "monk", "led", "hill_valley", "clean", "banana")


def likely_synthetic(name):
    return any(part in name.lower() for part in SYNTHETIC)


def feature_signature(x):
    """Feature-only column multiset, canonical numbers and row-order independent."""
    hashes = []
    for column in np.asarray(x).T:
        values = np.asarray(column, dtype=float).copy()
        values[values == 0] = 0
        hashes.append(sha("\n".join(format(v, ".9g") for v in np.sort(values)).encode()))
    return sorted(hashes)


def same_feature_family(a, b):
    overlap = sum((Counter(a) & Counter(b)).values())
    return bool(a and b) and overlap >= 0.8 * min(len(a), len(b))


def family(name):
    name = name.lower()
    for prefix in ("gametes", "feynman", "strogatz", "mfeat"):
        if name.startswith(prefix + "_"):
            return prefix
    name = re.sub(r"\d+", "", name)
    return re.sub(r"(?:[_-](?:v|variant|small|large|easy|hard|test|train|binary|multi))*[_-]*$", "", name)


def row_band(n):
    return 0 if n < 5000 else (1 if n < 20000 else 2)


def eligibility(candidate):
    name = candidate.get("name", candidate["key"])
    reason = next((f"name:{s}" for s in EXCLUDED if s in name.lower()), None)
    n, d, kind = candidate["n_rows"], candidate["n_features"], candidate["task_type"]
    if not reason and (kind not in {"classification", "regression"} or not 1600 <= n <= 150000 or not 3 <= d <= 200):
        reason = "task/size/features"
    k, fraction = candidate.get("n_classes"), candidate.get("minority_fraction")
    if not reason and kind == "classification" and ((k is not None and not 2 <= k <= 10) or
                                                    (fraction is not None and fraction < 0.02)):
        reason = "classes/minority"
    return reason


def _ordered(cards, rng):
    cells = defaultdict(list)
    for card in sorted(cards, key=lambda c: (c["source"], c["key"])):
        cells[card["task_type"], card["row_band"]].append(card)
    for cell in cells.values():
        rng.shuffle(cell)
    result = []
    turn = dict(classification=0, regression=0)
    while any(cells.values()):
        for kind in ("classification", "regression"):
            for offset in range(3):
                band = (turn[kind] + offset) % 3
                if cells[kind, band]:
                    result.append(cells[kind, band].pop())
                    turn[kind] = (band + 1) % 3
                    break
    return result


def select(summary_bytes, seed=20261114, count=None, openml_cards=()):
    if count is not None and count < 0:
        raise ValueError("count must be nonnegative")
    candidates, rejected = [], []
    rows = csv.DictReader(io.StringIO(summary_bytes.decode("utf-8")), delimiter="\t")
    for row in rows:
        n = int(float(row.get("n_instances", row.get("n_rows", 0))))
        kind = row.get("task", row.get("task_type", "")).lower()
        minority = row.get("minority_class_size")
        candidates.append(dict(source="pmlb", key=row.get("dataset", row.get("name", "")), task_type=kind,
            n_rows=n, n_features=int(float(row.get("n_features", 0))),
            n_classes=int(float(row["n_classes"])) if row.get("n_classes") else None,
            minority_fraction=float(minority) / n if minority and n and kind == "classification" else None,
            summary=row))
    candidates.extend(dict(c) for c in openml_cards)
    eligible = []
    for card in candidates:
        reason = eligibility(card)
        if reason:
            rejected.append(dict(source=card["source"], key=card["key"], reason=reason))
        else:
            card.update(synthetic=likely_synthetic(card.get("name", card["key"])), family=family(card.get("name", card["key"])), row_band=row_band(card["n_rows"]),
                        imbalanced=card["task_type"] == "classification" and
                        card.get("minority_fraction") is not None and card["minority_fraction"] < 0.05)
            eligible.append(card)
    rng = random.Random(f"{seed}:{sha(summary_bytes)}")
    ordered = _ordered([c for c in eligible if c["source"] == "pmlb"], rng)
    ordered += _ordered([c for c in eligible if c["source"] == "openml"], rng)
    chosen, families = [], Counter()
    imbalanced, formulas = 0, 0
    for card in ordered:
        formula = card["family"] in {"feynman", "strogatz"}
        if families[card["family"]] >= 2 or (formula and formulas >= 4) or (card["imbalanced"] and imbalanced >= 4):
            rejected.append(dict(source=card["source"], key=card["key"], reason="family/imbalance/formula cap"))
            continue
        chosen.append(card)
        families[card["family"]] += 1
        formulas += formula
        imbalanced += card["imbalanced"]
    # Family exclusions can remove one side of an alternating stream; restore alternation.
    interleaved = []
    for source in ("pmlb", "openml"):
        groups = [[c for c in chosen if c["source"] == source and c["task_type"] == kind]
                  for kind in ("classification", "regression")]
        interleaved.extend(group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group))
    chosen = interleaved
    counts = Counter(c["task_type"] for c in chosen)
    source_counts = Counter(f"{c['source']}:{c['task_type']}" for c in chosen)
    return dict(schema_version="eidos-card-catalog/0.2", seed=seed, summary_sha256=sha(summary_bytes),
                rules=RULES, cards=chosen if count is None else chosen[:count], exclusions=rejected,
                eligible_per_task=dict(counts), eligible_per_source_task=dict(source_counts),
                requested_count=count, balance=dict(counts))


def table_fingerprint(x, y):
    """Shape plus sorted column hashes: ignores column names/order, preserves row order."""
    hashes = []
    for column in [*np.asarray(x).T, np.asarray(y)]:
        try:
            values = np.asarray(column, dtype="<f8").copy()
            values[values == 0] = 0  # canonicalise negative zero
            data = values.tobytes()
        except ValueError:
            data = b"".join(len(v.encode()).to_bytes(8, "little") + v.encode() for v in map(str, column))
        hashes.append(sha(data))
    return sha(repr((len(y), np.asarray(x).shape[1] + 1, sorted(hashes))).encode())


def check_table(table, task_type, *, fingerprints=(), imbalanced_count=0, seed=20261114, max_classes=10):
    """Stage two is fail-closed on the actual table and on the exact packaging split."""
    from .package import prepare_arrays

    x, y = np.asarray(table["x"]), np.asarray(table["y"])
    if x.ndim != 2 or y.shape != (len(x),):
        raise ValueError("invalid table shape")
    if not 1600 <= len(y) <= 150000 or not 3 <= x.shape[1] <= 200:
        raise ValueError("real table size/features outside eligibility")
    if not np.isfinite(x).all() or any(str(v).strip().lower() in {"?", "", "nan", "inf", "-inf"} for v in y):
        raise ValueError("missing/nonfinite values")
    imbalanced, fraction = False, None
    if task_type == "classification":
        labels, counts = np.unique(y, return_counts=True)
        if not 2 <= len(labels) <= max_classes:
            raise ValueError("classification requires 2..max-classes classes")
        fraction = float(counts.min() / len(y))
        imbalanced = fraction < 0.05
        if fraction < 0.02 or (imbalanced and imbalanced_count >= 4):
            raise ValueError("minority class fraction/imbalanced cap")
    data, provenance = prepare_arrays(x, y, task_type, seed)
    if data["train"]["x"].shape[1] < 3:
        raise ValueError("fewer than 3 non-constant train features")
    fingerprint = table_fingerprint(x, y)
    if fingerprint in fingerprints:
        raise ValueError("duplicate table content")
    return dict(fingerprint=fingerprint, feature_signature=feature_signature(x), imbalanced=imbalanced, minority_fraction=fraction,
                n_rows=len(y), n_features=data["train"]["x"].shape[1], split_sizes=provenance["split_sizes"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--count", type=int)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--allow-root", action="store_true")
    args = parser.parse_args()
    suite = intake.openml_suite(args.cache, allow_root=args.allow_root)
    result = select(intake.summary(args.cache, allow_root=args.allow_root), args.seed, args.count, suite["cards"])
    result["openml_suite"] = suite
    write_json(args.out, result)
    print(f"ordered {len(result['cards'])} candidates; eligible per source/type: {result['eligible_per_source_task']}")
    if suite["errors"]:
        print(f"OpenML listing: {suite['errors']}")


if __name__ == "__main__":
    main()
