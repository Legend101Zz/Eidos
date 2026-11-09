"""Edit named parameter entries on a private copy and stage a child checkpoint."""

from __future__ import annotations

import os
from typing import Any

from eidos_pytorch import (
    apply_migration,
    load_checkpoint,
    save_checkpoint,
    state_fingerprint,
    verify_storage_independence,
)
from eidos_wire import sha256_file
from eidos_worker.errors import ContractError

OPS = ("set", "scale", "add")


def _validate(edits: Any) -> list[dict[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise ContractError("config.edits must be a non-empty list")
    for e in edits:
        if not isinstance(e, dict) or "entry" not in e or e.get("op", "set") not in OPS or "value" not in e:
            raise ContractError(f"edit must have entry, value and op in {OPS}: {e!r}")
    return edits


def _index(tensor, index):
    if index is None:
        return ()
    if not isinstance(index, list) or len(index) > tensor.dim() or not all(isinstance(i, int) for i in index):
        raise ContractError(f"index {index!r} is not a list of integers valid for shape {list(tensor.shape)}")
    return tuple(index)


def run(inputs, config, *, reader, writer, context):
    edits = _validate(config.get("edits"))
    parent_path = reader.path("checkpoint")
    digest_before, _ = sha256_file(parent_path)
    parent = load_checkpoint(parent_path)
    parent_model_fp = state_fingerprint(parent["entries"]["model"])
    applied: list[dict[str, Any]] = []

    def edit_model(model, parent_state, spec):
        import torch

        for e in edits:
            name = e["entry"]
            if name not in model:
                raise ContractError(f"model has no parameter/buffer entry {name!r}")
            tensor = model[name]
            if not torch.is_floating_point(tensor):
                raise ContractError(f"entry {name!r} is not floating point")
            idx = _index(tensor, e.get("index"))
            view = tensor[idx] if idx else tensor
            before = view.clone()
            op, value = e.get("op", "set"), float(e["value"])
            with torch.no_grad():
                if op == "set":
                    view.fill_(value)
                elif op == "scale":
                    view.mul_(value)
                else:
                    view.add_(value)
            small = view.numel() <= 16
            applied.append(
                {
                    "entry": name,
                    "index": e.get("index"),
                    "op": op,
                    "value": value,
                    "elements": int(view.numel()),
                    "before": before.tolist() if small else None,
                    "after": view.tolist() if small else None,
                    "norm_before": float(before.norm()),
                    "norm_after": float(view.norm()),
                }
            )
        return model

    migration = {
        "entries": {k: {"action": "copy"} for k in parent["entries"]},
        "reason": config.get("reason", "weight-edit"),
    }
    migration["entries"]["model"] = {"action": "transform", "transform": "edit"}
    child = apply_migration(parent, migration, {"edit": edit_model})
    child["manifest"]["edits"] = [
        {k: v for k, v in a.items() if k not in ("before", "after")} for a in applied
    ]
    shared_checked = verify_storage_independence(parent, child)
    path = save_checkpoint(child, os.path.join(str(context.scratch_dir), "child.ckpt"))

    digest_after, _ = sha256_file(parent_path)
    if digest_after != digest_before or state_fingerprint(parent["entries"]["model"]) != parent_model_fp:
        raise ContractError("parent checkpoint changed during the edit (storage independence violated)")
    meta = parent["entries"].get("component.train-meta") or {}
    writer.stage_file(
        "checkpoint",
        path,
        media_type="application/octet-stream",
        schema="eidos-checkpoint/1",
        role="checkpoint",
    )
    return {
        "edits": applied,
        "parent_digest": digest_before,
        "parent_unchanged": True,
        "storage_independent": True,
        "tensors_checked_for_shared_storage": shared_checked,
        "parent_model_fingerprint": parent_model_fp,
        "child_model_fingerprint": state_fingerprint(child["entries"]["model"]),
        "restore_class": "warm-start",
        "migration_actions": child["manifest"]["migration"]["actions"],
        "checkpoints": {
            "checkpoint": {
                "step": int(child["manifest"]["step"]),
                "state_entries": list(child["manifest"]["state_entries"]),
                "complete": True,
                "builder": meta.get("builder_ref") or meta.get("builder"),
            }
        },
        "metrics": {"edited_elements": sum(a["elements"] for a in applied)},
    }
