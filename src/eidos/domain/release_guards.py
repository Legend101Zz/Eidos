"""Guards the lab applies when a job of a release kept for pinned references is resolved (H2, R17 #4). Pure, no I/O.

A release kept for pinned references (``components/<id>@<release>/``) stays byte for byte as it shipped, so a reference
pinned to it still resolves to the same bundle digest. A safety fix made in a later release therefore cannot be made in
its code; the lab applies the fix at the invocation boundary instead, before anything is admitted:

* ``eidos.search.recipe-sweep`` before 1.1.0 checks held-out file names only at word boundaries, so ``test1.csv`` or
  ``testfeatures.csv`` passed ``command.copy`` (and a source bundle) and a sweep could rank recipes on held-out data.
  1.1.0 refuses any data file a word of whose name begins with ``test``, ``holdout`` or ``heldout``; the lab refuses the
  same names for the earlier release. Configurations 1.1.0 accepts are unaffected, so pinned work that was sound runs
  as before.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

SWEEP = "eidos.search.recipe-sweep"
# Releases of a component that need the lab's guard, by component id: those shipped before the fix.
GUARDED_RELEASES: dict[str, tuple[str, ...]] = {SWEEP: ("1.0.0",)}
# As in recipe-sweep 1.1.0: code may carry a held-out name; data under such a name is refused.
SOURCE_CODE_SUFFIXES = (".py", ".sh", ".md", ".toml")
_HELDOUT = re.compile(r"(^|[^a-z0-9])(test(?:set|data|split)?|hold[_\- ]?out|held[_\- ]?out)([^a-z0-9]|$)")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_WORD = re.compile(r"[a-z]+")
_HELDOUT_PREFIXES = ("test", "holdout", "heldout")


def guarded(ref: str) -> bool:
    """Whether ``ref`` (``<id>@<release>``) is a release the lab guards at invocation."""
    cid, _, release = str(ref).partition("@")
    return release in GUARDED_RELEASES.get(cid, ())


def names_heldout_file(name: Any) -> bool:
    """recipe-sweep 1.1.0's file-name rule: a held-out split name at word boundaries, or any word that begins with
    ``test``, ``holdout`` or ``heldout`` (``test1.csv``, ``testfeatures.csv``, ``x_test2.npy``, ``holdout3.npz``)."""
    text = _CAMEL.sub(r"\1_\2", str(name)).lower()
    return bool(_HELDOUT.search(text)) or any(w.startswith(_HELDOUT_PREFIXES) for w in _WORD.findall(text))


def sweep_problems(config: dict[str, Any] | None, source_files: list[str] | None) -> list[str]:
    """What the lab refuses in a guarded recipe-sweep invocation: ``command.copy`` entries and source-bundle data files
    named after the held-out split (1.1.0's rule). Empty when the invocation is acceptable."""
    out: list[str] = []
    command = (config or {}).get("command")
    copy = command.get("copy") if isinstance(command, dict) else None
    for name in copy if isinstance(copy, list) else []:
        if isinstance(name, str) and names_heldout_file(name):
            out.append(f"command.copy entry {name!r} is named after a held-out split; this search uses visible "
                       "validation only")
    for rel in source_files or []:
        parts = str(rel).replace("\\", "/").split("/")
        if any(names_heldout_file(p) for p in parts) and PurePosixPath(parts[-1]).suffix not in SOURCE_CODE_SUFFIXES:
            out.append(f"source bundle file {rel!r} is named after a held-out split; this search uses visible "
                       "validation only")
    return out


def problems(ref: str, config: dict[str, Any] | None, source_files: list[str] | None) -> list[str]:
    """The guard's refusals for an invocation of ``ref`` (empty for a release that is not guarded)."""
    if not guarded(ref):
        return []
    if str(ref).partition("@")[0] == SWEEP:
        return sweep_problems(config, source_files)
    return []
