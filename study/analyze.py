#!/usr/bin/env python3
"""Descriptive pilot tables and plots around the immutable draft estimator."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study.audit_transcripts import audit
from study.common import dev_command, read_json, write_json


def analyze(design_path, results_path, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    design, results = read_json(design_path), read_json(results_path)
    primary = dev_command(
        "analyze_results.py",
        "--design",
        design_path,
        "--results",
        results_path,
        "--out",
        out / "estimator.json",
    )
    print(primary.stdout.strip())
    observed = {(r["family"], r["task"], r["run"], r["arm"]): r for r in results}
    write_json(
        out / "transcript_audit.json", [audit(r["transcript"]) for r in results if r.get("transcript")]
    )
    rows, statuses, resources = [], [], []
    for arm in (1, 2, 3, 4):
        assigned = [r for r in design if r["arm"] == arm]
        outcomes = [observed.get((r["family"], r["task"], r["run"], arm), {}) for r in assigned]
        for card in sorted({r["task"] for r in assigned}):
            subset = [
                observed.get((r["family"], card, r["run"], arm), {}) for r in assigned if r["task"] == card
            ]
            rows.append(
                dict(
                    arm=arm,
                    card=card,
                    n=len(subset),
                    successes=sum(
                        bool(r.get("card_success", r.get("verified_success", False))) for r in subset
                    ),
                    verified_successes=sum(bool(r.get("verified_success")) for r in subset),
                    primary_eligible=card in {"D02","D03","D04","D05","D06"},
                )
            )
        counts = Counter(r.get("status", "missing") for r in outcomes)
        statuses.extend(dict(arm=arm, status=status, n=n) for status, n in sorted(counts.items()))
        tokens = [r["tokens"]["total"] for r in outcomes if r.get("tokens") is not None]
        wall = [r["wall_s"] for r in outcomes if r.get("wall_s") is not None]
        resources.append(
            dict(
                arm=arm,
                assigned=len(assigned),
                recorded=len(tokens),
                total_tokens=sum(tokens),
                mean_tokens=sum(tokens) / len(tokens) if tokens else None,
                wall_s=sum(wall),
                mean_wall_s=sum(wall) / len(wall) if wall else None,
                unknown_cost=sum(r.get("billed_usd") is None for r in outcomes),
            )
        )
    for name, table in (("successes", rows), ("statuses", statuses), ("resources", resources)):
        with (out / f"{name}.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    write_json(out / "descriptive.json", dict(successes=rows, statuses=statuses, resources=resources))
    lines = [
        "# Development pilot — descriptive only",
        "",
        "D02–D06 are in the improvement stratum. D01 reports operational equality; D12 reports",
        "null correctness (never a primary improvement success). Missing assignments remain in n.",
        "",
        "The draft interval is exploratory; these data cannot establish practical superiority.",
        "",
        "| Arm | Card | Correct task outcomes / assigned | Verified task successes |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r['arm']} | {r['card']} | {r['successes']}/{r['n']} | {r['verified_successes']} |" for r in rows
    ]
    lines += ["", "| Arm | Status | n |", "|---|---|---|"]
    lines += [f"| {r['arm']} | {r['status']} | {r['n']} |" for r in statuses]
    lines += ["", "| Arm | Recorded tokens | Recorded wall seconds | Unknown costs |", "|---|---|---|---|"]
    lines += [
        f"| {r['arm']} | {r['total_tokens']} | {r['wall_s']:.2f} | {r['unknown_cost']} |" for r in resources
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    # Use a local cache so plotting writes stay on the external SSD.
    import os

    os.environ["MPLCONFIGDIR"] = str(out / ".matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    cards = sorted({r["task"] for r in design})
    fig, ax = plt.subplots(figsize=(8, 4.5))
    positions = np.arange(len(cards))
    for i, arm in enumerate((1, 2, 3, 4)):
        selected = [next(r for r in rows if r["arm"] == arm and r["card"] == card) for card in cards]
        ax.bar(
            positions + (i - 1.5) * 0.18,
            [r["successes"] / r["n"] for r in selected],
            width=0.18,
            label=f"Arm {arm}",
        )
    ax.set(
        xticks=positions,
        xticklabels=cards,
        ylim=(0, 1.08),
        ylabel="Correct task outcomes / assigned",
        title="Development pilot: operational, improvement and null strata",
    )
    ax.legend(ncols=4, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    fig.tight_layout()
    fig.savefig(out / "success_by_arm_card.png", dpi=160)
    plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(9, 4))
    axes[0].bar([r["arm"] for r in resources], [r["total_tokens"] for r in resources])
    axes[0].set(title="Recorded processed tokens", xlabel="Arm", ylabel="Tokens", xticks=[1, 2, 3, 4])
    axes[1].bar([r["arm"] for r in resources], [r["wall_s"] for r in resources])
    axes[1].set(title="Recorded wall time", xlabel="Arm", ylabel="Seconds", xticks=[1, 2, 3, 4])
    fig.suptitle("Development pilot resources (missing receipts excluded)")
    fig.tight_layout()
    fig.savefig(out / "resources_by_arm.png", dpi=160)
    plt.close(fig)
    print(f"Wrote descriptive tables and 2 PNGs to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    analyze(args.design, args.results, args.out)
