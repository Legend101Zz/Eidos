"""Branch state migration: derive a child checkpoint state from a parent state.

``migration`` is a plain dict (JSON-able, so the server can validate the same document)::

    {"entries": {"model": {"action": "transform", "transform": "reset_head"},
                 "optimizer": {"action": "init", "transform": "fresh_adam"},
                 "scheduler": {"action": "drop"},
                 "step": {"action": "copy"}, ...},
     "reason": "..."}                       # optional free text, kept in the child manifest

Actions per entry: ``copy`` (cloned), ``transform`` (``transforms[name](value, parent_state, spec)``
returns the new value), ``init`` (``transforms[name](None, parent_state, spec)`` creates state; the
entry may be absent in the parent), ``drop``. Every entry of the parent must be declared — an
undeclared parent entry is rejected, never silently copied or dropped. (The server validates the
same rule; this is defense in depth.)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping

from eidos_pytorch._util import clone_tree, describe_tensors
from eidos_pytorch.checkpoint import SCHEMA, CheckpointError, validate_state
from eidos_pytorch.fingerprint import state_fingerprint

ACTIONS = ("copy", "transform", "init", "drop")


class MigrationError(CheckpointError):
    """The migration is malformed or does not account for every parent state entry."""


def apply_migration(
    parent_state: Mapping[str, Any],
    migration: Mapping[str, Any],
    transforms: Mapping[str, Callable[[Any, Mapping[str, Any], Mapping[str, Any]], Any]],
) -> Dict[str, Any]:
    """Return a new child state; ``parent_state`` is never modified and nothing is shared with it."""
    parent_manifest, parent_entries = validate_state(parent_state)
    declared = migration.get("entries")
    if not isinstance(declared, Mapping):
        raise MigrationError("migration must contain an 'entries' mapping")
    undeclared = sorted(set(parent_entries) - set(declared))
    if undeclared:
        raise MigrationError(f"parent state entries not declared by the migration: {undeclared}")
    child: Dict[str, Any] = {}
    record: Dict[str, str] = {}
    for name in sorted(declared):
        spec = declared[name]
        action = spec.get("action") if isinstance(spec, Mapping) else None
        if action not in ACTIONS:
            raise MigrationError(f"entry {name!r}: action must be one of {ACTIONS}, got {action!r}")
        present = name in parent_entries
        if action == "drop":
            if not present:
                raise MigrationError(f"entry {name!r}: cannot drop state the parent does not have")
        elif action == "copy":
            if not present:
                raise MigrationError(f"entry {name!r}: cannot copy state the parent does not have")
            child[name] = clone_tree(parent_entries[name])
        else:
            if action == "transform" and not present:
                raise MigrationError(
                    f"entry {name!r}: cannot transform state the parent does not have; use 'init'"
                )
            if action == "init" and present:
                raise MigrationError(
                    f"entry {name!r}: 'init' would overwrite parent state; use 'transform' or 'drop'"
                )
            tname = spec.get("transform")
            if tname not in transforms:
                raise MigrationError(f"entry {name!r}: transform {tname!r} was not supplied")
            source = clone_tree(parent_entries[name]) if present else None
            child[name] = transforms[tname](source, parent_state, spec)
        record[name] = action
    if "model" not in child:
        raise MigrationError("a child checkpoint needs a 'model' entry (copy, transform or init it)")
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "state_entries": sorted(child),
        "parameters": _tensor_description(child["model"], parent_manifest, "parameters"),
        "buffers": parent_manifest.get("buffers", {}),
        "step": child["step"] if isinstance(child.get("step"), int) else 0,
        "torch_version": parent_manifest.get("torch_version"),
        "complete": True,
        "migration": {
            "actions": record,
            "reason": migration.get("reason"),
            "parent": {
                "step": parent_manifest.get("step"),
                "state_fingerprint": state_fingerprint(parent_entries),
            },
        },
    }
    if "optimizer" in child and "optimizer" in record and record["optimizer"] == "copy":
        manifest["optimizer_param_names"] = parent_manifest.get("optimizer_param_names")
    return {"manifest": manifest, "entries": child}


def _tensor_description(model_entry: Any, parent_manifest: Mapping[str, Any], key: str) -> Dict[str, Any]:
    """Parameter map of the child: names in the model entry that were parameters in the parent
    keep their role; tensors new to the child are recorded too (shapes may have changed)."""
    described = describe_tensors(model_entry) if isinstance(model_entry, Mapping) else {}
    parent_params = set(parent_manifest.get(key, {}))
    buffers = set(parent_manifest.get("buffers", {}))
    return {n: d for n, d in described.items() if n in parent_params or n not in buffers}
