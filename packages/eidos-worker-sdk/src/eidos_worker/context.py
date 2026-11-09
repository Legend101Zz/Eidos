"""Per-run context: seed, scratch space, limits, receipts, log and the optional model client."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from eidos_wire import Invocation, canonical_json
from eidos_worker.errors import ContractError

STOP_FILE = ".eidos-stop-requested"  # checkpoint-and-stop request dropped by the runner into scratch

MODEL_PORT = "model.client/1"
LOG_RELPATH = "logs/run.log"


class ModelClient:
    """Minimal JSON-over-HTTP client for the run-scoped model broker (standard library only).

    ``call(payload)`` POSTs ``payload`` as JSON with a bearer token and returns the decoded JSON
    response. Budgeting, metering and policy are the broker's job, not this client's.
    """

    def __init__(self, url: str, token: str, timeout: float = 120.0):
        self.url, self._token, self.timeout = url, token, timeout

    def call(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def __repr__(self) -> str:  # never leak the token
        return f"ModelClient(url={self.url!r})"


class RunContext:
    """What a component may know about its run. ``record(key, value)`` stores realized-condition
    receipts (strict JSON) that the runner copies into the output manifest's ``receipt``."""

    def __init__(self, invocation: Invocation):
        self.invocation = invocation
        self.seed = invocation.seed
        self.scratch_dir = Path(invocation.scratch_dir)
        self.limits = dict(invocation.limits)
        self.config = invocation.config
        self.receipt: Dict[str, Any] = {}
        self._stop = False
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = Path(invocation.staging_dir) / LOG_RELPATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"eidos.run.{invocation.attempt_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self._handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(self._handler)
        self.model: Optional[ModelClient] = None
        port = invocation.ports.get(MODEL_PORT)
        if port is not None:
            if not isinstance(port, dict) or "url" not in port or "token" not in port:
                raise ContractError(f"port {MODEL_PORT} must be an object with 'url' and 'token'")
            self.model = ModelClient(port["url"], port["token"])
        # Scoped LLM calls through the lab's broker (ADR 0013); None without a grant.
        from eidos_worker.llm import LlmClient
        self.llm: Optional[LlmClient] = LlmClient.from_grant(getattr(invocation, "llm_grant", None))

    def request_stop(self) -> None:
        """Set by the runner's stop signal (SIGUSR1): checkpoint at the next safe boundary and return."""
        self._stop = True

    def stop_requested(self) -> bool:
        # A signal cannot cross every sandbox boundary (bwrap runs the worker in its own session and
        # PID namespace), so the runner also drops this file into the job's scratch directory.
        if not self._stop and os.path.exists(os.path.join(self.invocation.scratch_dir, STOP_FILE)):
            self._stop = True
        return self._stop

    def record(self, key: str, value: Any) -> None:
        try:
            canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"receipt {key!r} is not strict JSON: {exc}") from exc
        self.receipt[key] = value

    def close(self) -> None:
        self._handler.flush()
        self.logger.removeHandler(self._handler)
        self._handler.close()
