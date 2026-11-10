"""Tool/resource execution against the lab HTTP API. No MCP SDK types here, so it is unit-testable
with a fake client. All state lives in the lab; nothing is kept between calls except the
credential used to build a client."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

import httpx

from eidos.client import LabClient, LabError
from eidos.mcp.normalize import normalize, normalize_arguments
from eidos.mcp.tools import BY_NAME, Tool, build_call

ClientFactory = Callable[[str], Any]  # token -> object with command/query/whoami/close


def default_factory(api_url: str) -> ClientFactory:
    return lambda token: LabClient(api_url, token, client_name="eidos-mcp")


class ToolFailure(Exception):
    """A domain/validation failure to be reported as an ``isError`` tool result."""

    def __init__(self, code: str, message: str, next_action: str = "", details: dict | None = None,
                 status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.next_action = code, message, next_action
        self.details, self.status = details or {}, status

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message, "next_action": self.next_action,
                             "details": self.details}
        if self.status is not None:
            d["http_status"] = self.status
        return {"error": d}


def failure_from(exc: Exception) -> ToolFailure:
    """Map transport/domain exceptions to a machine-readable failure."""
    if isinstance(exc, ToolFailure):
        return exc
    if isinstance(exc, LabError):
        e = exc.error
        return ToolFailure(str(e.get("code") or "ERROR"), str(e.get("message") or ""),
                           str(e.get("next_action") or ""), e.get("details") or {}, exc.status)
    if isinstance(exc, httpx.HTTPError):
        return ToolFailure("LAB_UNAVAILABLE", f"cannot reach the lab API ({type(exc).__name__})",
                           "Check `lab status`; commands are idempotent, retry with the same command_key.")
    return ToolFailure("INTERNAL", f"{type(exc).__name__} in MCP adapter", "Report this; no ledger change was made "
                       "by the adapter itself.")


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict,
               "array": list}


def validate_args(tool: Tool, args: dict[str, Any]) -> None:
    schema = tool.input_schema()
    props = schema["properties"]
    problems = []
    for name in schema["required"]:
        if args.get(name) in (None, ""):
            problems.append(f"missing required argument '{name}'")
    for k, v in args.items():
        if k not in props:
            problems.append(f"unknown argument '{k}'")
            continue
        want = _JSON_TYPES[props[k]["type"]]
        if v is None:
            continue
        if (isinstance(v, bool) and props[k]["type"] in ("integer", "number")) or not isinstance(v, want):
            problems.append(f"'{k}' must be {props[k]['type']}")
    ck = args.get("command_key")
    if isinstance(ck, str) and len(ck) > 200:
        problems.append("'command_key' longer than 200 characters")
    if problems:
        hint = "Supply a caller-chosen command_key; reuse it to retry safely." if "command_key" in " ".join(problems) \
            else "Fix the arguments and call again."
        raise ToolFailure("INVALID_ARGUMENTS", "; ".join(problems), hint)


def call_tool(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool; returns a JSON object or raises ToolFailure."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolFailure("UNKNOWN_TOOL", f"no tool named {name!r}", "Call tools/list.")
    # ADR 0017: undo the two unambiguous client/provider re-encodings, only where a schema says what the value
    # must be; every change is recorded. The lab validates the result as strictly as any other call.
    args, changes = normalize_arguments(args, tool.input_schema())
    validate_args(tool, args)
    cfg, cfg_changes = _normalize_config(client, tool, args)
    if cfg_changes:
        args, changes = {**args, "config": cfg}, changes + cfg_changes
    payload, project, key, expected = build_call(tool, args)
    try:
        if tool.kind == "command":
            extra = {"normalized_arguments": changes} if changes else {}
            return client.command(tool.target, payload, project=project, key=key, expected_revision=expected,
                                  **extra)
        if name == "get_study_charter" and payload.pop("history", False):
            out = _wrap(client.query("record_history", {"kind": "charter", "id": payload["id"]}, project=project))
        elif tool.kind == "artifact_access":
            out = _artifact_access(client, project or "", payload)
        else:
            payload.pop("history", None)
            out = _wrap(client.query(tool.target, payload, project=project))
        return {**out, "normalized_arguments": changes} if changes else out
    except ToolFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a structured tool error
        raise failure_from(exc) from exc


def _maybe_reencoded(value: Any) -> bool:
    """Whether ``value`` contains a shape the normalizer could change (avoids a schema lookup otherwise)."""
    if isinstance(value, dict):
        if set(value) == {"item"} and isinstance(value["item"], list):
            return True
        return any(_maybe_reencoded(v) for v in value.values())
    if isinstance(value, list):
        return any(_maybe_reencoded(v) for v in value)
    return isinstance(value, str) and bool(value) and (value[0].isdigit() or value[0] == "-")


def _normalize_config(client: Any, tool: Tool, args: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Changes to ``config`` per the component's ``config_schema`` (read with the caller's own token). A component
    the caller cannot read, or one without a schema, gets no normalization: the lab reports the real error."""
    cfg, ref = args.get("config"), args.get("component_ref")
    if not (isinstance(cfg, dict) and isinstance(ref, str) and ref and _maybe_reencoded(cfg)
            and any(p.name == "config" for p in tool.params)):
        return cfg, []
    try:
        release = client.query("describe_component", {"component_ref": ref}, project=args.get("project"))
    except Exception:  # noqa: BLE001 - no schema, no normalization
        return cfg, []
    body = release.get("body") if isinstance(release, dict) and isinstance(release.get("body"), dict) else release
    manifest = body.get("manifest") if isinstance(body, dict) else None
    schema = manifest.get("config_schema") if isinstance(manifest, dict) else None
    if not isinstance(schema, dict):
        return cfg, []
    return normalize(cfg, schema, "config")


