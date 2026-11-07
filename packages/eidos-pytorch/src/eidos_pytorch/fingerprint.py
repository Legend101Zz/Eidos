"""State and behavior fingerprints, and the fresh-process behavioral restore check."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from eidos_pytorch._util import dtype_name, is_tensor, tensor_bytes, torch_module, walk


def state_fingerprint(obj: Any) -> str:
    """sha256 over every leaf of ``obj`` (path, dtype, shape and raw bytes for tensors).

    ``obj`` may be a state dict, a checkpoint state, an optimizer ``state_dict()`` or any nested
    structure; an ``nn.Module`` is fingerprinted through its ``state_dict()``. Bitwise: two
    states are equal only if every byte is.
    """
    if hasattr(obj, "state_dict") and callable(obj.state_dict):
        obj = obj.state_dict()
    h = hashlib.sha256()
    for path, leaf in walk(obj):
        h.update(path.encode())
        h.update(b"\0")
        if is_tensor(leaf):
            h.update(f"T:{dtype_name(leaf)}:{list(leaf.shape)}".encode())
            h.update(tensor_bytes(leaf))
        else:
            h.update(("V:" + json.dumps(leaf, sort_keys=True, default=repr)).encode())
        h.update(b"\1")
    return h.hexdigest()


# ---- behavior -----------------------------------------------------------------------------------
def to_jsonable(obj: Any) -> Any:
    """Convert outputs (tensors, numbers, nested containers) to a JSON structure that preserves
    dtype and shape. Floats stay exact (Python floats round-trip through JSON)."""
    if is_tensor(obj):
        t = obj.detach().cpu()
        return {"__tensor__": dtype_name(t), "shape": list(t.shape), "data": t.reshape(-1).tolist()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return repr(obj)


def _leaves(j: Any, path: str = ""):
    if isinstance(j, dict) and "__tensor__" in j:
        yield path, j
    elif isinstance(j, dict):
        for k in sorted(j):
            yield from _leaves(j[k], f"{path}.{k}")
    elif isinstance(j, list):
        for i, v in enumerate(j):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, j


def compare_outputs(expected: Any, actual: Any, atol: float = 1e-5, rtol: float = 1e-5) -> Dict[str, Any]:
    """Compare two ``to_jsonable`` structures. ``ok`` requires equal structure, dtypes and shapes
    and ``|a-e| <= atol + rtol*|e|`` for every element (non-finite values must match exactly)."""
    problems: List[str] = []
    max_abs = 0.0
    exp, act = dict(_leaves(expected)), dict(_leaves(actual))
    if set(exp) != set(act):
        problems.append(f"output structure differs: {sorted(set(exp) ^ set(act))}")
    for path in sorted(set(exp) & set(act)):
        e, a = exp[path], act[path]
        if isinstance(e, dict):
            if not isinstance(a, dict) or e["__tensor__"] != a["__tensor__"] or e["shape"] != a["shape"]:
                problems.append(f"{path}: dtype/shape differ")
                continue
            pairs: Any = zip(e["data"], a["data"])
        elif isinstance(e, float) and isinstance(a, (int, float)):
            pairs = [(e, a)]
        else:
            if e != a:
                problems.append(f"{path}: {e!r} != {a!r}")
            continue
        for x, y in pairs:
            if isinstance(x, bool) or isinstance(y, bool) or isinstance(x, str) or isinstance(y, str):
                if x != y:
                    problems.append(f"{path}: {x!r} != {y!r}")
                    break
                continue
            if not (math.isfinite(x) and math.isfinite(y)):
                if not (x == y or (math.isnan(x) and math.isnan(y))):
                    problems.append(f"{path}: non-finite mismatch {x} vs {y}")
                    break
                continue
            diff = abs(x - y)
            max_abs = max(max_abs, diff)
            if diff > atol + rtol * abs(x):
                problems.append(f"{path}: |{y} - {x}| = {diff} exceeds tolerance")
                break
    return {"ok": not problems, "max_abs_diff": max_abs, "problems": problems[:10]}


def reference_outputs(adapter: Any, model: Any, config: Dict[str, Any], seed: int = 0) -> Any:
    """Run ``adapter.predict`` on ``adapter.examples(config)`` (seeded, eval mode, no grad) and
    return the outputs as a JSON structure."""
    torch = torch_module()
    was_training = model.training
    model.eval()
    try:
        torch.manual_seed(seed)
        batch = adapter.examples(config)
        with torch.no_grad():
            out = adapter.predict(model, batch)
    finally:
        model.train(was_training)
    return to_jsonable(out)


def behavior_fingerprint(outputs: Any) -> str:
    """sha256 of a ``to_jsonable`` outputs structure (exact; use :func:`compare_outputs` for
    tolerance-based comparison)."""
    return hashlib.sha256(json.dumps(outputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_behavior(adapter: Any, model: Any, config: Dict[str, Any], seed: int = 0) -> Dict[str, Any]:
    """Record the behavior fixture to store beside a checkpoint/export."""
    outputs = reference_outputs(adapter, model, config, seed)
    return {"seed": seed, "outputs": outputs, "fingerprint": behavior_fingerprint(outputs)}


@dataclass
class BehaviorReport:
    ok: bool
    strict_load_ok: bool
    max_abs_diff: Optional[float] = None
    problems: List[str] = field(default_factory=list)
    stage: str = ""


def behavioral_restore_check(
    weights_path: Any,
    adapter_spec: str,
    config: Dict[str, Any],
    recorded: Dict[str, Any],
    *,
    builder_spec: Optional[str] = None,
    atol: float = 1e-5,
    rtol: float = 1e-5,
    python: Optional[str] = None,
    extra_paths: Sequence[str] = (),
    timeout: float = 300.0,
) -> BehaviorReport:
    """Load weights into a named builder in a NEW Python process and compare outputs to ``recorded``.

    ``weights_path`` is an Eidos checkpoint file (its ``model`` entry is used) or a plain
    ``torch.save`` state dict. ``adapter_spec`` (``module:attr``) supplies ``examples`` and
    ``predict``; ``builder_spec`` (``module:attr``, a callable taking ``config``) overrides
    ``adapter.build`` and is how a *wrong builder* is tested. Weights load with ``strict=True``;
    strict loading success says nothing about behavior, which is why this check exists.
    """
    with tempfile.TemporaryDirectory(prefix="eidos-behavior-") as tmp:
        req, res = os.path.join(tmp, "request.json"), os.path.join(tmp, "result.json")
        with open(req, "w") as f:
            json.dump(
                {
                    "weights": os.fspath(weights_path),
                    "adapter": adapter_spec,
                    "builder": builder_spec,
                    "config": config,
                    "seed": recorded.get("seed", 0),
                    "sys_path": [os.path.abspath(p) for p in extra_paths],
                },
                f,
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.abspath(p) for p in extra_paths] + [p for p in [env.get("PYTHONPATH", "")] if p]
        )
        proc = subprocess.run(
            [python or sys.executable, "-m", "eidos_pytorch._fresh", req, res],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if proc.returncode != 0 or not os.path.exists(res):
            return BehaviorReport(
                False, False, problems=[f"fresh process failed: {proc.stderr[-800:]}"], stage="process"
            )
        with open(res) as f:
            result = json.load(f)
    if not result["strict_load_ok"]:
        return BehaviorReport(
            False, False, problems=[result.get("error", "strict load failed")], stage="load"
        )
    cmp = compare_outputs(recorded["outputs"], result["outputs"], atol, rtol)
    return BehaviorReport(cmp["ok"], True, cmp["max_abs_diff"], cmp["problems"], stage="compare")
