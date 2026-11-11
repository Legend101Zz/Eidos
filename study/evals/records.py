"""Offline observations shared by scorecards and replay consumers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from study.common import ENVELOPE, STUDY, read_json
from study.evals.extract_replay import blob_bytes, read_transcript, timestamp
from study.evals.policy import POLICY

REQUIRED_INVARIANTS = tuple(POLICY["invariants"])


def inspect_run(run_dir, known_identifiers=()):
    root = Path(run_dir)
    record = read_json(root / "run_record.json")
    try:
        rows = read_transcript(root)
    except (OSError, ValueError):
        rows = []
    events = [r["data"] for r in rows if r.get("type") == "ledger_event"]
    turns = [r["data"] for r in rows if r.get("type") == "turn"]
    state_path = root / "investigation_latest.json"
    final = next((row["data"] for row in reversed(rows) if row.get("type") == "investigation"), {})
    state = final or (read_json(state_path) if state_path.exists() else {})
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
    attribution = body.get("controller_abstention")
    if attribution is None and not final and not conclusion:
        terminal = next(
            (
                event
                for event in reversed(events)
                if event["type"]
                in {"investigation.controller_abstained", "investigation.concluded", "investigation.resumed"}
            ),
            None,
        )
        if terminal and terminal["type"] == "investigation.controller_abstained":
            attribution = terminal["body"]
    controller_abstention = (
        isinstance(attribution, dict)
        and attribution.get("schema") == "controller-abstention/1"
        and attribution.get("author") == "controller"
        and attribution.get("investigator_concluded") is False
        and isinstance(attribution.get("reason"), str)
        and bool(attribution["reason"].strip())
    )
    concluded = bool(conclusion or concluded_events) and attribution is None
    if controller_abstention:
        disposition = "controller_abstention"
    elif attribution is not None:
        disposition = "unknown_controller_attribution"
    elif concluded:
        answer_status = (((conclusion or {}).get("scope") or {}).get("answer") or {}).get("status")
        disposition = "investigator_abstention" if answer_status == "abstained" else "investigator_conclusion"
    elif state.get("status") == "concluded":
        disposition = "unattributed_conclusion"
    else:
        disposition = "no_conclusion"
    if record.get("stub") or record.get("arm") == 2:
        concluded = record["status"] == "ok" and (root / "out/answer.json").is_file()
        disposition = (
            ("stub_answer" if record.get("stub") else "coding_answer") if concluded else "no_conclusion"
        )
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
    stub = bool(record.get("stub"))
    expected_turns = {turn.get("id") for turn in final.get("turns", [])}
    recorded_turns = {turn.get("id") for turn in turns if isinstance(turn.get("decisions"), list)}
    arm4_records = (
        record.get("arm") == 4
        and bool(events)
        and bool(expected_turns)
        and None not in expected_turns
        and expected_turns <= recorded_turns
    )
    isolation = scan_held_out(
        record, rows, events, root, known_identifiers, complete_turn_export=arm4_records
    )
    dead_end = bool(locked and not concluded and not controller_abstention)
    if (not dead_end and not (stub or arm4_records)) or disposition == "unknown_controller_attribution":
        dead_end = None
    fabricated_flag = bool(fabricated) if fabricated or stub or arm4_records else None
    return dict(
        concluded=bool(concluded),
        disposition=disposition,
        controller_abstention=attribution if controller_abstention else None,
        needs_input=needs_input,
        locked_candidate=locked,
        unused_time_at_conclusion_s=unused,
        invariant_flags=dict(
            dead_end=dead_end,
            runtime_deadline_limit=bool(deadline_limits),
            fabricated_empty_wait=fabricated_flag,
            held_out_reads=isolation["violation"],
        ),
        invariant_evidence=dict(
            deadline_limit_receipts=deadline_limits,
            fabricated_wait_decisions=fabricated,
            empty_wait_decisions=empty,
            held_out_scan=isolation,
        ),
    )


def scan_held_out(record, rows, events, run_dir=None, known_identifiers=(), *, complete_turn_export=False):
    """Match known evaluator digests/IDs in model-facing bytes and structured read receipts."""
    known = set(known_identifiers)
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
    model_facing = [r["data"] for r in rows if r.get("type") in {"model_response", "runtime_response"}]
    reads = [
        e["body"].get("usage", {}).get("structured_output", {}).get("reads", [])
        for e in events
        if e["type"] == "model.usage"
    ]
    model_facing.extend(reads)
    scanned, unavailable = 0, []
    packed = set()
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
            if event["body"]["schema"] == "packed-context/1":
                packed.add(event["subject"])
        except (OSError, ValueError) as exc:
            unavailable.append(dict(artifact_id=event["subject"], error=str(exc)))
    context_ids = {
        row["data"]["context"]["context_artifact_id"]
        for row in rows
        if row.get("type") == "turn" and (row["data"].get("context") or {}).get("context_artifact_id")
    }
    missing_contexts = sorted(context_ids - packed)
    content = json.dumps(model_facing)
    matches = {value for value in known if isinstance(value, str) and value in content}
    requests, request_error = audited_requests(run_dir, set(known_identifiers))
    for request in requests:
        matches.update(request["matched_identifiers"])
    usage_calls = sum(
        e["body"].get("usage", {}).get("calls", 0) or 0 for e in events if e["type"] == "model.usage"
    )
    coverage = "unknown"
    if record.get("stub"):
        coverage = "no-model-calls"
    elif (
        known
        and record.get("arm") == 4
        and complete_turn_export
        and packed
        and not missing_contexts
        and not unavailable
        and request_error is None
        and usage_calls > 0
        and len(requests) >= usage_calls
    ):
        coverage = "recorded-arm4-provider-requests-and-contexts"
    return dict(
        violation=True if matches else False if coverage != "unknown" else None,
        coverage=coverage,
        matched_identifiers=sorted(matches),
        known_identifiers=len(known),
        scanned_blobs=scanned,
        packed_contexts=len(packed),
        missing_contexts=missing_contexts,
        unavailable_blobs=unavailable,
        audited_provider_requests=len(requests),
        complete_turn_export=complete_turn_export,
        usage_model_calls=usage_calls,
        provider_audit_error=request_error,
        limitation="Checks known identifiers in model-facing records and recorded provider requests. "
        "Derived information and the optional coding arm's private context are outside this coverage.",
    )


def audited_requests(run_dir, known):
    if run_dir is None or not known:
        return [], "provider audit identifiers unavailable"
    try:
        if read_json(run_dir / "provider_bridge.json").get("eval_provider_audit") is not True:
            return [], "provider bridge did not attest audit activation"
        rows = [json.loads(line) for line in (run_dir / "eval_provider_audit.jsonl").read_text().splitlines()]
        if not rows or any(
            not re.fullmatch(r"[0-9a-f]{64}", row["request_sha256"])
            or not set(row["matched_identifiers"]) <= known
            for row in rows
        ):
            return [], "empty or invalid provider audit"
    except (OSError, KeyError, TypeError, ValueError):
        return [], "missing or unreadable provider audit"
    return rows, None
