#!/usr/bin/env python3
"""Controller-only development grading; candidate execution and scoring are separate."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import UTC

from study.common import DEVELOPMENT, FAMILIES, dev_command, read_json, research_python, write_json
from study.fresh_check import candidate_command, fresh_check, infer


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()



# Charter (docs/experiments/pilot-1/CHARTER.md, D12): only an improvement claim is a false claim on the null card.
# planted_diagnosis / operational_reliability / no_positive_claim are not improvement claims (addendum 1).
IMPROVEMENT_CLAIMS = frozenset({"checkpoint_improvement", "recipe_improvement", "architecture_improvement", "transfer"})

def success_rule(card, answer, numeric=None, reproduces=False, guardrails=False, scope_ok=False):
    """Pure, independently testable card rules. Null correctness stays out of primary success."""
    if card["id"] == "D01":
        passed = bool(reproduces and guardrails)
        return dict(
            verified_success=passed,
            card_success=passed,
            operational_pass=passed,
            null_correct=None,
            false_positive_claim=False,
        )
    if card["id"] == "D12":
        positive = answer["status"] == "improvement_claimed" or answer["claim_type"] in IMPROVEMENT_CLAIMS
        correct = not positive and answer["status"] in {"abstained", "no_improvement_found", "inconclusive"}
        return dict(
            verified_success=False,
            card_success=correct,
            operational_pass=None,
            null_correct=correct,
            false_positive_claim=positive,
        )
    positive = answer["status"] == "improvement_claimed"
    passed = bool(
        positive
        and numeric is not None
        and numeric["gain"] >= card["min_reference_gain"]
        and reproduces
        and guardrails
        and scope_ok
    )
    return dict(
        verified_success=passed,
        card_success=passed,
        operational_pass=None,
        null_correct=None,
        false_positive_claim=positive and not passed,
    )


def verify_artifacts(out, answer):
    checks = [(e["path"], e["sha256"]) for e in answer["evidence"]]
    c = answer["candidate"]
    if c:
        checks += [
            (c[key], c[key.replace("_path", "") + "_sha256"])
            for key in ("code_path", "model_path", "predictions_path")
        ]
        checks.append((c["environment_lock"], c["environment_lock_sha256"]))
    for relative, expected in checks:
        path = (out / relative).resolve()
        if not path.is_relative_to(out.resolve()) or not path.is_file() or digest(path) != expected:
            raise ValueError(f"missing, escaping or hash-mismatched artifact: {relative}")


def check_bundle_paths(out):
    # Never follow candidate-controlled links with controller permissions.
    if out.is_symlink():
        raise ValueError("candidate output directory is a symlink")
    for path in out.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("candidate bundle contains a symlink or special file")


def snapshot(out, workspace):
    check_bundle_paths(out)
    shutil.copytree(out, workspace / "candidate", symlinks=True)
    check_bundle_paths(workspace / "candidate")


def make_inputs(project, path):
    # Only this trusted subprocess owns labels. Its serialized export excludes y.
    code = (
        "import sys,numpy as np;sys.path.insert(0,sys.argv[1]);from data import dataset;"
        "d,_,_=dataset(sys.argv[2]);x,_,ids=d['test'];np.savez(sys.argv[3],x=x,ids=ids)"
    )
    import subprocess

    subprocess.run(
        [research_python(), "-c", code, str(DEVELOPMENT), project, str(path)],
        check=True,
        capture_output=True,
        timeout=120,
    )


def score(project, baseline, candidate):
    import subprocess

    try:
        result = dev_command(
            "grade.py",
            "--project",
            project,
            "--baseline",
            baseline,
            "--candidate",
            candidate,
            "--split",
            "test",
        )
    except subprocess.CalledProcessError as exc:
        if "ValueError:" in exc.stderr or "FileNotFoundError:" in exc.stderr:
            raise ValueError(
                "independent scorer rejected candidate predictions: " + exc.stderr[-2000:]
            ) from exc
        raise
    return json.loads(result.stdout)


def reproduction_command(command, code_path, inputs, output, seed):
    if len(command) < 3 or Path(command[0]).name not in {"python", "python3", "python3.12"}:
        raise ValueError("reproduce_command must invoke the submitted Python project directly")
    if command[1] != code_path or command[2] != "train":
        raise ValueError("reproduce_command must use candidate.code_path train")
    replaced = [research_python(), *command[1:]]
    substitutions = {"{inputs}": str(inputs), "{out}": str(output), "{seed}": str(seed)}
    replaced = [substitutions.get(token, token) for token in replaced]
    # Accept both placeholders and an ordinary recorded concrete command.
    for flag, value in (("--inputs", inputs), ("--out", output), ("--seed", seed)):
        if flag not in replaced or replaced.index(flag) + 1 >= len(replaced):
            raise ValueError("reproduce_command must declare " + flag)
        replaced[replaced.index(flag) + 1] = str(value)
    return replaced


def grade_run(run_dir, grading_dir, block_incident=None, expected_meta=None):
    root, destination = Path(run_dir), Path(grading_dir)
    destination.mkdir(parents=True, exist_ok=True)
    meta = expected_meta or read_json(root / "arm.json")
    record_path = root / "run_record.json"
    record = read_json(record_path) if record_path.exists() else {"status": "invalid_output"}
    result = dict(
        family=FAMILIES[meta["card"]],
        task=meta["card"],
        run=meta["run_index"],
        arm=meta["arm"],
        primary_eligible=meta["card"] in {"D02","D03","D04","D05","D06"},
        verified_success=False,
        card_success=False,
        status="invalid_output",
        billed_usd=record.get("billed_usd"),
        tokens=record.get("tokens"),
        wall_s=record.get("wall_s"),
        transcript=str(root / "transcript.jsonl"),
        run_status=record["status"],
        null_correct=None,
        operational_pass=None,
        false_positive_claim=False,
        grading_scope="public development pilot; exploratory; no confirmatory certification",
    )
    if block_incident or record["status"] == "incident":
        result.update(status="incident", reason=block_incident or record.get("incident_reason"))
    elif record["status"] != "ok":
        result.update(status=record["status"], reason="agent did not finish within the envelope")
    else:
        try:
            check_bundle_paths(root / "out")
            # Structural validation is a trusted data-only subprocess.
            import subprocess

            try:
                dev_command("validate_answer.py", root / "out/answer.json")
            except subprocess.CalledProcessError as exc:
                raise ValueError("answer validation failed: " + exc.stderr[-2000:]) from exc
            answer = read_json(root / "out/answer.json")
            if answer["task_id"] != meta["card"]:
                raise ValueError("answer task_id differs from assignment")
            verify_artifacts(root / "out", answer)
            card = read_json(DEVELOPMENT / "tasks" / f"{meta['card']}.json")
            candidate = answer["candidate"]
            numeric, reproduction, fresh = None, [], None
            reproduces, guardrails, scope_ok = False, False, False
            if candidate:
                workspace = destination / "workspace"
                if workspace.exists():
                    shutil.rmtree(workspace)
                workspace.mkdir()
                snapshot(root / "out", workspace)
                verify_artifacts(workspace / "candidate", answer)
                write_json(
                    destination / "bundle-manifest.json",
                    {
                        str(p.relative_to(workspace / "candidate")): digest(p)
                        for p in sorted((workspace / "candidate").rglob("*"))
                        if p.is_file()
                    },
                )
                # Regenerate trusted inputs. The agent could have edited run_dir/task.
                trusted_inputs = destination / "trusted-inputs"
                if trusted_inputs.exists():
                    shutil.rmtree(trusted_inputs)
                dev_command("package_task.py", "--task", meta["card"], "--out", trusted_inputs)
                shutil.copytree(trusted_inputs, workspace / "inputs")
                test_inputs = workspace / "test_inputs.npz"
                make_inputs(card["project"], test_inputs)
                expected_hash = digest(test_inputs)
                # Baseline is always the supplied functioning project, trained with seed 11.
                baseline = destination / "baseline"
                baseline.mkdir(exist_ok=True)
                import subprocess

                subprocess.run(
                    [
                        research_python(),
                        str(trusted_inputs / "project.py"),
                        "train",
                        "--inputs",
                        str(trusted_inputs),
                        "--out",
                        str(baseline),
                        "--seed",
                        "11",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                subprocess.run(
                    [
                        research_python(),
                        str(trusted_inputs / "project.py"),
                        "infer",
                        "--model",
                        str(baseline / "model.pt"),
                        "--inputs",
                        str(test_inputs),
                        "--out",
                        str(baseline / "test_predictions.npz"),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                code = workspace / "candidate" / candidate["code_path"]
                model = workspace / "candidate" / candidate["model_path"]
                locked_validation = destination / "locked_validation.npz"
                shutil.copyfile(workspace / "candidate" / candidate["predictions_path"], locked_validation)
                fresh = fresh_check(
                    workspace,
                    code,
                    model,
                    workspace / "inputs/validation.npz",
                    locked_validation,
                    test_inputs,
                )
                reproduces = fresh["fresh_process_export_equal"]
                numeric = score(card["project"], baseline / "test_predictions.npz", fresh["prediction_path"])
                # Cards specify no critical slices. Enforce their actual data/CPU/format boundaries;
                # do not invent a slice threshold from the protocol's explicitly draft defaults.
                guardrails = digest(test_inputs) == expected_hash
                if meta["card"] in {"D03","D04","D05","D06"} or (meta["card"] == "D02" and answer["claim_type"] in {"recipe_improvement", "architecture_improvement"}):
                    for seed in card["seeds"][1:]:
                        output = workspace / f"confirmation-{seed}"
                        output.mkdir()
                        command = reproduction_command(
                            candidate["reproduce_command"],
                            candidate["code_path"],
                            workspace / "inputs",
                            output,
                            seed,
                        )
                        candidate_command(workspace, command)
                        prediction = output / "test_predictions.npz"
                        # Pilot-3 addendum 4: confirm with the checkpoint name the candidate declared
                        # (model_path), not the baseline's "model.pt"; the task contract never fixes it.
                        infer(workspace, code, output / Path(candidate["model_path"]).name, test_inputs, prediction)
                        measured = score(card["project"], baseline / "test_predictions.npz", prediction)
                        reproduction.append({"seed": seed, **measured})
                    reproduces = reproduces and all(
                        r["gain"] >= card["min_reference_gain"] for r in reproduction
                    )
                scope_ok = answer["claim_type"] in {
                    "checkpoint_improvement",
                    "recipe_improvement",
                    "architecture_improvement",
                }
                guardrails = guardrails and digest(test_inputs) == expected_hash
            rule = success_rule(card, answer, numeric, reproduces, guardrails, scope_ok)
            result.update(
                rule,
                status="valid",
                numeric=numeric,
                fresh_check=fresh,
                reproduction=reproduction,
                guardrails_pass=guardrails,
                scope_check_pass=scope_ok,
                guardrails_note="No critical slices specified on these cards; checked immutable inference inputs and prediction schema.",
                answer_status=answer["status"],
                claim_type=answer["claim_type"],
            )
        except (ValueError, KeyError, OSError) as exc:
            result.update(status="invalid_output", reason=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            # Trusted preparation/baseline/grader failures are infrastructure incidents.
            result.update(status="incident", reason=f"grader infrastructure: {type(exc).__name__}: {exc}")
    write_json(destination / "grade.json", result)
    print(
        f"GRADE {meta['block_id']} arm {meta['arm']}: {result['status']} success={result['verified_success']}"
    )
    return result


def grade_block(block_record, grading_root, results_path):
    block = read_json(block_record)
    if not block.get("complete"):
        raise ValueError("grading is permitted only after the whole block ends")
    directory = Path(block_record).resolve().parent
    grade_directory = Path(grading_root) / block["block_id"]
    previous = grade_directory / "block_record.json"
    if previous.exists() and read_json(previous).get("started_at") != block.get("started_at"):
        from datetime import datetime

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archived = Path(grading_root) / "incidents" / f"{block['block_id']}-{stamp}"
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(grade_directory), archived)
    write_json(grade_directory / "block_record.json", block)
    records = []
    for arm in (1, 2, 3, 4):
        root = directory / f"arm{arm}"
        records.append(
            grade_run(
                root,
                Path(grading_root) / block["block_id"] / f"arm{arm}",
                block.get("incident_reason") if block["status"] == "incident" else None,
                dict(arm=arm, card=block["card"], run_index=block["run_index"], block_id=block["block_id"]),
            )
        )
    if any(r["status"] == "incident" for r in records):
        # A grader/infrastructure incident invalidates the entire paired block.
        reason = next(r.get("reason") for r in records if r["status"] == "incident")
        for arm, result in enumerate(records, 1):
            result.update(status="incident", verified_success=False, card_success=False, reason=reason)
            write_json(grade_directory / f"arm{arm}/grade.json", result)
        block.update(status="incident", incident_reason=reason)
        write_json(block_record, block)
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = read_json(results_path) if results_path.exists() else []
        keys = {(r["family"], r["task"], r["run"], r["arm"]) for r in records}
        existing = [r for r in existing if (r["family"], r["task"], r["run"], r["arm"]) not in keys]
        write_json(results_path, existing + records)
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--block-record", required=True, type=Path)
    parser.add_argument("--grading-root", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    args = parser.parse_args()
    grade_block(args.block_record, args.grading_root, args.results)
