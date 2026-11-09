"""Named training phases and a direct-call hook runner (no I/O in the hot path)."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from eidos_wire.manifest import PHASES

__all__ = ["PHASES", "PhaseError", "PhaseRunner"]


class PhaseError(Exception):
    """Unknown or unsupported phase, or phase counts that differ from what was declared."""


class PhaseRunner:
    """Registers hooks per phase, runs them in registration order and counts invocations.

    ``supported`` is the set of phases the trainer actually fires (default: all of ``PHASES``); a
    hook for a phase the trainer cannot fire fails at registration rather than silently never
    running. Hooks are called as ``hook(**event)`` and must accept ``**kwargs``; the trainer
    states in its docs which keys each phase carries (``step`` always).
    """

    def __init__(self, supported: Optional[Iterable[str]] = None):
        self.supported = tuple(supported) if supported is not None else PHASES
        bad = [p for p in self.supported if p not in PHASES]
        if bad:
            raise PhaseError(f"unknown phases {bad}")
        self._hooks: Dict[str, List[Tuple[str, Callable[..., Any]]]] = {p: [] for p in PHASES}
        self.counts: Dict[str, int] = OrderedDict((p, 0) for p in PHASES)
        self.hook_counts: Dict[str, int] = {}

    def register(self, phase: str, hook: Callable[..., Any], name: Optional[str] = None) -> str:
        if phase not in PHASES:
            raise PhaseError(f"unknown phase {phase!r}")
        if phase not in self.supported:
            raise PhaseError(f"this trainer does not fire phase {phase!r} (supports {list(self.supported)})")
        name = str(name or getattr(hook, "__name__", "hook"))
        key = f"{phase}:{name}"
        if key in self.hook_counts:
            raise PhaseError(f"hook {key!r} already registered")
        self._hooks[phase].append((name, hook))
        self.hook_counts[key] = 0
        return key

    def order(self) -> List[str]:
        """The pinned total order, ``"phase:name"`` per hook, in firing order within each phase."""
        return [f"{p}:{n}" for p in PHASES for n, _ in self._hooks[p]]

    def fire(self, phase: str, **event: Any) -> None:
        if phase not in PHASES:
            raise PhaseError(f"unknown phase {phase!r}")
        self.counts[phase] += 1
        for name, hook in self._hooks[phase]:
            hook(**event)
            self.hook_counts[f"{phase}:{name}"] += 1

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Counts for the receipt: phases that fired plus per-hook invocations."""
        return {
            "phases": {p: c for p, c in self.counts.items() if c},
            "hooks": dict(self.hook_counts),
        }

    def expect_counts(self, expected: Dict[str, int]) -> None:
        """Raise :class:`PhaseError` if fired counts differ (use on a short synthetic run)."""
        actual = {p: c for p, c in self.counts.items() if c}
        want = {p: c for p, c in expected.items() if c}
        if actual != want:
            raise PhaseError(f"phase counts {actual} != expected {want}")
