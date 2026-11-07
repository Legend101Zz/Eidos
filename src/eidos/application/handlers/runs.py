"""Resolve → admit → dispatch: component runs, trials, checkpoint branches and model export."""

from __future__ import annotations

import uuid
from typing import Any

from eidos.application.handlers.components import catalog, env_for
from eidos.application.handlers.investigations import check_exploration_allowed, inv_scope
from eidos.application.handlers.projects import get_profile
from eidos.application.service import Ctx, command
from eidos.domain import branching
from eidos.domain import resolver as res
from eidos.domain.errors import Invalid, NotFound
from eidos.domain.isolation import PROFILES

DEFAULT_WALL_SECONDS = 900
PURPOSES = ("reproduction", "measurement-development", "open-search", "hypothesis-test", "confirmation",
            "qualification", "build", "assessment", "export-check")


def admit_job(ctx: Ctx, *, kind: str, lock: dict, investigation_id: str | None, purpose: str, wall_seconds: int,
              trial_id: str | None = None, job_id: str | None = None, charter_check: bool = True,
              scope: str | None = None) -> str:
    """One transaction: budget reservation, job row, trial link, event and outbox dispatch."""
    if purpose not in PURPOSES:
        raise Invalid(f"purpose must be one of {PURPOSES}")
    if charter_check:
        check_exploration_allowed(ctx, investigation_id)
    jid = job_id or f"job_{uuid.uuid4().hex[:16]}"
    lock = {**lock, "limits": {"wall_seconds": wall_seconds, "max_output_bytes": int(lock.get("max_output_bytes", 512 << 20)),
                               **(lock.get("limits") or {})}, "job_id": jid, "project_id": ctx.project_id}
    from eidos_wire import canonical_json, sha256_bytes
    digest = "sha256:" + sha256_bytes(canonical_json(lock).encode())
    budget_scope = scope or (inv_scope(investigation_id) if investigation_id else f"project:{ctx.project_id}")
    ctx.tx.reserve(budget_scope, "compute_seconds", float(wall_seconds), jid)
    ctx.tx.reserve(budget_scope, "trials", 1.0, jid)
    lock["budget_scope"] = budget_scope
    ctx.tx.insert_job({"id": jid, "project_id": ctx.project_id, "investigation_id": investigation_id, "kind": kind,
                       "trial_id": trial_id, "purpose": purpose, "status": "admitted", "lock": lock,
                       "lock_digest": digest, "created_by": ctx.principal.id})
    for port, inp in (lock.get("inputs") or {}).items():
        ctx.tx.add_lineage(ctx.project_id, inp["artifact_id"], jid, f"input:{port}")
    seq = ctx.event("job.admitted", jid, {"kind": kind, "component": lock.get("component", {}).get("ref"),
                                          "purpose": purpose, "lock_digest": digest},
                    investigation_id=investigation_id)
    ctx.tx.enqueue("job.dispatch", f"dispatch:{jid}", {"job_id": jid, "project_id": ctx.project_id}, seq)
    return jid


def _inputs(ctx: Ctx, ids: dict[str, str]) -> dict[str, dict[str, Any]]:
    out = {}
    for port, aid in (ids or {}).items():
        art = ctx.tx.get_artifact(aid)
        if not art or art["project_id"] != ctx.project_id:
            raise NotFound(f"artifact {aid} not found")
        out[port] = {"artifact_id": art["id"], "digest": art["digest"], "schema": art["schema"], "size": art["size"],
                     "media_type": art["media_type"], "role": art["role"], "meta": art["meta"]}
    return out


def resolve_lock(ctx: Ctx, p: dict, *, extra_config: dict | None = None) -> dict:
    cat = catalog(ctx, include_inactive=True)
    target = next((r for r in cat if r["ref"] == p["component_ref"]), None)
    if target is None:
        raise NotFound(f"component {p['component_ref']} not found")
    env_name = p.get("environment") or target["environment"]["name"]
    env = env_for(ctx, env_name)
    inputs = _inputs(ctx, p.get("inputs") or {})
    prof = get_profile(ctx)
    adapters = [r for r in cat if r["status"] == "active" and "adapter" in r["manifest"]["implements"]]
    lock = res.resolve_invocation(
        target=target, catalog=[r for r in cat], profile={**prof.body, "revision": prof.revision},
        overrides=p.get("overrides") or {}, inputs=inputs, config={**(p.get("config") or {}), **(extra_config or {})},
        grants=set(ctx.principal.scopes) | set(p.get("grants") or []), installed=env.get("installed") or {},
        adapters=adapters)
    rel = ctx.tx.require("component_release", target["ref"])
    lock["component"]["bundle"] = {"artifact_id": rel.body["bundle_artifact_id"], "digest": rel.body["bundle_digest"]}
    lock["inputs"] = {k: {**lock["inputs"][k], "media_type": v["media_type"], "size": v["size"], "role": v["role"]}
                      for k, v in inputs.items()}
    lock["environment"] = env
    profile_name = p.get("isolation_profile") or prof.body.get("policy", {}).get("default_isolation_profile", "dev:same-user")
    if profile_name not in PROFILES:
        raise Invalid(f"unknown isolation profile {profile_name!r}", details={"available": sorted(PROFILES)})
    lock["isolation_profile"] = profile_name
    lock["seed"] = p.get("seed")
    return lock


