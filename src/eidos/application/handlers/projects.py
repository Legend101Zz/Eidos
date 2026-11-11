"""Projects, principals, environments and the project profile."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from eidos.application.scopes import scopes_for
from eidos.application.service import Ctx, command
from eidos.domain.errors import Invalid

_PID = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,62}$")


def profile_id(project_id: str) -> str:
    return f"profile:{project_id}"


def get_profile(ctx: Ctx, *, lock: bool = False):
    return ctx.tx.require("profile", profile_id(ctx.project_id), lock=lock)


def checked_concurrency(ctx: Ctx, wanted: Any) -> int:
    """ADR 0022: how many jobs may run at once for work that asked for ``wanted``. One, unless the request is explicit AND
    the project's policy (``policy.resources.max_concurrent_jobs``, an operator setting) allows that much."""
    if wanted is None:
        return 1
    if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted < 1:
        raise Invalid("max_concurrent_jobs must be an integer >= 1")
    if wanted == 1:
        return 1
    allowed = int(((get_profile(ctx).body.get("policy") or {}).get("resources") or {}).get("max_concurrent_jobs") or 1)
    if wanted > allowed:
        raise Invalid(f"max_concurrent_jobs is {wanted}, but this project's resource policy allows {allowed}: jobs queue "
                      "sequentially", details={"requested": wanted, "allowed": allowed})
    return wanted


def new_profile_revision(ctx: Ctx, body: dict[str, Any], *, expected_revision: int | None, reason: str):
    rec = ctx.tx.put_record("profile", profile_id(ctx.project_id), ctx.project_id, body, created_by=ctx.principal.id,
                            status="active", expected_revision=expected_revision)
    ctx.event("profile.revised", rec.id, {"revision": rec.revision, "reason": reason})
    return rec


@command("create_project", "admin", project_scoped=False)
def create_project(ctx: Ctx, p: dict) -> dict:
    """Create a project with an initial profile revision and project-level budgets."""
    pid = p.get("project_id", "")
    if not _PID.match(pid):
        raise Invalid("project_id must be lowercase letters, digits, '-' or '_' (2-63 chars)")
    if ctx.tx.get_project(pid):
        raise Invalid(f"project {pid} already exists")
    ctx.tx.insert_project(pid, p.get("name") or pid, ctx.principal.id)
    ctx.project_id = pid
    ctx.tx.project_id = pid
    policy = {"auto_freeze_approved_templates": bool(p.get("auto_freeze_approved_templates", False)),
              "data_egress": p.get("data_egress", "authorized-context-only"),
              "default_isolation_profile": "strongest-available"}
    ctx.tx.put_record("profile", profile_id(pid), pid,
                      {"bindings": {}, "defaults": {}, "environments": {}, "policy": policy},
                      created_by=ctx.principal.id, status="active")
    for dim, limit in (p.get("budgets") or {}).items():
        ctx.tx.ensure_budget(f"project:{pid}", pid, dim, float(limit))
    ctx.event("project.created", pid, {"name": p.get("name") or pid})
    return {"project_id": pid, "profile_revision": 1}


@command("create_principal", "admin", project_scoped=False)
def create_principal(ctx: Ctx, p: dict) -> dict:
    """Register a principal. The caller supplies only the SHA-256 of a token it generated, so no
    credential value is ever stored in the ledger or a command receipt."""
    roles = p.get("roles") or []
    token_hash = p.get("token_sha256")
    if not token_hash or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
        raise Invalid("token_sha256 (hex) is required")
    ttl = p.get("ttl_seconds")
    expires = datetime.now(UTC) + timedelta(seconds=int(ttl)) if ttl else None
    ctx.tx.upsert_principal(p["id"], p.get("kind", "human"), p.get("display", ""), p.get("project_ids", []),
                            scopes_for(roles) + list(p.get("extra_scopes", [])), token_hash,
                            {"roles": roles, **(p.get("attributes") or {})}, expires)
    return {"principal_id": p["id"], "roles": roles, "expires_at": expires.isoformat() if expires else None}


