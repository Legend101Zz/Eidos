"""In-memory stand-in for the lab, for the compact workflow tools (ADR 0025). Fixture-only: no lab, no ledger.

It mimics what the workflow relies on and nothing more:

* command idempotency as the real lab has it: the same (project, key) with the same fingerprint returns the original
  receipt with ``replayed: True``; another fingerprint is ``COMMAND_KEY_CONFLICT`` (a failed command stores nothing);
* jobs (``run_component``, ``launch_trial``, ``launch_branch``, ``validate_component``) whose status the test controls
  (``scripts`` per component ref: succeed at once, stay running until ``finish``/``fail``/``set_status``, or fail) and
  whose outputs are configured per component ref (``outputs``);
* notes, claims, artifact uploads, evidence plans and the assessment answer (launched or refused, test-configurable);
* ``get_job``, ``list_jobs``, ``get_claim``, ``get_investigation``.

Every call is recorded in ``lab.calls`` so a test can assert what reached the lab and with which keys.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from eidos.client import LabError

_ABSENT = object()


@dataclass
class Call:
    kind: str  # "command" | "query" | "upload"
    name: str
    payload: dict
    project: str | None
    key: str | None
    token: str
    outcome: str = ""  # commands/uploads: "committed" | "replayed" | "conflict" | "error"


@dataclass
class Job:
    id: str
    component: str
    status: str
    plan_id: str | None = None
    payload: dict = field(default_factory=dict)


class WorkflowLab:
    def __init__(self, tokens: dict[str, str] | None = None):
        self.tokens = tokens or {"good-token": "alice"}
        self._lock = threading.RLock()
        self.calls: list[Call] = []
        self.committed: list[tuple[str, str]] = []  # (command or "upload", key) of every freshly executed effect
        self._receipts: dict[tuple[str, str], tuple[str, dict]] = {}
        self._n: dict[str, int] = {}
        # jobs and their configuration
        self.jobs: dict[str, Job] = {}
        self.scripts: dict[str, str] = {}  # component ref -> "succeed" | "running" | "fail"
        self.default_script = "succeed"
        self.outputs: dict[str, list[dict]] = {}  # component ref -> [{"slot", "artifact_id"}] (visible once succeeded)
        self.job_receipts: dict[str, dict] = {}  # component ref -> the terminal receipt of a succeeded job
        self.job_errors: dict[str, dict] = {}  # component ref -> the terminal error of a failed job
        self.trials: dict[str, str] = {}  # trial id -> component ref
        # claims, notes, artifacts, assessments, investigations
        self.notes: list[dict] = []
        self.claims: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.assess_response: dict | Callable[[dict], dict] | None = None  # None: launched with fresh ids
        self.claim_state: dict[str, Any] = {"state": "candidate-submitted", "label": "Candidate submitted",
                                            "confirmed": False}
        self.claim_assessments: list[dict] = []
        self.investigations: dict[str, dict] = {}
        # test hooks
        self.command_errors: dict[str, tuple[LabError, bool]] = {}  # command -> (error, once)
        self.on_query: Callable[[str, dict], None] | None = None

    # -- construction ---------------------------------------------------------------------------------------------
    def client(self, token: str) -> WorkflowClient:
        return WorkflowClient(self, token)

    def next_id(self, kind: str) -> int:
        self._n[kind] = self._n.get(kind, 0) + 1
        return self._n[kind]

    # -- test controls --------------------------------------------------------------------------------------------
    def fail_command(self, name: str, error: LabError, *, once: bool = True) -> None:
        """Make the next (or every) fresh execution of ``name`` raise ``error`` (nothing is stored for it)."""
        self.command_errors[name] = (error, once)

    def refuse_assessment(self, *reasons: tuple[str, str]) -> None:
        self.assess_response = {"status": "refused", "reasons": [{"code": c, "message": m} for c, m in reasons]}

    def start_job(self, component: str, status: str | None = None, *, plan_id: str | None = None) -> str:
        """Create a job directly (not through a command); ``status`` overrides the component's script."""
        with self._lock:
            job = self._new_job(component, {}, plan_id=plan_id)
            if status:
                self.set_status(job.id, status)
            return job.id

    def add_plan_job(self, plan_id: str, status: str = "running", component: str = "plan-arm@1.0.0") -> str:
        return self.start_job(component, status, plan_id=plan_id)

    def set_status(self, job_id: str, status: str) -> None:
        self.jobs[job_id].status = status

    def finish(self, job_id: str) -> None:
        self.set_status(job_id, "succeeded")

    def fail(self, job_id: str) -> None:
        self.set_status(job_id, "failed")

    def add_claim(self, claim_id: str = "claim_x") -> str:
        self.claims[claim_id] = {"payload": {}, "revision": 1}
        return claim_id

    # -- inspection -----------------------------------------------------------------------------------------------
    def commands(self, name: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.kind == "command" and (name is None or c.name == name)]

    def queries(self, name: str | None = None) -> list[Call]:
        return [c for c in self.calls if c.kind == "query" and (name is None or c.name == name)]

    def effects(self) -> list[Call]:
        """Every command and upload that reached the lab (whether committed, replayed or refused)."""
        return [c for c in self.calls if c.kind in ("command", "upload")]

    def keys(self, name: str | None = None) -> list[str | None]:
        return [c.key for c in self.commands(name)]

    def count(self, name: str) -> int:
        """How many times ``name`` was freshly executed (replays and refusals do not count)."""
        return sum(1 for n, _ in self.committed if n == name)

    @property
    def executed(self) -> int:
        return len(self.committed)

    # -- internals ------------------------------------------------------------------------------------------------
    def _new_job(self, component: str, payload: dict, *, plan_id: str | None = None) -> Job:
        job = Job(f"job_{self.next_id('job')}", component, "queued", plan_id, dict(payload))
        script = self.scripts.get(component, self.default_script)
        job.status = {"succeed": "succeeded", "running": "running", "fail": "failed"}[script]
        self.jobs[job.id] = job
        return job

    def job_record(self, job: Job, *, include_lock: bool = False) -> dict:
        rec: dict[str, Any] = {"id": job.id, "status": job.status, "component": job.component, "outputs": [],
                               "terminal": None, "plan_id": job.plan_id}
        if job.status == "succeeded":
            rec["outputs"] = copy.deepcopy(self.outputs.get(job.component, []))
            rec["terminal"] = {"receipt": copy.deepcopy(self.job_receipts.get(job.component, {"ok": True}))}
        elif job.status in ("failed", "cancelled", "pruned"):
            rec["terminal"] = {"error": copy.deepcopy(self.job_errors.get(
                job.component, {"category": "component_error", "message": "the component exited with status 1"}))}
        if include_lock:
            rec["lock"] = {"component": {"ref": job.component}}
        return rec

    def execute(self, name: str, payload: dict) -> dict:
        """The result of a fresh command."""
        if name in self.command_errors:
            error, once = self.command_errors[name]
            if once:
                del self.command_errors[name]
            raise error
        if name in ("run_component", "launch_trial", "launch_branch", "validate_component"):
            component = payload.get("component_ref") or self.trials.get(payload.get("trial_id"), name)
            job = self._new_job(component, payload)
            trial = f"trial_{self.next_id('trial')}"
            digest = "sha256:" + hashlib.sha256(job.id.encode()).hexdigest()
            if name == "run_component":
                return {"job_id": job.id, "trial_id": trial, "lock_digest": digest, "component": component}
            if name == "validate_component":
                return {"job_id": job.id}
            return {"job_id": job.id, "trial_id": payload.get("trial_id", trial), "lock_digest": digest}
        if name in ("plan_trial", "plan_branch"):
            trial = f"trial_{self.next_id('trial')}"
            self.trials[trial] = payload.get("component_ref", name)
            out = {"trial_id": trial, "lock_digest": "sha256:" + hashlib.sha256(trial.encode()).hexdigest()}
            return {**out, "branch_id": f"branch_{trial}"} if name == "plan_branch" else out
        if name == "record_note":
            self.notes.append(copy.deepcopy(payload))
            return {"note_id": f"note_{len(self.notes)}"}
        if name == "file_claim":
            claim_id = payload.get("claim_id") or f"claim_{self.next_id('claim')}"
            self.claims[claim_id] = {"payload": copy.deepcopy(payload), "revision": 1}
            return {"claim_id": claim_id, "revision": 1}
        if name == "assess_candidate":
            if callable(self.assess_response):
                return self.assess_response(payload)
            if self.assess_response is not None:
                return copy.deepcopy(self.assess_response)
            n = self.next_id("plan")
            return {"status": "launched", "plan_id": f"plan_{n}", "request_id": f"req_{n}"}
        if name == "draft_evidence_plan":
            return {"plan_id": payload.get("plan_id") or f"plan_{self.next_id('plan')}"}
        if name == "freeze_evidence_plan":
            return {"plan_id": payload["plan_id"], "plan_digest": "sha256:" + hashlib.sha256(
                payload["plan_id"].encode()).hexdigest()}
        if name == "launch_evidence_plan":
            return {"plan_id": payload["plan_id"]}
        if name == "publish_component":
            return {"component_ref": f"published@1.0.{self.next_id('release')}", "digest": "sha256:" + "0" * 64}
        return {}


