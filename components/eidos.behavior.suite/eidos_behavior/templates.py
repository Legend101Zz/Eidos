"""Test templates for ``behavior-cases/1`` (CheckList-style invariance and direction-of-change tests).

Domain authors supply the transformation and the grouping; the template builds the transformed examples and the
case table. The model is then run once over originals and transformed examples (e.g. ``eidos.pytorch.evaluate``),
and the raw predictions are scored by ``eidos.behavior.suite``; a later scorer release can re-score the same raw
predictions without running the model again.

    examples = [{"id": "e1", "text": "great service"}, ...]
    t1 = invariance("typo-robustness", examples, transform=add_typo, group=lambda e: e["domain"],
                    capability="robustness", min_pass_rate=0.9, tolerance=0.1, on="score")
    t2 = direction("negation-lowers-score", examples, transform=add_negation, expect="decrease", on="score")
    cases, transformed = suite("sentiment-v1", [t1, t2])
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

DIRECTIONS = ("increase", "decrease", "not-increase", "not-decrease")
Example = Dict[str, Any]


def _build(name: str, examples: List[Example], transform: Callable[[Example], Optional[Example]],
           group: Optional[Callable[[Example], str]], id_key: str) -> Tuple[List[dict], List[Example]]:
    pairs, made = [], []
    for ex in examples:
        out = transform(dict(ex))
        if out is None:  # the transformation does not apply to this example (e.g. no negatable verb)
            continue
        tid = f"{ex[id_key]}~{name}"
        made.append({**out, id_key: tid})
        pairs.append({"original": ex[id_key], "transformed": tid, "group": str(group(ex)) if group else "all"})
    return pairs, made


def invariance(name: str, examples: List[Example], transform: Callable[[Example], Optional[Example]], *,
               group: Optional[Callable[[Example], str]] = None, capability: str = "", on: str = "label",
               tolerance: float = 0.0, min_pass_rate: float = 1.0, id_key: str = "id") -> dict:
    """The prediction must not change under ``transform`` (label equal, or score within ``tolerance``)."""
    pairs, made = _build(name, examples, transform, group, id_key)
    return {"name": name, "type": "invariance", "capability": capability, "on": on, "tolerance": float(tolerance),
            "min_pass_rate": float(min_pass_rate), "pairs": pairs, "_examples": made}


def direction(name: str, examples: List[Example], transform: Callable[[Example], Optional[Example]], *,
              expect: str, group: Optional[Callable[[Example], str]] = None, capability: str = "", on: str = "score",
              tolerance: float = 0.0, min_pass_rate: float = 1.0, id_key: str = "id") -> dict:
    """The score must move in the ``expect`` direction under ``transform`` (by more than ``tolerance`` for
    increase/decrease; not beyond ``tolerance`` the other way for not-increase/not-decrease)."""
    if expect not in DIRECTIONS:
        raise ValueError(f"expect must be one of {DIRECTIONS}")
    pairs, made = _build(name, examples, transform, group, id_key)
    return {"name": name, "type": "direction", "expect": expect, "capability": capability, "on": on,
            "tolerance": float(tolerance), "min_pass_rate": float(min_pass_rate), "pairs": pairs, "_examples": made}


def suite(name: str, tests: List[dict]) -> Tuple[dict, List[Example]]:
    """``behavior-cases/1`` plus the transformed examples to predict on (alongside the originals)."""
    transformed: List[Example] = []
    clean = []
    for t in tests:
        transformed += t.get("_examples", [])
        clean.append({k: v for k, v in t.items() if k != "_examples"})
    return {"schema": "behavior-cases/1", "suite": name, "tests": clean}, transformed
