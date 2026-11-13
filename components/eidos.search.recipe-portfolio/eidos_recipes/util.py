"""Canonical scientific identities; wall clocks never affect a selection digest."""
import hashlib
import json
import re
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evidence_digest(value):
    def stable(v):
        if isinstance(v, dict):
            return {k: stable(x) for k, x in v.items() if "seconds" not in k and k != "evidence_digest"}
        if isinstance(v, list):
            return [stable(x) for x in v]
        return v
    return hashlib.sha256(canonical(stable(value)).encode()).hexdigest()


def guarded_ports(ports, allowed):
    if set(ports) - set(allowed):
        raise ValueError("undeclared input ports; only " + ", ".join(allowed) + " are read")
    for path in ports.values():
        words = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", Path(path).name).lower())
        if any(w.startswith(("test", "holdout", "heldout")) for w in words):
            raise ValueError("held-out data filenames are refused")
