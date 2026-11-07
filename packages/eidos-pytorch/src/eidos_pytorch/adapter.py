"""The model adapter contract.

An adapter is any object (module, instance, or class with a no-argument constructor) exposing the
researcher's existing functions. Nothing here assumes logits, tensor batches, or that one batch is
one optimizer update.

Required
    ``build(config) -> model``            construct the model from a plain config dict.
    ``predict(model, batch) -> outputs``  forward pass; ``outputs`` may be any structure.

Optional (each is used only if present)
    ``examples(config) -> batch``         one fixed, deterministic reference batch (behavior fixtures).
    ``collate(items) -> batch``           assemble raw items into a batch.
    ``targets(batch) -> targets``         extract supervision from a batch.
    ``loss(model, batch) -> scalar tensor``  differentiable loss for one batch (generic loop).
    ``make_optimizer(model, config) -> optimizer``
    ``make_scheduler(optimizer, config) -> scheduler``
    ``train_step(model, batches, optimizer, step) -> None | float | dict``
        a full update over ``batches`` (a list of ``batches_per_update`` batches): zero grads,
        backward, optimizer step. The author defines what one update means.
    ``train(plan, context)``              whole-trainer escape hatch; see :func:`run_training`.
    ``phases``                            tuple of phase names (``eidos_wire.manifest.PHASES``)
                                          that this adapter's trainer fires.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Dict, List

from eidos_wire.manifest import PHASES

REQUIRED_METHODS = ("build", "predict")
OPTIONAL_METHODS = (
    "examples",
    "collate",
    "targets",
    "loss",
    "make_optimizer",
    "make_scheduler",
    "train_step",
    "train",
)


class AdapterError(Exception):
    """The adapter does not satisfy the documented protocol."""


try:  # typing.Protocol exists on 3.8+, but keep the contract usable without it
    from typing import Protocol, runtime_checkable

    @runtime_checkable
    class ModelAdapter(Protocol):
        """Required part of the adapter contract (see module docstring for the optional part)."""

        def build(self, config: Dict[str, Any]) -> Any: ...

        def predict(self, model: Any, batch: Any) -> Any: ...

except ImportError:  # pragma: no cover
    ModelAdapter = object  # type: ignore[assignment,misc]


def validate_adapter(adapter: Any) -> Dict[str, Any]:
    """Check the protocol and return ``{"optional": [...], "phases": [...]}``."""
    for name in REQUIRED_METHODS:
        if not callable(getattr(adapter, name, None)):
            raise AdapterError(f"adapter is missing required callable {name!r}")
    phases = list(getattr(adapter, "phases", ()) or ())
    unknown = [p for p in phases if p not in PHASES]
    if unknown:
        raise AdapterError(f"adapter declares unknown phases {unknown}; valid: {list(PHASES)}")
    return {
        "optional": [m for m in OPTIONAL_METHODS if callable(getattr(adapter, m, None))],
        "phases": phases,
    }


def load_adapter(spec: str) -> Any:
    """Resolve ``"module:attr"`` (attr may be dotted) to a validated adapter.

    A class is instantiated with no arguments. ``"module:"`` is not accepted; a bare module can be
    an adapter by naming its attribute explicitly, e.g. ``"my_pkg.model:adapter"``.
    """
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise AdapterError(f"adapter spec {spec!r} must be 'module:attr'")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise AdapterError(f"{spec!r}: no attribute {part!r}") from exc
    if inspect.isclass(obj):
        obj = obj()
    validate_adapter(obj)
    return obj


def resolve_callable(spec: str) -> Any:
    """Resolve ``"module:attr"`` to any object (used for named builders)."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise AdapterError(f"spec {spec!r} must be 'module:attr'")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def supported(adapter: Any, *names: str) -> List[str]:
    return [n for n in names if callable(getattr(adapter, n, None))]