@command("run_component", "invoke")
def run_component(ctx: Ctx, p: dict) -> dict:
    """Resolve, admit and dispatch one component invocation (same path as a trial)."""
    lock = resolve_lock(ctx, p)
    trial_id = f"trial_{uuid.uuid4().hex[:16]}"
    _trial_record(ctx, trial_id, p, lock, status="admitted")
    jid = admit_job(ctx, kind=lock["component"]["kind"], lock=lock, investigation_id=p.get("investigation_id"),
                    purpose=p.get("purpose", "open-search"), wall_seconds=int(p.get("wall_seconds", DEFAULT_WALL_SECONDS)),
                    trial_id=trial_id, job_id=trial_id.replace("trial_", "job_"))
    return {"job_id": jid, "trial_id": trial_id, "lock_digest": lock["digest"], "component": lock["component"]["ref"]}


def _trial_record(ctx: Ctx, trial_id: str, p: dict, lock: dict, *, status: str, branch: dict | None = None) -> None:
    body = {"lock": lock, "purpose": p.get("purpose", "open-search"), "prediction": p.get("prediction"),
            "falsifier": p.get("falsifier"), "hypothesis": p.get("hypothesis"), "investigation_id": p.get("investigation_id"),
            "wall_seconds": int(p.get("wall_seconds", DEFAULT_WALL_SECONDS)), "branch": branch,
            "estimated_cost": {"compute_seconds": int(p.get("wall_seconds", DEFAULT_WALL_SECONDS)), "trials": 1}}
    ctx.tx.put_record("trial", trial_id, ctx.project_id, body, created_by=ctx.principal.id, status=status,
                      parent_id=(branch or {}).get("parent", {}).get("trial_id"))


@command("plan_trial", "plan")
def plan_trial(ctx: Ctx, p: dict) -> dict:
    """Resolve an immutable plan with estimated cost; nothing runs until launch_trial."""
    lock = resolve_lock(ctx, p)
    trial_id = f"trial_{uuid.uuid4().hex[:16]}"
    _trial_record(ctx, trial_id, p, lock, status="planned")
    return {"trial_id": trial_id, "lock_digest": lock["digest"], "lock": lock,
            "estimated_cost": {"compute_seconds": int(p.get("wall_seconds", DEFAULT_WALL_SECONDS)), "trials": 1}}


@command("launch_trial", "spend_compute")
def launch_trial(ctx: Ctx, p: dict) -> dict:
    tr = ctx.tx.require("trial", p["trial_id"], lock=True)
    if tr.status != "planned":
        raise Invalid(f"trial {tr.id} is {tr.status}")
    lock = tr.body["lock"]
    for _cap, port in (lock.get("ports") or {}).items():
        if port.get("ref"):
            rel = ctx.tx.require("component_release", port["ref"])
            if rel.status == "withdrawn":
                raise Invalid(f"bound release {rel.id} was withdrawn after planning; re-plan the trial")
    ctx.tx.set_status("trial", tr.id, "admitted")
    kind = "branch" if tr.body.get("branch") else lock["component"]["kind"]
    jid = admit_job(ctx, kind=kind, lock=lock, investigation_id=tr.body.get("investigation_id"),
                    purpose=tr.body["purpose"], wall_seconds=tr.body["wall_seconds"], trial_id=tr.id,
                    job_id=tr.id.replace("trial_", "job_"))
    if tr.body.get("branch"):
        ctx.tx.set_status("branch", f"branch:{tr.id}", "launched", labels={"job_id": jid})
    return {"job_id": jid, "trial_id": tr.id, "lock_digest": lock["digest"]}


@command("cancel_run", "control")
def cancel_run(ctx: Ctx, p: dict) -> dict:
    """Request cancellation of an admitted experiment. The receipt means the request is committed;
    ``cancel_requested`` persists until the backend confirms termination."""
    job = ctx.tx.get_job(p["job_id"], lock=True)
    if not job or job["project_id"] != ctx.project_id:
        raise NotFound(f"job {p['job_id']} not found")
    if job["status"] in ("succeeded", "failed", "cancelled", "pruned"):
        return {"job_id": job["id"], "status": job["status"], "already_terminal": True}
    if job["status"] == "admitted" and not job["current_attempt"]:
        ctx.tx.update_job(job["id"], cancel_requested=True)
    else:
        ctx.tx.update_job(job["id"], cancel_requested=True, status="cancel-requested")
    ctx.tx.enqueue("job.cancel", f"cancel:{job['id']}", {"job_id": job["id"], "policy": p.get("policy", "cancel")})
    ctx.event("job.cancel_requested", job["id"], {"by": ctx.principal.id}, investigation_id=job["investigation_id"])
    return {"job_id": job["id"], "status": "cancel-requested"}