class WorkflowClient:
    def __init__(self, lab: WorkflowLab, token: str):
        self.lab, self.token = lab, token

    def close(self) -> None:
        pass

    def whoami(self) -> dict:
        if self.token not in self.lab.tokens:
            raise LabError(401, {"code": "UNAUTHENTICATED", "message": "invalid token"})
        return {"id": self.lab.tokens[self.token], "kind": "human", "scopes": [], "projects": ["*"]}

    # -- effects --------------------------------------------------------------------------------------------------
    def _effect(self, call: Call, fingerprint: str, run: Callable[[], dict]) -> dict:
        lab = self.lab
        hit = lab._receipts.get((call.project or "", call.key or ""))
        if hit:
            if hit[0] == fingerprint:
                call.outcome = "replayed"
                return {**copy.deepcopy(hit[1]), "replayed": True}
            call.outcome = "conflict"
            raise LabError(409, {"code": "COMMAND_KEY_CONFLICT", "message": "key reused with different arguments",
                                 "next_action": "Use a new command_key for a new operation."})
        try:
            result = run()
        except LabError:
            call.outcome = "error"
            raise
        receipt = {"status": "committed", "command": call.name, "replayed": False, "command_key": call.key,
                   "events": [1], "result": result}
        lab._receipts[(call.project or "", call.key or "")] = (fingerprint, copy.deepcopy(receipt))
        lab.committed.append((call.name, call.key or ""))
        call.outcome = "committed"
        return receipt

    def command(self, name, payload=None, *, project=None, key=None, expected_revision=None,
                normalized_arguments=_ABSENT, **unexpected):
        if unexpected:
            raise TypeError(f"command() got unexpected keyword arguments {sorted(unexpected)}")
        self.whoami()
        payload = payload or {}
        with self.lab._lock:
            call = Call("command", name, copy.deepcopy(payload), project, key, self.token)
            self.lab.calls.append(call)
            if not key:
                call.outcome = "error"
                raise LabError(400, {"code": "INVALID_ARGUMENTS", "message": "a command key is required"})
            fp = json.dumps([name, payload, expected_revision], sort_keys=True, default=str)
            return self._effect(call, fp, lambda: self.lab.execute(name, payload))

    def upload(self, project, path=None, *, data=None, media_type="application/octet-stream", schema=None,
               role="input", access_class="project", name=None, key=None, investigation=None, admission=None):
        self.whoami()
        if data is None:
            raise TypeError("the fake lab uploads bytes only (data=...)")
        meta = {"media_type": media_type, "schema": schema, "role": role, "access_class": access_class, "name": name,
                "investigation": investigation, "admission": admission}
        sha = hashlib.sha256(data).hexdigest()
        key = key or "upload:" + sha[:32] + ":" + (name or "")
        with self.lab._lock:
            call = Call("upload", "upload", {**meta, "sha256": sha}, project, key, self.token)
            self.lab.calls.append(call)
            fp = json.dumps([meta, sha], sort_keys=True, default=str)

            def run() -> dict:
                artifact_id = f"art_{self.lab.next_id('artifact')}"
                self.lab.uploads.append({"data": data, "key": key, "artifact_id": artifact_id, "project": project,
                                         **meta})
                return {"artifact_id": artifact_id, "digest": "sha256:" + sha, "size": len(data), "role": role}

            return self._effect(call, fp, run)

    # -- reads ----------------------------------------------------------------------------------------------------
    def query(self, name, params=None, *, project=None):
        self.whoami()
        params = params or {}
        lab = self.lab
        with lab._lock:
            lab.calls.append(Call("query", name, copy.deepcopy(params), project, None, self.token))
            if lab.on_query:
                lab.on_query(name, params)
            if name == "get_job":
                job = lab.jobs.get(params["job_id"])
                if job is None:
                    raise LabError(404, {"code": "NOT_FOUND", "message": f"job {params['job_id']} not found",
                                         "next_action": "Check the id."})
                return lab.job_record(job, include_lock=bool(params.get("include_lock", True)))
            if name == "list_jobs":
                jobs = [j for j in lab.jobs.values() if params.get("plan_id") in (None, j.plan_id)]
                if params.get("statuses"):
                    jobs = [j for j in jobs if j.status in params["statuses"]]
                return [lab.job_record(j) for j in jobs]
            if name == "get_claim":
                claim = lab.claims.get(params["claim_id"])
                if claim is None:
                    raise LabError(404, {"code": "NOT_FOUND", "message": "claim not found", "next_action": ""})
                return {"id": params["claim_id"], "revision": claim["revision"],
                        "claim_state": copy.deepcopy(lab.claim_state),
                        "assessments": copy.deepcopy(lab.claim_assessments)}
            if name == "get_investigation":
                inv = params["investigation_id"]
                return {"id": inv, "revision": 3, "status": "active", "needs_input": None, "turns": [], "jobs": [],
                        **lab.investigations.get(inv, {})}
            return {"query": name, "params": params}
