#!/usr/bin/env python3
"""Randomized paired blocks, concurrent launch and whole-block incident handling."""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study.common import (
    DEVELOPMENT,
    ENVELOPE,
    FAMILIES,
    MODEL,
    ROOT,
    STUDY,
    dev_command,
    read_json,
    research_python,
    write_json,
)
from study.run_arm import stop_process_tree, utc_now


def plan(seed, cards=None, runs=3):
    blocks = [(card, index) for card in (cards if cards is not None else ("D01", "D02", "D12")) for index in range(1, runs + 1)]
    random.Random(seed).shuffle(blocks)
    return [
        dict(
            family=FAMILIES[card],
            task=card,
            run=index,
            arm=arm,
            primary_eligible=card in {"D02","D03","D04","D05","D06"},
            block_id=f"{card}-r{index}",
            block_order=order,
            randomization_seed=seed,
        )
        for order, (card, index) in enumerate(blocks, 1)
        for arm in (1, 2, 3, 4)
    ]


def version_guard(records):
    models = set(model for record in records for model in record.get("model_ids_seen", []))
    if len(models) != 1:
        return "block must contain exactly one returned model id; saw " + repr(sorted(models))
    if any(not record.get("model_ids_seen") for record in records):
        return "one or more runs did not record a returned model id"
    return None


def archive_block(block_dir, reason):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = block_dir.parent / "incidents" / f"{block_dir.name}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (block_dir / "block_record.json").exists():
        write_json(
            block_dir / "block_record.json",
            {"block_id": block_dir.name, "complete": True, "status": "incident", "incident_reason": reason},
        )
    else:
        old = read_json(block_dir / "block_record.json")
        if not old.get("complete"):
            old.update(complete=True, status="incident", incident_reason=reason, ended_at=utc_now())
            write_json(block_dir / "block_record.json", old)
    shutil.move(str(block_dir), target)
    return str(target)


def prepare(row, run_dir):
    free = int(subprocess.check_output(["df", "-k", "/"], text=True).splitlines()[1].split()[3])
    if free < 14 * 1024 * 1024: raise RuntimeError("STOP: less than 14 GiB free")
    run_dir.mkdir(parents=True, exist_ok=True)
    dev_command("package_task.py", "--task", row["task"], "--out", run_dir / "task")
    # The common structural interface contains no graders, labels or references.
    shutil.copyfile(DEVELOPMENT / "validate_answer.py", run_dir / "task/answer_schema.py")
    if row["arm"] in (2, 3, 4):
        shutil.copyfile(STUDY / "METHOD.md", run_dir / "METHOD.md")
    (run_dir / "out").mkdir()
    charter = ROOT / "research/public/STUDY_PROTOCOL.md"
    write_json(
        run_dir / "arm.json",
        dict(
            arm=row["arm"],
            card=row["task"],
            run_index=row["run"],
            block_id=row["block_id"],
            model=MODEL,
            envelope=ENVELOPE,
            charter_sha=hashlib.sha256(charter.read_bytes()).hexdigest(),
            charter_amendment_commit=os.environ["EIDOS_PILOT_CHARTER_COMMIT"],
            charter_amendment_sha256=hashlib.sha256(
                (ROOT / "docs/experiments/pilot-1/CHARTER.md").read_bytes()).hexdigest(),
        ),
    )


