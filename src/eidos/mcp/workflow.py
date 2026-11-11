"""The compact research workflow for external agents (ADR 0025): source upload, dependent batches, bounded waits and
submission for confirmation, each in one MCP call.

Every effect is an ordinary lab command or upload under the caller's own token, with a command key derived from the
caller's: ``<key>:<index>`` for a batch's calls, ``<key>:lock`` / ``<key>:claim`` / ``<key>:assess`` for a submission.
A retry with the same key and arguments therefore replays every committed step, and the lab applies the same
validation, idempotency, provenance and claim rules as for the investigator's own calls. Batches use the investigator's
executor (``eidos.batches``, ADR 0022). Responses are small: ids, statuses and artifact handles, expanded on request
with ``get_run`` or ``get_artifact_access``.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from eidos.batches import ACTIVE_JOB_STATUSES, TERMINAL_JOB_STATUSES, BatchRunner
from eidos.client import LabError
from eidos.domain.errors import Invalid
from eidos.domain.turns import (
    LITERAL_ARGS,
    PLAN_TOOLS,
    REF_KEY,
    RESULT_FIELDS,
    iter_refs,
    parse_ref,
    validate_refs,
)

SOURCE_MEDIA_TYPE = "application/vnd.eidos.source-bundle+json"
SOURCE_SCHEMA = "source-bundle/1"
MAX_BATCH_CALLS = 12
DEFAULT_WAIT_SECONDS = 20
MAX_WAIT_SECONDS = 50  # below common client request timeouts (60 s), so a wait never looks like a lost reply
MAX_WAIT_JOBS = 20
POLL_SECONDS = 1.0
SMALL_JSON = 800  # a value longer than this (as JSON) is replaced by a handle in a compact receipt

# What a batch may contain: the effect tools of the investigator's vocabulary as MCP names them.
BATCH_TOOLS = ("upload_source", "run_component", "plan_trial", "launch_trial", "plan_branch", "launch_branch",
               "validate_component", "publish_component", "file_claim", "draft_evidence_plan", "freeze_evidence_plan",
               "launch_evidence_plan", "assess_candidate")
BATCH_JOB_TOOLS = ("run_component", "launch_trial", "launch_branch", "validate_component")
BATCH_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    **{t: f for t, f in RESULT_FIELDS.items() if t in BATCH_TOOLS},
    "upload_source": ("artifact_id",), "validate_component": ("job_id",),
}
BATCH_ARGS_FORBIDDEN = ("project", "command_key", "expected_revision")
# Calls without an investigation_id of their own: they run in the scope of the record they launch (R15).
LAUNCH_SCOPE = {"launch_trial": ("trial_id", "trial"), "launch_branch": ("trial_id", "trial"),
                "freeze_evidence_plan": ("plan_id", "evidence_plan"), "launch_evidence_plan": ("plan_id", "evidence_plan")}
REFUSAL_REASONS = 50  # reasons kept in a compact refusal (a refusal lists each failed rule once; there are fewer)
_PLACEHOLDER = {"string": "ref", "integer": 0, "number": 0, "boolean": False, "object": {}, "array": []}


class WorkflowFailure(Exception):
    """Raised as a ToolFailure by the gateway (kept separate to avoid an import cycle)."""

    def __init__(self, code: str, message: str, next_action: str = "", details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.next_action, self.details = code, message, next_action, details or {}


# -- compact views --------------------------------------------------------------------------------------------------

def _small(value: Any) -> bool:
    return len(json.dumps(value, default=str)) <= SMALL_JSON


def job_summary(job: dict) -> dict[str, Any]:
    """A job in a few fields: outputs are artifact handles (``get_artifact_access`` expands them), the component's own
    receipt is inlined only when small (``get_run`` has it in full)."""
    out: dict[str, Any] = {"job_id": job.get("id"), "status": job.get("status")}
    component = job.get("component") or ((job.get("lock") or {}).get("component") or {}).get("ref")
    if component:
        out["component"] = component
    outputs = {o["slot"]: o["artifact_id"] for o in job.get("outputs") or [] if "slot" in o and "artifact_id" in o}
    if outputs:
        out["outputs"] = outputs
    terminal = job.get("terminal") or {}
    receipt = terminal.get("receipt")
    if receipt:
        out["receipt"] = receipt if _small(receipt) else {"keys": sorted(receipt)[:20], "expand": "get_run"}
    error = terminal.get("error")
    if isinstance(error, dict):
        out["error"] = {"category": error.get("category"), "message": str(error.get("message", ""))[:300]}
    return out


def refusal_reasons(reasons: Any) -> list[dict[str, Any]]:
    """Every reason of a recorded refusal, compactly: its code, its message and its details (when small). These are what
    the caller acts on, so a compact receipt never drops them (R15)."""
    out = []
    for r in (reasons if isinstance(reasons, list) else [])[:REFUSAL_REASONS]:
        if not isinstance(r, dict):
            continue
        reason: dict[str, Any] = {"code": r.get("code"), "message": str(r.get("message", ""))[:500]}
        if r.get("details"):
            reason["details"] = r["details"] if _small(r["details"]) else {"keys": sorted(r["details"])[:20]}
        out.append(reason)
    return out


def _compact_result(tool: str, result: Any) -> Any:
    if _small(result) or not isinstance(result, dict):
        return result
    keep = {k: result[k] for k in (*BATCH_RESULT_FIELDS.get(tool, ()), "status", "request_id", "exploratory")
            if k in result}
    if result.get("reasons"):  # a recorded refusal (assess_candidate): its reasons are the answer
        keep["reasons"] = refusal_reasons(result["reasons"])
    return {**keep, "truncated": True}


def _compact_receipt(r: dict) -> dict[str, Any]:
    out: dict[str, Any] = {"i": r["index"], "tool": r["tool"], "ok": r["ok"]}
    if r.get("replayed"):
        out["replayed"] = True
    if r["ok"]:
        out["result"] = _compact_result(r["tool"], r.get("result"))
    else:
        err = r.get("error") or {}
        out["error"] = {"code": r.get("code") or err.get("code"), "message": str(err.get("message", ""))[:300]}
        if err.get("next_action"):
            out["error"]["next_action"] = str(err["next_action"])[:200]
        details = err.get("details")
        if details and _small(details):
            out["error"]["details"] = details
    if r.get("resolved"):
        out["resolved"] = [{"path": u["path"], "value": u["value"]} for u in r["resolved"]]
    if r.get("normalized_arguments"):
        out["normalized_arguments"] = r["normalized_arguments"]
    return out


def _bounded_wait(args: dict[str, Any]) -> float:
    value = args.get("wait_seconds")
    return float(DEFAULT_WAIT_SECONDS if value is None else max(0, min(int(value), MAX_WAIT_SECONDS)))


def _failed(tool: str, index: int, exc: LabError) -> dict:
    return {"tool": tool, "index": index, "ok": False, "code": exc.code, "error": exc.error}


# -- upload_source --------------------------------------------------------------------------------------------------

def source_bundle(files: Any) -> bytes:
    """The canonical bytes of a ``source-bundle/1`` artifact, as the investigator's ``author_source`` writes them."""
    if not isinstance(files, dict) or not files:
        raise WorkflowFailure("INVALID_ARGUMENTS", "files must be a non-empty object of relative path -> text",
                              "Pass files as {\"train.py\": \"...\"}.")
    bad = [k for k, v in files.items() if not isinstance(k, str) or not k or k.startswith("/") or ".." in k.split("/")
           or not isinstance(v, str)]
    if bad:
        raise WorkflowFailure("INVALID_ARGUMENTS", f"files: invalid entries {sorted(map(str, bad))[:5]}",
                              "Use relative paths without '..' and text contents.")
    return json.dumps({"files": files}, sort_keys=True).encode()


