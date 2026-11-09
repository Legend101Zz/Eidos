"""Explicit ports the application depends on. Adapters implement them; the application never
imports an adapter (Import Linter contract)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Record:
    """One immutable revision of a scientific record plus its current head status."""

    kind: str
    id: str
    revision: int
    project_id: str
    body: dict[str, Any]
    digest: str
    status: str
    created_by: str
    parent_id: str | None = None
    labels: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    head_revision: int | None = None

    @property
    def ref(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "revision": self.revision, "digest": self.digest}

    def view(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "revision": self.revision, "head_revision": self.head_revision,
                "status": self.status, "digest": self.digest, "created_by": self.created_by,
                "parent_id": self.parent_id, "labels": self.labels,
                "created_at": self.created_at.isoformat() if self.created_at else None, "body": self.body}


@dataclass(frozen=True)
class Principal:
    id: str
    kind: str  # human | service | job
    scopes: frozenset[str]
    project_ids: frozenset[str]
    attributes: dict[str, Any] = field(default_factory=dict)
    display: str = ""

    def can_access(self, project_id: str) -> bool:
        return "*" in self.project_ids or project_id in self.project_ids

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes


class LedgerTx(Protocol):
    """A single ledger transaction (implemented by ``eidos.persistence.ledger.LedgerTx``).

    Groups: idempotent commands; principals/projects; revisioned records and heads; events and
    outbox; jobs/attempts with fencing; artifacts/publications/lineage; budgets; turns/decisions;
    exposures. See the implementation for exact signatures.
    """

    command_id: int | None
    project_id: str

    def claim_command(self, project_id: str, principal_id: str, key: str, ctype: str, payload_hash: str,
                      delegation: dict) -> dict | None: ...
    def complete_command(self, receipt: dict) -> None: ...
    def put_record(self, kind: str, rid: str, project_id: str, body: dict, *, created_by: str, status: str,
                   expected_revision: int | None = None, parent_id: str | None = None,
                   labels: dict | None = None) -> Record: ...
    def get_record(self, kind: str, rid: str, revision: int | None = None, *, lock: bool = False) -> Record | None: ...
    def require(self, kind: str, rid: str, revision: int | None = None, *, lock: bool = False) -> Record: ...
    def append_event(self, project_id: str, etype: str, subject: str | None, body: dict,
                     investigation_id: str | None = None) -> int: ...
    def enqueue(self, topic: str, dedupe_key: str, payload: dict, event_seq: int | None = None) -> None: ...
    def __getattr__(self, name: str) -> Any: ...


class Ledger(Protocol):
    def transaction(self) -> contextlib.AbstractContextManager[LedgerTx]: ...


class ArtifactStore(Protocol):
    """Content-addressed immutable blob storage."""

    def has(self, digest: str, size: int) -> bool: ...
    def verify(self, digest: str, size: int) -> bool: ...
    def check_with_retry(self, digest: str, size: int, *, attempts: int = 3, delay: float = 0.05) -> str | None: ...
    def path_for(self, digest: str) -> str: ...
    def put_bytes(self, data: bytes) -> tuple[str, int]: ...
    def put_file(self, path: str) -> tuple[str, int]: ...
    def free_bytes(self) -> int: ...


class Notifier(Protocol):
    """Wakes the outbox relay after a commit (best effort; the outbox is the durable copy)."""

    def notify(self) -> None: ...
