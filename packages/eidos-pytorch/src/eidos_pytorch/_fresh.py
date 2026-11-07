"""Child side of :func:`eidos_pytorch.fingerprint.behavioral_restore_check` (runs in a new process)."""

from __future__ import annotations

import json
import sys


def main(argv) -> int:
    req_path, res_path = argv
    with open(req_path) as f:
        req = json.load(f)
    for p in reversed(req["sys_path"]):
        sys.path.insert(0, p)
    import torch

    from eidos_pytorch.adapter import load_adapter, resolve_callable
    from eidos_pytorch.fingerprint import reference_outputs

    adapter = load_adapter(req["adapter"])
    build = resolve_callable(req["builder"]) if req["builder"] else adapter.build
    model = build(req["config"])
    loaded = torch.load(req["weights"], map_location="cpu", weights_only=True)
    weights = loaded["entries"]["model"] if isinstance(loaded, dict) and "entries" in loaded else loaded
    result: dict = {"strict_load_ok": True}
    try:
        model.load_state_dict(weights, strict=True)
    except Exception as exc:  # noqa: BLE001
        result = {"strict_load_ok": False, "error": f"{type(exc).__name__}: {exc}"[:800]}
    else:
        result["outputs"] = reference_outputs(adapter, model, req["config"], req["seed"])
    with open(res_path, "w") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