def upload_source(client: Any, project: str, key: str, args: dict[str, Any]) -> dict[str, Any]:
    data = source_bundle(args.get("files"))
    up = client.upload(project, data=data, media_type=SOURCE_MEDIA_TYPE, schema=SOURCE_SCHEMA, role="source",
                       name=str(args.get("name") or "authored-source"), key=key,
                       investigation=args.get("investigation_id"))
    result = up.get("result") or {}
    out = {"artifact_id": result.get("artifact_id"), "sha256": hashlib.sha256(data).hexdigest(), "files": len(args["files"]),
           "use_as": "run_component inputs.source"}
    return {"ok": True, "replayed": bool(up.get("replayed")), "result": out}


# -- run_batch -------------------------------------------------------------------------------------------------------

def validate_batch(calls: Any, investigation_id: str | None, client: Any = None,
                   project: str | None = None) -> list[dict[str, Any]]:
    """Every call checked before anything runs: the tool, its arguments against the tool's own schema (a reference stands
    in for a value of the declared type), and every reference by the investigator's rules (ADR 0022). Each call's
    arguments get the same ADR 0017 normalization as a single MCP call, recorded in ``normalized_arguments``."""
    from eidos.mcp.gateway import ToolFailure, _normalize_config, validate_args  # noqa: PLC0415 - imports this module
    from eidos.mcp.normalize import normalize_arguments  # noqa: PLC0415
    from eidos.mcp.tools import BY_NAME  # noqa: PLC0415

    if not isinstance(calls, list) or not calls:
        raise WorkflowFailure("INVALID_BATCH", "calls must be a non-empty array of {tool, args}")
    if len(calls) > MAX_BATCH_CALLS:
        raise WorkflowFailure("INVALID_BATCH", f"a batch has at most {MAX_BATCH_CALLS} calls; got {len(calls)}",
                              "Split the work into several batches.")
    out: list[dict[str, Any]] = []
    for i, c in enumerate(calls):
        where = f"call {i}"
        if not isinstance(c, dict) or not set(c) <= {"tool", "args"} or "tool" not in c:
            raise WorkflowFailure("INVALID_BATCH", f"{where}: a call is exactly {{tool, args}}", details={"call": i})
        tool = c["tool"]
        if tool not in BATCH_TOOLS:
            raise WorkflowFailure("INVALID_BATCH", f"{where}: {tool!r} cannot be batched",
                                  details={"call": i, "allowed": list(BATCH_TOOLS)})
        args = c.get("args") or {}
        if not isinstance(args, dict):
            raise WorkflowFailure("INVALID_BATCH", f"{where} ({tool}): args must be an object", details={"call": i})
        extra = [k for k in BATCH_ARGS_FORBIDDEN if k in args]
        if extra:
            raise WorkflowFailure("INVALID_BATCH", f"{where} ({tool}): {extra} are set by the batch, not by its calls",
                                  details={"call": i})
        spec = BY_NAME[tool]
        try:
            payload_refs = list(iter_refs(args.get("payload") or {}, "payload"))
        except Invalid as exc:
            raise WorkflowFailure("INVALID_REF", f"{where} ({tool}): {exc.message}", details={"call": i}) from exc
        for path, _text in payload_refs:
            if path.split(".")[1].split("[")[0] in LITERAL_ARGS:  # as for named arguments (ADR 0022)
                raise WorkflowFailure("INVALID_REF", f"{where} ({tool}): {path} must be literal, not a reference",
                                      details={"call": i, "path": path})
        args, changes = normalize_arguments(dict(args), spec.input_schema())
        if investigation_id and "investigation_id" not in args and any(p.name == "investigation_id" for p in spec.params):
            args["investigation_id"] = investigation_id
        props = spec.input_schema()["properties"]
        probe = {k: (_PLACEHOLDER[props[k]["type"]] if isinstance(v, dict) and REF_KEY in v and k in props else v)
                 for k, v in args.items()}
        try:
            validate_args(spec, {**probe, "project": "p", **({"command_key": "k"} if spec.mutating else {})})
        except ToolFailure as exc:
            raise WorkflowFailure("INVALID_BATCH", f"{where} ({tool}): {exc.message}", details={"call": i}) from exc
        if tool == "upload_source" and not iter_has_ref(args.get("files")):
            try:
                source_bundle(args.get("files"))  # refused here, before any effect, as a single upload_source call is
            except WorkflowFailure as exc:
                raise WorkflowFailure("INVALID_BATCH", f"{where} ({tool}): {exc.message}", details={"call": i}) from exc
        if client is not None and not iter_has_ref(args.get("config")):
            cfg, cfg_changes = _normalize_config(client, spec, {**args, "project": project})
            if cfg_changes:
                args, changes = {**args, "config": cfg}, changes + cfg_changes
        out.append({"tool": tool, "args": args, **({"normalized_arguments": changes} if changes else {})})
    try:
        validate_refs(out, result_fields=BATCH_RESULT_FIELDS, job_tools=BATCH_JOB_TOOLS)
    except Invalid as exc:
        raise WorkflowFailure("INVALID_REF", exc.message, "Reference an earlier call: {\"$ref\": \"call[i].field\"} or "
                              "{\"$ref\": \"call[i].outputs.<slot>\"} of a job.", exc.details) from exc
    return out