def _parent_manifest(ctx: Ctx, artifact_id: str) -> tuple[dict, dict]:
    art = ctx.tx.get_artifact(artifact_id)
    if not art or art["project_id"] != ctx.project_id:
        raise NotFound(f"checkpoint {artifact_id} not found")
    ck = (art["meta"] or {}).get("checkpoint")
    if art["role"] != "checkpoint" or not ck:
        raise Invalid(f"artifact {artifact_id} is not a committed checkpoint manifest")
    job = ctx.tx.get_job(art["producer_job"]) if art["producer_job"] else None
    parent = {"checkpoint_id": art["id"], "trial_id": job["trial_id"] if job else None, "step": ck.get("step"),
              "complete": bool(ck.get("complete")), "state_entries": ck.get("state_entries", []),
              "trajectory_id": ck.get("trajectory_id") or (job["trial_id"] if job else art["id"]),
              "cumulative_cost": ck.get("cumulative_cost") or {"steps": ck.get("step")},
              "restore_class": ck.get("restore_class", "exact"), "builder": ck.get("builder")}
    return art, parent


@command("plan_branch", "plan")
def plan_branch(ctx: Ctx, p: dict) -> dict:
    """Plan a child trial from an immutable parent checkpoint with explicit state migration,
    stream policy, restore class and inherited/additional cost."""
    art, parent = _parent_manifest(ctx, p["parent_checkpoint_id"])
    intent = branching.validate_branch(parent, p)
    inputs = dict(p.get("inputs") or {})
    inputs.setdefault("parent_checkpoint", art["id"])
    lock = resolve_lock(ctx, {**p, "inputs": inputs}, extra_config={"branch": {k: intent[k] for k in (
        "migration", "stream_policy", "seed", "intervention", "restore_class", "required_fixtures")}})
    trial_id = f"trial_{uuid.uuid4().hex[:16]}"
    _trial_record(ctx, trial_id, p, lock, status="planned", branch=intent)
    ctx.tx.put_record("branch", f"branch:{trial_id}", ctx.project_id, {**intent, "trial_id": trial_id},
                      created_by=ctx.principal.id, status="planned", parent_id=art["id"])
    ctx.tx.add_lineage(ctx.project_id, art["id"], trial_id, "branched-from")
    return {"trial_id": trial_id, "branch_id": f"branch:{trial_id}", "restore_class": intent["restore_class"],
            "independence_unit": intent["independence_unit"], "cost": intent["cost"], "lock_digest": lock["digest"]}


@command("launch_branch", "spend_compute")
def launch_branch(ctx: Ctx, p: dict) -> dict:
    return launch_trial(ctx, p)


@command("export_model", "export")
def export_model(ctx: Ctx, p: dict) -> dict:
    """Record a loadable-model export. Requires a passing fresh-process behavioral check of the
    same checkpoint bytes with the named builder (strict loading alone is not acceptance)."""
    art = ctx.tx.get_artifact(p["checkpoint_artifact_id"])
    if not art or art["project_id"] != ctx.project_id:
        raise NotFound("checkpoint not found")
    job = ctx.tx.get_job(p["behavior_check_job_id"])
    if not job or job["status"] != "succeeded":
        raise Invalid("behavior_check_job_id must name a succeeded behavioral check job")
    receipt = (job.get("terminal") or {}).get("receipt", {})
    checked = [i for i in job["lock"].get("inputs", {}).values() if i["digest"] == art["digest"]]
    if not checked:
        raise Invalid("the behavioral check did not evaluate this checkpoint's bytes")
    if receipt.get("behavior_match") is not True:
        raise Invalid("fresh-process behavioral check did not match; export refused",
                      details={"receipt": {k: receipt.get(k) for k in ("behavior_match", "max_abs_diff", "builder")}})
    eid = p.get("export_id") or f"export_{uuid.uuid4().hex[:12]}"
    env = env_for(ctx, p.get("environment", "default"))
    body = {"checkpoint": {"artifact_id": art["id"], "digest": art["digest"]},
            "builder": receipt.get("builder") or p.get("builder"), "adapter": p.get("adapter") or env.get("adapter"),
            "preprocessing": p.get("preprocessing"), "environment": {"id": env["id"], "lock_digest": env.get("lock_digest"),
                                                                       "python_version": env.get("python_version"),
                                                                       "installed": env.get("installed")},
            "behavior_check": {"job_id": job["id"], "receipt": receipt}, "recipe": p.get("recipe"),
            "evaluation": p.get("evaluation"), "claims": p.get("claims", []),
            "investigation_id": p.get("investigation_id")}
    ctx.tx.put_record("export", eid, ctx.project_id, body, created_by=ctx.principal.id, status="exported")
    ctx.tx.add_lineage(ctx.project_id, art["id"], eid, "exported-as")
    ctx.event("model.exported", eid, {"checkpoint": art["id"]}, investigation_id=p.get("investigation_id"))
    return {"export_id": eid, "checkpoint": art["id"]}
