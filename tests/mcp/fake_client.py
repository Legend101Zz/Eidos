"""In-memory stand-in for LabClient (fixture-only: no lab, no ledger). It mimics only the
idempotency/conflict/revision behaviour the adapter relies on."""

from __future__ import annotations

import json
from typing import Any

from eidos.client import LabError

_ABSENT = object()


class FakeLab:
    def __init__(self, tokens: dict[str, str] | None = None):
        self.tokens = tokens or {"good-token": "alice"}
        self.receipts: dict[tuple[str, str], tuple[str, dict]] = {}
        self.calls: list[tuple] = []  # (kind, name, payload, project, key, token)
        self.executed = 0
        self.revision = 3
        self.queries: dict[str, Any] = {}
        self.command_extras: list[dict] = []  # extra keyword arguments each command() call was given (ADR 0017)

    def client(self, token: str) -> FakeClient:
        return FakeClient(self, token)


class FakeClient:
    def __init__(self, lab: FakeLab, token: str):
        self.lab, self.token = lab, token

    def close(self) -> None:
        pass

    def whoami(self) -> dict:
        if self.token not in self.lab.tokens:
            raise LabError(401, {"code": "UNAUTHENTICATED", "message": "invalid token"})
        return {"id": self.lab.tokens[self.token], "kind": "human", "scopes": [], "projects": ["*"]}

    def command(self, name, payload=None, *, project=None, key=None, expected_revision=None,
                normalized_arguments=_ABSENT, **unexpected):
        if unexpected:
            raise TypeError(f"command() got unexpected keyword arguments {sorted(unexpected)}")
        self.whoami()
        payload = payload or {}
        self.lab.calls.append(("command", name, payload, project, key, self.token))
        # Only keyword arguments the adapter actually passed are recorded, so a test can tell "not sent" from "empty".
        self.lab.command_extras.append({} if normalized_arguments is _ABSENT
                                       else {"normalized_arguments": normalized_arguments})
        if name == "launch_trial" and payload.get("trial_id") == "t-broke":
            raise LabError(409, {"code": "BUDGET_EXHAUSTED", "message": "exploration budget exhausted",
                                 "details": {"dimension": "compute_seconds"}, "next_action": "Raise the budget."})
        if expected_revision is not None and expected_revision != self.lab.revision:
            raise LabError(409, {"code": "STALE_REVISION", "message": "revision changed",
                                 "details": {"observed_revision": self.lab.revision}, "next_action": "Re-read."})
        fp = json.dumps([name, payload, expected_revision], sort_keys=True)  # the record is not part of the hash
        hit = self.lab.receipts.get((project or "", key))
        if hit:
            if hit[0] == fp:
                # as the real service: ``normalized_arguments`` is THIS call's (absent if it had none) and the
                # committing call's record comes back as ``admission_normalized_arguments``; the store is unchanged
                replay = {k: v for k, v in hit[1].items() if k != "normalized_arguments"}
                replay["replayed"] = True
                if hit[1].get("normalized_arguments"):
                    replay["admission_normalized_arguments"] = hit[1]["normalized_arguments"]
                if normalized_arguments is not _ABSENT and normalized_arguments:
                    replay["normalized_arguments"] = list(normalized_arguments)
                return replay
            if hit[0] != fp:
                raise LabError(409, {"code": "COMMAND_KEY_CONFLICT", "message": "key reused with different payload",
                                     "next_action": "Use a new command_key."})
            return hit[1]
        self.lab.executed += 1
        receipt = {"status": "committed", "command": name, "replayed": False, "command_key": key, "events": [1],
                   "result": {"investigation_id": f"inv_{self.lab.executed}", "revision": 1}}
        if normalized_arguments is not _ABSENT and normalized_arguments:
            receipt["normalized_arguments"] = list(normalized_arguments)
        self.lab.receipts[(project or "", key)] = (fp, receipt)
        return receipt

    def query(self, name, params=None, *, project=None):
        self.whoami()
        self.lab.calls.append(("query", name, params or {}, project, None, self.token))
        if name in self.lab.queries:
            r = self.lab.queries[name]
            return r(params or {}) if callable(r) else r
        if name == "get_investigation":
            return {"id": params["investigation_id"], "revision": 3, "status": "active", "turns": [], "jobs": []}
        if name == "search_components":
            return {"components": [{"ref": "test.echo@1.0.0"}], "total": 1, "next_cursor": None}
        if name == "get_job" and params["job_id"] == "missing":
            raise LabError(404, {"code": "NOT_FOUND", "message": "job missing not found", "next_action": ""})
        if name == "get_record":
            return {"id": params["id"], "kind": params["kind"], "revision": 1, "body": {}}
        return {"query": name, "params": params}
