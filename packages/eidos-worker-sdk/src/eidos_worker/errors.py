"""Error classes and the error-category classification used in failed output manifests."""

from __future__ import annotations

import traceback
from typing import Any, Dict

from eidos_wire import ManifestError, WireError

CATEGORIES = ("numerical", "resource", "user-code", "contract")
TRACEBACK_TAIL_CHARS = 4000
MESSAGE_CHARS = 2000


class ContractError(Exception):
    """The component or the runner violated the worker contract (undeclared input, unsafe slot...)."""


class NonFiniteError(ArithmeticError):
    """A value that must be finite contained NaN or infinity. Classified as ``numerical``."""


def classify(exc: BaseException) -> str:
    """Map an exception to one of :data:`CATEGORIES`.

    ``resource``: ``MemoryError`` or any exception whose class (or a base) is named like
    ``OutOfMemoryError`` (torch CUDA/CPU OOM) — detected by name so torch is never imported here.
    """
    if isinstance(exc, (ContractError, WireError, ManifestError)):
        return "contract"
    if isinstance(exc, (NonFiniteError, FloatingPointError)):
        return "numerical"
    if isinstance(exc, MemoryError) or any(c.__name__ == "OutOfMemoryError" for c in type(exc).__mro__):
        return "resource"
    return "user-code"


def _line_tail(text: str, chars: int) -> str:
    """The end of ``text`` from a line boundary: the lab redacts what is stored, and a cut mid-line could separate a
    secret from the name that identifies it."""
    if len(text) <= chars:
        return text
    tail = text[-chars:]
    return tail.split("\n", 1)[-1] if "\n" in tail else tail


def error_record(exc: BaseException) -> Dict[str, Any]:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {
        "category": classify(exc),
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc)[:MESSAGE_CHARS],
        "traceback_tail": _line_tail(tb, TRACEBACK_TAIL_CHARS),
    }


def check_finite(value: Any, name: str = "value") -> Any:
    """Raise :class:`NonFiniteError` if ``value`` holds a non-finite number; return it otherwise.

    Accepts numbers, nested lists/tuples/dicts, torch tensors and numpy arrays (duck-typed; neither
    library is imported).
    """
    import math

    def bad(v: Any) -> bool:
        if isinstance(v, bool):
            return False
        if isinstance(v, (int,)):
            return False
        if isinstance(v, float):
            return not math.isfinite(v)
        if isinstance(v, dict):
            return any(bad(x) for x in v.values())
        if isinstance(v, (list, tuple)):
            return any(bad(x) for x in v)
        if hasattr(v, "isfinite") and hasattr(v, "detach"):  # torch tensor
            return (
                not bool(v.detach().isfinite().all().item())
                if v.dtype.is_floating_point or v.dtype.is_complex
                else False
            )
        if hasattr(v, "dtype") and hasattr(v, "shape") and hasattr(v, "tolist"):  # numpy
            try:
                return any(bad(x) for x in v.reshape(-1).tolist())
            except Exception:  # noqa: BLE001 - non-numeric dtype
                return False
        return False

    if bad(value):
        raise NonFiniteError(f"{name} contains non-finite values")
    return value
