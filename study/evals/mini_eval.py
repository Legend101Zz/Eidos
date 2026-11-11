#!/usr/bin/env python3
"""Tier-1 development mini-eval using the pilot launcher, adapters and local grader."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from study.common import DEVELOPMENT, ENVELOPE, MODEL, ROOT, STUDY, read_json, research_python, write_json
from study.eidos_arms import lab
from study.evals.policy import POLICY
from study.evals.provenance import EVAL_ROOT, controller_record, digest, disk_guard, verify_build
from study.evals.records import REQUIRED_INVARIANTS, inspect_run
from study.evals.regenerate_references import CARDS
from study.grade_block import grade_block
from study.launcher import plan, run_block

SCHEMA = "eidos-mini-eval/2"
SSD_ROOT = EVAL_ROOT.parent
FORBIDDEN_PORTS = {28767, 25546, 28766, 25544, 28765} | set(range(27633, 27644))


def writable_path(path):
    path = Path(path).absolute()
    # Check both spelling and resolution so a symlink cannot route output into a live pilot.
    for candidate in (path, path.resolve()):
        if any(part.startswith("pilot-3") or part in {"p3", "r4"} for part in candidate.parts):
            raise ValueError(f"eval output cannot use a live-pilot path {path}")
    return path.resolve()


def configure_lab(lab_home, lab_python, stub, expected_build_sha=None):
    lab_home = writable_path(lab_home)
    lab_python = str(Path(lab_python).absolute())  # Preserve the venv's interpreter symlink.
    os.environ.update(
        EIDOS_STUDY_HOME=str(lab_home), EIDOS_STUDY_PYTHON=lab_python, EIDOS_STUDY_ROOT=str(lab_home.parent)
    )
    build = lab.build_record()
    if not stub:
        verify_build(ROOT, lab_home, Path(lab_python), expected_build_sha, build)
        config = read_json(lab_home / "config/lab.json")
        ports = read_json(lab_home.parent / "ports.json")
        selected = [ports[key] for key in ("api", "pg", "temporal", "mcp")]
        if len(set(selected)) != 4 or any(p in FORBIDDEN_PORTS or not 1024 <= p <= 65535 for p in selected):
            raise ValueError("eval lab requires four distinct, unreserved ports")
        if any(not 33000 <= ports[key] <= 58999 for key in ("api", "pg", "mcp")):
            raise ValueError("eval lab service ports differ from the dedicated allocation range")
        temporal_block = set(range(ports["temporal"], ports["temporal"] + 9))
        if (
            min(temporal_block) < 30000
            or max(temporal_block) > 32767
            or temporal_block.intersection(FORBIDDEN_PORTS)
        ):
            raise ValueError("Temporal requires nine unreserved ports ending at or below 32767")
        if temporal_block.intersection(ports[key] for key in ("api", "pg", "mcp")):
            raise ValueError("eval service overlaps the Temporal port block")
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
        runs=len(rows),
        concluded=sum(r["concluded"] for r in rows),
        verified=sum(r["verified_success"] for r in rows),
        crash=sum(r["run_status"] == "crash" for r in rows),
        timeout=sum(r["run_status"] == "timeout" for r in rows),
        token_budget=sum(r["run_status"] == "token_budget" for r in rows),
        incident=sum(r["status"] == "incident" for r in rows),
        invalid_output=sum(r["status"] == "invalid_output" for r in rows),
        dead_ends=sum(r["invariant_flags"]["dead_end"] is True for r in rows),
        controller_abstentions=sum(r["disposition"] == "controller_abstention" for r in rows),
        invariant_counts={
            flag: sum(r["invariant_flags"][flag] is True for r in rows)
            for flag in ("dead_end", "runtime_deadline_limit", "fabricated_empty_wait", "held_out_reads")
        },
        invariant_unknown_counts={
            flag: sum(r["invariant_flags"][flag] is None for r in rows) for flag in REQUIRED_INVARIANTS
        },
        tokens=sum(r["tokens"]["total"] for r in rows if r.get("tokens") is not None),
        token_receipts=sum(r.get("tokens") is not None for r in rows),
        uncertain_token_receipts=sum(r.get("usage_uncertain") is True for r in rows),
        wall_s=sum(r["wall_s"] for r in rows if r.get("wall_s") is not None),
        wall_receipts=sum(r.get("wall_s") is not None for r in rows),
        reference_cards=sum(r["reference_median"] is not None for r in rows),
        median_gain_ratio=statistics.median(ratios) if ratios else None,
        gain_ratio_cards=len(ratios),
    )


def scorecard(
    out,
    cards,
    arms,
    seed_index,
    stub,
    build,
    reference_path,
    controller,
    attempt_id,
    identifiers,
    audit_config_sha,
):
    references = read_json(reference_path)
    rows = []
    for card in cards:
        for arm in arms:
            run_dir = out / "runs" / f"{card}-r{seed_index}" / f"arm{arm}"
            record = read_json(run_dir / "run_record.json")
            grade = read_json(out / "grading" / f"{card}-r{seed_index}" / f"arm{arm}/grade.json")
            observations = inspect_run(run_dir, identifiers.get(card, []))
            gain = (grade.get("numeric") or {}).get("gain")
            verified = grade["verified_success"]
            reference = references["cards"][card]
            median = reference["median"]
            ratio = None
            if median is not None and median > 0 and (gain is not None or not verified):
                ratio = (gain if verified else 0.0) / median
            failures = []
            if grade["status"] != "valid":
                failures.append(grade["status"])
            if not observations["concluded"]:
                failures.append("no_conclusion")
            if not verified:
                failures.append("unverified")
            failures.extend(flag for flag, value in observations["invariant_flags"].items() if value is True)
            failures.extend(
                f"{flag}_unknown"
                for flag in REQUIRED_INVARIANTS
                if observations["invariant_flags"][flag] is None
            )
            rows.append(
                dict(
                    card=card,
                    arm=arm,
                    seed_index=seed_index,
                    status=grade["status"],
                    run_status=record["status"],
                    verified_success=verified,
                    test_gain=gain,
                    verified_test_gain=gain if verified else None,
                    gain_ratio=ratio,
                    reference_median=median,
                    reference_range=reference["range"],
                    reference_missing_reason=reference.get("missing_reason"),
                    tokens=record.get("tokens"),
                    wall_s=record.get("wall_s"),
                    usage_uncertain=record.get("usage_uncertain"),
                    evidence_sha256={
                        name: digest(run_dir / name)
                        for name in ("run_record.json", "transcript.jsonl", "eval_provider_audit.jsonl")
                        if (run_dir / name).is_file()
                    },
                    grade_sha256=digest(out / "grading" / f"{card}-r{seed_index}" / f"arm{arm}/grade.json"),
                    failure_classes=failures,
                    reason=grade.get("reason") or record.get("incident_reason"),
                    **observations,
                )
            )
    build_path = Path(lab.python()).parent.parent / "pilot-build.json"
    arm4 = [r for r in rows if r["arm"] == 4]
    complete_ladder = set(cards) == set(CARDS) and len(cards) == len(CARDS)
    return dict(
        schema_version=SCHEMA,
        generated_at=datetime.now(UTC).isoformat(),
        stub=stub,
        purpose="Development tuning data",
        cards=list(cards),
        arms=list(arms),
        seed_index=seed_index,
        envelope=ENVELOPE,
        model=MODEL,
        concurrency=2,
        provenance=dict(
            **controller,
            attempt_id=attempt_id,
            design_sha256=digest(out / "design.json"),
            expected_build_sha=None if stub else build["eidos_commit_sha"],
            provider_audit_identifiers_sha256=audit_config_sha,
            pilot_build_sha256=hashlib.sha256(build_path.read_bytes()).hexdigest(),
            pilot_build=build,
            reference_gains_sha256=hashlib.sha256(Path(reference_path).read_bytes()).hexdigest(),
        ),
        comparison_policy=POLICY,
        gain_ratio_policy="Verified test gain divided by coding-arm median. Unverified runs contribute zero. Missing or nonpositive reference medians yield null.",
        runs=rows,
        aggregate=summarize(rows),
        by_arm={str(arm): summarize([r for r in rows if r["arm"] == arm]) for arm in arms},
        tier1=dict(
            full_development_set=complete_ladder,
            completion_pass=complete_ladder and all(r["concluded"] for r in arm4),
            no_dead_ends=all(r["invariant_flags"]["dead_end"] is False for r in arm4),
            invariant_pass=all(
                r["invariant_flags"][flag] is False for r in rows for flag in REQUIRED_INVARIANTS
            ),
            coverage_complete=all(
                r["invariant_flags"][flag] is not None for r in rows for flag in REQUIRED_INVARIANTS
            ),
            run_evidence_pass=all(r["status"] == "valid" and r["run_status"] == "ok" for r in arm4),
            comparison_required=True,
            eligible=not stub,
        ),
    )


def markdown(card):
    def number(value):
        return "—" if value is None else f"{value:.4f}"

    summary = card["by_arm"]["4"]
    lines = [
        "# Tier-1 development mini-eval",
        "",
        f"Git `{card['provenance']['git_sha']}`. Stub `{card['stub']}`.",
        "",
        f"Build receipt SHA-256 `{card['provenance']['pilot_build_sha256']}`.",
        "",
        f"Arm 4 concluded {summary['concluded']}/{summary['runs']}, verified {summary['verified']}, "
        f"dead ends {summary['dead_ends']}, median gain ratio {number(summary['median_gain_ratio'])}.",
        "",
        "| Card | Arm | Status | Concluded | Verified | Test gain | Gain ratio | Tokens | Wall s | Unused s | Flags |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in card["runs"]:
        flags = (
            ", ".join(
                k if v is True else f"{k} unknown"
                for k, v in row["invariant_flags"].items()
                if v is not False
            )
            or "—"
        )
        lines.append(
            f"| {row['card']} | {row['arm']} | {row['status']} / {row['run_status']} | {row['disposition']} | "
            f"{row['verified_success']} | {number(row['test_gain'])} | {number(row['gain_ratio'])} | "
            f"{(row['tokens'] or {}).get('total', '—')} | {number(row['wall_s'])} | "
            f"{number(row['unused_time_at_conclusion_s'])} | {flags} |"
        )
    lines += [
        "",
        "Unverified runs contribute zero to gain ratios. Missing references produce null ratios. "
        "The identifier audit checks recorded model-facing requests. Missing coverage is unknown; derived exposure may evade detection.",
        "",
        "```json",
        json.dumps(card["aggregate"], indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def audit_identifiers(cards):
    frozen = read_json(STUDY / "packaging/p3_frozen.json")
    identifiers = {}
    for card in cards:
        if card in frozen:
            identifiers[card] = sorted(set(frozen[card]["splits"]["test"]["hashes"].values()))
            continue
        task = read_json(DEVELOPMENT / "tasks" / f"{card}.json")
        code = """
