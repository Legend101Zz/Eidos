"""Run a control suite against a target component, in process, and report raw results.

The runner decides nothing about approval: it executes every control input x repetition through the
target's real entry point (via the worker runner, in this interpreter) and records what happened.
``eidos.domain.qualification.evaluate_qualification`` turns the results into a gate decision.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from eidos.domain.qualification import validate_control_suite
from eidos_wire import load_manifest
from eidos_worker.errors import ContractError
from eidos_worker.run import execute
from eidos_worker.testing import make_invocation
from qualification_runner.generators import GENERATORS

TARGET_PORT = "target.source/1"


def _resolve(value: Any, repetition: int) -> Any:
    if isinstance(value, dict):
        out = {k: _resolve(v, repetition) for k, v in value.items() if k != "$generate"}
        if "$generate" in value:
            spec = value["$generate"]
            if spec.get("name") not in GENERATORS:
                raise ContractError(f"unknown generator {spec.get('name')!r}")
            out.update(GENERATORS[spec["name"]](spec.get("params") or {}, repetition))
        return out
    if isinstance(value, list):
        return [_resolve(v, repetition) for v in value]
    return value


def _purge(source: str) -> None:
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and os.path.abspath(file).startswith(source + os.sep):
            del sys.modules[name]


def _run_once(
    manifest, source: str, control: dict[str, Any], repetition: int, slot: str, outcome_key: str
) -> dict[str, Any]:
    spec = control["input"]
    inputs = {port: _resolve(value, repetition) for port, value in (spec.get("inputs") or {}).items()}
    with tempfile.TemporaryDirectory(prefix="eidos-qual-") as tmp:
        inv = make_invocation(
            tmp,
            manifest.entry,
            inputs=inputs,
            config=copy.deepcopy(spec.get("config") or {}),
            source_dir=source,
            component_id=manifest.id,
            seed=repetition,
        )
        out = execute(inv)
        if out.status != "succeeded":
            category = (out.error or {}).get("category", "unknown")
            return {"detected": False, "outcome": f"error:{category}", "valid": False}
        entry = next((e for e in out.outputs if e.slot == slot), None)
        if entry is None:
            return {"detected": False, "outcome": "error:missing-output", "valid": False}
        evaluation = json.loads((Path(inv.staging_dir) / entry.filename).read_text(encoding="utf-8"))
    outcome = evaluation.get(outcome_key)
    return {"detected": outcome == "protocol-passed", "outcome": outcome, "valid": outcome is not None}


def run(inputs, config, *, writer, context):
    suite = validate_control_suite(config["suite"])
    port = context.invocation.ports.get(TARGET_PORT)
    if isinstance(port, dict) and "path" in port:
        target_path = port["path"]
    elif config.get("target_source"):  # fixtures only: a path relative to this component's source
        target_path = os.path.join(
            context.invocation.component.get("source_dir") or ".", config["target_source"]
        )
    else:
        raise ContractError(
            f"invocation.ports[{TARGET_PORT!r}]['path'] (the target's source directory) is required"
        )
    source = str(Path(target_path).resolve())
    manifest = load_manifest(Path(source) / "eidos-component.json")
    declared = (config.get("target") or {}).get("ref") or (suite.get("target") or {}).get("ref")
    if declared and declared != manifest.ref:
        raise ContractError(f"suite targets {declared!r} but the supplied source is {manifest.ref!r}")
    slot, outcome_key = config.get("result_slot", "evaluation"), config.get("outcome_key", "outcome")
    results: dict[str, list[dict[str, Any]]] = {}
    sys.path.insert(0, source)
    try:
        for control in suite["controls"]:
            reps = int(control.get("repetitions", 1))
            results[control["id"]] = [
                _run_once(manifest, source, control, k, slot, outcome_key) for k in range(reps)
            ]
            context.logger.info("control %s: %d repetitions", control["id"], reps)
    finally:
        _purge(source)
        while source in sys.path:
            sys.path.remove(source)
    writer.stage_json("qualification_results", results, schema="qualification-results/1")
    return {
        "target": manifest.ref,
        "controls": {cid: len(reps) for cid, reps in results.items()},
        "invalid_runs": sum(1 for reps in results.values() for r in reps if not r["valid"]),
        "metrics": {"controls": len(results), "runs": sum(len(r) for r in results.values())},
    }
