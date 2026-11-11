#!/usr/bin/env python3
"""Extract a self-contained coordinator replay from immutable arm-4 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from study.common import read_json, write_json

SCHEMA = "eidos-coordinator-replay/1"
# Match credential values, including provider keys, rather than field names in source code.
SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|[\"']?(?:api_key|auth_token|authorization)[\"']?\s*[:=]\s*[\"'](?:Bearer\s+)?[A-Za-z0-9_./+-]{24,}[\"']",
    re.IGNORECASE,
)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def read_transcript(run_dir):
    return [
        json.loads(line)
        for line in (Path(run_dir) / "transcript.jsonl").read_text().splitlines()
        if line.strip()
    ]


def blob_bytes(lab_home, digest):
    digest = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid artifact digest")
    root = Path(lab_home) / "artifacts/blobs/sha256"
    # Current Eidos uses two-character shards and the complete digest as filename.
    candidates = (root / digest[:2] / digest, root / digest)
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"missing artifact blob {digest} under {root}")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError(f"artifact hash mismatch {digest}")
    return payload


def blob(lab_home, digest):
    return json.loads(blob_bytes(lab_home, digest))


def extract(run_dir, lab_home=None):
    run_dir = Path(run_dir)
    record = read_json(run_dir / "run_record.json")
    if record["arm"] != 4:
        raise ValueError("replays require an arm-4 run")
    lab_home = Path(lab_home or record["lab_home"])
    transcript = read_transcript(run_dir)
    events = sorted(
        (row["data"] for row in transcript if row["type"] == "ledger_event"), key=lambda row: row["seq"]
    )
    turns = sorted((row["data"] for row in transcript if row["type"] == "turn"), key=lambda row: row["seq"])
    observed = read_json(run_dir / "investigation_latest.json")
    states = [row["data"] for row in transcript if row["type"] == "investigation"]
    state = states[-1] if states else observed
    deadline = observed["body"]["wall"]["deadline_unix"]
    decisions = {d["body"].get("response_artifact_id"): d for t in turns for d in t.get("decisions", [])}
    artifacts, requests = [], []
    last_usage = None
    for event in events:
        if event["type"] == "model.usage":
            last_usage = event
        if event["type"] != "artifact.registered" or event["body"].get("schema") != "model-response/1":
            continue
        artifact_id = event["subject"]
        name = event["body"]["name"]
        turn = next((t for t in turns if name.startswith("response-" + t["id"] + "-")), None)
        if turn is None:
            raise ValueError(f"response artifact has no recorded turn {name}")
        messages = blob(lab_home, event["body"]["digest"])
        decision = decisions.get(artifact_id)
        usage = last_usage if last_usage and last_usage["subject"] == turn["id"] else None
        artifacts.append(
            dict(
                artifact_id=artifact_id,
                sha256=event["body"]["digest"],
                name=name,
                ledger_seq=event["seq"],
                turn_id=turn["id"],
                decision_id=decision["id"] if decision else None,
                accepted=decision["accepted"] if decision else None,
                usage_receipt=usage,
                messages=messages,
            )
        )
        request_started = None
        for index, message in enumerate(messages):
            if message.get("kind") == "request":
                request_started = timestamp(message["timestamp"])
            if message.get("kind") != "response":
                continue
            if request_started is None:
                raise ValueError(f"response lacks a timestamped request {artifact_id}")
            ended = timestamp(message["timestamp"])
            boundary = timestamp(turn["created_at"]) + turn["spec"]["limits"]["deadline_seconds"]
            requests.append(
                dict(
                    index=len(requests),
                    artifact_id=artifact_id,
                    message_index=index,
                    turn_id=turn["id"],
                    provider_response_id=message.get("provider_response_id"),
                    started_unix=request_started,
                    ended_unix=ended,
                    elapsed_s=round(ended - request_started, 6),
                    remaining_s=round(max(0, deadline - request_started), 6),
                    decision_remaining_s=round(max(0, boundary - request_started), 6),
                    finish_reason=(message.get("provider_details") or {}).get("finish_reason"),
                )
            )
            request_started = None
    if not artifacts or not requests:
        raise ValueError("no response artifacts found")
    fixture = dict(
        schema_version=SCHEMA,
        source=dict(
            block_id=record["block_id"],
            card=record["card"],
            arm=4,
            eidos_commit_sha=record.get("eidos_commit_sha"),
            transcript_sha256=hashlib.sha256((run_dir / "transcript.jsonl").read_bytes()).hexdigest(),
        ),
        timing=dict(
            deadline_unix=deadline,
            unit="seconds",
            scale=1.0,
            method="request message timestamp to following response message timestamp",
        ),
        response_artifacts=artifacts,
        requests=requests,
        turns=[
            dict(
                turn_id=t["id"],
                seq=t["seq"],
                status=t["status"],
                created_at=t["created_at"],
                ended_at=t.get("ended_at"),
                spec=t["spec"],
                stop_reason=(t.get("result") or {}).get("stop_reason"),
                result=t.get("result"),
                decisions=t.get("decisions", []),
            )
            for t in turns
        ],
        needs_input=observed.get("needs_input") or observed.get("labels", {}).get("needs_input"),
        needs_input_events=[e for e in events if e["type"] == "investigation.needs_input"],
        receipts=[
            e
            for e in events
            if e["type"]
            in {
                "model.usage",
                "decision.accepted",
                "turn.completed",
                "incumbent.changed",
                "note.recorded",
                "investigation.needs_input",
                "investigation.input_received",
                "investigation.stopped",
                "investigation.concluded",
            }
        ],
        observed_state=observed,
        final_state=state,
        run_record=record,
    )
    if SECRET.search(json.dumps(fixture)):
        raise ValueError("credential-shaped value found in fixture; inspect source before publishing")
    return fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("fixture_out", type=Path)
    parser.add_argument("--lab-home", type=Path, help="override a relocated read-only artifact store")
    args = parser.parse_args()
    write_json(args.fixture_out, extract(args.run_dir, args.lab_home))


if __name__ == "__main__":
    main()
