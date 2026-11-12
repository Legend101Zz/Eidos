"""Eval-only identifier scan of exact outgoing provider requests."""

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path


def read_config(config_path, run_dir):
    """Read only the eval-owned private config pinned to this assignment's attempt."""
    config = Path(config_path)
    try:
        attempt = config.parent / "attempt.json"
        if (
            config.name != "audit-identifiers.json" or config.parent.parent.parent.name != "evals-history"
            or config != config.resolve(strict=True) or attempt != attempt.resolve(strict=True)
            or not config.is_file() or not attempt.is_file()
            or stat.S_IMODE(config.stat().st_mode) != 0o600
            or stat.S_IMODE(config.parent.stat().st_mode) != 0o700
            or any(path.stat().st_uid != os.getuid() for path in (config, config.parent, attempt))
        ):
            raise ValueError
        arm = json.loads((run_dir / "arm.json").read_text())
        receipt = json.loads(attempt.read_text())
        raw = config.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        block = f"{arm['card']}-r{arm['run_index']}"
        expected = Path(receipt["out"]).resolve() / "runs" / block / "arm4"
        if (
            arm["arm"] != 4 or arm["block_id"] != block or run_dir.resolve() != expected
            or arm["card"] not in receipt["cards"] or 4 not in receipt["arms"]
            or arm["run_index"] != receipt["seed_index"]
            or sha != receipt["provider_audit_identifiers_sha256"]
        ):
            raise ValueError
        identifiers = json.loads(raw)[arm["card"]]
        if not isinstance(identifiers, list) or not identifiers or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in identifiers
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError("missing, malformed, or unbound eval provider audit configuration") from error
    return sorted(set(identifiers)), sha


class RequestAudit:
    def __init__(self, config_path, state):
        self.identifiers, self.config_sha256 = read_config(config_path, state.parent)
        self.path = state.parent / "eval_provider_audit.jsonl"
        self.path.touch(exist_ok=False)
        self.lock = threading.Lock()

    def scan(self, body):
        # Persist digests and findings only. No request bodies, headers or credentials.
        matches = [value for value in self.identifiers if value.encode() in body]
        row = {"request_sha256": hashlib.sha256(body).hexdigest(), "matched_identifiers": matches}
        with self.lock, self.path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
