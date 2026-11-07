"""Run a planting/null control suite against a *planning template* and report raw results.

Journey-owned author fixture (not independent qualification). ``eidos.qualification-runner`` only
detects ``outcome == "protocol-passed"``, which a template never emits, so this runner judges what a
template can be judged on: given a request with a real candidate difference it must plan a comparison whose
arms differ (detected), given identical recipes it must plan none (not detected), and given a request that
smuggles a success threshold it must refuse. Refusals count as valid, undetected runs.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from eidos.domain.qualification import validate_control_suite
from eidos_wire import load_manifest
from eidos_worker.run import execute
from eidos_worker.testing import make_invocation


def _purge(source):
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file and os.path.abspath(file).startswith(source + os.sep):
            del sys.modules[name]


def _distinct(draft):
    pipes = {}
    for arm in draft["arms"]:
        pipes[arm["role"]] = [[(j["component"], json.dumps(j.get("config"), sort_keys=True)) for j in run["jobs"][:-1]]
                              for run in arm["runs"]]
    return pipes.get("baseline") != pipes.get("candidate")


def _once(manifest, source, control, rep):
    spec = control["input"]
    with tempfile.TemporaryDirectory(prefix="eidos-tq-") as tmp:
        inv = make_invocation(tmp, manifest.entry, inputs=spec["inputs"], config=spec.get("config") or {},
                              source_dir=source, component_id=manifest.id, seed=rep)
        out = execute(inv)
        if out.status != "succeeded":
            category = (out.error or {}).get("category", "unknown")
            if control.get("expect_refusal") and category == "contract":
                return {"detected": False, "outcome": "refused", "valid": True}
            return {"detected": False, "outcome": f"error:{category}", "valid": False}
        entry = next(e for e in out.outputs if e.slot == "draft")
        draft = json.loads((Path(inv.staging_dir) / entry.filename).read_text(encoding="utf-8"))
    if control.get("expect_refusal"):
        return {"detected": False, "outcome": "accepted-forbidden-request", "valid": True}
    distinct = _distinct(draft)
    return {"detected": distinct, "outcome": "distinct-arms-planned" if distinct else "identical-arms", "valid": True}


def run(inputs, config, *, writer, context):
    suite = validate_control_suite(config["suite"])
    source = str(Path(context.invocation.ports["target.source/1"]["path"]).resolve())
    manifest = load_manifest(Path(source) / "eidos-component.json")
    results = {}
    sys.path.insert(0, source)
    try:
        for control in suite["controls"]:
            results[control["id"]] = [_once(manifest, source, control, k) for k in range(int(control.get("repetitions", 1)))]
    finally:
        _purge(source)
        while source in sys.path:
            sys.path.remove(source)
    writer.stage_json("qualification_results", results, schema="qualification-results/1")
    return {"target": manifest.ref, "controls": {k: len(v) for k, v in results.items()},
            "invalid_runs": sum(1 for v in results.values() for r in v if not r["valid"]),
            "metrics": {"controls": len(results)}}
