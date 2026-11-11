#!/usr/bin/env python3
"""Compare two Tier-1 scorecards; exit 1 on regression and 2 on incompatible inputs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from study.common import read_json
from study.evals.mini_eval import SCHEMA


def compare(before, after, tolerance=0.05):
    """Tolerance is an absolute decrease in median gain ratio, default 0.05 ratio points."""
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and nonnegative")
    for value in (before, after):
        if value.get("schema_version") != SCHEMA:
            raise ValueError("unsupported scorecard schema")
    for field in ("cards", "arms"):
        if set(before[field]) != set(after[field]):
            raise ValueError(f"incompatible {field}")
    for field in ("seed_index", "envelope", "stub"):
        if before[field] != after[field]:
            raise ValueError(f"incompatible {field}")
    if before["provenance"]["reference_gains_sha256"] != after["provenance"]["reference_gains_sha256"]:
        raise ValueError("reference snapshots differ; rescore both builds against the same references")
    regressions, changes = [], []
    groups = [("all arms", before["aggregate"], after["aggregate"])]
    groups.extend(
        (f"arm {arm}", before["by_arm"][str(arm)], after["by_arm"][str(arm)]) for arm in before["arms"]
    )
    for label, old, new in groups:
        if old["runs"] != new["runs"] or old["gain_ratio_cards"] != new["gain_ratio_cards"]:
            raise ValueError(f"incompatible run or ratio coverage for {label}")
        for metric in (
            "concluded",
            "verified",
            "dead_ends",
            "crash",
            "timeout",
            "token_budget",
            "incident",
            "invalid_output",
        ):
            previous, current = old[metric], new[metric]
            if previous != current:
                message = f"{label} {metric} {previous} -> {current}"
                changes.append(message)
                is_success_count = metric in {"concluded", "verified"}
                regressed = current < previous if is_success_count else current > previous
                if regressed:
                    regressions.append(message)
        previous, current = old["median_gain_ratio"], new["median_gain_ratio"]
        if (previous is None) != (current is None):
            raise ValueError(f"incompatible median gain ratio coverage for {label}")
        if previous is not None:
            if not math.isfinite(previous) or not math.isfinite(current):
                raise ValueError("median gain ratios must be finite")
            change = f"{label} median gain ratio {previous:.6f} -> {current:.6f}"
            changes.append(change)
            if previous - current > tolerance + 1e-12:
                regressions.append(change)

    # Equal aggregate counts can conceal a different card losing completion or becoming a dead end.
    def keyed(value):
        result = {(r["card"], r["arm"], r["seed_index"]): r for r in value["runs"]}
        if len(result) != len(value["runs"]):
            raise ValueError("duplicate scorecard assignments")
        return result

    old_rows, new_rows = keyed(before), keyed(after)
    if old_rows.keys() != new_rows.keys():
        raise ValueError("scorecard assignments differ")
    for key, previous in old_rows.items():
        current = new_rows[key]
        if previous["concluded"] and not current["concluded"]:
            regressions.append(f"{key[0]} arm {key[1]} lost completion")
        if not previous["invariant_flags"]["dead_end"] and current["invariant_flags"]["dead_end"]:
            regressions.append(f"{key[0]} arm {key[1]} acquired a dead end")
    return dict(
        regression=bool(regressions),
        tolerance=tolerance,
        tolerance_unit="absolute median gain-ratio points",
        regressions=regressions,
        changes=changes,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="allowed absolute median gain-ratio decrease (default 0.05)",
    )
    args = parser.parse_args()
    try:
        report = compare(read_json(args.before), read_json(args.after), args.tolerance)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(2, f"Cannot compare scorecards. {exc}\n")
    print(json.dumps(report, indent=2))
    return int(report["regression"])


if __name__ == "__main__":
    raise SystemExit(main())
