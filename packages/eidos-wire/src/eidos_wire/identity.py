"""Stable identities: canonical JSON, digests and derived command keys."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for every digest and fingerprint in Eidos."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> tuple[str, int]:
    """Return (hex digest, size) of a file, streaming."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def fingerprint(payload: Any) -> str:
    """Normalized payload fingerprint stored beside a command key."""
    return "sha256:" + sha256_bytes(canonical_json(payload).encode("utf-8"))


def command_key(
    project_id: str, turn_id: str, decision_id: str, call_index: int, subcommand_index: int = 0
) -> str:
    """Command identity derived from a *persisted accepted decision* (design §Stable intent).

    The indices must come from the persisted decision, never from in-memory counters or
    completion order, so a retry reproduces exactly the same key.
    """
    if call_index < 0 or subcommand_index < 0:
        raise ValueError("indices must be non-negative")
    identity = ["lab-command/1", project_id, turn_id, decision_id, call_index, subcommand_index]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
