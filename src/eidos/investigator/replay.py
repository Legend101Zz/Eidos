"""Offline decision evaluation over recorded turns (design §"Recorded turns as an evaluation dataset").

Three different tests are kept apart:
* recovery replay — orchestration resumes a recorded execution (tests/integration/test_workflows.py);
* offline decision evaluation — this module: an alternate context policy or model chooses from the
  *recorded authorized view at the original horizon*; nothing is executed and the outcome of an
  unexecuted counterfactual action is reported as unknown;
* end-to-end research evaluation — runs choices in a task environment (separate harness).

Isolation: run this with a read-only principal (``reader`` role). Such a principal cannot submit
commands, so a benchmark replay can never resubmit production work.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from typing import Any

from eidos.application.handlers.finalization_evidence import model_projection
from eidos.domain.turns import validate_decision
from eidos.investigator.context import POLICY_ID, POLICY_V1, POLICY_V2, POLICY_V4, pack
from eidos.investigator.runtime import DecisionRequest, InvestigatorRuntime

ContextPolicy = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]


def minimal_policy(view: dict, catalog: list[dict]) -> dict:
    """An alternate context policy for comparison: question, charter and job metrics only."""
    slim = {**view, "events": [], "claims": [], "jobs": [{**j, "config": None, "outputs": j.get("outputs", [])[:2]}
                                                       for j in view.get("jobs", [])[-10:]]}
    out = pack(slim, catalog[:10])
    out["policy"] = "context-policy/minimal-1"
    return out


def _policy_v1(view: dict, catalog: list[dict]) -> dict:
    return pack(view, catalog, policy=POLICY_V1)


def _policy_v2(view: dict, catalog: list[dict]) -> dict:
    return pack(view, catalog, policy=POLICY_V2)


CONTEXT_POLICIES: dict[str, ContextPolicy] = {POLICY_ID: pack, POLICY_V2: _policy_v2, POLICY_V1: _policy_v1,
                                              POLICY_V4: lambda view, catalog: pack(view, catalog, policy=POLICY_V4),
                                              "context-policy/minimal-1": minimal_policy}


def load_recorded_turn(client: Any, project: str, turn_id: str) -> dict[str, Any]:
    turn = client.query("get_turn", {"turn_id": turn_id}, project=project)
    ctx = turn.get("context") or {}
    if not ctx.get("source_manifest_artifact_id"):
        raise ValueError(f"turn {turn_id} has no recorded source manifest")
    with tempfile.NamedTemporaryFile() as tmp:
        client.download(project, ctx["source_manifest_artifact_id"], tmp.name)
        source = json.load(open(tmp.name))
    if int(source.get("horizon", -1)) != int(turn["horizon"]):
        raise ValueError("source manifest horizon does not match the turn specification")
    leaked = [e for e in source.get("events", []) if int(e["seq"]) > int(turn["horizon"])]
    if leaked:
        raise ValueError(f"source manifest contains {len(leaked)} events after the horizon")
    accepted = [d for d in turn["decisions"] if d["accepted"] and not d.get("revoked")]  # revoked: never run (F2c)
    return {"turn": turn, "source": source, "recorded_decisions": accepted}


def evaluate_turn(client: Any, project: str, turn_id: str, runtime: InvestigatorRuntime, *,
                  context_policy: str = POLICY_ID) -> dict[str, Any]:
    rec = load_recorded_turn(client, project, turn_id)
    source = dict(rec["source"])
    catalog = source.pop("catalog", [])
    source.pop("audit_held_out_attestation", None)
    if not source.pop("model_evidence_projected", False) and (
            (source.get("charter") or {}).get("held_out") or rec["turn"]["spec"].get("evidence_allowlist") is not None):
        blocked = client.query("model_evidence_exclusions", {"turn_id": turn_id}, project=project)["excluded"]
        source, catalog = model_projection(source, catalog, set(blocked))
    packed = CONTEXT_POLICIES[context_policy](source, catalog)
    req = DecisionRequest(turn_id=f"eval:{turn_id}", slot=0, context=packed["text"], turn_so_far=[],
                          limits=rec["turn"]["spec"]["limits"], settings={}, read_tools={}, view=source,
                          conclude_only=bool(rec["turn"]["spec"].get("finalization")))
    result = runtime.decide(req)
    try:
        proposal = validate_decision(result.proposal, max_calls=rec["turn"]["spec"]["limits"]["max_calls"])
        valid = True
    except Exception as exc:  # an invalid proposal is itself an evaluation outcome
        proposal, valid = {"error": str(exc), "raw": result.proposal}, False
    original = rec["recorded_decisions"][0]["body"] if rec["recorded_decisions"] else None
    orig_calls = [(c["tool"], json.dumps(c["args"], sort_keys=True)) for c in (original or {}).get("calls", [])]
    alt_calls = [(c["tool"], json.dumps(c["args"], sort_keys=True)) for c in proposal.get("calls", [])] if valid else []
    return {
        "evaluation": "offline-decision/1", "turn_id": turn_id, "horizon": rec["turn"]["horizon"],
        "context_policy": packed["policy"], "runtime": result.model_settings, "usage": result.usage,
        "proposal_valid": valid, "proposal": proposal, "recorded_decision": original,
        "same_calls_as_recorded": orig_calls == alt_calls,
        "executed": False,
        "outcome": "unknown: counterfactual actions were not executed" if orig_calls != alt_calls
                   else "identical calls; recorded outcome applies to these inputs only",
    }
