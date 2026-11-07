"""Generic helpers for descriptive capability cards (``eidos-task/0.2`` card JSON).

A card is a reference description, not a final-admission manifest. These helpers only translate the
fields a card declares (permitted/forbidden operations, limits, exposure status) into an
exploration-only study charter and build run configurations for ``eidos.project.command``. They
contain no task identifiers, reference methods or answers; the caller supplies the metric, direction
and the command to run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CARD_SCHEMA = "eidos-task/0.2"


def load_card(path: str | Path) -> dict[str, Any]:
    card = json.loads(Path(path).read_text())
    if card.get("schema_version") != CARD_SCHEMA:
        raise ValueError(f"unsupported card schema {card.get('schema_version')!r}")
    for key in ("id", "project", "permitted_operations", "limits"):
        if key not in card:
            raise ValueError(f"card is missing {key!r}")
    return card


def card_charter(card: dict[str, Any], *, metric: str, direction: str, question: str | None = None) -> dict[str, Any]:
    """Exploration-only charter dict for ``commit_study_charter``. No success rule is claimed: the card
    declares no final margin, and its reference gain bar is a development acceptance threshold."""
    if direction not in ("increase", "decrease"):
        raise ValueError("direction must be 'increase' or 'decrease'")
    cid = str(card["id"])
    return {
        "study_id": f"{cid.lower()}-card",
        "question": question or f"Capability card {cid} ({card.get('title', '')}): measure {metric} ({direction} is better).",
        "exploration_only": True,
        "resource_limits": dict(card.get("limits") or {}),
        "notes": ("Reference-capability card run; the card is not a final-admission manifest. "
                  f"metric={metric} direction={direction}; permitted operations: {card['permitted_operations']}; "
                  f"forbidden operations: {card.get('forbidden_operations', [])}. Exploration only; no prospective "
                  "success rule is claimed."),
        "known_exposures": [{"description": "public development card; generators, references and test labels are public",
                             "ref": f"{cid}@{card.get('schema_version')}"}],
    }


def command_config(argv: list[str], *, seed: int | None = None, files: dict[str, str] | None = None,
                   timeout_seconds: int = 120) -> dict[str, Any]:
    """Config for ``eidos.project.command``: ``argv`` is an argument array (``python`` is the project
    interpreter; ``{work}``, ``{out}``, ``{seed}`` are substituted); ``files`` maps input ports to plain names."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    cfg: dict[str, Any] = {"argv": list(argv), "timeout_seconds": timeout_seconds}
    if seed is not None:
        cfg["seed"] = seed
    if files:
        cfg["files"] = dict(files)
    return cfg
