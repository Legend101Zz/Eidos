"""Score a checkpoint with the project's adapter and stage paired prediction rows."""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Mapping
from typing import Any

from eidos_pytorch import load_adapter, load_checkpoint
from eidos_worker.errors import ContractError

FIELDS = [
    "pair_id",
    "reference_logit",
    "variant_logit",
    "reference_label",
    "variant_label",
    "stratum",
    "cluster",
]


def _extend_path(config: Mapping[str, Any], context: Any) -> None:
    base = context.invocation.component.get("source_dir") or "."
    for entry in config.get("python_path") or []:
        path = os.path.abspath(entry if os.path.isabs(entry) else os.path.join(base, entry))
        if path not in sys.path:
            sys.path.insert(0, path)


def _check_rows(rows) -> dict[str, float]:
    seen, both, ref = set(), 0, 0
    for row in rows:
        missing = [k for k in FIELDS if k not in row]
        if missing:
            raise ContractError(f"adapter.evaluation_rows row lacks {missing}")
        pid = str(row["pair_id"])
        if pid in seen:
            raise ContractError(f"duplicate pair id {pid}")
        seen.add(pid)
        a, b = float(row["reference_logit"]), float(row["variant_logit"])
        if not (math.isfinite(a) and math.isfinite(b)):
            raise ContractError(f"non-finite score in pair {pid}")
        first, second = int(row["reference_label"]), int(row["variant_label"])
        if first not in (0, 1) or second not in (0, 1):
            raise ContractError(f"non-binary label in pair {pid}")
        ok = (a > 0) == bool(first)
        ref += ok
        both += ok and ((b > 0) == bool(second))
    n = len(seen)
    if not n:
        raise ContractError("adapter.evaluation_rows returned no rows")
    return {"pairs": n, "both_correct_rate": both / n, "reference_accuracy": ref / n}


def run(inputs, config, *, reader, writer, context):
    import torch

    _extend_path(config, context)
    torch.set_num_threads(int(config.get("torch_threads", 1)))
    if "adapter" not in config:
        raise ContractError("config.adapter is required")
    adapter = load_adapter(config["adapter"])
    if not callable(getattr(adapter, "evaluation_rows", None)):
        raise ContractError("adapter must define evaluation_rows(model, split, config)")
    state = load_checkpoint(reader.path("checkpoint"))
    meta = state["entries"].get("component.train-meta") or {}
    build_cfg: dict[str, Any] = dict(config.get("adapter_config") or {})
    builder = config.get("builder", meta.get("builder"))
    if builder is not None:
        build_cfg["builder"] = builder
    model = adapter.build(build_cfg)
    model.load_state_dict(state["entries"]["model"], strict=True)
    model.eval()
    evaluation = dict(config.get("evaluation") or {})
    split = evaluation.pop("split", "test")
    cluster = evaluation.pop("cluster", config.get("cluster", meta.get("seed", 0)))
    eval_cfg = {**build_cfg, "seed": meta.get("seed", 0), **evaluation, "cluster": str(cluster)}
    with torch.no_grad():
        rows = list(adapter.evaluation_rows(model, split, eval_cfg))
    metrics = _check_rows(rows)
    writer.stage_rows("predictions", rows, format="csv", fieldnames=FIELDS, schema="labeled-binary-pairs/1")
    context.record("input_checkpoint_step", int(state["manifest"]["step"]))
    context.record("builder", builder)
    context.record("split", split)
    context.record("cluster", str(cluster))
    context.record("rows", metrics["pairs"])
    context.record("metrics", metrics)
