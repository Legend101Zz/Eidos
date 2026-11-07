"""Internal helpers. torch is imported lazily so that ``import eidos_pytorch`` works without it."""

from __future__ import annotations

import copy
import ctypes
from collections import OrderedDict
from typing import Any, Dict, Iterator, Mapping, Tuple


def torch_module() -> Any:
    import torch  # noqa: PLC0415 - lazy by design

    return torch


def is_tensor(obj: Any) -> bool:
    return type(obj).__module__.startswith("torch") and hasattr(obj, "data_ptr") and hasattr(obj, "detach")


def clone_tree(obj: Any) -> Any:
    """Deep copy that gives every tensor its own storage (``detach().clone()``)."""
    if is_tensor(obj):
        return obj.detach().clone()
    if isinstance(obj, OrderedDict):
        return OrderedDict((k, clone_tree(v)) for k, v in obj.items())
    if isinstance(obj, dict):
        return {k: clone_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clone_tree(v) for v in obj]
    if isinstance(obj, tuple) and not hasattr(obj, "_fields"):
        return tuple(clone_tree(v) for v in obj)
    return copy.deepcopy(obj)


def storage_ptr(tensor: Any) -> int:
    """Address of the tensor's underlying storage (0 for empty tensors)."""
    if tensor.numel() == 0:
        return 0
    try:
        return int(tensor.untyped_storage().data_ptr())
    except AttributeError:  # torch < 2.0
        return int(tensor.storage().data_ptr())


def tensor_bytes(tensor: Any) -> bytes:
    """Raw bytes of a tensor in row-major order (copies to contiguous CPU first)."""
    t = tensor.detach().cpu().contiguous()
    n = t.numel() * t.element_size()
    return ctypes.string_at(t.data_ptr(), n) if n else b""


def walk(obj: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    """Yield ``(path, leaf)`` for every leaf of nested dict/list/tuple structures, sorted by key."""
    if isinstance(obj, dict):
        for key in sorted(obj, key=str):
            yield from walk(obj[key], f"{path}.{key}" if path else str(key))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def dtype_name(tensor: Any) -> str:
    return str(tensor.dtype).replace("torch.", "")


def describe_tensors(state_dict: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        k: {"shape": list(v.shape), "dtype": dtype_name(v)} for k, v in state_dict.items() if is_tensor(v)
    }
