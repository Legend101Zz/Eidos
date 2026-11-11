"""FastAPI transport for the command service. Thin: authentication, envelope validation and
error mapping only; every rule lives in the application/domain layers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from eidos.application.ports import Principal
from eidos.application.service import COMMANDS, QUERIES, CommandService
from eidos.domain.errors import DomainError, Forbidden, Invalid, NotFound, Unauthenticated

log = logging.getLogger("eidos.api")
API_VERSION = "eidos-api/1"
MAX_BODY = 1 << 20  # command/query envelopes; large content goes through artifact upload


class CommandRequest(BaseModel):
    project_id: str | None = None
    command_key: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    delegation: dict[str, Any] = Field(default_factory=dict)
    # Argument shapes the MCP adapter normalized before sending (ADR 0017); recorded in the receipt, nothing else.
    normalized_arguments: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    # F5c: the accepted decision an effect belongs to ({decision_id, execution}); checked at the effect's admission.
    admission: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    project_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def principal_from_row(row: dict) -> Principal:
    return Principal(row["id"], row["kind"], frozenset(row["scopes"] or []), frozenset(row["project_ids"] or []),
                     row["attributes"] or {}, row["display"] or "")


def create_app(service: CommandService, *, relay: Any = None, maintenance_flag: Any = None) -> FastAPI:
    app = FastAPI(title="Eidos lab API", version=API_VERSION)
    cache: dict[str, tuple[float, Principal]] = {}

    def authenticate(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise Unauthenticated("missing bearer token")
        th = token_hash(auth[7:].strip())
        hit = cache.get(th)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        with service.ledger.transaction() as tx:
            row = tx.principal_by_token_hash(th)
        if not row:
            raise Unauthenticated("invalid, expired or revoked token")
        pr = principal_from_row(row)
        cache[th] = (time.monotonic() + 5.0, pr)
        return pr

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    from eidos.runtime.artifacts import DiskPressure, LocalArtifactStore, StoreError

    @app.exception_handler(StoreError)
    async def store_error(_: Request, exc: StoreError):
        code = "DISK_PRESSURE" if isinstance(exc, DiskPressure) else "STORE_ERROR"
        return JSONResponse(status_code=507 if code == "DISK_PRESSURE" else 422, content={"error": {
            "code": code, "message": str(exc), "next_action": "Free disk space or raise the reserve; committed evidence is unaffected."}})

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        writes = request.url.path.startswith("/v1/commands") or (
            request.method == "POST" and request.url.path.endswith("/artifacts"))
        if writes and maintenance_flag is not None and maintenance_flag.exists():
            return JSONResponse(status_code=503, content={"error": {"code": "MAINTENANCE", "message":
                                "lab is quiesced for backup/upgrade", "next_action": "retry shortly with the same command key"}})
        if request.url.path.startswith("/v1/commands") or request.url.path.startswith("/v1/queries"):
            cl = int(request.headers.get("content-length") or 0)
            if cl > MAX_BODY:
                return JSONResponse(status_code=413, content={"error": {"code": "PAYLOAD_TOO_LARGE",
                                    "message": f"envelope {cl} bytes > {MAX_BODY}", "next_action":
                                    "Upload large content as an artifact and pass its id."}})
        return await call_next(request)

    @app.get("/v1/health")
    async def health():
        return {"status": "ok", "api": API_VERSION, "relay": relay.status() if relay else None}

    @app.get("/v1/whoami")
    async def whoami(request: Request):
        pr = authenticate(request)
        return {"id": pr.id, "kind": pr.kind, "scopes": sorted(pr.scopes), "projects": sorted(pr.project_ids)}

    @app.get("/v1/catalog")
    async def catalog_of_operations(request: Request):
        authenticate(request)
        return {"commands": {n: {"scope": s.scope, "doc": s.doc} for n, s in sorted(COMMANDS.items())},
                "queries": {n: {"scope": s.scope, "doc": s.doc} for n, s in sorted(QUERIES.items())}}

    @app.post("/v1/commands/{name}")
    async def run_command(name: str, body: CommandRequest, request: Request):
        pr = authenticate(request)
        client = {"client": request.headers.get("x-eidos-client", "unknown"),
                  "delegation": body.delegation}
        return await asyncio.to_thread(service.execute, pr, name, project_id=body.project_id,
                                       command_key=body.command_key, payload=body.payload,
                                       expected_revision=body.expected_revision, delegation=client,
                                       normalized_arguments=body.normalized_arguments,
                                       admission=body.admission)

    @app.post("/v1/queries/{name}")
    async def run_query(name: str, body: QueryRequest, request: Request):
        pr = authenticate(request)
        return {"result": await asyncio.to_thread(service.query, pr, name, project_id=body.project_id,
                                                  params=body.params)}

    @app.post("/v1/projects/{project_id}/artifacts")
    async def upload(project_id: str, request: Request):
        """Stream bytes into the store, then publish through the normal command path."""
        pr = authenticate(request)
        if not pr.has("artifact:write"):
            raise Forbidden("artifact:write grant required")
        if not pr.can_access(project_id):
            raise NotFound(f"project {project_id} not found")
        key = request.headers.get("x-eidos-command-key")
        if not key:
            raise Invalid("X-Eidos-Command-Key header is required")
        import os
        import tempfile
        # The API process is always wired to the local store (it owns the staging directory).
        store = cast(LocalArtifactStore, service.store)
        fd, tmp = tempfile.mkstemp(dir=store.staging)
        h = hashlib.sha256()
        size = 0
        limit = int(service.settings.get("max_upload_bytes", 2 << 30))
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    out.close()
                    os.unlink(tmp)
                    raise Invalid(f"upload exceeds {limit} bytes")
                h.update(chunk)
                out.write(chunk)
        declared = request.headers.get("x-eidos-sha256")
        if declared and declared != h.hexdigest():
            os.unlink(tmp)
            raise Invalid("uploaded bytes do not match X-Eidos-Sha256")
        digest, size = await asyncio.to_thread(store.put_file, tmp)
        os.unlink(tmp)
        payload = {"digest": digest, "size": size,
                   "media_type": request.headers.get("content-type", "application/octet-stream"),
                   "schema": request.headers.get("x-eidos-schema"), "role": request.headers.get("x-eidos-role", "input"),
                   "access_class": request.headers.get("x-eidos-access-class", "project"),
                   "name": request.headers.get("x-eidos-name"), "origin": "upload"}
        if request.headers.get("x-eidos-investigation"):
            payload["investigation_id"] = request.headers["x-eidos-investigation"]
        admission = None
        if request.headers.get("x-eidos-admission"):  # F5c: an effect of an accepted decision (author_source)
            try:
                admission = json.loads(request.headers["x-eidos-admission"])
            except ValueError as exc:
                raise Invalid("X-Eidos-Admission must be a JSON object") from exc
            if not isinstance(admission, dict):
                raise Invalid("X-Eidos-Admission must be a JSON object")
        return await asyncio.to_thread(service.execute, pr, "register_artifact", project_id=project_id,
                                       command_key=key, payload=payload, admission=admission)

    @app.get("/v1/projects/{project_id}/artifacts/{artifact_id}/content")
    async def download(project_id: str, artifact_id: str, request: Request):
        pr = authenticate(request)
        art = await asyncio.to_thread(service.query, pr, "get_artifact", project_id=project_id,
                                      params={"artifact_id": artifact_id})
        if not service.store.verify(art["digest"], art["size"]):
            raise NotFound("artifact bytes unavailable or failed verification")
        return FileResponse(service.store.path_for(art["digest"]), media_type=art["media_type"])

    return app
