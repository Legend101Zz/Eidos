#!/usr/bin/env python3
"""Tier-1 development mini-eval using the pilot launcher, adapters and local grader."""
from __future__ import annotations

import argparse
import hashlib
import os
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from study.common import ENVELOPE, ROOT, read_json, research_python, write_json
from study.eidos_arms import lab
from study.evals.records import inspect_run
from study.evals.regenerate_references import CARDS
from study.grade_block import grade_block
from study.launcher import plan, run_block

SCHEMA = "eidos-mini-eval/1"
FORBIDDEN_PORTS = {28767, 25546, 27635, 28766, 25544, 27633, 28765}


def writable_path(path):
    path = Path(path).absolute()
    # Check both spelling and resolution so a symlink cannot route output into a live pilot.
    for candidate in (path, path.resolve()):
        if any(part.startswith("pilot-3") or part in {"p3", "r4"} for part in candidate.parts):
            raise ValueError(f"eval output cannot use a live-pilot path {path}")
    return path.resolve()


def configure_lab(lab_home, lab_python, stub):
    lab_home = writable_path(lab_home)
    lab_python = str(Path(lab_python).absolute())  # Preserve the venv's interpreter symlink.
    os.environ.update(EIDOS_STUDY_HOME=str(lab_home), EIDOS_STUDY_PYTHON=lab_python,
                      EIDOS_STUDY_ROOT=str(lab_home.parent))
    build = lab.build_record()
    if not stub:
        config = read_json(lab_home / "config/lab.json")
        ports = read_json(lab_home.parent / "ports.json")
        selected = [ports[key] for key in ("api", "pg", "temporal", "mcp")]
        if len(set(selected)) != 4 or any(p in FORBIDDEN_PORTS or not 1024 <= p <= 65535 for p in selected):
            raise ValueError("eval lab requires four distinct, unreserved ports")
        if config.get("lab_python") != lab_python:
            raise ValueError("eval lab config interpreter differs from --lab-python")
        for key in ("api", "pg", "temporal"):
            if config[key + "_port"] != ports[key]:
                raise ValueError("eval lab config differs from ports.json")
        os.environ["EIDOS_STUDY_MCP_PORT"] = str(ports["mcp"])
    return build


def summarize(rows):
    ratios = [r["gain_ratio"] for r in rows if r["gain_ratio"] is not None]
    return dict(
        runs=len(rows), concluded=sum(r["concluded"] for r in rows),
        verified=sum(r["verified_success"] for r in rows),
        crash=sum(r["run_status"] == "crash" for r in rows),
        timeout=sum(r["run_status"] == "timeout" for r in rows),
        token_budget=sum(r["run_status"] == "token_budget" for r in rows),
        incident=sum(r["status"] == "incident" for r in rows),
        invalid_output=sum(r["status"] == "invalid_output" for r in rows),
        dead_ends=sum(r["invariant_flags"]["dead_end"] for r in rows),
        invariant_counts={flag: sum(r["invariant_flags"][flag] for r in rows)
                          for flag in ("dead_end", "runtime_deadline_limit", "fabricated_empty_wait", "held_out_reads")},
        tokens=sum((r.get("tokens") or {}).get("total", 0) for r in rows),
        wall_s=sum(r.get("wall_s") or 0 for r in rows),
        median_gain_ratio=statistics.median(ratios) if ratios else None,
        gain_ratio_cards=len(ratios),
    )


def scorecard(out, cards, arms, seed_index, stub, build, reference_path):
    references = read_json(reference_path)
    rows = []
    for card in cards:
        for arm in arms:
            run_dir = out / "runs" / f"{card}-r{seed_index}" / f"arm{arm}"
            record = read_json(run_dir / "run_record.json")
            grade = read_json(out / "grading" / f"{card}-r{seed_index}" / f"arm{arm}/grade.json")
            observations = inspect_run(run_dir)
            gain = (grade.get("numeric") or {}).get("gain")
            verified = grade["verified_success"]
            reference = references["cards"][card]
            median = reference["median"]
            ratio = (gain if verified else 0.0) / median if median is not None and median > 0 else None
            failures = []
            if grade["status"] != "valid":
                failures.append(grade["status"])
            if not observations["concluded"]:
                failures.append("no_conclusion")
            if not verified:
                failures.append("unverified")
            failures.extend(flag for flag, value in observations["invariant_flags"].items() if value)
            rows.append(dict(
                card=card, arm=arm, seed_index=seed_index, status=grade["status"], run_status=record["status"],
                verified_success=verified, test_gain=gain, verified_test_gain=gain if verified else None,
                gain_ratio=ratio, reference_median=median, reference_range=reference["range"],
                reference_missing_reason=reference.get("missing_reason"),
                tokens=record.get("tokens"), wall_s=record.get("wall_s"),
                failure_classes=failures, reason=grade.get("reason") or record.get("incident_reason"),
                **observations,
            ))
    build_path = Path(lab.python()).parent.parent / "pilot-build.json"
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    arm4 = [r for r in rows if r["arm"] == 4]
    complete_ladder = set(cards) == set(CARDS) and len(cards) == len(CARDS)
    return dict(
        schema_version=SCHEMA, generated_at=datetime.now(UTC).isoformat(), stub=stub,
        purpose="Development tuning data", cards=list(cards), arms=list(arms), seed_index=seed_index,
        envelope=ENVELOPE, concurrency=2,
        provenance=dict(git_sha=git_sha, git_dirty=bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip()),
                        pilot_build_sha256=hashlib.sha256(build_path.read_bytes()).hexdigest(),
                        pilot_build=build, reference_gains_sha256=hashlib.sha256(Path(reference_path).read_bytes()).hexdigest()),
        gain_ratio_policy="Verified test gain divided by coding-arm median; unverified runs contribute zero; unavailable/nonpositive reference medians yield null",
        runs=rows, aggregate=summarize(rows), by_arm={str(arm): summarize([r for r in rows if r["arm"] == arm]) for arm in arms},
        tier1=dict(full_development_set=complete_ladder,
                   completion_pass=complete_ladder and all(r["concluded"] for r in arm4),
                   no_dead_ends=all(not r["invariant_flags"]["dead_end"] for r in arm4),
                   comparison_required=True, eligible=not stub),
    )


