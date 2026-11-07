"""Template/assessor qualification gate (design §"Qualification before template or assessor approval").

A control suite is committed *before* the qualification run. Approval requires executed
planted-effect detection and null-case silence for the exact release, suite and environment.
A human signature cannot substitute for missing or failing receipts.
"""

from __future__ import annotations

from typing import Any

from eidos.domain.errors import Invalid, QualificationMissing
from eidos.domain.stats import wilson_interval


def validate_control_suite(suite: dict[str, Any]) -> dict[str, Any]:
    problems = []
    target = suite.get("target") or {}
    if target.get("kind") not in ("template", "assessor", "template+assessor"):
        problems.append("target.kind must be template, assessor or template+assessor")
    controls = suite.get("controls") or []
    kinds = {c.get("type") for c in controls}
    if "planted" not in kinds:
        problems.append("at least one planted-effect control is required")
    if "null" not in kinds:
        problems.append("at least one null-case control is required")
    ids = set()
    for c in controls:
        if not c.get("id") or c["id"] in ids:
            problems.append(f"control id {c.get('id')!r} missing or duplicated")
        ids.add(c.get("id"))
        reps = c.get("repetitions", 1)
        if not isinstance(reps, int) or reps < 1:
            problems.append(f"control {c.get('id')!r}: repetitions must be >= 1")
        if not c.get("deterministic", True) and reps < 10:
            problems.append(f"control {c.get('id')!r}: stochastic controls need >= 10 repetitions")
        if not c.get("input"):
            problems.append(f"control {c.get('id')!r}: a fixed input (evidence/receipt fixture) is required")
    acc = suite.get("acceptance") or {}
    if any(not c.get("deterministic", True) for c in controls):
        for k in ("min_power", "max_false_positive"):
            if not isinstance(acc.get(k), (int, float)):
                problems.append(f"acceptance.{k} is required when stochastic controls are present")
    if not suite.get("scope"):
        problems.append("scope (approved uses) is required")
    if problems:
        raise Invalid("invalid control suite", details={"problems": problems})
    return suite


def evaluate_qualification(suite: dict[str, Any], results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Decide the gate from executed results.

    ``results[control_id]`` is a list of repetitions, each ``{"detected": bool, "outcome": str,
    "valid": bool}``. ``detected`` means the pipeline asserted an effect (e.g. protocol-passed for an
    improvement template). Returns {"status": passed|failed|inconclusive, "per_control": [...]}.
    """
    acc = suite.get("acceptance") or {}
    conf = acc.get("confidence", 0.95)
    per = []
    status = "passed"
    for c in suite["controls"]:
        reps = results.get(c["id"], [])
        want = c.get("repetitions", 1)
        entry: dict[str, Any] = {"control": c["id"], "type": c["type"], "repetitions": len(reps), "required": want}
        if len(reps) < want or any(not r.get("valid", True) for r in reps):
            entry["status"] = "incomplete"
            status = "inconclusive" if status == "passed" else status
            per.append(entry)
            continue
        detected = sum(1 for r in reps if r.get("detected"))
        expected_outcome = c.get("expected_outcome")
        mismatched = [r.get("outcome") for r in reps if expected_outcome and r.get("outcome") != expected_outcome]
        entry["detected"] = detected
        if c.get("deterministic", True):
            ok = detected == len(reps) if c["type"] == "planted" else detected == 0
            if expected_outcome and mismatched:
                ok = False
                entry["mismatched_outcomes"] = mismatched
        else:
            lo, hi = wilson_interval(detected, len(reps), conf)
            entry["interval"] = [lo, hi]
            if c["type"] == "planted":
                ok = lo >= float(acc["min_power"])
            else:
                ok = hi <= float(acc["max_false_positive"])
        entry["status"] = "passed" if ok else "failed"
        if not ok:
            status = "failed"
        per.append(entry)
    return {"status": status, "per_control": per}


def check_promotion(
    *, release_digest: str, requested_scope: list[str], qualifications: list[dict[str, Any]],
    withdrawn_refs: set[str],
) -> dict[str, Any]:
    """Return the qualification record that authorizes promotion, or raise.

    Called by the promotion handler. ``qualifications`` are committed records for this release.
    A passing record must cover the exact release digest and every requested use, and neither
    the release nor its control suite may be withdrawn.
    """
    if release_digest in withdrawn_refs:
        raise QualificationMissing("release has been withdrawn; approval unavailable")
    candidates = []
    for q in qualifications:
        if q.get("release_digest") != release_digest:
            continue
        if q.get("suite_digest") in withdrawn_refs:
            continue
        if q.get("status") != "passed":
            continue
        if not set(requested_scope) <= set(q.get("scope", [])):
            continue
        candidates.append(q)
    if not candidates:
        raise QualificationMissing(
            "no passing planted-effect/null qualification receipt covers this release and scope",
            details={"release_digest": release_digest, "requested_scope": requested_scope,
                     "records_seen": [{"status": q.get("status"), "scope": q.get("scope")} for q in qualifications]},
        )
    return candidates[-1]
