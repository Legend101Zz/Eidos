"""Investigator-worker activities. Credentials: an ``investigator`` principal (exploration and
proposals) for effectful calls and a ``workflow`` principal for turn bookkeeping. No database.

Recovery rules implemented here:
* ``decide``: each activity attempt is a new model response → new decision identity. If a prior
  attempt already got its decision accepted for this slot, the service returns that decision
  and records the new response as unaccepted (its usage is still charged).
* ``execute_decision``: re-executes calls of the *persisted* decision with their recorded
  command keys, so a retry after a committed effect returns the existing receipt.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import tempfile
import threading
from contextlib import contextmanager

from temporalio import activity
from temporalio.exceptions import ApplicationError

from eidos.client import LabClient, LabError
from eidos.domain.turns import EFFECT_TOOLS, WORKFLOW_PAYLOAD_LIMIT, check_payload
from eidos.investigator.context import pack
from eidos.investigator.runtime import DecisionRequest, ModelCallFailed, build_runtime
from eidos_wire import command_key


@contextmanager
def _heartbeating(every: float = 5.0):
    """Heartbeat while an activity body runs so Temporal notices a killed worker within the heartbeat
    timeout (30 s in TurnWorkflow) instead of waiting for the 10-minute start-to-close timeout."""
    ctx = contextvars.copy_context()
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            try:
                activity.heartbeat()
            except Exception:  # noqa: BLE001 - the activity is gone; nothing to heartbeat
                return

    threading.Thread(target=lambda: ctx.run(loop), daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def _hb(fn):
    @functools.wraps(fn)
    def wrapper(self, a):
        with _heartbeating():
            return fn(self, a)
    return wrapper


def _app_error(exc: LabError) -> ApplicationError:
    return ApplicationError(str(exc), exc.error, type=exc.code or "LAB_ERROR",
                            non_retryable=exc.status in (400, 403, 404, 409, 413, 422))


def bounded(result: dict) -> dict:
    try:
        check_payload(result, limit=WORKFLOW_PAYLOAD_LIMIT)
        return result
    except Exception:
        return {k: result[k] for k in ("accepted", "decision_id", "n_calls", "control", "reason") if k in result} | {
            "truncated": True}


class InvestigatorActivities:
    def __init__(self, investigator: LabClient, workflow: LabClient, *, runtime_overrides: dict | None = None):
        self.inv = investigator
        self.wf = workflow
        self.runtime_overrides = runtime_overrides or {}

    # -- turn lifecycle ----------------------------------------------------------------------
    @activity.defn(name="begin_turn")
    @_hb
    def begin_turn(self, a: dict) -> dict:
        try:
            r = self.wf.command("begin_turn", {"investigation_id": a["investigation_id"],
                                               "runtime_release": "eidos-investigator/1"},
                                project=a["project_id"], key=f"begin:{a['investigation_id']}:{a['index']}")["result"]
        except LabError as exc:
            raise _app_error(exc) from exc
        if not r.get("started"):
            return {"started": False, "reason": r.get("reason")}
        lim = r["spec"]["limits"]
        return {"started": True, "turn_id": r["turn_id"], "max_model_calls": lim["max_model_calls"],
                "resumed": r.get("resumed", False), "model_timeout": lim.get("deadline_seconds", 300)}

    @activity.defn(name="build_context")
    @_hb
    def build_context(self, a: dict) -> dict:
        p = a["project_id"]
        turn = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)
        if turn.get("context"):
            return {"context_artifact_id": turn["context"]["context_artifact_id"], "reused": True}
        view = self.inv.query("investigation_view", {"investigation_id": a["investigation_id"], "horizon": turn["horizon"]},
                              project=p)
        view["budgets"] = turn["spec"].get("budgets")  # immutable snapshot taken at begin_turn
        catalog = self.inv.query("search_components", {"limit": 100, "as_of_horizon": turn["horizon"]}, project=p)["components"]
        packed = pack(view, catalog)
        # The source manifest includes the catalog snapshot so offline replays see exactly what was
        # installed at the horizon, not components published later.
        src_bytes = json.dumps({**view, "catalog": catalog}, sort_keys=True, default=str).encode()
        digest = hashlib.sha256(src_bytes + packed["text"].encode()).hexdigest()[:24]
        src = self.inv.upload(p, data=src_bytes, media_type="application/json",
                              schema="investigation-view/1", role="context", name=f"source-{a['turn_id']}",
                              key=f"src:{a['turn_id']}:{digest}")
        ctx_art = self.inv.upload(p, data=packed["text"].encode(), media_type="text/plain", schema="packed-context/1",
                                  role="context", name=f"context-{a['turn_id']}", key=f"ctx:{a['turn_id']}:{digest}")
        self.wf.command("record_turn_context", {
            "turn_id": a["turn_id"], "context_artifact_id": ctx_art["result"]["artifact_id"],
            "source_manifest_artifact_id": src["result"]["artifact_id"],
            "source_manifest_digest": packed["source_manifest_digest"], "policy": packed["policy"],
            "selections": packed["selections"], "omissions": packed["omissions"], "truncations": packed["truncations"],
            "horizon": turn["horizon"], "chars": packed["chars"]}, project=p, key=f"ctxrec:{a['turn_id']}:{digest}")
        rec = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)["context"]  # first write wins
        return {"context_artifact_id": rec["context_artifact_id"], "chars": rec.get("chars")}

    def _turn_so_far(self, p: str, turn: dict) -> list[dict]:
        out = []
        for d in turn["decisions"]:
            if not d["accepted"]:
                if (d["body"] or {}).get("reason") == "invalid":  # feedback: why the last proposal was refused
                    out.append({"slot": d["slot"], "rejected": True, "error": d["body"].get("error")})
                continue
            try:
                rec = self.wf.query("get_record", {"kind": "decision_receipts", "id": d["id"]}, project=p)["body"]["receipts"]
            except LabError:
                rec = []
            out.append({"slot": d["slot"], "calls": [{"tool": c["tool"], "args": c["args"]} for c in d["body"]["calls"]],
                        "control": d["body"].get("control"), "receipts": rec})
        return out

    @activity.defn(name="decide")
    @_hb
    def decide(self, a: dict) -> dict:
        p = a["project_id"]
        turn = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)
        existing = next((d for d in turn["decisions"] if d["accepted"] and d["slot"] == a["slot"]), None)
        if existing:  # a previous attempt's decision was accepted; never regenerate it
            return {"accepted": True, "decision_id": existing["id"], "n_calls": len(existing["body"]["calls"]),
                    "control": existing["body"].get("control"), "reason": "recovered"}
        ctxrec = turn["context"]
        with tempfile.NamedTemporaryFile() as tmp:
            self.inv.download(p, ctxrec["context_artifact_id"], tmp.name)
            context_text = open(tmp.name, encoding="utf-8").read()
        with tempfile.NamedTemporaryFile() as tmp:
            self.inv.download(p, ctxrec["source_manifest_artifact_id"], tmp.name)
            view = json.load(open(tmp.name))
        cfg = {**turn["spec"]["runtime"].get("settings", {}), "runtime": turn["spec"]["runtime"]["runtime"],
               "model": turn["spec"]["runtime"].get("model"), **self.runtime_overrides}
        runtime = build_runtime(cfg)
        # Reserve this decision's maximum envelope before any request (checked inside the turn,
        # not only between turns). Failure paths settle the reservation via record_model_usage.
        # A fixture runtime makes no model requests: reserve nothing unless an envelope is configured.
        default_env = 0 if cfg.get("runtime", "fixture") == "fixture" else 120_000
        est_tokens = float(cfg.get("max_tokens_per_decision", default_env))
        price = cfg.get("price_per_mtok") or {}
        estimate = {"model_tokens": est_tokens}
        if price:
            estimate["model_usd"] = est_tokens * max(price.values()) / 1e6
        try:
            rr = self.wf.command("reserve_model_budget", {"turn_id": a["turn_id"], "slot": a["slot"],
                                 "attempt": activity.info().attempt, "estimate": estimate}, project=p,
                                 key=f"reserve:{a['turn_id']}:{a['slot']}:{activity.info().attempt}")["result"]
            reserved, reservation_ref = rr["reserved"], rr.get("reservation_ref")
        except LabError as exc:
            if exc.code == "BUDGET_EXHAUSTED":
                return {"accepted": False, "reason": "budget-exhausted", "n_calls": 0}
            raise
        req = DecisionRequest(turn_id=a["turn_id"], slot=a["slot"], context=context_text,
                              turn_so_far=self._turn_so_far(p, turn), limits=turn["spec"]["limits"], settings=cfg,
                              read_tools=self._read_tools(p), view=view)
        attempt = activity.info().attempt
        try:
            res = runtime.decide(req)
        except ModelCallFailed as exc:
            self.wf.command("record_model_usage", {"turn_id": a["turn_id"], "usage": {**exc.possible_usage, "uncertain": True,
                            "error": str(exc)[:500]}, "reserved": reserved,
                            "reservation_ref": reservation_ref}, project=p,
                            key=f"usage-fail:{a['turn_id']}:{a['slot']}:{attempt}")
            raise ApplicationError(str(exc), type="MODEL_CALL_FAILED", non_retryable=not exc.retryable) from exc
        # Spend is the first durable write: a crash before acceptance must not lose billed requests.
        self.wf.command("record_model_usage", {"turn_id": a["turn_id"], "usage": res.usage, "reserved": reserved,
                                                      "reservation_ref": reservation_ref},
                        project=p, key=f"usage:{a['turn_id']}:{a['slot']}:{attempt}")
        resp = self.inv.upload(p, data=json.dumps(res.raw_response, default=str, sort_keys=True).encode(),
                               media_type="application/json", schema="model-response/1", role="context",
                               name=f"response-{a['turn_id']}-{a['slot']}-{attempt}",
                               key=f"resp:{a['turn_id']}:{a['slot']}:{attempt}")
        try:
            r = self.wf.command("accept_decision", {"turn_id": a["turn_id"], "slot": a["slot"], "proposal": res.proposal,
                                "usage": res.usage, "usage_recorded": True, "model_settings": res.model_settings,
                                "prompt_revision": res.prompt_revision,
                                "response_artifact_id": resp["result"]["artifact_id"]},
                                project=p, key=f"accept:{a['turn_id']}:{a['slot']}:{attempt}")["result"]
        except LabError as exc:
            raise _app_error(exc) from exc
        return bounded({"accepted": r["accepted"], "decision_id": r["decision_id"], "n_calls": len(r.get("calls") or []),
                        "control": r.get("control"), "reason": r.get("reason")})

    def _read_tools(self, p: str) -> dict:
        return {
            "inspect_evidence": lambda artifact_id, max_bytes=8192: self.inv.query(
                "artifact_preview", {"artifact_id": artifact_id, "max_bytes": max_bytes}, project=p),
            "search_components": lambda text="", capability=None: self.inv.query(
                "search_components", {"text": text, "capability": capability, "limit": 20}, project=p),
            "describe_component": lambda component_ref: self.inv.query(
                "describe_component", {"component_ref": component_ref}, project=p),
            "get_job": lambda job_id: self.inv.query("get_job", {"job_id": job_id, "include_lock": False}, project=p),
        }

    # -- effects -------------------------------------------------------------------------------
    @activity.defn(name="execute_decision")
    @_hb
    def execute_decision(self, a: dict) -> dict:
        p = a["project_id"]
        turn = self.wf.query("get_turn", {"turn_id": a["turn_id"]}, project=p)
        dec = next(d for d in turn["decisions"] if d["id"] == a["decision_id"] and d["accepted"])
        receipts = []
        for call in dec["body"]["calls"]:
            receipts.append(self._execute_call(p, a["investigation_id"], turn["id"], dec["id"], call))
        # Replay-invariant record: a retry after committed effects yields identical receipts.
        stable = [{k: v for k, v in r.items() if k != "replayed"} for r in receipts]
        self.wf.command("record_decision_receipts", {"decision_id": dec["id"], "receipts": stable}, project=p,
                        key=f"receipts:{dec['id']}")
        summary = [{"tool": r["tool"], "ok": r["ok"], "replayed": r.get("replayed"), "code": r.get("code")} for r in receipts]
        return bounded({"summary": summary})

    def _execute_call(self, p: str, inv: str, turn_id: str, dec_id: str, call: dict) -> dict:
        tool, args, idx = call["tool"], dict(call["args"]), call["index"]
        key = call["command_key"]
        assert key == command_key(p, turn_id, dec_id, idx), "persisted command key mismatch"
        args.setdefault("investigation_id", inv)
        try:
            if tool == "author_component":
                bundle = json.dumps({"files": args.get("files") or {}}, sort_keys=True).encode()
                up = self.inv.upload(p, data=bundle, media_type="application/vnd.eidos.source-bundle+json",
                                     schema="source-bundle/1", role="source", name="authored-component",
                                     key=command_key(p, turn_id, dec_id, idx, 1))
                aid = up["result"]["artifact_id"]
                r = self.inv.command("validate_component", {"bundle_artifact_id": aid, "investigation_id": inv,
                                     "environment": args.get("environment", "default")}, project=p, key=key)
                r["result"]["bundle_artifact_id"] = aid
            else:
                r = self.inv.command(EFFECT_TOOLS[tool], args, project=p, key=key)
            return {"tool": tool, "index": idx, "ok": True, "replayed": r.get("replayed"), "result": r["result"]}
        except LabError as exc:
            return {"tool": tool, "index": idx, "ok": False, "code": exc.code, "error": exc.error}

    OPERATOR_TYPES = ["investigation.steered", "investigation.charter_attached", "investigation.resumed",
                      "investigation.paused", "investigation.stopped", "charter.amended"]
    NEWS_TYPES = ["job.succeeded", "job.failed", "job.cancelled", "job.pruned", "evidence.ready", "assessment.committed",
                  "investigation.steered", "investigation.charter_attached", "investigation.resumed",
                  "investigation.paused", "investigation.stopped", "component.published", "component.withdrawn",
                  "charter.amended", "exposure.recorded",
                  # effects of the investigator's own calls that need a fresh view to act on
                  "claim.filed", "model.exported", "component.retired", "evidence_plan.frozen", "evidence_plan.drafted"]

    @activity.defn(name="check_news")
    def check_news(self, a: dict) -> dict:
        """Cheap ledger check (no model call): has anything relevant happened since the last turn's horizon?"""
        inv = self.wf.query("get_investigation", {"investigation_id": a["investigation_id"]}, project=a["project_id"])
        if inv["status"] != "active":
            return {"news": True, "reason": f"investigation is {inv['status']}"}
        horizon = max((t["horizon"] for t in inv["turns"]), default=0)
        types = self.OPERATOR_TYPES if a.get("operator_only") else self.NEWS_TYPES
        ev = self.wf.query("list_events", {"investigation_id": a["investigation_id"], "cursor": horizon,
                                           "types": types, "limit": 1}, project=a["project_id"])
        return {"news": bool(ev["events"]), "after_horizon": horizon}

    @activity.defn(name="complete_turn")
    @_hb
    def complete_turn(self, a: dict) -> dict:
        r = self.wf.command("complete_turn", {"turn_id": a["turn_id"], "stop_reason": a["stop_reason"],
                            "control": a.get("control"), "receipts": a.get("receipts", [])},
                            project=a["project_id"], key=f"complete:{a['turn_id']}")
        return {"status": r["result"].get("status")}