def _arg(args: dict[str, Any], name: str) -> Any:
    """An argument as the lab receives it: a named argument wins over the same key in ``payload`` (``build_call``)."""
    payload = args.get("payload")
    given = payload.get(name) if isinstance(payload, dict) else None
    return args.get(name) if args.get(name) is not None else given


def batch_investigation(calls: list[dict[str, Any]], investigation_id: str | None, client: Any = None,
                        project: str | None = None) -> str | None:
    """The one investigation a batch runs in (R15): every call's effective scope, named or through ``payload``, must be
    the batch's, so no ``$ref`` can carry one investigation's evidence into a call charged to another. A launch takes the
    scope of what it launches: the referenced call's, or the planned trial's own record. A literal evidence plan id is
    not read here (a plan read is an exposure); the lab runs such a plan in its own scope, and only its id can flow on.
    Returns the batch's investigation (None: the project, outside any investigation)."""
    unknown = object()
    scopes: list[Any] = []
    for i, c in enumerate(calls):
        tool, args = c["tool"], c["args"]
        named, payload = args.get("investigation_id"), args.get("payload")
        given = payload.get("investigation_id") if isinstance(payload, dict) else None
        if named is not None and given is not None and named != given:
            raise WorkflowFailure("INVESTIGATION_MISMATCH", f"call {i} ({tool}): investigation_id {named!r} and "
                                  f"payload.investigation_id {given!r} disagree", "Name one investigation per batch.",
                                  {"call": i})
        explicit = named if named is not None else given
        scope: Any = unknown if explicit is None and tool in LAUNCH_SCOPE else explicit
        if tool in LAUNCH_SCOPE:
            field, kind = LAUNCH_SCOPE[tool]
            target = _arg(args, field)
            launched: Any = unknown
            if isinstance(target, dict) and REF_KEY in target:
                parsed = parse_ref(str(target[REF_KEY]))
                launched = scopes[parsed[0]] if parsed else unknown
            elif isinstance(target, str) and kind == "trial" and client is not None:
                try:
                    record = client.query("get_record", {"kind": kind, "id": target}, project=project)
                except LabError:
                    record = None  # the launch itself is refused by the lab
                body = record.get("body") if isinstance(record, dict) else None
                launched = body.get("investigation_id") if isinstance(body, dict) else unknown
            if launched is not unknown and explicit is not None and launched != explicit:
                raise WorkflowFailure("INVESTIGATION_MISMATCH", f"call {i} ({tool}): {field} runs in investigation "
                                      f"{launched!r}, not {explicit!r}", "Name one investigation per batch.",
                                      {"call": i, "investigation_id": launched})
            if launched is not unknown:
                scope = launched
        scopes.append(scope)
    known = [(i, s) for i, s in enumerate(scopes) if s is not unknown]
    effective = investigation_id if investigation_id is not None else (known[0][1] if known else None)
    for i, s in known:
        if s != effective:
            crossing = sorted({*(d for d in calls[i].get("depends_on") or [] if scopes[d] is not unknown and
                                 scopes[d] != s),
                               *(j for j, c in enumerate(calls) if i in (c.get("depends_on") or []) and
                                 scopes[j] is not unknown and scopes[j] != s)})
            what = f"; it is referenced across investigations with call(s) {crossing}" if crossing else ""
            raise WorkflowFailure("INVESTIGATION_MISMATCH",
                                  f"call {i} ({calls[i]['tool']}) runs in {_scope_text(s)}, but this batch runs in "
                                  f"{_scope_text(effective)}{what}; a batch has one investigation",
                                  "Set run_batch investigation_id and leave it out of the calls, or split the batch.",
                                  {"call": i, "investigation_id": s, "batch_investigation_id": effective,
                                   **({"references": crossing} if crossing else {})})
    return effective


