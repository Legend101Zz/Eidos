"""Fresh-process behavioural check: do these weights behave the same under the named builder?

The reference behaviour is recorded in-process with the *reference builder* (the one that produced the
checkpoint: ``config.reference_builder`` or the builder saved in the checkpoint's train-meta). The
checkpoint is then loaded with the *checked builder* (``config.builder``) in a new Python process and
the adapter's reference batch is compared. A strict ``load_state_dict`` that succeeds proves nothing
about behaviour; that is why this check exists.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from eidos_pytorch import (
    behavioral_restore_check,
    load_adapter,
    load_checkpoint,
    record_behavior,
)
from eidos_worker.errors import ContractError


def _extend_path(config: Mapping[str, Any], context: Any) -> list:
    base = context.invocation.component.get("source_dir") or "."
    extra = []
    for entry in config.get("python_path") or []:
        path = os.path.abspath(entry if os.path.isabs(entry) else os.path.join(base, entry))
        if path not in sys.path:
            sys.path.insert(0, path)
        extra.append(path)
    return extra


def run(inputs, config, *, reader, writer, context):
    import torch

    extra = _extend_path(config, context)
    for key in ("adapter", "builder"):
        if key not in config:
            raise ContractError(f"config.{key} is required")
    torch.set_num_threads(1)
    adapter = load_adapter(config["adapter"])
    if not callable(getattr(adapter, "examples", None)):
        raise ContractError("adapter must define examples(config) for a behavioural check")
    path = reader.path("checkpoint")
    state = load_checkpoint(path)
    meta = state["entries"].get("component.train-meta") or {}
    base: dict[str, Any] = dict(config.get("adapter_config") or {})
    reference_builder = config.get("reference_builder", meta.get("builder"))
    if reference_builder is None:
        raise ContractError(
            "no reference builder: pass config.reference_builder (checkpoint has no train-meta)"
        )
    ref_cfg = dict(base, builder=reference_builder)
    model = adapter.build(ref_cfg)
    model.load_state_dict(state["entries"]["model"], strict=True)
    seed = int(config.get("seed", 0))
    recorded = record_behavior(adapter, model, ref_cfg, seed)
    check_cfg = dict(base, builder=config["builder"])
    # The fresh process inherits PYTHONPATH; add the directories this process imports project code from.
    paths = extra + [p for p in sys.path if p and os.path.isdir(p) and "site-packages" not in p]
    report = behavioral_restore_check(
        path,
        config["adapter"],
        check_cfg,
        recorded,
        python=config.get("python", sys.executable),
        extra_paths=list(dict.fromkeys(paths)),
        atol=float(config.get("atol", 1e-5)),
        rtol=float(config.get("rtol", 1e-5)),
    )
    result = {
        "behavior_match": bool(report.ok),
        "strict_load_ok": bool(report.strict_load_ok),
        "max_abs_diff": report.max_abs_diff,
        "builder": config["builder"],
        "reference_builder": reference_builder,
        "stage": report.stage,
        "problems": list(report.problems),
        "reference_fingerprint": recorded["fingerprint"],
    }
    writer.stage_json("behavior", result, schema="behavior-check-report/1")
    return result