def run_block(rows, runs_root, stub=False, replace_incident=False):
    block_id = rows[0]["block_id"]
    block_dir = Path(runs_root).resolve() / block_id
    previous = None
    record_path = block_dir / "block_record.json"
    if record_path.exists():
        existing = read_json(record_path)
        if existing.get("complete") and not (replace_incident and existing.get("status") == "incident"):
            print(f"SKIP {block_id}: complete ({existing['status']})")
            return existing
        previous = archive_block(block_dir, "interrupted block; replace all paired arms")
    elif block_dir.exists():
        previous = archive_block(block_dir, "interrupted block before record publication")
    block_dir.mkdir(parents=True)
    run_dirs = {str(row["arm"]): str(block_dir / f"arm{row['arm']}") for row in rows}
    block = dict(
        block_id=block_id,
        card=rows[0]["task"],
        run_index=rows[0]["run"],
        complete=False,
        status="running",
        started_at=utc_now(),
        run_dirs=run_dirs,
        previous_incident=previous,
        stub=stub,
    )
    write_json(record_path, block)
    processes, logs = [], []
    try:
        for row in rows:
            prepare(row, Path(run_dirs[str(row["arm"])]))
        for row in rows:
            run_dir = Path(run_dirs[str(row["arm"])])
            script = "stub_agent.py" if stub else "run_arm.py"
            command = [
                research_python(),
                str(STUDY / script),
                "--arm",
                str(row["arm"]),
                "--run-dir",
                str(run_dir),
            ]
            log = (run_dir / "launcher.log").open("w")
            logs.append(log)
            processes.append(
                subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
            )
        deadline = time.monotonic() + ENVELOPE["wall_s"] + 60
        while any(process.poll() is None for process in processes):
            if time.monotonic() > deadline:
                for process in processes:
                    if process.poll() is None:
                        stop_process_tree(process)
                break
            time.sleep(0.2)
        records = []
        for row in rows:
            root = Path(run_dirs[str(row["arm"])])
            if not (root / "run_record.json").exists():
                now = utc_now()
                write_json(
                    root / "run_record.json",
                    dict(
                        arm=row["arm"],
                        card=row["task"],
                        run_index=row["run"],
                        block_id=block_id,
                        status="incident",
                        incident_reason="arm infrastructure exited without a record",
                        started_at=now,
                        ended_at=now,
                        wall_s=0,
                        tokens=dict(input=0, cache_read=0, cache_write=0, output=0, total=0),
                        billed_usd=None,
                        model_ids_seen=[],
                        agent_claim="",
                        transcript=str(root / "transcript.jsonl"),
                    ),
                )
            records.append(read_json(root / "run_record.json"))
        reason = version_guard(records)
        incident = reason or next(
            (
                r.get("incident_reason") or "arm infrastructure incident"
                for r in records
                if r["status"] == "incident"
            ),
            None,
        )
        if incident:
            for run_dir, record in zip(run_dirs.values(), records, strict=True):
                record["pre_block_status"] = record["status"]
                record["status"] = "incident"
                record["incident_reason"] = incident
                write_json(Path(run_dir) / "run_record.json", record)
        block.update(
            complete=True,
            status="incident" if incident else "ok",
            incident_reason=incident,
            ended_at=utc_now(),
            model_ids_seen=sorted({m for r in records for m in r["model_ids_seen"]}),
        )
        write_json(record_path, block)
        print(f"BLOCK {block_id}: {block['status']}")
        return block
    except BaseException as exc:
        for process in processes:
            if process.poll() is None:
                stop_process_tree(process)
        block.update(
            complete=True,
            status="incident",
            incident_reason=f"launcher: {type(exc).__name__}: {exc}",
            ended_at=utc_now(),
        )
        write_json(record_path, block)
        raise
    finally:
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    planning = sub.add_parser("plan")
    planning.add_argument("--cards", help="comma-separated card IDs")
    planning.add_argument("--runs", type=int, default=3)
    planning.add_argument("--seed", required=True, type=int)
    planning.add_argument("--out", required=True, type=Path)
    running = sub.add_parser("run")
    running.add_argument("--concurrency", type=int, default=1, choices=(1,2))
    running.add_argument("--design", required=True, type=Path)
    running.add_argument("--runs-root", required=True, type=Path)
    running.add_argument("--block", help="1-based randomized block order, or stable ID such as D02-r1")
    running.add_argument(
        "--stub", action="store_true", help="no model calls, all four arms use baseline stubs"
    )
    running.add_argument(
        "--replace-incident", action="store_true", help="archive incident and re-run all four arms"
    )
    args = parser.parse_args()
    if args.command == "plan":
        cards = args.cards.split(",") if args.cards else None
        if args.runs < 1 or (cards and (len(set(cards)) != len(cards) or any(c not in FAMILIES for c in cards))): parser.error("invalid cards or runs")
        design = plan(args.seed, cards, args.runs)
        write_json(args.out, design)
        print(f"Planned {len(design)//4} randomized paired blocks, {len(design)} assignments.")
        return
    design = read_json(args.design)
    blocks = {}
    for row in design:
        blocks.setdefault(row["block_id"], []).append(row)
    selected = list(blocks)
    if args.block:
        selected = (
            [list(blocks)[int(args.block) - 1]]
            if args.block.isdecimal() and 1 <= int(args.block) <= len(blocks)
            else [args.block]
        )
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    for block_id in selected:
        if block_id not in blocks:
            parser.error(f"unknown block {block_id}")
        rows = blocks[block_id]
        if sorted(r["arm"] for r in rows) != [1, 2, 3, 4]:
            parser.error("each block must contain exactly four arms")
        jobs.append(pool.submit(run_block, rows, args.runs_root, args.stub, args.replace_incident))
    try:
        for job in jobs: job.result()
    finally:
        pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