def _scope_text(scope: str | None) -> str:
    return f"investigation {scope!r}" if scope is not None else "no investigation"


def iter_has_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return REF_KEY in value or any(iter_has_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(iter_has_ref(v) for v in value)
    return False


def run_batch(client: Any, project: str, key: str, args: dict[str, Any], *, poll: float = POLL_SECONDS) -> dict[str, Any]:
    """Run a batch's calls in order. Returns when every call has been attempted, or ``pending`` when a call still waits
    for a job after ``wait_seconds``: calling again with the same key and calls replays what was committed and goes on."""
    from eidos.mcp.tools import BY_NAME, build_call  # noqa: PLC0415

    calls = validate_batch(args.get("calls"), args.get("investigation_id"), client, project)
    inv = batch_investigation(calls, args.get("investigation_id"), client, project)
    keyed = [{**c, "index": i, "command_key": f"{key}:{i}"} for i, c in enumerate(calls)]
    digest = hashlib.sha256(json.dumps(calls, sort_keys=True, default=str).encode()).hexdigest()
    # Prospective record of the whole batch, references included, before any effect (as an accepted decision is). The
    # key binds the batch: the same key with other calls is refused here, before anything runs.
    plan = {"command_key": key, "calls_sha256": digest, "calls": [{"tool": c["tool"], "depends_on": c.get("depends_on", [])}
                                                                  for c in keyed]}
    client.command("record_note", {"kind": "mcp-batch", "investigation_id": inv, "status": "recorded",
                                   "text": json.dumps(plan, sort_keys=True)[:4000]},
                   project=project, key=f"{key}:batch")
    runner = BatchRunner(lambda name, params: client.query(name, params, project=project), max_concurrent=1,
                         slice_ends=time.monotonic() + _bounded_wait(args), poll=poll, job_tools=BATCH_JOB_TOOLS,
                         plan_tools=PLAN_TOOLS, compute_tools=(*BATCH_JOB_TOOLS, *PLAN_TOOLS),
                         deadline_message="the batch ran out of time waiting for its jobs")
    receipts: list[dict] = []
    status, next_call = "done", None
    for call in keyed:
        gate = runner.gate(call, receipts)
        if gate == "pending":
            status, next_call = "pending", call["index"]
            break
        if isinstance(gate, dict):
            receipts.append(gate)
            continue
        resolved = runner.resolve(call, receipts)
        if isinstance(resolved, dict):
            receipts.append(resolved)
            continue
        call_args, provenance = resolved
        if call["tool"] in PLAN_TOOLS:
            call_args["max_concurrent_jobs"] = runner.max_concurrent  # the batch's allowance (ADR 0022 addendum R11)
        try:
            if call["tool"] == "upload_source":
                r = {**upload_source(client, project, call["command_key"], call_args), "tool": "upload_source",
                     "index": call["index"]}
            else:
                spec = BY_NAME[call["tool"]]
                payload, _, _, _ = build_call(spec, call_args)
                extra = {"normalized_arguments": call["normalized_arguments"]} if call.get("normalized_arguments") else {}
                rec = client.command(spec.target, payload, project=project, key=call["command_key"], **extra)
                r = {"tool": call["tool"], "index": call["index"], "ok": True, "replayed": rec.get("replayed"),
                     "result": rec.get("result"), **extra}
        except LabError as exc:
            r = _failed(call["tool"], call["index"], exc)
        except WorkflowFailure as exc:
            r = {"tool": call["tool"], "index": call["index"], "ok": False, "code": exc.code,
                 "error": {"code": exc.code, "message": exc.message}}
        receipts.append({**r, **provenance})
        runner.record(receipts[-1])
    out: dict[str, Any] = {"status": status, "batch": key, "calls": [_compact_receipt(r) for r in receipts]}
    jobs = [job_summary(runner.job(r)) for r in runner.launched if r["tool"] in BATCH_JOB_TOOLS]
    if jobs:
        out["jobs"] = jobs
    plans = [r["result"]["plan_id"] for r in runner.launched if r["tool"] in PLAN_TOOLS]
    if plans:
        out["plans"] = plans
    if status == "pending":
        out["next_call"] = next_call
        out["next"] = "run_batch again with the same command_key and calls: committed calls replay, the rest continue."
    elif any(j["status"] not in TERMINAL_JOB_STATUSES for j in jobs) or plans:
        out["next"] = "wait_status with these job_ids / plan_id."
    return out


# -- wait_status -----------------------------------------------------------------------------------------------------

def wait_status(client: Any, project: str, args: dict[str, Any], *, poll: float = POLL_SECONDS) -> dict[str, Any]:
    """Wait (bounded) until the named jobs are terminal and the named plan has no active job, then summarize them with
    the claim and investigation, if named. Read-only."""
    job_ids = args.get("job_ids") or []
    if not isinstance(job_ids, list) or not all(isinstance(j, str) and j for j in job_ids):
        raise WorkflowFailure("INVALID_ARGUMENTS", "job_ids must be an array of job ids")
    if len(job_ids) > MAX_WAIT_JOBS:
        raise WorkflowFailure("INVALID_ARGUMENTS", f"at most {MAX_WAIT_JOBS} job_ids per call")
    plan_id, claim_id, inv = args.get("plan_id"), args.get("claim_id"), args.get("investigation_id")
    if not (job_ids or plan_id or claim_id or inv):
        raise WorkflowFailure("INVALID_ARGUMENTS", "name job_ids, plan_id, claim_id or investigation_id")

    def q(name: str, params: dict[str, Any]) -> Any:
        return client.query(name, params, project=project)

    ends, started = time.monotonic() + _bounded_wait(args), time.monotonic()
    while True:
        jobs = [q("get_job", {"job_id": j, "include_lock": False}) for j in job_ids]
        plan_jobs = q("list_jobs", {"plan_id": plan_id}) if plan_id else []
        active_plan = [j for j in plan_jobs if j.get("status") in ACTIVE_JOB_STATUSES]
        done = all(j.get("status") in TERMINAL_JOB_STATUSES for j in jobs) and not active_plan
        if done or time.monotonic() >= ends:
            break
        time.sleep(poll)
    out: dict[str, Any] = {"done": done, "waited_seconds": round(time.monotonic() - started, 1)}
    if job_ids:
        out["jobs"] = [job_summary(j) for j in jobs]
    if plan_id:
        counts: dict[str, int] = {}
        for j in plan_jobs:
            counts[str(j.get("status"))] = counts.get(str(j.get("status")), 0) + 1
        out["plan"] = {"plan_id": plan_id, "jobs": counts, "active": len(active_plan)}
    if claim_id:
        out["claim"] = claim_summary(q("get_claim", {"claim_id": claim_id}))
    if inv:
        i = q("get_investigation", {"investigation_id": inv})
        out["investigation"] = {k: i.get(k) for k in ("id", "status", "revision", "needs_input") if k in i}
    if not done:
        out["next"] = "wait_status again; work continues in the lab whether or not you wait."
    return out


def claim_summary(claim: dict) -> dict[str, Any]:
    state = claim.get("claim_state") or {}
    return {"claim_id": claim.get("id"), "revision": claim.get("revision"), "state": state.get("state"),
            "label": state.get("label"), "confirmed": bool(state.get("confirmed")),
            "assessments": [{k: a.get(k) for k in ("id", "outcome", "conformance", "health")}
                            for a in claim.get("assessments") or []]}


# -- submit_candidate ------------------------------------------------------------------------------------------------

def submit_candidate(client: Any, project: str, key: str, args: dict[str, Any]) -> dict[str, Any]:
    """Lock a candidate, file its 'improves' claim and, if asked, request its assessment, in one call. The state comes
    back from the lab's own derivation (ADR 0021): a filed claim without a confirming assessment is
    ``candidate-submitted``; this tool never asserts a state."""
    inv, job_id = args["investigation_id"], args["job_id"]

    def step(name: str, payload: dict[str, Any], suffix: str) -> dict[str, Any]:
        try:
            return client.command(name, payload, project=project, key=f"{key}:{suffix}")
        except LabError as exc:
            raise WorkflowFailure(exc.code or "ERROR", f"{name}: {exc.error.get('message', '')}",
                                  str(exc.error.get("next_action") or "Fix and retry with the same command_key; "
                                      "committed steps replay."),
                                  {"step": suffix, **(exc.error.get("details") or {})}) from exc

    assessment = args.get("assessment") or None  # {} asks for nothing, as an absent assessment does
    if assessment is not None and not isinstance(assessment, dict):
        raise WorkflowFailure("INVALID_ARGUMENTS", "assessment must be an object of assess_candidate arguments")
    job = client.query("get_job", {"job_id": job_id, "include_lock": False}, project=project)
    if job.get("status") != "succeeded":
        raise WorkflowFailure("CANDIDATE_NOT_READY", f"job {job_id} is {job.get('status')}; a candidate is a succeeded job",
                              "wait_status {job_ids: [job_id]} first.", {"job_id": job_id, "status": job.get("status")})
    # R15: the key binds the whole normalized submission before any step, so a retry that adds, drops or changes
    # anything (the assessment included) is COMMAND_KEY_CONFLICT here, with nothing replayed or started.
    request = {"investigation_id": inv, "job_id": job_id, "statement": args["statement"], "scope": args["scope"],
               "assessment": assessment}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
    step("record_note", {"investigation_id": inv, "kind": "mcp-submission", "status": "recorded",
                         "text": json.dumps({"command_key": key, "request_sha256": digest,
                                             "assessment": assessment is not None}, sort_keys=True)}, "submit")
    lock = step("record_note", {"investigation_id": inv, "kind": "candidate_lock", "text": json.dumps({"job_id": job_id}),
                                "evidence_refs": [job_id]}, "lock")
    claim = step("file_claim", {"statement": args["statement"], "kind": "improves", "scope": args["scope"],
                                "investigation_id": inv}, "claim")
    claim_id = claim["result"]["claim_id"]
    out: dict[str, Any] = {"candidate_job_id": job_id, "lock_note_id": lock["result"].get("note_id"), "claim_id": claim_id}
    if assessment is not None:
        assess = {**assessment, "claim_id": claim_id, "candidate": {"job_id": job_id}, "investigation_id": inv}
        a = step("assess_candidate", assess, "assess")["result"]
        out["assessment"] = {k: a.get(k) for k in ("status", "plan_id", "request_id", "exploratory") if a.get(k) is not None}
        if a.get("reasons"):
            out["assessment"]["reasons"] = refusal_reasons(a["reasons"])
    out["claim"] = claim_summary(client.query("get_claim", {"claim_id": claim_id}, project=project))
    if (out.get("assessment") or {}).get("plan_id"):
        out["next"] = "wait_status {plan_id, claim_id}: the assessment runs by itself."
    return out