def _wrap(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {"result": result}


def _artifact_access(client: Any, project: str, p: dict[str, Any]) -> dict[str, Any]:
    rep = p.get("representation") or "metadata"
    if rep not in ("metadata", "preview", "content"):
        raise ToolFailure("INVALID_ARGUMENTS", "representation must be metadata, preview or content")
    art = client.query("get_artifact", {"artifact_id": p["artifact_id"]}, project=project)
    out: dict[str, Any] = {"artifact": art, "representation": rep, "purpose": p.get("purpose")}
    if rep == "preview":
        q: dict[str, Any] = {"artifact_id": p["artifact_id"]}
        if p.get("max_bytes"):
            q["max_bytes"] = p["max_bytes"]
        out["preview"] = client.query("artifact_preview", q, project=project)
    elif rep == "content":
        out["content_access"] = {
            "method": "GET", "path": f"/v1/projects/{project}/artifacts/{p['artifact_id']}/content",
            "auth": "Authorization: Bearer <your own lab token>", "digest": art.get("digest"),
            "size": art.get("size"), "note": "Bytes are streamed by the lab API, not through MCP."}
    return out


# -- resources ----------------------------------------------------------------------------------

_RES = re.compile(r"^lab://projects/([^/]+)/(investigations|runs|artifacts|claims|evidence-plans|assessments|charters)"
                  r"/([^/]+)(?:/(manifest))?$")

RESOURCE_TEMPLATES = [
    ("investigation", "lab://projects/{project}/investigations/{investigation}",
     "Investigation: status, revision, turns, jobs, budgets."),
    ("run", "lab://projects/{project}/runs/{job}", "A job: status, lock, attempts, outputs, receipt."),
    ("artifact-manifest", "lab://projects/{project}/artifacts/{artifact}/manifest",
     "Artifact metadata (digest, size, media type, access class, lineage meta)."),
    ("claim", "lab://projects/{project}/claims/{claim}", "Claim with assessments, evidence health and support."),
    ("evidence-plan", "lab://projects/{project}/evidence-plans/{plan}", "Evidence plan record."),
    ("assessment", "lab://projects/{project}/assessments/{assessment}",
     "Assessment outcome, conformance and evidence health."),
    ("charter", "lab://projects/{project}/charters/{charter}", "Study charter (latest revision)."),
]


class BadUri(Exception):
    pass


def read_resource(client: Any, uri: str) -> dict[str, Any]:
    m = _RES.match(uri)
    if not m:
        raise BadUri(f"unrecognized resource URI {uri!r}")
    project, kind, rid, tail = m.group(1), m.group(2), unquote(m.group(3)), m.group(4)
    if (kind == "artifacts") != (tail == "manifest"):
        raise BadUri("artifact resources are .../artifacts/{id}/manifest; no other kind takes a suffix")
    try:
        if kind == "investigations":
            return client.query("get_investigation", {"investigation_id": rid}, project=project)
        if kind == "runs":
            return client.query("get_job", {"job_id": rid}, project=project)
        if kind == "artifacts":
            return client.query("get_artifact", {"artifact_id": rid}, project=project)
        if kind == "claims":
            return client.query("get_claim", {"claim_id": rid}, project=project)
        if kind == "assessments":
            return client.query("get_assessment", {"assessment_id": rid}, project=project)
        rec_kind = "evidence_plan" if kind == "evidence-plans" else "charter"
        return client.query("get_record", {"kind": rec_kind, "id": rid}, project=project)
    except Exception as exc:  # noqa: BLE001
        raise failure_from(exc) from exc