def markdown(card):
    def number(value):
        return "—" if value is None else f"{value:.4f}"
    summary = card["by_arm"]["4"]
    lines = ["# Tier-1 development mini-eval", "", f"Git `{card['provenance']['git_sha']}`. Stub `{card['stub']}`.", "",
             f"Build receipt SHA-256 `{card['provenance']['pilot_build_sha256']}`.", "",
             f"Arm 4 concluded {summary['concluded']}/{summary['runs']}, verified {summary['verified']}, "
             f"dead ends {summary['dead_ends']}, median gain ratio {number(summary['median_gain_ratio'])}.", "",
             "| Card | Arm | Status | Concluded | Verified | Test gain | Gain ratio | Tokens | Wall s | Unused s | Flags |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in card["runs"]:
        flags = ", ".join(k for k, v in row["invariant_flags"].items() if v) or "—"
        lines.append(f"| {row['card']} | {row['arm']} | {row['status']} / {row['run_status']} | {row['concluded']} | "
                     f"{row['verified_success']} | {number(row['test_gain'])} | {number(row['gain_ratio'])} | "
                     f"{(row['tokens'] or {}).get('total', '—')} | {number(row['wall_s'])} | "
                     f"{number(row['unused_time_at_conclusion_s'])} | {flags} |")
    lines += ["", "Unverified runs contribute zero to gain ratios. Missing references produce null ratios. "
              "Held-out detection scans known identifiers and digests on a best-effort basis.", "",
              "```json", __import__("json").dumps(card["aggregate"], indent=2), "```", ""]
    return "\n".join(lines)


def run_eval(cards, arms, seed_index, out, lab_home, lab_python, *, stub=False, reference_path=None):
    if not cards or len(set(cards)) != len(cards) or any(c not in CARDS for c in cards):
        raise ValueError("cards must be distinct development card IDs")
    if tuple(arms) not in {(4,), (2, 4)} or seed_index not in (1, 2, 3):
        raise ValueError("arms must be 4 or 2,4; seed-index must be 1, 2 or 3")
    research_python()
    out = writable_path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("output directory must be empty")
    if out == Path(lab_home).resolve() or out in Path(lab_home).resolve().parents or Path(lab_home).resolve() in out.parents:
        raise ValueError("run output and lab home must be separate directories")
    build = configure_lab(lab_home, lab_python, stub)
    os.environ.pop("EIDOS_STUDY_SMOKE_PROMPT", None)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[name] = "1"
    charter = "docs/experiments/pilot-3/CHARTER.md"
    os.environ["EIDOS_PILOT_CHARTER_PATH"] = charter
    os.environ["EIDOS_PILOT_CHARTER_COMMIT"] = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", charter], cwd=ROOT, text=True).strip()
    design = [dict(row, run=seed_index, block_id=f"{row['task']}-r{seed_index}")
              for row in plan(20261111, cards, runs=1) if row["arm"] in arms]
    write_json(out / "design.json", design)
    blocks = [[r for r in design if r["task"] == card] for card in cards]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(run_block, rows, out / "runs", stub) for rows in blocks]
        for job in jobs:
            job.result()
    for card in cards:
        grade_block(out / "runs" / f"{card}-r{seed_index}" / "block_record.json",
                    out / "grading", out / "results.json", arms=arms)
    result = scorecard(out, cards, arms, seed_index, stub, build,
                       reference_path or Path(__file__).with_name("reference_gains.json"))
    write_json(out / "scorecard.json", result)
    (out / "scorecard.md").write_text(markdown(result))
    if not stub:
        write_json(ROOT / "evals/history" / (result["provenance"]["git_sha"] + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default=",".join(CARDS))
    parser.add_argument("--arms", default="4", choices=("4", "2,4"))
    parser.add_argument("--seed-index", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--lab-home", required=True, type=Path)
    parser.add_argument("--lab-python", required=True, type=Path)
    parser.add_argument("--stub", action="store_true", help="existing baseline stubs; zero model calls and no lab startup")
    args = parser.parse_args()
    try:
        result = run_eval(args.cards.split(","), tuple(map(int, args.arms.split(","))), args.seed_index,
                          args.out, args.lab_home, args.lab_python, stub=args.stub)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"mini-eval failed {exc}\n")
    print(args.out / "scorecard.json")
    return int(any(row["status"] != "valid" for row in result["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())
