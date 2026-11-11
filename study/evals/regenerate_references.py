#!/usr/bin/env python3
"""Freeze development references from a reconciled Pilot 3 package and corrected Pilot 2 records."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CARDS = ("N01", "N02", "N03", "N04", "D02", "D03", "D04")


def regenerate(pilot3, pilot2):
    package = Path(pilot3)
    manifest_path = package / "manifest.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("complete") is not True:
        raise ValueError("Pilot 3 reference requires complete reconciled evidence")
    sources = [dict(source="pilot3/manifest.json", sha256=hashlib.sha256(manifest_raw).hexdigest())]
    snapshots = {}
    for name in ("design.json", "results-record.json"):
        path = package / name
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != manifest["output_sha256"][name]:
            raise ValueError(f"reconciled Pilot 3 artifact hash differs for {name}")
        sources.append(dict(source=f"pilot3/{name}", sha256=sha))
        snapshots[name] = json.loads(raw)

    def key(row):
        return row["task"], row["run"], row["arm"]

    expected = {(card, run, arm) for card in CARDS[:4] for run in range(1, 4) for arm in range(1, 5)}
    for name, snapshot in snapshots.items():
        if len(snapshot) != len(expected) or {key(row) for row in snapshot} != expected:
            raise ValueError(f"invalid 48-assignment Pilot 3 roster in {name}")
    rows = [(row, "pilot3/results-record.json") for row in snapshots["results-record.json"]]
    pilot2_raw = Path(pilot2).read_bytes()
    sources.append(
        dict(
            source="pilot2/results-corrected.json",
            sha256=hashlib.sha256(pilot2_raw).hexdigest(),
        )
    )
    corrected = [row for row in json.loads(pilot2_raw) if row["task"] in {"D02", "D03", "D04"}]
    if len({key(row) for row in corrected}) != len(corrected):
        raise ValueError("duplicate assignments in corrected Pilot 2 reference")
    rows.extend((row, "pilot2/results-corrected.json") for row in corrected)
    cards = {}
    for card in CARDS:
        samples = [
            dict(arm=r["arm"], run=r["run"], gain=r["numeric"]["gain"], source=source)
            for r, source in rows
            if r["task"] == card
            and r["arm"] in (1, 2, 3)
            and r.get("verified_success")
            and r.get("status") == "valid"
            and r.get("run_status") == "ok"
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
        pilot3_snapshot="Complete, status-gated verdict-of-record snapshot",
        pilot3_snapshot_complete=True,
        sources=sources,
        cards=cards,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot3-package", type=Path, required=True)
    parser.add_argument(
        "--pilot2-results",
        type=Path,
        default=Path.home() / "eidos-internal/pilot-2/regrade-addendum4/results-corrected.json",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    references = regenerate(args.pilot3_package, args.pilot2_results)
    with args.out.open("x") as stream:
        json.dump(references, stream, indent=2, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
