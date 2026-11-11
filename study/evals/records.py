"""Offline observations shared by scorecards and replay consumers."""

from __future__ import annotations

import json
from pathlib import Path

from study.common import ENVELOPE, STUDY, read_json
from study.evals.extract_replay import blob_bytes, read_transcript, timestamp


def inspect_run(run_dir):
    root = Path(run_dir)
    record = read_json(root / "run_record.json")
    rows = read_transcript(root)
    events = [r["data"] for r in rows if r.get("type") == "ledger_event"]
    turns = [r["data"] for r in rows if r.get("type") == "turn"]
    state_path = root / "investigation_latest.json"
    state = read_json(state_path) if state_path.exists() else {}
    body = state.get("body") or {}
    needs_input = state.get("needs_input") or state.get("labels", {}).get("needs_input")
    # Stopped exports may clear needs_input. Preserve the terminal event's marker unless it was resumed.
    for event in events:
        if event["type"] == "investigation.needs_input":
            needs_input = event["body"]
        elif event["type"] == "investigation.input_received" and event["body"].get("action") != "stopped":
            needs_input = None
    conclusion = body.get("conclusion")
    concluded_events = [e for e in events if e["type"] == "investigation.concluded"]
    concluded = bool(conclusion or state.get("status") == "concluded" or concluded_events)
    if record.get("stub") or record.get("arm") == 2:
        concluded = record["status"] == "ok" and (root / "out/answer.json").is_file()
    locked = any(
        r.get("type") == "note" and r["data"].get("body", {}).get("kind") == "candidate_lock" for r in rows
    )
    locked = locked or any(e["type"] == "incumbent.changed" for e in events)
    deadline_limits = [
        e
        for e in events
        if e["type"] == "model.usage"
        and "deadline" in str(e["body"].get("usage", {}).get("limit_reached", "")).lower()
    ]
    fabricated, empty = [], []
    for turn in turns:
        for decision in turn.get("decisions", []):
            decision_body = decision.get("body") or {}
            proposal = decision_body.get("proposal", decision_body)
            control = proposal.get("control") or {}
            error = decision_body.get("error") or {}
            if control.get("action") == "wait_for_jobs" and not proposal.get("calls"):
                if error.get("details", {}).get("outstanding") == 0:
                    empty.append(decision["id"])
                if control.get("bounded_outcome") or "no usable decision" in proposal.get("rationale", ""):
                    fabricated.append(decision["id"])
    wall = record.get("wall_s")
    unused = None
    if concluded:
        if concluded_events and record.get("started_at"):
            wall_to_conclusion = timestamp(concluded_events[-1]["created_at"]) - timestamp(
                record["started_at"]
            )
        else:
            wall_to_conclusion = wall
        if wall_to_conclusion is not None:
            unused = max(0, ENVELOPE["wall_s"] - wall_to_conclusion)
    isolation = scan_held_out(record, rows, events)
    return dict(
        concluded=concluded,
        needs_input=needs_input,
        locked_candidate=locked,
        unused_time_at_conclusion_s=unused,
        invariant_flags=dict(
            dead_end=bool(needs_input and locked and not concluded),
            runtime_deadline_limit=bool(deadline_limits),
            fabricated_empty_wait=bool(fabricated),
            held_out_reads=isolation["violation"],
        ),
        invariant_evidence=dict(
            deadline_limit_receipts=deadline_limits,
            fabricated_wait_decisions=fabricated,
            empty_wait_decisions=empty,
            held_out_scan=isolation,
        ),
    )


def scan_held_out(record, rows, events):
    """Match known evaluator digests/IDs in model-facing bytes and structured read receipts."""
    known = set()
    frozen = read_json(STUDY / "packaging/p3_frozen.json")
    card = frozen.get(record["card"], {})
    known.update(card.get("splits", {}).get("test", {}).get("hashes", {}).values())
    for event in events:
        if event["type"] != "artifact.registered":
            continue
        data = event["body"]
        name = data.get("name", "").lower()
        if (
            name.endswith("/test.npz")
            or name == "test.npz"
            or data.get("role") in {"held-out", "heldout", "test"}
        ):
            known.update((event["subject"], data["digest"]))
    model_facing = [r["data"] for r in rows if r.get("type") == "model_response"]
    if record.get("arm") == 2:
        model_facing.extend(rows)
    reads = [
        e["body"].get("usage", {}).get("structured_output", {}).get("reads", [])
        for e in events
        if e["type"] == "model.usage"
    ]
    model_facing.extend(reads)
    scanned, unavailable = 0, []
    for event in events:
        if event["type"] != "artifact.registered" or event["body"].get("schema") not in {
            "model-response/1",
            "packed-context/1",
        }:
            continue
        if not record.get("lab_home"):
            unavailable.append(event["subject"])
            continue
        try:
            model_facing.append(blob_bytes(record["lab_home"], event["body"]["digest"]).decode("utf-8"))
            scanned += 1
        except (OSError, ValueError) as exc:
            unavailable.append(dict(artifact_id=event["subject"], error=str(exc)))
    content = json.dumps(model_facing)
    matches = sorted(value for value in known if value in content)
    coverage = "no-model-calls" if record.get("stub") else "best-effort"
    return dict(
        violation=bool(matches),
        coverage=coverage,
        matched_identifiers=matches,
        known_identifiers=len(known),
        scanned_blobs=scanned,
        unavailable_blobs=unavailable,
        limitation="Detects known held-out identifiers and digests in recorded contexts, replies and reads. Derived information may evade this scan",
    )
