"""Resumable-training checkpoints with storage isolation.

A checkpoint *state* is ``{"manifest": {...}, "entries": {name: value}}``. Entry names:

``model`` ``optimizer`` ``scheduler`` ``grad_scaler`` ``rng.python`` ``rng.torch`` ``rng.numpy``
``rng.cuda`` ``data_cursor`` ``step`` and ``component.<name>`` (component-owned state).

The manifest has ``schema`` (``"eidos-checkpoint/1"``), ``state_entries`` (sorted entry names),
``parameters``/``buffers`` (name -> shape/dtype), ``optimizer_param_names`` (parameter names in
optimizer order, so optimizer slots can be mapped by name), ``step``, ``torch_version`` and
``complete``. :func:`save_checkpoint` writes one file via ``torch.save`` (atomically).

Isolation rule: everything captured *and* everything restored is cloned. ``optimizer.state_dict()``
returns references and ``optimizer.load_state_dict`` may keep the incoming tensors, so loading one
optimizer's ``state_dict()`` into another aliases Adam moments; :func:`restore_checkpoint` avoids
this and :func:`verify_storage_independence` proves it.
"""

from __future__ import annotations

import os
import random
from typing import Any, Dict, List, Mapping, Optional, Tuple

from eidos_pytorch._util import (
    clone_tree,
    describe_tensors,
    is_tensor,
    storage_ptr,
    torch_module,
)

SCHEMA = "eidos-checkpoint/1"


class CheckpointError(Exception):
    """A checkpoint is incomplete, inconsistent with the target, or malformed."""


class StorageSharedError(CheckpointError):
    """Two supposedly independent states share tensor storage."""


# ---- RNG ----------------------------------------------------------------------------------------
def _numpy():
    try:
        import numpy

        return numpy
    except ImportError:
        return None


def capture_rng() -> Dict[str, Any]:
    torch = torch_module()
    version, internal, gauss = random.getstate()
    rng: Dict[str, Any] = {
        "rng.python": [version, list(internal), gauss],
        "rng.torch": torch.get_rng_state().clone(),
    }
    np = _numpy()
    if np is not None:
        name, keys, pos, has_gauss, cached = np.random.get_state()
        rng["rng.numpy"] = [name, [int(k) for k in keys], int(pos), int(has_gauss), float(cached)]
    if torch.cuda.is_available():
        rng["rng.cuda"] = [s.clone() for s in torch.cuda.get_rng_state_all()]
    return rng


def restore_rng(entries: Mapping[str, Any]) -> List[str]:
    """Restore whichever RNG entries exist; return notes about anything skipped."""
    torch = torch_module()
    notes: List[str] = []
    if "rng.python" in entries:
        version, internal, gauss = entries["rng.python"]
        random.setstate((version, tuple(internal), gauss))
    if "rng.torch" in entries:
        torch.set_rng_state(entries["rng.torch"].clone().cpu())
    if "rng.numpy" in entries:
        np = _numpy()
        if np is None:
            notes.append("numpy RNG state present but numpy is not importable; skipped")
        else:
            name, keys, pos, has_gauss, cached = entries["rng.numpy"]
            np.random.set_state((name, np.array(keys, dtype="uint32"), pos, has_gauss, cached))
    if "rng.cuda" in entries:
        if torch.cuda.is_available() and torch.cuda.device_count() == len(entries["rng.cuda"]):
            torch.cuda.set_rng_state_all([s.clone().cpu() for s in entries["rng.cuda"]])
        else:
            notes.append("CUDA RNG state present but device count/availability differs; skipped")
    return notes


