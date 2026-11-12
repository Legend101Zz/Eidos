#!/usr/bin/env python3
"""Compare Tier-1 scorecards and exit 1 on gate failure or 2 on incompatible inputs."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from study.common import read_json
from study.evals.mini_eval import SCHEMA
from study.evals.policy import POLICY
from study.evals.records import REQUIRED_INVARIANTS


def compare(before, after, tolerance=0):
    """Apply the same predeclared no-regression policy to both scorecards."""
    if tolerance != POLICY["tolerance"]:
        raise ValueError("Tier 1 uses prospective tolerance zero")
    epsilon = POLICY["absolute_float_epsilon"]
    for value in (before, after):
        if value.get("schema_version") != SCHEMA:
            raise ValueError("unsupported scorecard schema")
        if value.get("comparison_policy") != POLICY:
            raise ValueError("scorecard did not record the prospective comparison policy")
    for field in ("cards", "arms"):
        if before[field] != after[field]:
            raise ValueError(f"incompatible {field}")
    for field in ("seed_index", "envelope", "model", "stub", "concurrency"):
        if before[field] != after[field]:
            raise ValueError(f"incompatible {field}")
    if before["provenance"]["reference_gains_sha256"] != after["provenance"]["reference_gains_sha256"]:
        raise ValueError("Reference snapshots differ. Rescore both builds against the same references")
    for field in ("git_sha", "design_sha256", "provider_audit_identifiers_sha256"):
        if before["provenance"][field] != after["provenance"][field]:
            raise ValueError(f"both builds require the same controller and audit instrument ({field})")
    regressions, changes, blockers = [], [], []
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
            "controller_abstentions",
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
            if previous - current > epsilon:
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
        label = f"{key[0]} arm {key[1]}"
        for field in ("concluded", "verified_success"):
            old, new = previous[field], current[field]
            if old != new:
                message = f"{label} {field} {old} -> {new}"
                changes.append(message)
                if old and not new:
                    regressions.append(message)
        for diagnostic in ("disposition", "runtime_deadline_limit"):
            if diagnostic == "disposition":
                old, new = previous[diagnostic], current[diagnostic]
            else:
                old, new = (r["invariant_flags"][diagnostic] for r in (previous, current))
            if old != new:
                changes.append(f"{label} {diagnostic} {old} -> {new}")
        for flag in REQUIRED_INVARIANTS:
            old, new = (r["invariant_flags"][flag] for r in (previous, current))
            if old != new:
                changes.append(f"{label} {flag} {old} -> {new}")
            if key[1] not in POLICY["required_invariant_arms"]:
                continue
            if old is None or new is None:
                blockers.append(f"{label} required {flag} evidence is unknown")
            if old is False and new is True:
                regressions.append(f"{label} acquired {flag}")
            if new is True:
                blockers.append(f"{label} has {flag}")
        if previous["reference_median"] != current["reference_median"]:
            raise ValueError(f"{label} reference median differs")
        for field in ("verified_test_gain", "gain_ratio"):
            old, new = previous[field], current[field]
            if any(value is not None and not math.isfinite(value) for value in (old, new)):
                raise ValueError(f"{label} nonfinite {field}")
            if old != new:
                changes.append(f"{label} {field} {old} -> {new}")
            if old is not None and new is not None and old - new > epsilon:
                regressions.append(f"{label} {field} decreased by {old - new:.8g}")
        for value in (previous, current):
            if value["verified_success"] and value["verified_test_gain"] is None:
                blockers.append(f"{label} verified gain missing")
            median = value["reference_median"]
            if median is not None and median > 0 and value["gain_ratio"] is None:
                blockers.append(f"{label} expected reference ratio is missing")
    if not after["stub"]:
        for value in (before, after):
            identity = value["provenance"]
            expected = identity.get("expected_build_sha") or ""
            if (
                identity.get("git_dirty") is not False
                or not re.fullmatch(r"[0-9a-f]{40}", expected)
                or expected != identity["pilot_build"].get("eidos_commit_sha")
                or not re.fullmatch(r"[0-9a-f]{64}", identity.get("pilot_build_sha256") or "")
            ):
                blockers.append("missing verified controller or frozen-build identity")
        for field in (
            "full_development_set",
            "completion_pass",
            "invariant_pass",
            "run_evidence_pass",
            "eligible",
        ):
            if after["tier1"].get(field) is not True:
                blockers.append(f"candidate Tier-1 prerequisite failed: {field}")
    return dict(
        regression=bool(regressions),
        gate_pass=not after["stub"] and not blockers and not regressions,
        policy=POLICY,
        regressions=regressions,
        changes=changes,
        blockers=blockers,
        provenance={key: value["provenance"] for key, value in (("before", before), ("after", after))},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0,
        help="prospective allowed decrease (must be zero)",
    )
    args = parser.parse_args()
    try:
        before, after = read_json(args.before), read_json(args.after)
        report = compare(before, after, args.tolerance)
        from study.evals.provenance import digest

        report["scorecard_sha256"] = {"before": digest(args.before), "after": digest(args.after)}
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(2, f"Cannot compare scorecards. {exc}\n")
    print(json.dumps(report, indent=2))
    return int(report["regression"] or (not after["stub"] and not report["gate_pass"]))


if __name__ == "__main__":
    raise SystemExit(main())