@command("revoke_principal", "admin", project_scoped=False)
def revoke_principal(ctx: Ctx, p: dict) -> dict:
    ctx.tx.revoke_principal(p["id"])
    return {"principal_id": p["id"], "revoked": True}


@command("register_environment", "manage_profile")
def register_environment(ctx: Ctx, p: dict) -> dict:
    """Register a project execution environment (interpreter + installed packages) and bind it
    under a name in a new profile revision. Eidos never changes the packages it records here."""
    name = p.get("name") or "default"
    for k in ("python_executable", "project_root", "python_version"):
        if not p.get(k):
            raise Invalid(f"{k} is required")
    lab_root = ctx.settings.get("lab_root")
    if lab_root:
        from pathlib import Path as _P
        root = _P(lab_root).resolve()
        for k in ("project_root", "python_executable"):
            target = _P(p[k]).resolve()
            if target == root or root in target.parents:
                raise Invalid(f"{k} must not lie inside the lab directory (it would expose lab data to jobs)")
    env_id = p.get("env_id") or f"env:{ctx.project_id}:{name}"
    body = {k: p.get(k) for k in ("python_executable", "project_root", "python_version", "installed",
                                  "lock_digest", "platform", "devices", "adapter", "isolation_profiles", "notes")}
    body["installed"] = {k.lower().replace("_", "-"): v for k, v in (p.get("installed") or {}).items()}
    rec = ctx.tx.put_record("environment", env_id, ctx.project_id, body, created_by=ctx.principal.id, status="active")
    prof = get_profile(ctx, lock=True)
    nb = dict(prof.body)
    nb["environments"] = {**nb.get("environments", {}), name: {"id": env_id, "revision": rec.revision}}
    pr = new_profile_revision(ctx, nb, expected_revision=None, reason=f"environment {name} registered")
    return {"env_id": env_id, "revision": rec.revision, "profile_revision": pr.revision}


@command("update_profile_policy", "manage_profile")
def update_profile_policy(ctx: Ctx, p: dict) -> dict:
    prof = get_profile(ctx, lock=True)
    nb = dict(prof.body)
    nb["policy"] = {**nb.get("policy", {}), **(p.get("policy") or {})}
    pr = new_profile_revision(ctx, nb, expected_revision=ctx.expected_revision, reason="policy changed")
    return {"profile_revision": pr.revision}


@command("register_artifact", "artifact:write")
def register_artifact(ctx: Ctx, p: dict) -> dict:
    """Publish uploaded/imported bytes (already verified in the store) as an external artifact."""
    if not ctx.store.verify(p["digest"], int(p["size"])):
        raise Invalid("uploaded bytes are missing or fail verification")
    access = p.get("access_class", "project")
    if access == "sealed-labels" and not ctx.principal.has("sealed:write"):
        raise Invalid("registering sealed labels requires the sealed:write grant")
    import uuid as _uuid
    # Artifact ids are minted by the lab; caller-chosen ids could pre-empt job output ids.
    aid = f"art_{_uuid.uuid4().hex[:16]}"
    ctx.tx.insert_artifact({"id": aid, "project_id": ctx.project_id, "digest": p["digest"], "size": int(p["size"]),
                            "media_type": p.get("media_type", "application/octet-stream"), "schema": p.get("schema"),
                            "role": p.get("role", "input"), "access_class": access,
                            "origin": p.get("origin", "imported"), "meta": p.get("meta") or {}})
    if access == "project":
        ctx.event("artifact.registered", aid, {"digest": p["digest"], "schema": p.get("schema"), "name": p.get("name")},
                  investigation_id=p.get("investigation_id"))
    else:  # restricted ids/digests are not broadcast in the shared event stream
        ctx.event("artifact.registered", None, {"access_class": access})
    return {"artifact_id": aid, "digest": p["digest"], "size": int(p["size"])}
