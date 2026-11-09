"""Outbox relay and reconciler (runs in the API process, which holds the ledger writer role).

The outbox row is written in the same transaction as the scientific change; the relay starts or
signals deterministically named Temporal workflows. Starting is idempotent (id conflict → use
the existing run), so a crash between dispatch and marking only causes a harmless repeat. The
reconciler periodically re-dispatches accepted work that has no live workflow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from eidos.orchestration.names import (
    RUNNER_QUEUE,
    SEALED_QUEUE,
    WORKFLOW_QUEUE,
    assessment_workflow_id,
    investigation_workflow_id,
    job_workflow_id,
)

log = logging.getLogger("eidos.relay")


class OutboxRelay:
    def __init__(self, ledger: Any, temporal: Client, *, poll_seconds: float = 1.0, reconcile_seconds: float = 30.0,
                 investigation_args: dict | None = None):
        self.ledger = ledger
        self.temporal = temporal
        self.poll = poll_seconds
        self.reconcile_every = reconcile_seconds
        self.inv_args = investigation_args or {}
        self._event = asyncio.Event()
        self._stats = {"dispatched": 0, "errors": 0, "last_error": None, "last_reconcile": None, "paused": False}
        self._task: asyncio.Task | None = None
        self.maintenance_flag = None

    def notify(self) -> None:
        try:
            self._event.set()
        except RuntimeError:
            pass

    def status(self) -> dict:
        return dict(self._stats)

    def pause(self, paused: bool) -> None:
        """Quiesce dispatch (used by backup)."""
        self._stats["paused"] = paused

    async def run(self) -> None:
        last_rec = 0.0
        while True:
            try:
                quiesced = self.maintenance_flag is not None and self.maintenance_flag.exists()
                if not self._stats["paused"] and not quiesced:
                    await self.drain()
                    if time.monotonic() - last_rec > self.reconcile_every:
                        last_rec = time.monotonic()
                        await self.reconcile()
            except Exception as exc:  # keep relaying; record the failure for `lab status`
                self._stats["errors"] += 1
                self._stats["last_error"] = repr(exc)[:500]
                log.exception("relay iteration failed")
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self.poll)
            except TimeoutError:
                pass
            self._event.clear()

    async def drain(self) -> int:
        n = 0
        while True:
            rows = await asyncio.to_thread(self._claim)
            if not rows:
                return n
            for row in rows:
                try:
                    await self.dispatch(row["topic"], row["payload"])
                    await asyncio.to_thread(self._mark, row["id"], True, None)
                    self._stats["dispatched"] += 1
                    n += 1
                except Exception as exc:
                    self._stats["errors"] += 1
                    self._stats["last_error"] = repr(exc)[:500]
                    await asyncio.to_thread(self._mark, row["id"], False, repr(exc))

    def _claim(self) -> list[dict]:
        with self.ledger.transaction() as tx:
            return tx.pending_outbox(limit=20)

    def _mark(self, oid: int, ok: bool, err: str | None) -> None:
        with self.ledger.transaction() as tx:
            tx.mark_outbox(oid, ok=ok, error=err)

    def _last_turn_seq(self, investigation_id: str) -> int:
        with self.ledger.transaction() as tx:
            return max((int(t["seq"]) for t in tx.list_turns(investigation_id)), default=0)

    def _job(self, job_id: str) -> dict | None:
        with self.ledger.transaction() as tx:
            return tx.get_job(job_id)

    async def _start(self, wf: str, arg: dict, wid: str, queue: str, **kw) -> None:
        await self.temporal.start_workflow(wf, arg, id=wid, task_queue=queue,
                                           id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                                           id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE, **kw)

    async def dispatch(self, topic: str, p: dict) -> None:
        if topic in ("investigation.start", "investigation.wake"):
            arg = {"investigation_id": p["investigation_id"], "project_id": p["project_id"], **self.inv_args}
            # A coordinator started again for an investigation that already has turns continues after its last
            # turn; starting at 0 replayed old turn indices (G5 live run: COMMAND_KEY_CONFLICT on complete_turn).
            last = await asyncio.to_thread(self._last_turn_seq, p["investigation_id"])
            if last:
                arg["turn_counter"] = last
            await self._start("InvestigationWorkflow", arg, investigation_workflow_id(p["investigation_id"]),
                              WORKFLOW_QUEUE, start_signal="wake", start_signal_args=[p.get("event_seq", 0)])
        elif topic == "job.dispatch":
            job = await asyncio.to_thread(self._job, p["job_id"])
            if job is None or job["status"] in ("succeeded", "failed", "cancelled", "pruned"):
                return
            queue = SEALED_QUEUE if job["lock"].get("isolation_profile", "").endswith("sealed") else RUNNER_QUEUE
            await self._start("JobWorkflow", {"job_id": job["id"], "project_id": job["project_id"], "queue": queue,
                                              "kind": job["kind"], "wall_seconds": job["lock"]["limits"]["wall_seconds"]},
                              job_workflow_id(job["id"]), WORKFLOW_QUEUE)
        elif topic == "job.cancel":
            try:
                await self.temporal.get_workflow_handle(job_workflow_id(p["job_id"])).signal("cancel", p.get("policy", "cancel"))
            except RPCError:
                pass  # not started yet: start_attempt observes cancel_requested
        elif topic == "assessment.request":
            await self._start("AssessmentWorkflow", {"plan_id": p["plan_id"], "project_id": p["project_id"]},
                              assessment_workflow_id(p["plan_id"]), WORKFLOW_QUEUE)
        elif topic == "qualification.finished":
            return
        else:
            raise ValueError(f"unknown outbox topic {topic!r}")

    async def reconcile(self) -> dict:
        """Accepted work without live workflow progress is re-dispatched (idempotently)."""
        def load():
            with self.ledger.transaction() as tx:
                jobs = tx.all_unfinished_jobs()
                invs = []
                for pr in tx.list_projects():
                    tx.project_id = pr["id"]
                    # An investigation that needs the operator (its coordinator stopped on a limit) is not restarted
                    # here; steering or resuming it wakes it through the outbox.
                    invs += [(pr["id"], r.id) for r in tx.list_records(pr["id"], "investigation", status="active",
                                                                       limit=100000)
                             if not (r.labels or {}).get("needs_input")]
                return jobs, invs
        jobs, invs = await asyncio.to_thread(load)
        redispatched = []
        for job in jobs:
            if not await self._running(job_workflow_id(job["id"])):
                await self.dispatch("job.dispatch", {"job_id": job["id"]})
                redispatched.append(job["id"])
        for pid, inv in invs:
            if not await self._running(investigation_workflow_id(inv)):
                await self.dispatch("investigation.wake", {"investigation_id": inv, "project_id": pid})
                redispatched.append(inv)
        self._stats["last_reconcile"] = {"at": time.time(), "redispatched": redispatched}
        return {"redispatched": redispatched}

    async def _running(self, wid: str) -> bool:
        try:
            d = await self.temporal.get_workflow_handle(wid).describe()
            return d.status == WorkflowExecutionStatus.RUNNING
        except RPCError:
            return False
