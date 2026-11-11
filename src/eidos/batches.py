"""Dependent-batch execution shared by the investigator and the MCP adapter (ADR 0022; ADR 0025).

A batch is an ordered list of already validated calls (``eidos.domain.turns.validate_refs``), each with its ``index``,
its stable ``command_key`` and the ``depends_on`` the validator derived. The caller runs the calls in order; before each
one it asks :meth:`BatchRunner.gate` whether the call may run now, and resolves the call's ``{"$ref": ...}`` arguments with
:meth:`BatchRunner.resolve`. The rules are ADR 0022's:

* a reference to a job means "after it has succeeded"; a job that ends any other way gives every call that needs it a
  ``DEPENDENCY_FAILED`` receipt, and a missing output slot gives ``REF_UNRESOLVED``;
* jobs are admitted one at a time unless the batch's checked allowance (``max_concurrent``) is larger; an evidence plan
  starts on an idle batch and holds the whole allowance until its last job has ended (addendum R11);
* waiting is bounded: past ``slice_ends`` the gate answers ``"pending"`` and the caller hands the wait back. Committed
  calls replay by command key when the batch is run again, so nothing is admitted twice.

Nothing here talks to Temporal or a database: reads go through ``query(name, params)``, effects stay with the caller (the
investigator's admission-checked effects, or the MCP adapter's calls under the caller's own token).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from eidos.domain.turns import COMPUTE_TOOLS, JOB_TOOLS, PLAN_TOOLS, resolve_refs

TERMINAL_JOB_STATUSES = ("succeeded", "failed", "cancelled", "pruned")
ACTIVE_JOB_STATUSES = ["admitted", "queued", "running", "reconciling", "cancel-requested"]

Query = Callable[[str, dict[str, Any]], Any]


class UnresolvedRef(Exception):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


def not_run(call: dict, code: str, message: str, **details: Any) -> dict:
    """The recorded receipt of a call that never ran. It still has its index and command key."""
    return {"tool": call["tool"], "index": call["index"], "ok": False, "code": code, "skipped": True,
            "command_key": call["command_key"], "depends_on": call.get("depends_on") or [],
            "error": {"code": code, "message": message, "details": details}}


def job_error(job: dict) -> dict | None:
    terminal = job.get("terminal") or {}
    error = terminal.get("error")
    return {"category": error.get("category"), "message": str(error.get("message", ""))[:500]} \
        if isinstance(error, dict) else None


class BatchRunner:
    """What one execution slice knows about its batch's jobs (ADR 0022).

    ``interrupted`` is asked while waiting; a non-empty answer (a stop, a pause, a cancelled activity) ends the wait with
    that reason. ``final`` means there is no later slice: a call that would have to wait is recorded ``BATCH_DEADLINE``.
    """

    def __init__(self, query: Query, *, max_concurrent: int = 1, slice_ends: float, final: bool = False,
                 poll: float = 0.5, interrupted: Callable[[], str | None] = lambda: None,
                 job_tools: Sequence[str] = JOB_TOOLS, plan_tools: Sequence[str] = PLAN_TOOLS,
                 compute_tools: Sequence[str] = COMPUTE_TOOLS,
                 deadline_message: str = "the batch ran out of time waiting for its jobs"):
        self.deadline_message = deadline_message
        self.query, self.max_concurrent, self.slice_ends, self.final = query, max_concurrent, slice_ends, final
        self.poll, self.interrupted = poll, interrupted
        self.job_tools, self.plan_tools, self.compute_tools = tuple(job_tools), tuple(plan_tools), tuple(compute_tools)
        self.launched: list[dict] = []

    def admitted_compute(self, receipt: dict) -> bool:
        """Did this receipt admit compute? A refused ``assess_candidate`` is a recorded answer, not an error, and admitted
        nothing: it neither holds the batch's CPU allowance nor gives a wait something to wait for."""
        return bool(receipt["ok"]) and receipt["tool"] in self.compute_tools \
            and (receipt.get("result") or {}).get("status") != "refused"

    def record(self, receipt: dict) -> None:
        """Note a call's receipt; one that admitted compute counts against the allowance."""
        if self.admitted_compute(receipt):
            self.launched.append(receipt)

    def gate(self, call: dict, receipts: list[dict]) -> dict | str | None:
        """Before one call. None when the call may run, a receipt when it must not (a dependency failed, or the batch
        ran out of time for it), ``"pending"`` when it has to wait longer than this slice, or the reason an interruption
        ended the wait."""
        deps = [receipts[d] for d in call.get("depends_on") or []]
        failed = next((d for d in deps if not d["ok"]), None)
        if failed is not None:
            return not_run(call, "DEPENDENCY_FAILED", f"call {failed['index']} ({failed['tool']}) did not succeed",
                           depends_on=failed["index"], dependency_code=failed.get("code"))
        waiting_for = [d for d in deps if d["tool"] in self.job_tools]
        is_job = call["tool"] in self.compute_tools
        # addendum R11: a plan needs the batch's whole allowance, because its jobs share it; it starts on an idle batch
        slots = self.max_concurrent if call["tool"] in self.plan_tools else 1
        plan_launched = any(r["tool"] in self.plan_tools for r in self.launched)
        if not waiting_for and not (is_job and (plan_launched or len(self.launched) + slots > self.max_concurrent)):
            return None
        while True:
            jobs = {d["index"]: self.job(d) for d in waiting_for}
            bad = next(((i, j) for i, j in jobs.items() if j["status"] in TERMINAL_JOB_STATUSES
                        and j["status"] != "succeeded"), None)
            if bad is not None:  # a job that can no longer succeed: nothing that depends on it runs
                i, job = bad
                return not_run(call, "DEPENDENCY_FAILED", f"job {job['id']} of call {i} ended {job['status']}",
                               depends_on=i, job_id=job["id"], job_status=job["status"], error=job_error(job))
            ready = all(j["status"] == "succeeded" for j in jobs.values())
            running, plan_open = self.running()
            if ready and (not is_job or (not plan_open and len(running) + slots <= self.max_concurrent)):
                return None
            if self.final:
                return not_run(call, "BATCH_DEADLINE", self.deadline_message,
                               running_jobs=running)
            reason = self.interrupted()
            if reason:
                return reason
            if time.monotonic() >= self.slice_ends:
                return "pending"
            time.sleep(self.poll)

    def running(self) -> tuple[list[str], bool]:
        """(the batch's active job ids, whether a launched plan still holds the whole allowance; addendum R11). A plan's
        next step is admitted in the transaction that ends its previous one, so a plan with no active job has finished."""
        running, plan_open = [], False
        for r in self.launched:
            if r["tool"] in self.plan_tools:
                jobs = self.query("list_jobs", {"plan_id": r["result"]["plan_id"], "statuses": ACTIVE_JOB_STATUSES})
                running += [str(j["id"]) for j in jobs]
                plan_open = plan_open or bool(jobs)
            elif self.job(r)["status"] not in TERMINAL_JOB_STATUSES:
                running.append(str(r["result"]["job_id"]))
        return running, plan_open

    def job(self, receipt: dict) -> dict:
        """The current record of a job this batch admitted (terminal states are final, so they are cached)."""
        cached = receipt.get("_job")
        if cached is not None:
            return cached
        job = self.query("get_job", {"job_id": receipt["result"]["job_id"], "include_lock": False})
        if job["status"] in TERMINAL_JOB_STATUSES:
            receipt["_job"] = job
        return job

    def ref_value(self, receipt: dict, field: str) -> Any:
        """ADR 0022: the value of ``call[i].<field>`` or ``call[i].outputs.<slot>`` from that call's receipt or job."""
        if field.startswith("outputs."):
            slot = field.split(".", 1)[1]
            job = self.job(receipt)
            for output in job.get("outputs") or []:
                if output["slot"] == slot:
                    return output["artifact_id"]
            raise UnresolvedRef(f"job {job['id']} published no output {slot!r}",
                                {"depends_on": receipt["index"], "job_id": job["id"],
                                 "published": sorted(o["slot"] for o in job.get("outputs") or [])})
        value = (receipt.get("result") or {}).get(field)
        if value is None:
            raise UnresolvedRef(f"call {receipt['index']} ({receipt['tool']}) returned no {field!r}",
                                {"depends_on": receipt["index"]})
        return value

    def resolve(self, call: dict, receipts: list[dict]) -> tuple[dict, dict] | dict:
        """(the call's arguments with every reference replaced, its provenance for the receipt), or the ``REF_UNRESOLVED``
        receipt of a call that cannot run."""
        args = dict(call["args"])
        if not call.get("depends_on"):
            return args, {}
        try:
            args, used = resolve_refs(args, lambda i, field: self.ref_value(receipts[i], field))
        except UnresolvedRef as exc:
            return not_run(call, "REF_UNRESOLVED", str(exc), **exc.details)
        return args, {"command_key": call["command_key"], "depends_on": call["depends_on"], "resolved": used}
