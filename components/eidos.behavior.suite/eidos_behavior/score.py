"""``eidos.behavior.suite``: score a ``behavior-cases/1`` table against raw predictions (no model run).

Predictions (``raw-predictions/1``): rows ``{"id", "label"?, "score"?}`` (a list, or ``{"rows": [...]}``), e.g. the
raw per-example output an evaluation component already published. Each test reports its pass rate overall and per
group, its status against ``min_pass_rate``, and the first failing pairs. Missing predictions are counted and make
the test ``incomplete``: never a silent pass.
"""

from __future__ import annotations

from typing import Any, Dict, List

from eidos_worker import ContractError

TYPES = ("invariance", "direction")
DIRECTIONS = ("increase", "decrease", "not-increase", "not-decrease")


def _pred_index(raw: Any) -> Dict[str, dict]:
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ContractError("predictions must be a list of rows (or {'rows': [...]})")
    out = {}
    for r in rows:
        if not isinstance(r, dict) or "id" not in r:
            raise ContractError("every prediction row needs an 'id'")
        out[str(r["id"])] = r
    return out


def _check(test: dict) -> None:
    if test.get("type") not in TYPES:
        raise ContractError(f"test {test.get('name')!r}: type must be one of {TYPES}")
    if test["type"] == "direction" and test.get("expect") not in DIRECTIONS:
        raise ContractError(f"test {test.get('name')!r}: expect must be one of {DIRECTIONS}")
    if not isinstance(test.get("pairs"), list):
        raise ContractError(f"test {test.get('name')!r}: pairs must be a list")


def _passes(test: dict, a: dict, b: dict) -> bool:
    on, tol = test.get("on", "label" if test["type"] == "invariance" else "score"), float(test.get("tolerance", 0))
    if on not in a or on not in b:
        raise KeyError(on)
    if test["type"] == "invariance":
        if on == "label":
            return a[on] == b[on]
        return abs(float(b[on]) - float(a[on])) <= tol
    delta = float(b[on]) - float(a[on])
    return {"increase": delta > tol, "decrease": delta < -tol, "not-increase": delta <= tol,
            "not-decrease": delta >= -tol}[test["expect"]]


def score_suite(cases: dict, predictions: Any, *, max_failures: int = 20) -> dict:
    if not isinstance(cases, dict) or not isinstance(cases.get("tests"), list):
        raise ContractError("cases must be a behavior-cases/1 object with a 'tests' list")
    preds = _pred_index(predictions)
    tests_out: List[dict] = []
    for t in cases["tests"]:
        _check(t)
        n = passed = missing = 0
        groups: Dict[str, Dict[str, int]] = {}
        failures = []
        for p in t["pairs"]:
            g = str(p.get("group", "all"))
            a, b = preds.get(str(p["original"])), preds.get(str(p["transformed"]))
            if a is None or b is None:
                missing += 1
                continue
            try:
                ok = _passes(t, a, b)
            except (KeyError, TypeError, ValueError):
                missing += 1
                continue
            n += 1
            passed += ok
            grp = groups.setdefault(g, {"n": 0, "passed": 0})
            grp["n"] += 1
            grp["passed"] += ok
            if not ok and len(failures) < max_failures:
                on = t.get("on", "label" if t["type"] == "invariance" else "score")
                failures.append({"original": p["original"], "transformed": p["transformed"], "group": g,
                                 "before": a.get(on), "after": b.get(on)})
        rate = passed / n if n else None
        need = float(t.get("min_pass_rate", 1.0))
        status = "incomplete" if missing or not n else ("pass" if rate is not None and rate >= need else "fail")
        tests_out.append({"name": t.get("name"), "type": t["type"], "expect": t.get("expect"),
                          "capability": t.get("capability", ""), "n": n, "passed": passed, "missing": missing,
                          "pass_rate": rate, "min_pass_rate": need, "status": status,
                          "by_group": {g: {**v, "pass_rate": v["passed"] / v["n"]} for g, v in sorted(groups.items())},
                          "failures": failures})
    by_cap: Dict[str, Dict[str, int]] = {}
    for t in tests_out:
        c = by_cap.setdefault(t["capability"] or "unspecified", {"tests": 0, "failed": 0})
        c["tests"] += 1
        c["failed"] += t["status"] != "pass"
    return {"schema": "behavior-report/1", "suite": cases.get("suite"), "tests": tests_out, "capabilities": by_cap,
            "summary": {"tests": len(tests_out), "passed": sum(t["status"] == "pass" for t in tests_out),
                        "failed": sum(t["status"] == "fail" for t in tests_out),
                        "incomplete": sum(t["status"] == "incomplete" for t in tests_out)}}


def run(inputs, config, *, reader=None, writer=None):
    report = score_suite(reader.json("cases"), reader.json("predictions"),
                         max_failures=int((config or {}).get("max_failures", 20)))
    writer.stage_json("report", report, schema="behavior-report/1")
    s = report["summary"]
    return {"suite": report["suite"], "tests": s["tests"], "passed": s["passed"], "failed": s["failed"],
            "incomplete": s["incomplete"]}
