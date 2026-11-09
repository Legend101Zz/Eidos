#!/usr/bin/env python3
"""Live-provider qualification of the investigator runtime port (Pydantic AI adapter).

Runs WITHOUT PostgreSQL, Temporal or lab services: it drives ``PydanticAIRuntime.decide`` directly
on contexts packed by ``context-policy/1`` and checks the results with the same validator the
command service uses. Hard caps: at most 24 model requests in total, at most 4096 output tokens
per request, temperature not set. The API key is read only from ANTHROPIC_API_KEY and is never
printed. Output: one JSON receipt on stdout.

Install (from a checkout, in a small venv):
    python3.12 -m venv /tmp/eidos-live && /tmp/eidos-live/bin/pip install -r scripts/qualify/requirements-live.txt
Run:
    ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src \
        /tmp/eidos-live/bin/python scripts/qualify/live_provider_check.py --model anthropic:claude-sonnet-5-5 > receipt.json
OpenAI-compatible endpoint (same scenarios; the key, if the endpoint needs one, only from --api-key-env):
    PYTHONPATH=src:packages/eidos-wire/src python scripts/qualify/live_provider_check.py \
        --model openai-compatible:qwen2.5:3b-instruct --base-url http://127.0.0.1:11535/v1 > receipt.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _contract(component_dir: str) -> dict:
    """Config contract + summary from the shipped manifest (the same one the lab enforces)."""
    m = json.loads((_ROOT / "components" / component_dir / "eidos-component.json").read_text())
    return {"ref": f"{m['id']}@{m['release']}", "summary": m.get("summary", ""), "config_schema": m.get("config_schema")}


def contract_problems(calls: list, catalog: list) -> list:
    from eidos.domain.contracts import validate_config
    by_id = {c["ref"].split("@")[0]: c for c in catalog}
    out = []
    for i, c in enumerate(calls):
        if c["tool"] in ("run_component", "plan_trial"):
            entry = by_id.get(str(c["args"].get("component_ref", "")).split("@")[0])
            if entry is None:
                out.append(f"call {i}: unknown component {c['args'].get('component_ref')!r}")
            elif entry.get("config_schema"):
                out += [f"call {i}: {p}" for p in validate_config(c["args"].get("config") or {}, entry["config_schema"])]
    return out

MAX_TOTAL_REQUESTS = 24
MAX_OUTPUT_TOKENS = 4096


def _view(jobs=None, catalog_size=6, steering=None):
    jobs = jobs or []
    return {
        "investigation": {"id": "inv-live-check", "status": "active", "body": {
            "question": "Improve this functioning digit classifier within the assigned research envelope; "
                        "use training and validation data for selection.",
            "inputs": {"train": "art_train", "validation": "art_validation", "task": "art_task"},
            "environment": "default", "steering": steering or []}},
        "horizon": 42,
        "charter": {"study_id": "live-check", "exploration_only": True, "metrics": {}, "success_rules": []},
        "jobs": jobs, "claims": [], "events": [],
        "budgets": {"compute_seconds": {"limit_amount": 6000, "reserved": 0, "used": 120, "uncertain": 0}},
    }


def _catalog(n):
    pc = _contract("eidos.project.command")
    base = [
        {"ref": pc["ref"], "implements": "project-command/1", "kind": "artifact-op", "config_schema": pc["config_schema"],
         "effects": ["change-weights"], "input": {}, "output": {"receipt": "project-receipt/1"}, "summary": pc["summary"]},
        {"ref": "eidos.task.score-classification@1.0.0", "implements": "classification-score/1", "kind": "artifact-op",
         "effects": ["observe"], "input": {"predictions": "prediction-array/1", "reference": "task-array/1"},
         "output": {"report": "classification-report/1"}, "summary": "validation accuracy with per-example correctness"},
    ]
    for i in range(max(0, n - len(base))):
        base.append({"ref": f"lab.generated.method-{i:03d}@1.0.0", "implements": f"method-{i % 7}/1", "kind": "artifact-op",
                     "effects": ["observe"], "input": {}, "output": {}, "summary": "synthetic catalog entry (size test)"})
    return base


def _job(i, acc=None, error=None):
    return {"id": f"job_{i}", "component": "eidos.project.command@1.0.4", "purpose": "reproduction",
            "status": "failed" if error else "succeeded", "inputs": {"train": "art_train"},
            "config": {"argv": ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", str(11 + i)]},
            "receipt": {"metrics": {"accuracy": acc}} if acc is not None else None, "error": error,
            "outputs": [] if error else [{"slot": "validation_predictions", "artifact_id": f"art_vp{i}", "role": "output",
                                          "schema": "prediction-array/1", "media_type": "application/x-npz", "size": 6000}]}


SCENARIOS = [
    ("conformance-fresh", _view(), 6, None),
    ("conformance-after-baselines", _view([_job(0, 0.83), _job(1, 0.87), _job(2, 0.85)]), 6, None),
    ("conformance-failed-job", _view([_job(0, error={"category": "user-code", "message": "RuntimeError: shape mismatch"})]), 6, None),
    ("conformance-large-catalog", _view([_job(0, 0.84)]), 120, None),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="anthropic:claude-sonnet-5-5")
    ap.add_argument("--base-url", help="OpenAI-compatible endpoint, e.g. http://127.0.0.1:11535/v1")
    ap.add_argument("--api-key-env", help="environment variable holding the endpoint key (default: provider's own)")
    ap.add_argument("--output-mode", default="tool", choices=["tool", "repair", "json"],
                    help="decision output mode (ADR 0014): tool calls (default), JSON text with bounded repair, or "
                         "repair plus the endpoint's JSON mode")
    ap.add_argument("--request-timeout", type=float, default=120.0,
                    help="seconds per model request (a local model on a laptop may need several minutes)")
    args = ap.parse_args()
    provider = args.model.partition(":")[0]
    key_env = args.api_key_env or ("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY")
    if provider == "anthropic" and not os.environ.get(key_env):
        print(json.dumps({"error": f"{key_env} is not set; nothing was called"}))
        return 2
    if provider == "openai-compatible" and not args.base_url:
        print(json.dumps({"error": "--base-url is required for openai-compatible"}))
        return 2
    endpoint = {"api_key_env": key_env, **({"base_url": args.base_url} if args.base_url else {})}
    from eidos.domain.turns import validate_decision
    from eidos.investigator.context import pack
    from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime, build_model
    from eidos.investigator.runtime import DecisionRequest, ModelCallFailed

    spent = {"requests": 0}
    receipt = {"check": "eidos-live-provider/1", "model_requested": args.model, "base_url": args.base_url,
               "output_mode": args.output_mode, "caps": {
        "max_total_requests": MAX_TOTAL_REQUESTS, "max_output_tokens": MAX_OUTPUT_TOKENS, "temperature": "unset"},
        "request_timeout_seconds": args.request_timeout, "started_at": time.time(), "scenarios": []}
    m = build_model({"model": args.model, **endpoint})
    receipt["transport_retries_disabled"] = getattr(getattr(m, "client", None), "max_retries", None) == 0

    def run(name, cfg_extra, view, catalog_n, tools, budget_needed, expect):
        if spent["requests"] + budget_needed > MAX_TOTAL_REQUESTS:
            receipt["scenarios"].append({"name": name, "skipped": "request cap would be exceeded"})
            return
        cfg = {"model": args.model, "max_output_tokens": MAX_OUTPUT_TOKENS, "output_retries": 1,
               "max_requests_per_decision": budget_needed, "request_timeout": args.request_timeout, **endpoint,
               "output_mode": args.output_mode, **cfg_extra}
        packed = pack(view, _catalog(catalog_n))
        req = DecisionRequest(turn_id=f"live:{name}", slot=0, context=packed["text"], turn_so_far=[],
                              limits={"max_calls": 6}, read_tools=tools or {}, view=view)
        entry = {"name": name, "expect": expect, "context_chars": packed["chars"]}
        t0 = time.time()
        try:
            res = PydanticAIRuntime(cfg).decide(req)
            spent["requests"] += int(res.usage.get("calls") or 0)
            entry.update({"outcome": "bounded-no-decision" if res.usage.get("limit_reached") else "decision",
                          "usage": res.usage, "served_models": res.model_settings.get("served_models")})
            # The model's text replies (recorded so malformed outputs can become repair fixtures).
            replies = [p.get("content") for m in (res.raw_response or []) if m.get("kind") == "response"
                       for p in m.get("parts", []) if p.get("part_kind") == "text"]
            if replies:
                entry["replies"] = [str(r)[:6000] for r in replies]
            try:
                d = validate_decision(res.proposal, max_calls=6)  # rejects calls missing required args
                entry["schema_valid"] = True
                problems = contract_problems(d["calls"], _catalog(catalog_n))
                entry["executable"] = not problems
                if problems:
                    entry["contract_problems"] = problems[:10]
                entry["tools"] = [c["tool"] for c in d["calls"]]
                entry["calls"] = [{"tool": c["tool"], "args": json.dumps(c["args"], sort_keys=True)[:600]} for c in d["calls"]]
                entry["control"] = d["control"]
            except Exception as exc:
                entry["schema_valid"] = False
                entry["validation_error"] = str(exc)[:300]
        except ModelCallFailed as exc:
            spent["requests"] += max(1, int((exc.possible_usage or {}).get("calls") or 0))
            entry.update({"outcome": "model-call-failed", "retryable": exc.retryable, "error": str(exc)[:300],
                          "possible_usage": exc.possible_usage})
        entry["seconds"] = round(time.time() - t0, 2)
        if expect == "valid-decision":
            entry["pass"] = (entry.get("outcome") == "decision" and bool(entry.get("schema_valid"))
                             and bool(entry.get("executable")) and bool(entry.get("calls") or entry.get("control")))
        elif expect == "bounded-or-decision":
            entry["pass"] = entry.get("outcome") in ("decision", "bounded-no-decision") and bool(entry.get("schema_valid"))
        else:
            entry["pass"] = entry.get("outcome") == expect.split(":")[0] and (
                ":" not in expect or str(entry.get("retryable")).lower() == expect.split(":")[1])
        receipt["scenarios"].append(entry)

    for name, view, cat_n, _ in SCENARIOS:
        run(name, {}, view, cat_n, None, 3, "valid-decision")
    previews = {"art_vp0": {"preview": "ids,predictions\n0,3\n1,7\n...", "truncated": True}}
    tools = {"inspect_evidence": lambda a, m=8192: previews.get(a, {"available": False}),
             "search_components": lambda t="", c=None: {"components": _catalog(4)},
             "describe_component": lambda r: {"ref": r, "body": {"manifest": {"entry": "x:y"}}},
             "get_job": lambda j: {"id": j, "status": "succeeded"}}
    run("read-tools-available", {}, _view([_job(0, 0.84)]), 6, tools, 6, "valid-decision")
    run("provider-timeout", {"request_timeout": 0.001}, _view(), 6, None, 1, "model-call-failed:true")
    run("request-limit-with-tool-loop", {}, _view([_job(0, 0.84)],
        steering=[{"by": "check", "instruction": "Inspect art_vp0 with inspect_evidence before deciding."}]), 6, tools, 1,
        "bounded-or-decision")
    run("unknown-model-id", {"model": f"{provider}:eidos-no-such-model"}, _view(), 6, None, 1, "model-call-failed:false")
    receipt["total_requests_counted"] = spent["requests"]
    receipt["finished_at"] = time.time()
    receipt["summary"] = {"scenarios": len(receipt["scenarios"]),
                          "passed": sum(1 for s in receipt["scenarios"] if s.get("pass")),
                          "schema_conformance": [s.get("schema_valid") for s in receipt["scenarios"]
                                                 if s.get("expect") == "valid-decision"]}
    print(json.dumps(receipt, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
