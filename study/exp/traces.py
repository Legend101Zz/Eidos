"""Offline mechanism measurements; absent evidence is None, never an estimate.

Counts of tool calls describe accepted, non-revoked decisions. Jobs and notes
are counted from exported records, rather than inferred from proposed calls.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

METRICS = (
    "decisions", "requests", "time_to_first_lock_s", "locks", "authoring_calls",
    "prepare_candidate_calls", "portfolio_jobs", "jobs", "job_seconds",
    "refused_decisions", "unused_wall_s",
)


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def read_rows(run_dir):
    path = Path(run_dir) / "transcript.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def _unique(rows):
    return list({r.get("id", json.dumps(r, sort_keys=True)): r for r in rows}.values())


def measure(run_dir):
    root = Path(run_dir)
    path = root / "run_record.json"
    record = json.loads(path.read_text()) if path.is_file() else {}
    return from_records(read_rows(root), record, transcript_present=(root / "transcript.jsonl").is_file())


def from_records(rows, record, *, transcript_present=True):
    """Accept the launcher's typed transcript export (also useful for replays)."""
    result = dict.fromkeys(METRICS)
    if record.get("arm") != 4:
        return result
    groups = {}
    for row in rows:
        groups.setdefault(row.get("type"), []).append(row.get("data", {}))
    turns = groups.get("turn", [])
    decisions = groups.get("decision", []) + [d for t in turns for d in t.get("decisions", [])]
    if decisions or any(isinstance(t.get("decisions"), list) for t in turns):
        decisions = _unique(decisions)
        result["decisions"] = len(decisions)
        result["refused_decisions"] = sum(d.get("accepted") is False for d in decisions)
        calls = [c for d in decisions if d.get("accepted") is True and not d.get("revoked")
                 for c in (d.get("body") or {}).get("calls", [])]
        result["authoring_calls"] = sum(c.get("tool") == "author_source" for c in calls)
        result["prepare_candidate_calls"] = 0
        for decision in decisions:
            if decision.get("accepted") is not True or decision.get("revoked"):
                continue
            proposed = (decision.get("body") or {}).get("calls", [])
            # One prepare_candidate expands into four stamped component calls.
            stamps = {json.dumps(c["macro"], sort_keys=True) for c in proposed
                      if (c.get("macro") or {}).get("tool") == "prepare_candidate"}
            result["prepare_candidate_calls"] += len(stamps) + sum(c.get("tool") == "prepare_candidate" for c in proposed)
    events = groups.get("ledger_event", [])
    usages = [e.get("body", {}).get("usage", {}) for e in events if e.get("type") == "model.usage"]
    usages += [r.get("usage", r) for r in groups.get("usage", [])]
    if usages and all(isinstance(u.get("calls"), int) for u in usages):
        result["requests"] = sum(u["calls"] for u in usages)
    elif groups.get("model_response"):
        # A response export can contain several provider messages. Only count
        # explicit request/response ids, not the number of export wrappers.
        ids = {r.get("request_id") or r.get("response_id") for r in groups["model_response"]}
        if None not in ids:
            result["requests"] = len(ids)
        else:
            responses = [message for r in groups["model_response"] for message in r.get("response", [])
                         if isinstance(message, dict) and message.get("kind") == "response"]
            ids = {r.get("provider_response_id") for r in responses}
            if ids and None not in ids:
                result["requests"] = len(ids)
    notes = _unique(groups.get("note", []))
    if notes:
        locks = [n for n in notes if n.get("body", {}).get("kind") == "candidate_lock"]
        result["locks"] = len(locks)
        times = [timestamp(n.get("created_at")) for n in locks]
        start = timestamp(record.get("started_at"))
        if locks and start is not None and all(t is not None for t in times):
            result["time_to_first_lock_s"] = max(0, min(times) - start)
    jobs = _unique(groups.get("job", []))
    states = groups.get("investigation", [])
    # Summary job lists establish job identity/component, but not job duration.
    if not jobs and states and isinstance(states[-1].get("jobs"), list):
        jobs = _unique(states[-1]["jobs"])
        result["jobs"] = len(jobs)
    elif jobs:
        result["jobs"] = len(jobs)
    if result["jobs"] is not None:
        components = [j.get("component") or j.get("component_ref") or j.get("lock", {}).get("component") for j in jobs]
        components = [c.get("ref") if isinstance(c, dict) else c for c in components]
        if all(isinstance(c, str) for c in components):
            result["portfolio_jobs"] = sum("eidos.search.recipe-portfolio" in c for c in components)
        intervals = []
        for job in jobs:
            parts = [job] if job.get("started_at") is not None else job.get("attempts") or [job]
            intervals.extend((timestamp(p.get("started_at")), timestamp(p.get("ended_at"))) for p in parts)
        if intervals and all(a is not None and b is not None and b >= a for a, b in intervals):
            end, total = float("-inf"), 0.0
            for a, b in sorted(intervals):
                total += max(0, b - max(a, end))
                end = max(end, b)
            result["job_seconds"] = total
    envelope = record.get("envelope", {})
    deadline = timestamp(record.get("deadline_at"))
    if deadline is None and states:
        deadline = states[-1].get("body", {}).get("wall", {}).get("deadline_unix")
    concluded = [e for e in events if e.get("type") == "investigation.concluded"]
    conclusion = timestamp(concluded[-1].get("created_at")) if concluded else None
    if conclusion is not None and deadline is not None:
        result["unused_wall_s"] = max(0, deadline - conclusion)
    elif isinstance(record.get("unused_wall_s"), (int, float)):
        result["unused_wall_s"] = record["unused_wall_s"]
    elif concluded and isinstance(envelope.get("wall_s"), (int, float)):
        start = timestamp(record.get("started_at"))
        if conclusion is not None and start is not None:
            result["unused_wall_s"] = max(0, envelope["wall_s"] - (conclusion - start))
    return result
