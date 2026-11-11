"""PostgreSQL implementation of the application's ``LedgerTx`` port.

All statements run inside one transaction opened by ``SqlLedger.transaction``; the command
service commits intent, scientific changes, reservations and outbox events atomically.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from eidos.application.ports import Record
from eidos.domain.errors import BudgetExhausted, NotFound, StaleRevision
from eidos_wire import canonical_json, sha256_bytes


def _j(v: Any) -> str:
    return json.dumps(v, default=_default)


def _default(o: Any):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(type(o))


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# Declared on every write transaction; migration 0004's fence refuses writers below the ledger's
# ``writer_fence.min_generation`` (an old version still running after an upgrade).
WRITER_GENERATION = 4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class SqlLedger:
    def __init__(self, url: Any, *, pool_size: int = 5):
        self.engine: Engine = create_engine(url, pool_size=pool_size, max_overflow=5, pool_pre_ping=True)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[LedgerTx]:
        with self.engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL search_path TO ledger")
            conn.exec_driver_sql(f"SET LOCAL eidos.writer_generation TO '{WRITER_GENERATION}'")
            yield LedgerTx(conn)

    def dispose(self) -> None:
        self.engine.dispose()


class LedgerTx:
    """One ledger transaction. Methods map one-to-one to the ``LedgerTx`` port."""

    def __init__(self, conn: Connection):
        self.c = conn
        self.command_id: int | None = None
        # Records are project-scoped: identical ids (e.g. a component ref reused in another
        # project) never collide. The command service binds the project for each transaction.
        self.project_id: str = "_system"

    # -- generic helpers -------------------------------------------------------------------
    def _one(self, sql: str, **p) -> dict | None:
        row = self.c.execute(text(sql), p).mappings().first()
        return dict(row) if row else None

    def _all(self, sql: str, **p) -> list[dict]:
        return [dict(r) for r in self.c.execute(text(sql), p).mappings().all()]

    def now(self) -> datetime:
        return datetime.now(UTC)

    def savepoint(self):
        return self.c.begin_nested()

    # -- commands (idempotency) ------------------------------------------------------------
    def claim_command(self, project_id: str, principal_id: str, key: str, ctype: str, payload_hash: str,
                      delegation: dict) -> dict | None:
        """Insert the command row; return the existing row if the key was already used."""
        row = self._one(
            "INSERT INTO commands(project_id, principal_id, command_key, command_type, payload_hash, status, delegation)"
            " VALUES (:p,:u,:k,:t,:h,'pending',CAST(:d AS jsonb))"
            " ON CONFLICT (project_id, principal_id, command_key) DO NOTHING RETURNING id",
            p=project_id, u=principal_id, k=key, t=ctype, h=payload_hash, d=_j(delegation))
        if row:
            self.command_id = row["id"]
            return None
        return self._one("SELECT * FROM commands WHERE project_id=:p AND principal_id=:u AND command_key=:k",
                         p=project_id, u=principal_id, k=key)

    def complete_command(self, receipt: dict) -> None:
        self.c.execute(text("UPDATE commands SET status='committed', receipt=CAST(:r AS jsonb) WHERE id=:i"),
                       {"r": _j(receipt), "i": self.command_id})

    # -- principals / projects -------------------------------------------------------------
    def principal_by_token_hash(self, token_hash: str) -> dict | None:
        return self._one("SELECT * FROM principals WHERE token_hash=:h AND revoked_at IS NULL"
                         " AND (expires_at IS NULL OR expires_at > now())", h=token_hash)

    def get_principal(self, pid: str) -> dict | None:
        return self._one("SELECT * FROM principals WHERE id=:i", i=pid)

    def upsert_principal(self, pid: str, kind: str, display: str, project_ids: list[str], scopes: list[str],
                         token_hash: str | None, attributes: dict, expires_at: datetime | None = None) -> None:
        self.c.execute(text(
            "INSERT INTO principals(id, kind, display, project_ids, scopes, token_hash, attributes, expires_at)"
            " VALUES (:i,:k,:d,:p,:s,:h,CAST(:a AS jsonb),:e) ON CONFLICT (id) DO UPDATE SET"
            " kind=EXCLUDED.kind, display=EXCLUDED.display, project_ids=EXCLUDED.project_ids, scopes=EXCLUDED.scopes,"
            " token_hash=COALESCE(EXCLUDED.token_hash, principals.token_hash), attributes=EXCLUDED.attributes,"
            " expires_at=EXCLUDED.expires_at, revoked_at=NULL"),
            {"i": pid, "k": kind, "d": display, "p": project_ids, "s": scopes, "h": token_hash,
             "a": _j(attributes), "e": expires_at})

    def revoke_principal(self, pid: str) -> None:
        self.c.execute(text("UPDATE principals SET revoked_at=now() WHERE id=:i"), {"i": pid})

    def insert_project(self, pid: str, name: str, created_by: str) -> None:
        self.c.execute(text("INSERT INTO projects(id, name, created_by) VALUES (:i,:n,:c)"),
                       {"i": pid, "n": name, "c": created_by})

    def get_project(self, pid: str) -> dict | None:
        return self._one("SELECT * FROM projects WHERE id=:i", i=pid)

    def list_projects(self) -> list[dict]:
        return self._all("SELECT * FROM projects ORDER BY created_at")

    # -- revisioned records ----------------------------------------------------------------
    def put_record(self, kind: str, rid: str, project_id: str, body: dict, *, created_by: str, status: str,
                   expected_revision: int | None = None, parent_id: str | None = None,
                   labels: dict | None = None) -> Record:
        if project_id != self.project_id:
            raise ValueError(f"record project {project_id} != transaction project {self.project_id}")
        head = self._one("SELECT * FROM heads WHERE project_id=:p AND kind=:k AND id=:i FOR UPDATE",
                         p=project_id, k=kind, i=rid)
        current = head["revision"] if head else 0
        if expected_revision is not None and expected_revision != current:
            raise StaleRevision(f"{kind} {rid} is at revision {current}, not {expected_revision}",
                                details={"kind": kind, "id": rid, "observed_revision": current})
        rev = current + 1
        digest = "sha256:" + sha256_bytes(canonical_json(body).encode())
        self.c.execute(text(
            "INSERT INTO records(kind,id,revision,project_id,body,digest,created_by,command_id)"
            " VALUES (:k,:i,:r,:p,CAST(:b AS jsonb),:d,:c,:cmd)"),
            {"k": kind, "i": rid, "r": rev, "p": project_id, "b": _j(body), "d": digest, "c": created_by,
             "cmd": self.command_id})
        if head:
            self.c.execute(text(
                "UPDATE heads SET revision=:r, status=:s, labels=COALESCE(CAST(:l AS jsonb), labels), updated_at=now()"
                " WHERE project_id=:p AND kind=:k AND id=:i"),
                {"r": rev, "s": status, "l": _j(labels) if labels is not None else None, "k": kind, "i": rid,
                 "p": project_id})
        else:
            self.c.execute(text(
                "INSERT INTO heads(kind,id,project_id,revision,status,parent_id,labels)"
                " VALUES (:k,:i,:p,:r,:s,:par,CAST(:l AS jsonb))"),
                {"k": kind, "i": rid, "p": project_id, "r": rev, "s": status, "par": parent_id,
                 "l": _j(labels or {})})
        return Record(kind, rid, rev, project_id, body, digest, status, created_by, parent_id, labels or {})

    def set_status(self, kind: str, rid: str, status: str, *, labels: dict | None = None,
                   expected_revision: int | None = None) -> None:
        head = self._one("SELECT revision FROM heads WHERE project_id=:p AND kind=:k AND id=:i FOR UPDATE",
                         p=self.project_id, k=kind, i=rid)
        if not head:
            raise NotFound(f"{kind} {rid} not found")
        if expected_revision is not None and head["revision"] != expected_revision:
            raise StaleRevision(f"{kind} {rid} is at revision {head['revision']}",
                                details={"observed_revision": head["revision"]})
        self.c.execute(text("UPDATE heads SET status=:s, labels=labels || CAST(:l AS jsonb), updated_at=now()"
                            " WHERE project_id=:p AND kind=:k AND id=:i"),
                       {"s": status, "l": _j(labels or {}), "k": kind, "i": rid, "p": self.project_id})

    def get_record(self, kind: str, rid: str, revision: int | None = None, *, lock: bool = False) -> Record | None:
        h = self._one("SELECT * FROM heads WHERE project_id=:p AND kind=:k AND id=:i" + (" FOR UPDATE" if lock else ""),
                      p=self.project_id, k=kind, i=rid)
        if not h:
            return None
        rev = revision or h["revision"]
        r = self._one("SELECT * FROM records WHERE project_id=:p AND kind=:k AND id=:i AND revision=:r",
                      p=self.project_id, k=kind, i=rid, r=rev)
        if not r:
            return None
        return Record(kind, rid, rev, r["project_id"], r["body"], r["digest"], h["status"], r["created_by"],
                      h["parent_id"], h["labels"], created_at=r["created_at"], head_revision=h["revision"])

    def require(self, kind: str, rid: str, revision: int | None = None, *, lock: bool = False) -> Record:
        rec = self.get_record(kind, rid, revision, lock=lock)
        if rec is None:
            raise NotFound(f"{kind} {rid}" + (f"@{revision}" if revision else "") + " not found",
                           details={"kind": kind, "id": rid})
        return rec

    def list_records(self, project_id: str | None, kind: str, *, status: str | None = None,
                     parent_id: str | None = None, label: tuple[str, Any] | None = None,
                     limit: int = 500) -> list[Record]:
        sql = ("SELECT h.*, r.body, r.digest, r.created_by, r.created_at FROM heads h JOIN records r"
               " ON r.project_id=h.project_id AND r.kind=h.kind AND r.id=h.id AND r.revision=h.revision WHERE h.kind=:k")
        p: dict[str, Any] = {"k": kind, "lim": limit}
        if project_id is not None:
            sql += " AND h.project_id=:p"
            p["p"] = project_id
        if status:
            sql += " AND h.status=:s"
            p["s"] = status
        if parent_id:
            sql += " AND h.parent_id=:par"
            p["par"] = parent_id
        if label:
            sql += " AND h.labels->>:lk = :lv"
            p["lk"], p["lv"] = label[0], str(label[1])
        sql += " ORDER BY r.created_at, h.id LIMIT :lim"
        return [Record(kind, r["id"], r["revision"], r["project_id"], r["body"], r["digest"], r["status"],
                       r["created_by"], r["parent_id"], r["labels"], created_at=r["created_at"],
                       head_revision=r["revision"]) for r in self._all(sql, **p)]

    def record_history(self, kind: str, rid: str) -> list[dict]:
        return self._all("SELECT revision, body, digest, created_by, created_at FROM records"
                         " WHERE project_id=:p AND kind=:k AND id=:i ORDER BY revision", p=self.project_id, k=kind, i=rid)

    # -- events and outbox -----------------------------------------------------------------
    def append_event(self, project_id: str, etype: str, subject: str | None, body: dict,
                     investigation_id: str | None = None) -> int:
        # Gap-free, commit-ordered sequence: the counter row stays locked until this transaction
        # commits, so no reader can observe seq N+1 while seq N is still uncommitted.
        counter = self._one("UPDATE event_counter SET last_seq = last_seq + 1 WHERE id = 1 RETURNING last_seq")
        assert counter is not None  # the singleton row is seeded by migration 0003
        seq = counter["last_seq"]
        self.c.execute(text("INSERT INTO events(seq, project_id, investigation_id, type, subject, body, command_id)"
                            " VALUES (:q,:p,:i,:t,:s,CAST(:b AS jsonb),:c)"),
                       {"q": seq, "p": project_id, "i": investigation_id, "t": etype, "s": subject, "b": _j(body),
                        "c": self.command_id})
        return seq

    def enqueue(self, topic: str, dedupe_key: str, payload: dict, event_seq: int | None = None) -> None:
        self.c.execute(text("INSERT INTO outbox(event_seq, topic, dedupe_key, payload) VALUES (:e,:t,:k,CAST(:p AS jsonb))"
                            " ON CONFLICT (dedupe_key) DO NOTHING"),
                       {"e": event_seq, "t": topic, "k": dedupe_key, "p": _j(payload)})

    def pending_outbox(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM outbox WHERE dispatched_at IS NULL AND available_at <= now()"
                         " ORDER BY id LIMIT :l FOR UPDATE SKIP LOCKED", l=limit)

    def mark_outbox(self, oid: int, *, ok: bool, error: str | None = None, retry_seconds: int = 5) -> None:
        if ok:
            self.c.execute(text("UPDATE outbox SET dispatched_at=now(), attempts=attempts+1 WHERE id=:i"), {"i": oid})
        else:
            self.c.execute(text("UPDATE outbox SET attempts=attempts+1, last_error=:e,"
                                " available_at=now() + make_interval(secs => :s) WHERE id=:i"),
                           {"i": oid, "e": (error or "")[:2000], "s": retry_seconds})

    def events_after(self, project_id: str, after: int, *, investigation_id: str | None = None,
                     upto: int | None = None, limit: int = 200, types: list[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE project_id=:p AND seq > :a"
        p: dict[str, Any] = {"p": project_id, "a": after, "l": limit}
        if investigation_id:
            sql += " AND (investigation_id=:i OR investigation_id IS NULL)"
            p["i"] = investigation_id
        if upto is not None:
            sql += " AND seq <= :u"
            p["u"] = upto
        if types:
            sql += " AND type = ANY(:ts)"
            p["ts"] = types
        return self._all(sql + " ORDER BY seq LIMIT :l", **p)

    def event_time(self, seq: int):
        row = self._one("SELECT created_at FROM events WHERE seq <= :s ORDER BY seq DESC LIMIT 1", s=seq)
        return row["created_at"] if row else None

    def horizon(self) -> int:
        row = self._one("SELECT last_seq AS h FROM event_counter WHERE id = 1")
        return int(row["h"]) if row else 0

    # -- jobs and attempts -----------------------------------------------------------------
    def insert_job(self, job: dict) -> None:
        self.c.execute(text(
            "INSERT INTO jobs(id, project_id, investigation_id, kind, trial_id, purpose, status, lock, lock_digest, created_by)"
            " VALUES (:id,:project_id,:investigation_id,:kind,:trial_id,:purpose,:status,CAST(:lock AS jsonb),:lock_digest,:created_by)"),
            {**job, "lock": _j(job["lock"])})

    def get_job(self, jid: str, *, lock: bool = False) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id=:i" + (" FOR UPDATE" if lock else ""), i=jid)

    def update_job(self, jid: str, **fields: Any) -> None:
        sets, p = [], {"i": jid}
        for k, v in fields.items():
            if k in ("terminal", "lock"):
                sets.append(f"{k}=CAST(:{k} AS jsonb)")
                p[k] = _j(v)
            else:
                sets.append(f"{k}=:{k}")
                p[k] = v
        self.c.execute(text(f"UPDATE jobs SET {', '.join(sets)}, updated_at=now() WHERE id=:i"), p)

    def list_jobs(self, project_id: str, *, investigation_id: str | None = None, statuses: list[str] | None = None,
                  limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM jobs WHERE project_id=:p"
        p: dict[str, Any] = {"p": project_id, "l": limit}
        if investigation_id:
            sql += " AND investigation_id=:i"
            p["i"] = investigation_id
        if statuses:
            sql += " AND status = ANY(:s)"
            p["s"] = statuses
        return self._all(sql + " ORDER BY created_at LIMIT :l", **p)

    def all_unfinished_jobs(self) -> list[dict]:
        return self._all("SELECT * FROM jobs WHERE status IN ('admitted','queued','running','reconciling','cancel-requested')")

    def insert_attempt(self, att: dict) -> None:
        self.c.execute(text("INSERT INTO attempts(id, job_id, epoch, status, launcher, backend_handle, lease_expires_at)"
                            " VALUES (:id,:job_id,:epoch,:status,:launcher,CAST(:bh AS jsonb),:lease)"),
                       {**att, "bh": _j(att.get("backend_handle", {})), "lease": att.get("lease_expires_at")})

    def get_attempt(self, aid: str, *, lock: bool = False) -> dict | None:
        return self._one("SELECT * FROM attempts WHERE id=:i" + (" FOR UPDATE" if lock else ""), i=aid)

    def update_attempt(self, aid: str, **fields: Any) -> None:
        sets, p = [], {"i": aid}
        for k, v in fields.items():
            if k in ("usage", "receipt", "backend_handle"):
                sets.append(f"{k}=CAST(:{k} AS jsonb)")
                p[k] = _j(v)
            else:
                sets.append(f"{k}=:{k}")
                p[k] = v
        self.c.execute(text(f"UPDATE attempts SET {', '.join(sets)} WHERE id=:i"), p)

    def list_attempts(self, job_id: str) -> list[dict]:
        return self._all("SELECT * FROM attempts WHERE job_id=:j ORDER BY epoch", j=job_id)

    # -- artifacts, publication, lineage ---------------------------------------------------
    def insert_artifact(self, art: dict) -> None:
        self.c.execute(text(
            "INSERT INTO artifacts(id, project_id, digest, size, media_type, schema, role, access_class, origin,"
            " producer_job, producer_attempt, meta) VALUES (:id,:project_id,:digest,:size,:media_type,:schema,:role,"
            ":access_class,:origin,:producer_job,:producer_attempt,CAST(:meta AS jsonb))"),
            {"schema": None, "role": "output", "access_class": "project", "producer_job": None,
             "producer_attempt": None, **art, "meta": _j(art.get("meta", {}))})

    def get_artifact(self, aid: str) -> dict | None:
        return self._one("SELECT * FROM artifacts WHERE id=:i", i=aid)

    def list_artifacts(self, project_id: str, *, producer_job: str | None = None, limit: int = 500) -> list[dict]:
        if producer_job:
            return self._all("SELECT * FROM artifacts WHERE project_id=:p AND producer_job=:j ORDER BY created_at",
                             p=project_id, j=producer_job)
        return self._all("SELECT * FROM artifacts WHERE project_id=:p ORDER BY created_at LIMIT :l", p=project_id, l=limit)

    def artifacts_with_schemas(self, project_id: str, schemas: tuple[str, ...]) -> list[dict]:
        return self._all("SELECT id, digest, schema FROM artifacts WHERE project_id=:p AND schema = ANY(:s)",
                         p=project_id, s=list(schemas))

    def all_artifact_digests(self) -> list[dict]:
        return self._all("SELECT id, digest, size FROM artifacts")

    def insert_publication(self, job_id: str, slot: str, epoch: int, attempt_id: str, artifact_id: str) -> bool:
        try:
            with self.c.begin_nested():
                self.c.execute(text("INSERT INTO publications(job_id, slot, epoch, attempt_id, artifact_id)"
                                    " VALUES (:j,:s,:e,:a,:art)"),
                               {"j": job_id, "s": slot, "e": epoch, "a": attempt_id, "art": artifact_id})
            return True
        except IntegrityError:
            return False

    def publications(self, job_id: str) -> list[dict]:
        return self._all("SELECT p.*, a.digest, a.size, a.media_type, a.schema, a.role FROM publications p"
                         " JOIN artifacts a ON a.id=p.artifact_id WHERE p.job_id=:j ORDER BY slot", j=job_id)

    def add_lineage(self, project_id: str, src: str, dst: str, relation: str) -> None:
        self.c.execute(text("INSERT INTO lineage(src, dst, relation, project_id) VALUES (:s,:d,:r,:p)"
                            " ON CONFLICT DO NOTHING"), {"s": src, "d": dst, "r": relation, "p": project_id})

    def lineage(self, project_id: str) -> list[dict]:
        return self._all("SELECT src, dst, relation FROM lineage WHERE project_id=:p", p=project_id)

    # -- budgets ----------------------------------------------------------------------------
    def ensure_budget(self, scope: str, project_id: str, dimension: str, limit: float) -> None:
        """Create a budget dimension. An existing scope keeps its limit (changes are explicit
        amendments), and a scope owned by another project is never touched."""
        owner = self._one("SELECT project_id FROM budgets WHERE scope_id=:s LIMIT 1", s=scope)
        if owner and owner["project_id"] != project_id:
            from eidos.domain.errors import Invalid
            raise Invalid(f"budget scope {scope} belongs to another project")
        self.c.execute(text("INSERT INTO budgets(scope_id, dimension, project_id, limit_amount) VALUES (:s,:d,:p,:l)"
                            " ON CONFLICT (scope_id, dimension) DO NOTHING"),
                       {"s": scope, "d": dimension, "p": project_id, "l": limit})

    def budget_owner(self, scope: str) -> str | None:
        row = self._one("SELECT project_id FROM budgets WHERE scope_id=:s LIMIT 1", s=scope)
        return row["project_id"] if row else None

    def budgets(self, scope: str) -> dict[str, dict]:
        return {r["dimension"]: {k: float(r[k]) for k in ("limit_amount", "reserved", "used", "uncertain")}
                for r in self._all("SELECT * FROM budgets WHERE scope_id=:s", s=scope)}

    def reserve(self, scope: str, dimension: str, amount: float, ref: str) -> None:
        row = self._one("UPDATE budgets SET reserved = reserved + :a WHERE scope_id=:s AND dimension=:d"
                        " AND reserved + used + :a <= limit_amount RETURNING reserved",
                        s=scope, d=dimension, a=amount)
        if row is None:
            have = self._one("SELECT * FROM budgets WHERE scope_id=:s AND dimension=:d", s=scope, d=dimension)
            if have is None:
                return  # unlimited dimension for this scope
            raise BudgetExhausted(f"{dimension}: reserving {amount} exceeds remaining budget",
                                  details={"scope": scope, "dimension": dimension, "requested": amount,
                                           "limit": float(have["limit_amount"]), "reserved": float(have["reserved"]),
                                           "used": float(have["used"])})
        self._entry(scope, dimension, "reserve", amount, ref, {})

    def settle(self, scope: str, dimension: str, *, reserved: float, actual: float, ref: str,
               uncertain: bool = False, detail: dict | None = None) -> None:
        """Convert a reservation into actual usage; release the rest. Overspend is still recorded."""
        self.c.execute(text("UPDATE budgets SET reserved = GREATEST(reserved - :r, 0),"
                            " used = used + :a,"
                            " uncertain = uncertain + :u WHERE scope_id=:s AND dimension=:d"),
                       {"s": scope, "d": dimension, "r": reserved, "a": actual, "u": actual if uncertain else 0})
        self._entry(scope, dimension, "possible-usage" if uncertain else "usage", actual, ref, detail or {})
        if reserved - actual > 0:
            self._entry(scope, dimension, "release", reserved - actual, ref, {})

    def reverse_possible_usage(self, scope: str, dimension: str, amount: float, ref: str,
                               detail: dict | None = None) -> None:
        """Take back ``amount`` of uncertain usage charged under ``ref`` (F2c: a provisional possible-usage charge is
        replaced by what a late settlement reports). Booked as a ``correction`` entry of ``-amount``; it neither
        reopens nor releases a reservation."""
        self.c.execute(text("UPDATE budgets SET used = used - :a, uncertain = GREATEST(uncertain - :a, 0)"
                            " WHERE scope_id=:s AND dimension=:d"), {"s": scope, "d": dimension, "a": amount})
        self._entry(scope, dimension, "correction", -amount, ref, detail or {})

    def record_usage(self, scope: str, dimension: str, amount: float, ref: str, *, uncertain: bool = False,
                     detail: dict | None = None) -> None:
        self.settle(scope, dimension, reserved=0, actual=amount, ref=ref, uncertain=uncertain, detail=detail)

    def open_reservations(self, scope: str, ref_prefix: str) -> dict[str, float]:
        """Per dimension: amount reserved under refs with this prefix minus what was settled
        (released or charged) under the same refs."""
        rows = self._all("SELECT dimension, ref, kind, amount FROM budget_entries WHERE scope_id=:s AND ref LIKE :p",
                         s=scope, p=_like_prefix(ref_prefix))
        res: dict[tuple[str, str], float] = {}
        done: dict[tuple[str, str], float] = {}
        for r in rows:
            amt = float(r["amount"])
            if r["kind"] == "reserve":
                res[(r["dimension"], r["ref"])] = res.get((r["dimension"], r["ref"]), 0.0) + amt
            elif r["kind"] in ("release", "usage", "possible-usage"):
                done[(r["dimension"], r["ref"])] = done.get((r["dimension"], r["ref"]), 0.0) + amt
        out: dict[str, float] = {}
        for (dim, ref), amt in res.items():
            out[dim] = out.get(dim, 0.0) + max(0.0, amt - done.get((dim, ref), 0.0))
        return out

    def budget_entries(self, scope: str) -> list[dict]:
        return self._all("SELECT * FROM budget_entries WHERE scope_id=:s ORDER BY id", s=scope)

    def _entry(self, scope, dimension, kind, amount, ref, detail) -> None:
        self.c.execute(text("INSERT INTO budget_entries(scope_id, dimension, kind, amount, ref, detail)"
                            " VALUES (:s,:d,:k,:a,:r,CAST(:x AS jsonb))"),
                       {"s": scope, "d": dimension, "k": kind, "a": amount, "r": ref, "x": _j(detail)})

    # -- turns and decisions ---------------------------------------------------------------
    def insert_turn(self, turn: dict) -> None:
        self.c.execute(text("INSERT INTO turns(id, investigation_id, project_id, seq, status, study_revision, horizon, spec)"
                            " VALUES (:id,:investigation_id,:project_id,:seq,:status,:study_revision,:horizon,CAST(:spec AS jsonb))"),
                       {**turn, "spec": _j(turn["spec"])})

    def get_turn(self, tid: str, *, lock: bool = False) -> dict | None:
        return self._one("SELECT * FROM turns WHERE id=:i" + (" FOR UPDATE" if lock else ""), i=tid)

    def last_turn(self, investigation_id: str) -> dict | None:
        return self._one("SELECT * FROM turns WHERE investigation_id=:i ORDER BY seq DESC LIMIT 1", i=investigation_id)

    def list_turns(self, investigation_id: str) -> list[dict]:
        return self._all("SELECT * FROM turns WHERE investigation_id=:i ORDER BY seq", i=investigation_id)

    def update_turn(self, tid: str, *, status: str, result: dict | None = None) -> None:
        self.c.execute(text("UPDATE turns SET status=:s, result=COALESCE(CAST(:r AS jsonb), result),"
                            " ended_at=CASE WHEN :s IN ('completed','cancelled','failed') THEN now() ELSE ended_at END"
                            " WHERE id=:i"), {"s": status, "r": _j(result) if result is not None else None, "i": tid})

    def insert_decision(self, did: str, turn_id: str, slot: int, accepted: bool, body: dict) -> bool:
        try:
            with self.c.begin_nested():
                self.c.execute(text("INSERT INTO decisions(id, turn_id, slot, accepted, body) VALUES (:i,:t,:s,:a,CAST(:b AS jsonb))"),
                               {"i": did, "t": turn_id, "s": slot, "a": accepted, "b": _j(body)})
            return True
        except IntegrityError:
            return False

    def accepted_decision(self, turn_id: str, slot: int) -> dict | None:
        return self._one("SELECT * FROM decisions WHERE turn_id=:t AND slot=:s AND accepted", t=turn_id, s=slot)

    def get_decision(self, did: str) -> dict | None:
        return self._one("SELECT * FROM decisions WHERE id=:i", i=did)

    def decisions(self, turn_id: str) -> list[dict]:
        return self._all("SELECT * FROM decisions WHERE turn_id=:t ORDER BY created_at", t=turn_id)

    # -- exposures -------------------------------------------------------------------------
    def add_exposure(self, project_id: str, study_id: str | None, principal_id: str, subject: str, disclosed: dict) -> int:
        row = self._one("INSERT INTO exposures(project_id, study_id, principal_id, subject, disclosed)"
                        " VALUES (:p,:s,:u,:sub,CAST(:d AS jsonb)) RETURNING id",
                        p=project_id, s=study_id, u=principal_id, sub=subject, d=_j(disclosed))
        assert row is not None  # INSERT ... RETURNING always yields the new row
        return row["id"]

    def exposures(self, project_id: str, study_id: str | None = None, since: datetime | None = None) -> list[dict]:
        sql = "SELECT * FROM exposures WHERE project_id=:p"
        p: dict[str, Any] = {"p": project_id}
        if study_id:
            sql += " AND study_id=:s"
            p["s"] = study_id
        if since:
            sql += " AND created_at >= :t"
            p["t"] = since
        return self._all(sql + " ORDER BY id", **p)
