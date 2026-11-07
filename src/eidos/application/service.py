"""The single authoritative command/commit path and the authorized query path.

Every operator (human CLI, MCP client, internal investigator, planner, assessor, launcher) reaches
scientific state only through ``CommandService.execute``. It authenticates the principal (done by
the transport), checks the grant and project access, enforces command-key idempotency with a
normalized payload fingerprint, and commits intent, changes, reservations and outbox rows in one
transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from eidos.application.ports import ArtifactStore, Ledger, LedgerTx, Notifier, Principal
from eidos.domain.errors import CommandConflict, Forbidden, Invalid, NotFound
from eidos_wire import fingerprint

log = logging.getLogger("eidos.commands")


@dataclass(frozen=True)
class Spec:
    name: str
    scope: str
    fn: Callable[..., dict]
    project_scoped: bool = True
    doc: str = ""


COMMANDS: dict[str, Spec] = {}
QUERIES: dict[str, Spec] = {}


def command(name: str, scope: str, *, project_scoped: bool = True):
    def deco(fn):
        COMMANDS[name] = Spec(name, scope, fn, project_scoped, (fn.__doc__ or "").strip())
        return fn
    return deco


def query(name: str, scope: str = "project:read", *, project_scoped: bool = True):
    def deco(fn):
        QUERIES[name] = Spec(name, scope, fn, project_scoped, (fn.__doc__ or "").strip())
        return fn
    return deco


@dataclass
class Ctx:
    """Everything a handler may use. Handlers never open their own transactions."""

    tx: LedgerTx
    principal: Principal
    project_id: str
    command_key: str | None
    store: ArtifactStore
    settings: dict[str, Any]
    expected_revision: int | None = None
    emitted: list[int] = field(default_factory=list)

    def event(self, etype: str, subject: str | None, body: dict, *, investigation_id: str | None = None,
              wake: bool = False) -> int:
        seq = self.tx.append_event(self.project_id, etype, subject, body, investigation_id)
        self.emitted.append(seq)
        if wake and investigation_id:
            self.tx.enqueue("investigation.wake", f"wake:{investigation_id}:{seq}",
                            {"investigation_id": investigation_id, "project_id": self.project_id, "event_seq": seq},
                            seq)
        return seq

    def require_scope(self, scope: str) -> None:
        if not self.principal.has(scope):
            raise Forbidden(f"{self.principal.id} lacks grant '{scope}'", details={"required": scope})


class CommandService:
    def __init__(self, ledger: Ledger, store: ArtifactStore, *, settings: dict[str, Any] | None = None,
                 notifier: Notifier | None = None):
        self.ledger = ledger
        self.store = store
        self.settings = settings or {}
        self.notifier = notifier

    def _authorize(self, spec: Spec, principal: Principal, project_id: str | None) -> None:
        if not principal.has(spec.scope):
            raise Forbidden(f"{principal.id} lacks grant '{spec.scope}' for {spec.name}",
                            details={"required": spec.scope})
        if spec.project_scoped:
            if not project_id:
                raise Invalid("project_id is required")
            if not principal.can_access(project_id):
                # Same error shape as a missing project: do not reveal other projects' existence.
                raise NotFound(f"project {project_id} not found")

    def execute(self, principal: Principal, name: str, *, project_id: str | None, command_key: str,
                payload: dict[str, Any], expected_revision: int | None = None,
                delegation: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = COMMANDS.get(name)
        if spec is None:
            raise NotFound(f"unknown command {name!r}")
        if not command_key or len(command_key) > 200:
            raise Invalid("a command_key (<=200 chars) is required for every command")
        self._authorize(spec, principal, project_id)
        pid = project_id or "_system"
        payload_hash = fingerprint({"command": name, "payload": payload, "expected_revision": expected_revision})
        with self.ledger.transaction() as tx:
            if spec.project_scoped and tx.get_project(pid) is None:
                raise NotFound(f"project {pid} not found")
            existing = tx.claim_command(pid, principal.id, command_key, name, payload_hash, delegation or {})
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise CommandConflict(
                        f"command key {command_key} was already used with a different payload",
                        details={"command_key": command_key, "original_command": existing["command_type"]})
                receipt = dict(existing["receipt"] or {})
                receipt["replayed"] = True
                return receipt
            ctx = Ctx(tx, principal, pid, command_key, self.store, self.settings, expected_revision)
            result = spec.fn(ctx, payload)
            receipt = {"command": name, "command_key": command_key, "status": "committed",
                       "result": result, "events": ctx.emitted, "replayed": False}
            tx.complete_command(receipt)
        log.info("command committed", extra={"command": name, "principal": principal.id, "project": pid,
                                             "command_key": command_key})
        if self.notifier is not None:
            self.notifier.notify()
        return receipt

    def query(self, principal: Principal, name: str, *, project_id: str | None, params: dict[str, Any]) -> Any:
        spec = QUERIES.get(name)
        if spec is None:
            raise NotFound(f"unknown query {name!r}")
        self._authorize(spec, principal, project_id)
        with self.ledger.transaction() as tx:
            ctx = Ctx(tx, principal, project_id or "_system", None, self.store, self.settings)
            return spec.fn(ctx, params)


def load_handlers() -> None:
    """Import handler modules so their decorators register commands and queries."""
    from eidos.application.handlers import (  # noqa: F401
        components,
        evidence,
        investigations,
        launcher,
        projects,
        queries,
        runs,
        study,
    )
