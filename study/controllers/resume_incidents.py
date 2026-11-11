"""Admit only completed Pilot 3 incident blocks to the unchanged paired launcher."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
from threading import Event
import time

DESIGN_SHA256 = "f44a174c5bdcd3e1595ae61894b9cee215a37de38b3a25e67ef1d79d3dce5903"
CUTOFF = int(datetime.fromisoformat("2026-11-12T03:00:00+05:30").timestamp())
MIN_FREE = int(12.5 * 1024**3)
CONCURRENCY = 2


def read_design(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DESIGN_SHA256:
        raise ValueError("design.json differs from the original Pilot 3 assignment file")
    blocks = {}
    for row in json.loads(raw):
        blocks.setdefault(row["block_id"], []).append(row)
    return blocks


def eligible_record(rows, runs_root):
    row = rows[0]
    path = runs_root / row["block_id"] / "block_record.json"
    try:
        raw = path.read_bytes()
        record = json.loads(raw)
    except (FileNotFoundError, ValueError):
        return None
    if (
        not isinstance(record, dict)
        or record.get("complete") is not True
        or record.get("status") != "incident"
        or record.get("stub") is not False
        or record.get("block_id") != row["block_id"]
        or record.get("card") != row["task"]
        or record.get("run_index") != row["run"]
        or not record.get("started_at")
        or not record.get("ended_at")
    ):
        return None
    return record["started_at"], hashlib.sha256(raw).hexdigest()


def archive_marker(marker, history, identity):
    """Archive any old dispatch marker and retain the previous attempt identity."""
    attempt = history / marker.name / identity[1]
    attempt.mkdir(parents=True, exist_ok=False)
    receipt = {"block_id": marker.name, "previous_started_at": identity[0],
               "previous_block_record_sha256": identity[1], "previous_marker_present": marker.exists()}
    (attempt / "admission.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if receipt["previous_marker_present"]:
        marker.rename(attempt / "sweep-hosted-marker")
    return attempt


def dispatch(blocks, runs_root, markers, history, cutoff, run_block, *,
             now=time.time, free_bytes=lambda: shutil.disk_usage("/").free):
    """Submit one eligible block at a time when a paired slot is free."""
    candidates = [(rows, identity) for rows in blocks.values()
                  if (identity := eligible_record(rows, runs_root)) is not None]

    def admit(rows, identity, checked):
        block = rows[0]["block_id"]
        marker = markers / block
        try:
            if now() >= cutoff or eligible_record(rows, runs_root) != identity:
                return
            if free_bytes() < MIN_FREE:
                raise RuntimeError("internal disk is below the 12.5 GiB admission floor")
            attempt = archive_marker(marker, history, identity)
            # Check after marker I/O, at the handoff to the original launcher.
            if eligible_record(rows, runs_root) != identity or now() >= cutoff:
                archived_marker = attempt / "sweep-hosted-marker"
                if archived_marker.exists():
                    archived_marker.rename(marker)
                (attempt / "not-admitted").touch()
                return
            print(f"ADMIT {block} previous_started_at={identity[0]}", flush=True)
        finally:
            checked.set()
        run_block(rows, runs_root, stub=False, replace_incident=True)

    running = set()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for rows, identity in candidates:
            if len(running) == CONCURRENCY:
                done, running = wait(running, return_when=FIRST_COMPLETED)
                for job in done:
                    job.result()
            if now() >= cutoff:
                break
            checked = Event()
            running.add(pool.submit(admit, rows, identity, checked))
            # Preserve admission order even if the two workers start on different schedules.
            checked.wait()
        for job in running:
            job.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "dispatch", "cutoff"))
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--history", type=Path)
    args = parser.parse_args()
    if args.command == "cutoff":
        print(CUTOFF)
        return
    if args.pilot is None or (args.command == "dispatch" and args.history is None):
        parser.error("list and dispatch require --pilot, and dispatch requires --history")
    pilot = args.pilot.resolve()
    blocks = read_design(pilot / "design.json")
    runs = pilot / "runs"
    if args.command == "list":
        for block, rows in blocks.items():
            if eligible_record(rows, runs):
                print(block)
        return
    from study.launcher import run_block

    dispatch(blocks, runs, pilot / "sweep-hosted", args.history, CUTOFF, run_block)


if __name__ == "__main__":
    main()
