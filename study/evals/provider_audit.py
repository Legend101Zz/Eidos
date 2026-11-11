"""Eval-only identifier scan of exact outgoing provider requests."""

import hashlib
import json
import re
import threading
from pathlib import Path


class RequestAudit:
    def __init__(self, config_path, state):
        arm = json.loads((state.parent / "arm.json").read_text())
        identifiers = json.loads(Path(config_path).read_text())[arm["card"]]
        if not identifiers or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in identifiers):
            raise ValueError("missing or malformed eval audit identifiers")
        self.identifiers = sorted(set(identifiers))
        self.path = state.parent / "eval_provider_audit.jsonl"
        self.path.touch(exist_ok=False)
        self.lock = threading.Lock()

    def scan(self, body):
        # Persist digests and findings only. No request bodies, headers or credentials.
        matches = [value for value in self.identifiers if value.encode() in body]
        row = {"request_sha256": hashlib.sha256(body).hexdigest(), "matched_identifiers": matches}
        with self.lock, self.path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