# ---- capture / restore --------------------------------------------------------------------------
def capture_checkpoint(
    model: Any,
    optimizer: Any = None,
    scheduler: Any = None,
    scaler: Any = None,
    *,
    step: int = 0,
    data_cursor: Any = None,
    components: Optional[Mapping[str, Any]] = None,
    rng: bool = True,
    accumulation_position: int = 0,
    extra_manifest: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Snapshot live training objects into an isolated state (all tensors cloned).

    ``components`` maps names to objects with ``state_dict()``. Capturing with
    ``accumulation_position != 0`` (mid-accumulation) marks the checkpoint incomplete, because
    accumulated gradients and microbatch position are not saved; prefer a post-step boundary.
    """
    torch = torch_module()
    entries: Dict[str, Any] = {"model": clone_tree(model.state_dict()), "step": int(step)}
    if optimizer is not None:
        entries["optimizer"] = clone_tree(optimizer.state_dict())
    if scheduler is not None:
        entries["scheduler"] = clone_tree(scheduler.state_dict())
    if scaler is not None:
        entries["grad_scaler"] = clone_tree(scaler.state_dict())
    if rng:
        entries.update(capture_rng())
    if data_cursor is not None:
        entries["data_cursor"] = clone_tree(data_cursor)
    for name, comp in (components or {}).items():
        entries[f"component.{name}"] = clone_tree(comp.state_dict())
    param_names = {id(p): n for n, p in model.named_parameters()}
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "state_entries": sorted(entries),
        "parameters": describe_tensors(dict(model.named_parameters())),
        "buffers": describe_tensors(dict(model.named_buffers())),
        "step": int(step),
        "torch_version": str(torch.__version__),
        "complete": accumulation_position == 0,
    }
    if optimizer is not None:
        manifest["optimizer_param_names"] = [
            [param_names.get(id(p)) for p in group["params"]] for group in optimizer.param_groups
        ]
    if accumulation_position != 0:
        manifest["incomplete_reason"] = f"captured mid-accumulation at microbatch {accumulation_position}"
    manifest.update(extra_manifest or {})
    return {"manifest": manifest, "entries": entries}


def _check_model_matches(model: Any, manifest: Mapping[str, Any]) -> None:
    live = describe_tensors(dict(model.named_parameters()))
    saved = manifest.get("parameters", {})
    if set(live) != set(saved):
        raise CheckpointError(
            f"parameter names differ: only in checkpoint {sorted(set(saved) - set(live))}, "
            f"only in model {sorted(set(live) - set(saved))}"
        )
    for name, spec in live.items():
        if spec != saved[name]:
            raise CheckpointError(f"parameter {name!r}: model has {spec}, checkpoint has {saved[name]}")


def restore_checkpoint(
    state: Mapping[str, Any],
    model: Any,
    optimizer: Any = None,
    scheduler: Any = None,
    scaler: Any = None,
    *,
    components: Optional[Mapping[str, Any]] = None,
    restore_rng_state: bool = True,
    allow_incomplete: bool = False,
) -> Dict[str, Any]:
    """Load ``state`` into live objects, cloning everything so the child never aliases the parent.

    Every stateful entry in the checkpoint needs a target and every target needs an entry
    (``CheckpointError`` otherwise): silently dropping optimizer or component state would turn an
    exact resume into an unlabelled warm start. Use :func:`apply_migration` for intentional changes.
    Returns ``{"step", "data_cursor", "notes"}``.
    """
    manifest, entries = validate_state(state, allow_incomplete=allow_incomplete)
    _check_model_matches(model, manifest)
    targets = {"optimizer": optimizer, "scheduler": scheduler, "grad_scaler": scaler}
    for name, target in targets.items():
        if name in entries and target is None:
            raise CheckpointError(f"checkpoint has {name!r} state but no target was supplied")
        if target is not None and name not in entries:
            raise CheckpointError(f"a {name} target was supplied but the checkpoint has no {name!r} state")
    comp_entries = {k.split(".", 1)[1] for k in entries if k.startswith("component.")}
    comps = dict(components or {})
    if comp_entries != set(comps):
        raise CheckpointError(
            f"component state mismatch: checkpoint {sorted(comp_entries)} vs supplied {sorted(comps)}"
        )
    model.load_state_dict(clone_tree(entries["model"]), strict=True)
    for name, target in targets.items():
        if target is not None:
            target.load_state_dict(clone_tree(entries[name]))
    for name, comp in comps.items():
        comp.load_state_dict(clone_tree(entries[f"component.{name}"]))
    notes = restore_rng(entries) if restore_rng_state else []
    return {"step": entries["step"], "data_cursor": clone_tree(entries.get("data_cursor")), "notes": notes}


def validate_state(
    state: Mapping[str, Any], allow_incomplete: bool = False
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    manifest, entries = state.get("manifest"), state.get("entries")
    if not isinstance(manifest, dict) or not isinstance(entries, dict):
        raise CheckpointError("not an Eidos checkpoint state (missing manifest/entries)")
    if manifest.get("schema") != SCHEMA:
        raise CheckpointError(f"unsupported checkpoint schema {manifest.get('schema')!r}")
    if sorted(entries) != sorted(manifest.get("state_entries", [])):
        raise CheckpointError(
            "manifest.state_entries does not match the stored entries (truncated or edited?)"
        )
    if not manifest.get("complete") and not allow_incomplete:
        raise CheckpointError(
            f"checkpoint is incomplete: {manifest.get('incomplete_reason', 'complete flag unset')}"
        )
    return manifest, entries


# ---- files --------------------------------------------------------------------------------------
def save_checkpoint(state: Mapping[str, Any], path: Any) -> str:
    """Write a single-file checkpoint atomically (temp file then rename); returns the path."""
    torch = torch_module()
    validate_state(state, allow_incomplete=True)
    path = os.fspath(path)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        torch.save({"manifest": state["manifest"], "entries": state["entries"]}, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def load_checkpoint(
    path: Any, *, weights_only: bool = True, allow_incomplete: bool = False
) -> Dict[str, Any]:
    """Read a checkpoint file onto CPU.

    ``weights_only=True`` (default) uses torch's restricted unpickler: it accepts everything the
    SDK writes plus plain-data component state. Pass ``False`` only for trusted files whose
    component state contains arbitrary pickled objects.
    """
    torch = torch_module()
    state = torch.load(os.fspath(path), map_location="cpu", weights_only=weights_only)
    validate_state(state, allow_incomplete=allow_incomplete)
    return state


# ---- storage independence -----------------------------------------------------------------------
def collect_tensors(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """Map a path to every tensor reachable from a module (params+buffers), optimizer (state
    tensors), checkpoint state, or nested dict/list/tuple of these."""
    out: Dict[str, Any] = {}

    def add(path: str, value: Any) -> None:
        if is_tensor(value):
            out[path] = value
        elif isinstance(value, Mapping):
            for k, v in value.items():
                add(f"{path}.{k}" if path else str(k), v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                add(f"{path}[{i}]", v)
        elif hasattr(value, "named_parameters") and hasattr(value, "named_buffers"):
            for k, v in list(value.named_parameters()) + list(value.named_buffers()):
                add(f"{path}.{k}" if path else k, v)
        elif hasattr(value, "param_groups") and hasattr(value, "state_dict"):
            add(f"{path}.optimizer" if path else "optimizer", value.state_dict()["state"])

    add(prefix, obj)
    return out


def find_shared_storage(parent: Any, child: Any) -> List[Tuple[str, str]]:
    """List ``(parent_path, child_path)`` pairs that point at the same storage."""
    by_ptr: Dict[int, List[str]] = {}
    for path, tensor in collect_tensors(parent).items():
        ptr = storage_ptr(tensor)
        if ptr:
            by_ptr.setdefault(ptr, []).append(path)
    shared = []
    for path, tensor in collect_tensors(child).items():
        for parent_path in by_ptr.get(storage_ptr(tensor), []) if storage_ptr(tensor) else []:
            shared.append((parent_path, path))
    return shared


def verify_storage_independence(parent: Any, child: Any) -> int:
    """Raise :class:`StorageSharedError` if any tensor of ``child`` shares storage with ``parent``.

    Each argument may be an ``nn.Module`` (parameters and buffers), an optimizer (state tensors),
    a checkpoint state, or a (nested) dict/list/tuple of these, e.g. ``{"model": m, "opt": o}``.
    Returns the number of child tensors checked.
    """
    shared = find_shared_storage(parent, child)
    if shared:
        sample = ", ".join(f"{p} <-> {c}" for p, c in shared[:5])
        raise StorageSharedError(f"{len(shared)} tensor(s) share storage: {sample}")
    return len(collect_tensors(child))