import hashlib, io, json, sys
import numpy as np
sys.path.insert(0, sys.argv[1])
from data import dataset
(x, y, ids) = dataset(sys.argv[2], seed=int(sys.argv[3]))[0]['test']
values = [x.tobytes(), y.tobytes(), ids.tobytes()]
for dump, fields in ((np.savez, dict(x=x, ids=ids)), (np.savez_compressed, dict(x=x, ids=ids)),
                     (np.savez, dict(ids=ids, y=y))):
    buffer = io.BytesIO()
    dump(buffer, **fields)
    values.append(buffer.getvalue())
print(json.dumps(sorted({hashlib.sha256(value).hexdigest() for value in values})))
"""
        identifiers[card] = json.loads(
            subprocess.check_output(
                [research_python(), "-c", code, str(DEVELOPMENT), task["project"], str(task["dataset_seed"])],
                text=True,
            )
        )
    return identifiers


def snapshot_references(path, expected, *, live):
    content = Path(path).read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    references = json.loads(content)
    if references.get("schema_version") != "eidos-reference-gains/1" or set(references["cards"]) != set(
        CARDS
    ):
        raise ValueError("reference snapshot must list every development card, including unavailable values")
    if live and (expected != sha or references.get("pilot3_snapshot_complete") is not True):
        raise ValueError(
            "live evaluation requires the explicit hash of the complete reconciled reference snapshot"
        )
    return content


def run_eval(
    cards,
    arms,
    seed_index,
    out,
    lab_home,
    lab_python,
    *,
    stub=False,
    reference_path=None,
    reference_sha=None,
    expected_build_sha=None,
):
    if not cards or len(set(cards)) != len(cards) or any(c not in CARDS for c in cards):
        raise ValueError("cards must be distinct development card IDs")
    if tuple(arms) not in {(4,), (2, 4)} or seed_index not in (1, 2, 3):
        raise ValueError("Arms must be 4 or 2,4 and seed-index must be 1, 2 or 3")
    disk_guard()
    research_python()
    controller = controller_record(ROOT, live=not stub)
    out = writable_path(out)
    if out.exists():
        raise ValueError("output directory must be empty and newly allocated")
    if not stub and not out.is_relative_to(SSD_ROOT.resolve()):
        raise ValueError("live eval output must be on the external SSD")
    if (
        out == Path(lab_home).resolve()
        or out in Path(lab_home).resolve().parents
        or Path(lab_home).resolve() in out.parents
    ):
        raise ValueError("run output and lab home must be separate directories")
    build = configure_lab(lab_home, lab_python, stub, expected_build_sha)
    reference = snapshot_references(
        reference_path or Path(__file__).with_name("reference_gains.json"), reference_sha, live=not stub
    )
    out.mkdir(parents=True, exist_ok=False)
    (out / "reference-gains.json").write_bytes(reference)
    attempt_id = uuid.uuid4().hex
    identifiers = {}
    audit_config_sha = None
    os.environ.pop("EIDOS_EVAL_AUDIT_IDENTIFIERS", None)
    history = None
    build_receipt = Path(lab.python()).parent.parent / "pilot-build.json"
    initial_build_receipt_sha = digest(build_receipt)
    if not stub:
        cache = SSD_ROOT / "tmp"
        for key, name in {
            "TMPDIR": "tier1-tmp",
            "XDG_CACHE_HOME": "tier1-cache",
            "UV_CACHE_DIR": "uv-cache",
            "PIP_CACHE_DIR": "pip-cache",
            "PYTHONPYCACHEPREFIX": "pycache",
        }.items():
            directory = cache / name
            directory.mkdir(parents=True, exist_ok=True)
            os.environ[key] = str(directory)
        history = (
            SSD_ROOT
            / "evals-history"
            / build["eidos_commit_sha"]
            / (f"{controller['git_sha']}-seed{seed_index}-{attempt_id}")
        )
        history.mkdir(parents=True, mode=0o700, exist_ok=False)
        write_json(
            history / "attempt.json",
            {
                **controller,
                "attempt_id": attempt_id,
                "seed_index": seed_index,
                "out": str(out),
                "cards": list(cards),
                "arms": list(arms),
                "pilot_build": build,
                "pilot_build_sha256": initial_build_receipt_sha,
                "reference_gains_sha256": hashlib.sha256(reference).hexdigest(),
            },
        )
        identifiers = audit_identifiers(cards)
        config = history / "audit-identifiers.json"
        write_json(config, identifiers)
        config.chmod(0o600)
        audit_config_sha = digest(config)
    os.environ.pop("EIDOS_STUDY_SMOKE_PROMPT", None)
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[name] = "1"
    charter = "docs/experiments/pilot-3/CHARTER.md"
    os.environ["EIDOS_PILOT_CHARTER_PATH"] = charter
    os.environ["EIDOS_PILOT_CHARTER_COMMIT"] = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", charter], cwd=ROOT, text=True
    ).strip()
    design = [
        dict(row, run=seed_index, block_id=f"{row['task']}-r{seed_index}")
        for row in plan(20261111, cards, runs=1)
        if row["arm"] in arms
    ]
    write_json(out / "design.json", design)
    blocks = {}
    for row in design:
        blocks.setdefault(row["task"], []).append(row)

    def guarded_run(rows):
        disk_guard()
        return run_block(rows, out / "runs", stub)

    if history is not None:
        os.environ["EIDOS_EVAL_AUDIT_IDENTIFIERS"] = str(history / "audit-identifiers.json")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(guarded_run, rows) for rows in blocks.values()]
            for job in jobs:
                job.result()
    finally:
        os.environ.pop("EIDOS_EVAL_AUDIT_IDENTIFIERS", None)
    for card in cards:
        disk_guard()
        grade_block(
            out / "runs" / f"{card}-r{seed_index}" / "block_record.json",
            out / "grading",
            out / "results.json",
            arms=arms,
        )
    disk_guard()
    if not stub and (
        controller_record(ROOT, live=True) != controller or digest(build_receipt) != initial_build_receipt_sha
    ):
        raise ValueError("controller or build receipt changed during evaluation")
    result = scorecard(
        out,
        cards,
        arms,
        seed_index,
        stub,
        build,
        out / "reference-gains.json",
        controller,
        attempt_id,
        identifiers,
        audit_config_sha,
    )
    write_json(out / "scorecard.json", result)
    (out / "scorecard.md").write_text(markdown(result))
    if history is not None:
        with (history / "scorecard.json").open("xb") as stream:
            stream.write((out / "scorecard.json").read_bytes())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default=",".join(CARDS))
    parser.add_argument("--arms", default="4", choices=("4", "2,4"))
    parser.add_argument("--seed-index", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--lab-home", required=True, type=Path)
    parser.add_argument("--lab-python", required=True, type=Path)
    parser.add_argument("--expected-build-sha")
    parser.add_argument("--reference-path", type=Path)
    parser.add_argument("--reference-sha256")
    parser.add_argument(
        "--preflight", action="store_true", help="verify live prerequisites without starting a lab"
    )
    parser.add_argument(
        "--stub", action="store_true", help="Run existing baseline stubs with zero model calls"
    )
    args = parser.parse_args()
    try:
        if args.preflight:
            if args.stub or args.reference_path is None:
                raise ValueError("preflight requires live build and reference inputs")
            disk_guard()
            controller_record(ROOT, live=True)
            snapshot_references(args.reference_path, args.reference_sha256, live=True)
            configure_lab(args.lab_home, args.lab_python, False, args.expected_build_sha)
            print("Live build and reference preflight passed. No model or lab was started.")
            return 0
        if args.out is None:
            raise ValueError("--out is required for an evaluation attempt")
        result = run_eval(
            args.cards.split(","),
            tuple(map(int, args.arms.split(","))),
            args.seed_index,
            args.out,
            args.lab_home,
            args.lab_python,
            stub=args.stub,
            reference_path=args.reference_path,
            reference_sha=args.reference_sha256,
            expected_build_sha=args.expected_build_sha,
        )
    except (KeyError, ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"mini-eval failed {exc}\n")
    print(args.out / "scorecard.json")
    invalid_grade = any(row["status"] != "valid" for row in result["runs"])
    gate = result["tier1"]
    return int(
        invalid_grade
        or (
            not args.stub
            and not all(gate[key] for key in ("completion_pass", "invariant_pass", "run_evidence_pass"))
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
