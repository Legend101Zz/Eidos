#!/usr/bin/env python3
"""Refresh development references from read-only local pilot grades."""

from __future__ import annotations

import argparse
import hashlib
import statistics
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from study.common import read_json, write_json

CARDS = ("N01", "N02", "N03", "N04", "D02", "D03", "D04")


def regenerate(pilot3, pilot2):
    sources, rows = [], []
    for path in sorted(Path(pilot3).glob("N0[1-4]-r*/arm[123]/grade.json")):
        value = read_json(path)
        source = str(path.relative_to(pilot3))
        sources.append(dict(source="pilot3/" + source, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        rows.append((value, "pilot3/" + source))
    sources.append(
        dict(
            source="pilot2/results-corrected.json",
            sha256=hashlib.sha256(Path(pilot2).read_bytes()).hexdigest(),
        )
    )
    rows.extend(
        (r, "pilot2/results-corrected.json") for r in read_json(pilot2) if r["task"] in {"D02", "D03", "D04"}
    )
    cards = {}
    for card in CARDS:
        samples = [
            dict(arm=r["arm"], run=r["run"], gain=r["numeric"]["gain"], source=source)
            for r, source in rows
            if r["task"] == card
            and r["arm"] in (1, 2, 3)
            and r.get("verified_success")
            and r.get("status") == "valid"
            and r.get("numeric")
        ]
        gains = [s["gain"] for s in samples]
        cards[card] = dict(
            median=statistics.median(gains) if gains else None,
            range=[min(gains), max(gains)] if gains else None,
            n=len(samples),
            samples=samples,
            missing_reason=None
            if gains
            else "No verified coding-arm grades for this card in the specified sources",
        )
    return dict(
        schema_version="eidos-reference-gains/1",
        arms=[1, 2, 3],
        statistic="median and min/max of verified numeric test gains, pooled over runs and coding arms",
        pilot3_snapshot="Partial snapshot of locally completed grades; regenerate as the pilot finishes",
        sources=sources,
        cards=cards,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pilot3-grading", type=Path, default=Path("/Volumes/Mrigesh SSD/eidos-v1/pilot-3/grading")
    )
    parser.add_argument(
        "--pilot2-results",
        type=Path,
        default=Path.home() / "eidos-internal/pilot-2/regrade-addendum4/results-corrected.json",
    )
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("reference_gains.json"))
    args = parser.parse_args()
    write_json(args.out, regenerate(args.pilot3_grading, args.pilot2_results))


if __name__ == "__main__":
    main()
