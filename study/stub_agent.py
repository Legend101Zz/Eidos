#!/usr/bin/env python3
"""A baseline candidate for infrastructure qualification. Never calls a model."""

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study.common import MODEL, dev_command, read_json, research_python, write_json
from study.run_arm import utc_now


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stub(run_dir):
    root = Path(run_dir).resolve()
    meta = read_json(root / "arm.json")
    start, clock = utc_now(), time.monotonic()
    out = root / "out"
    out.mkdir(exist_ok=True)
    for source, target in (("project.py", "project.py"), ("requirements.lock", "requirements.lock")):
        shutil.copyfile(root / "task" / source, out / target)
    subprocess.run(
        [
            research_python(),
            str(out / "project.py"),
            "train",
            "--inputs",
            str(root / "task"),
            "--out",
            str(out),
            "--seed",
            "11",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    nullable = [
        "elapsed_seconds",
        "cpu_core_seconds",
        "gpu_device_seconds",
        "gpu_type",
        "model_input_tokens",
        "model_output_tokens",
        "model_reasoning_tokens",
        "model_calls",
        "billed_usd",
    ]
    answer = dict(
        schema_version="eidos-answer/0.2",
        task_id=meta["card"],
        run_id=meta["block_id"] + f"-arm{meta['arm']}",
        status="no_improvement_found",
        claim_type="no_positive_claim",
        claim="Baseline exported and reloaded; no model improvement claimed.",
        scope="Supplied development fixture only; a bounded search does not establish impossibility.",
        candidate=dict(
            code_path="project.py",
            code_sha256=digest(out / "project.py"),
            model_path="model.pt",
            model_sha256=digest(out / "model.pt"),
            predictions_path="validation_predictions.npz",
            predictions_sha256=digest(out / "validation_predictions.npz"),
            reproduce_command=[
                "python",
                "project.py",
                "train",
                "--inputs",
                "{inputs}",
                "--out",
                "{out}",
                "--seed",
                "{seed}",
            ],
            environment_lock="requirements.lock",
            environment_lock_sha256=digest(out / "requirements.lock"),
            parent_checkpoint=None,
            restore_class="fresh",
        ),
        measurements=[],
        evidence=[
            dict(
                path="receipt.json",
                sha256=digest(out / "receipt.json"),
                role="training receipt",
                supports="Baseline execution",
                limitations="No independent improvement evidence",
            )
        ],
        alternative_explanations=["This stub performs no exploration."],
        abstention=dict(abstained=False, reason=None, unresolved=[], next_discriminating_test=None),
        cost={**dict.fromkeys(nullable), "unknown_fields": nullable, "receipt_paths": ["receipt.json"]},
        exposures=["Baseline-only infrastructure stub"],
        deviations=[],
        confidence=None,
    )
    write_json(out / "answer.json", answer)
    dev_command("validate_answer.py", out / "answer.json")
    record = {k: meta[k] for k in ("arm", "card", "run_index", "block_id")}
    record.update(
        status="ok",
        incident_reason=None,
        started_at=start,
        ended_at=utc_now(),
        wall_s=time.monotonic() - clock,
        tokens=dict(input=0, cache_read=0, cache_write=0, output=0, total=0),
        billed_usd=None,
        model_ids_seen=[MODEL],
        agent_claim=answer["claim"],
        transcript=str(root / "transcript.jsonl"),
        stub=True,
    )
    write_json(root / "run_record.json", record)
    (root / "transcript.jsonl").write_text('{"type":"stub","model_calls":0}\n')
    print("STUB baseline candidate written and structurally valid")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, type=int)
    parser.add_argument("--run-dir", required=True, type=Path)
    stub(parser.parse_args().run_dir)
