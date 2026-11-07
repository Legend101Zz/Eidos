"""Device availability. Reports facts only; it never promises numerical restore across devices."""

from __future__ import annotations

from typing import Any, Dict


def devices() -> Dict[str, Any]:
    """Return availability of cpu/cuda/mps.

    Restoring a checkpoint on a different device (or device type) can change reduction order and
    kernels, so ``exact_restore_across_devices`` is always ``False``: such restores belong to the
    "numerically equivalent under protocol" class and need their own tolerance check.
    """
    info: Dict[str, Any] = {
        "torch": None,
        "cpu": {"available": True},
        "cuda": {"available": False, "count": 0, "names": []},
        "mps": {"available": False},
        "exact_restore_across_devices": False,
    }
    try:
        import torch
    except ImportError:
        return info
    info["torch"] = str(torch.__version__)
    if torch.cuda.is_available():
        count = torch.cuda.device_count()
        info["cuda"] = {
            "available": True,
            "count": count,
            "names": [torch.cuda.get_device_name(i) for i in range(count)],
        }
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        info["mps"] = {"available": True}
    return info
