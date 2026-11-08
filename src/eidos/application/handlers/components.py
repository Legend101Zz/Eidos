"""Author → validate → publish → bind → retire / withdraw (design §"Author, install, replace, remove")."""

from __future__ import annotations

import json

from eidos.application.handlers.projects import get_profile, new_profile_revision
from eidos.application.service import Ctx, command
from eidos.domain import resolver as res
from eidos.domain import support
from eidos.domain.errors import Invalid, NotFound
from eidos_wire import ManifestError, canonical_json, parse_manifest, sha256_bytes

BUNDLE_SCHEMA = "source-bundle/1"
MANIFEST_NAMES = ("eidos-component.toml", "eidos-component.json")


def read_bundle(ctx: Ctx, artifact_id: str) -> tuple[dict, dict]:
    art = ctx.tx.get_artifact(artifact_id)
    if not art or art["project_id"] != ctx.project_id:
        raise NotFound(f"artifact {artifact_id} not found")
    if art["schema"] != BUNDLE_SCHEMA:
        raise Invalid(f"artifact {artifact_id} is not a {BUNDLE_SCHEMA}")
    if not ctx.store.verify(art["digest"], art["size"]):
        raise Invalid("bundle bytes failed verification")
    with open(ctx.store.path_for(art["digest"]), "rb") as fh:
        bundle = json.load(fh)
    return art, bundle


def manifest_from_bundle(bundle: dict) -> dict:
    files = bundle.get("files") or {}
    for name in MANIFEST_NAMES:
        if name in files:
            raw = files[name]
            if isinstance(raw, dict):  # an authored bundle may carry the manifest as an object
                return raw
            if not isinstance(raw, str):
                raise Invalid(f"{name} must be a JSON/TOML text (or an object)", details={"got": type(raw).__name__})
            try:
                if name.endswith(".json"):
                    return json.loads(raw)
                import tomllib
                return tomllib.loads(raw)
            except ValueError as exc:  # json.JSONDecodeError and tomllib.TOMLDecodeError are ValueErrors
                raise Invalid(f"{name} is not valid {'JSON' if name.endswith('.json') else 'TOML'}: {exc}",
                              details={"manifest_file": name}) from None
    raise Invalid("bundle has no eidos-component.toml/json")


def parse_or_invalid(data: dict):
    try:
        return parse_manifest(data)
    except ManifestError as exc:
        raise Invalid("component manifest violates the contract", details={"problems": exc.problems}) from exc


def release_digest(manifest: dict, bundle_digest: str, env_lock: str | None) -> str:
    return "sha256:" + sha256_bytes(canonical_json({"manifest": manifest, "bundle": bundle_digest,
                                                    "environment_lock": env_lock}).encode())


def env_for(ctx: Ctx, name: str) -> dict:
    prof = get_profile(ctx)
    env = (prof.body.get("environments") or {}).get(name)
    if not env:
        raise Invalid(f"environment {name!r} is not registered in the project profile",
                      details={"available": sorted(prof.body.get("environments") or {})})
    rec = ctx.tx.require("environment", env["id"])
    return {"name": name, "id": rec.id, "revision": rec.revision, **rec.body}


@command("validate_component", "build")
def validate_component(ctx: Ctx, p: dict) -> dict:
    """Admit a build/validation job for an immutable source bundle in a project environment.
    Package installation and tests are code execution and run under the selected profile."""
    from eidos.application.handlers.runs import admit_job
    art, bundle = read_bundle(ctx, p["bundle_artifact_id"])
    man = parse_or_invalid(manifest_from_bundle(bundle))
    env = env_for(ctx, p.get("environment", "default"))
    conflicts = res.dependency_conflicts({man.ref: man.dependencies}, env.get("installed") or {})
    if conflicts:
        raise Invalid("component dependencies conflict with the project environment",
                      details={"conflicts": conflicts},
                      next_action="Use a separate environment; Eidos will not upgrade the project's packages.")
    lock = {"lock": "eidos-build-lock/1", "component": {"ref": "builtin:validator", "id": "eidos.validator",
            "entry": "eidos_worker.validate:run_component", "kind": "build", "implements": "component-validation/1",
            "effects": ["observe"], "digest": "builtin"},
            "target": {"ref": man.ref, "manifest": man.to_dict()},
            "inputs": {"bundle": {"artifact_id": art["id"], "digest": art["digest"], "schema": BUNDLE_SCHEMA}},
            "outputs": {"validation_report": "component-validation-report/1"},
            "environment": env, "config": {}, "ports": {}, "grants": [],
            "isolation_profile": None}
    from eidos.application.handlers.runs import choose_profile
    lock["isolation_choice"] = choose_profile(ctx, p.get("isolation_profile"), p.get("isolation_waiver"))
    lock["isolation_profile"] = lock["isolation_choice"]["profile"]
    job = admit_job(ctx, kind="build", lock=lock, investigation_id=p.get("investigation_id"), purpose="build",
                    wall_seconds=int(p.get("wall_seconds", 300)), charter_check=False)
    return {"job_id": job, "component_ref": man.ref}


@command("publish_component", "publish_component")
def publish_component(ctx: Ctx, p: dict) -> dict:
    """Publish a validated release. Requires the succeeded validation job for the same bundle."""
    art, bundle = read_bundle(ctx, p["bundle_artifact_id"])
    man = parse_or_invalid(manifest_from_bundle(bundle))
    job = ctx.tx.get_job(p["validation_job_id"])
    if not job or job["project_id"] != ctx.project_id or job["kind"] != "build" or job["lock"]["inputs"]["bundle"]["digest"] != art["digest"]:
        raise Invalid("validation_job_id does not refer to a validation of this bundle")
    if job["status"] != "succeeded":
        raise Invalid(f"validation job is {job['status']}; only a passing validation can be published")
    report = (job.get("terminal") or {}).get("receipt", {})
    if not report.get("passed", False):
        raise Invalid("validation did not pass", details={"report": report})
    env = job["lock"]["environment"]
    digest = release_digest(man.to_dict(), art["digest"], env.get("lock_digest"))
    existing = ctx.tx.get_record("component_release", man.ref)
    if existing:
        if existing.body["digest"] != digest:
            raise Invalid(f"release {man.ref} already exists with different content; bump the version",
                          details={"existing_digest": existing.body["digest"]})
        return {"component_ref": man.ref, "digest": digest, "already_published": True}
    body = {"manifest": man.to_dict(), "digest": digest, "bundle_artifact_id": art["id"], "bundle_digest": art["digest"],
            "environment": {"name": env["name"], "id": env["id"], "revision": env["revision"]},
            "build": {"job_id": job["id"], "report": report, "isolation": job["lock"].get("isolation_profile")},
            "qualification": "author-tests-only" if report.get("fixtures_run") else "interface-checked",
            "author": ctx.principal.id, "origin": p.get("origin", "human" if ctx.principal.kind == "human" else ctx.principal.id)}
    ctx.tx.put_record("component_release", man.ref, ctx.project_id, body, created_by=ctx.principal.id, status="active",
                      labels={"implements": man.implements, "kind": man.kind})
    ctx.tx.add_lineage(ctx.project_id, art["id"], man.ref, "source-of")
    ctx.event("component.published", man.ref, {"digest": digest, "implements": man.implements},
              investigation_id=p.get("investigation_id"))
    return {"component_ref": man.ref, "digest": digest}


def catalog(ctx: Ctx, *, include_inactive: bool = False) -> list[dict]:
    out = []
    for rec in ctx.tx.list_records(ctx.project_id, "component_release", limit=5000):
        if include_inactive or rec.status == "active":
            out.append({"ref": rec.id, "digest": rec.body["digest"], "status": rec.status, "manifest": rec.body["manifest"],
                        "environment": rec.body["environment"], "qualification": rec.body.get("qualification"),
                        "bundle_digest": rec.body.get("bundle_digest")})
    return out


@command("bind_component", "manage_profile")
def bind_component(ctx: Ctx, p: dict) -> dict:
    """Bind a release to a capability in a new profile revision. Affects future resolutions only;
    in-flight trials keep their pinned locks. A changed metric definition is a new metric identity
    and must be acknowledged; results are not comparable across it."""
    rel = ctx.tx.require("component_release", p["component_ref"])
    if rel.status != "active":
        raise Invalid(f"{rel.id} is {rel.status}; it cannot be bound")
    cap = p.get("capability") or rel.body["manifest"]["implements"]
    if res._major(cap) != res._major(rel.body["manifest"]["implements"]):
        raise Invalid(f"{rel.id} implements {rel.body['manifest']['implements']}, not {cap}")
    prof = get_profile(ctx, lock=True)
    body = json.loads(json.dumps(prof.body))
    current = list((body.get("bindings") or {}).get(cap, []))
    changed_metrics = []
    for old_ref in current:
        old = ctx.tx.get_record("component_release", old_ref)
        if not old:
            continue
        for metric in (rel.body["manifest"].get("metrics") or {}):
            a, b = res.metric_identity(old.body["manifest"], metric), res.metric_identity(rel.body["manifest"], metric)
            if a and b and a != b:
                changed_metrics.append({"metric": metric, "previous": old_ref, "previous_identity": a, "new_identity": b})
    if changed_metrics and not p.get("acknowledge_metric_change"):
        raise Invalid("replacement changes metric semantics; results would not be comparable",
                      details={"changed_metrics": changed_metrics},
                      next_action="Re-submit with acknowledge_metric_change=true to start a new comparison group.")
    mode = p.get("mode", "replace")
    body.setdefault("bindings", {})[cap] = [rel.id] if mode == "replace" else sorted(set(current + [rel.id]))
    if p.get("as_default"):
        body.setdefault("defaults", {})[cap] = rel.id
    pr = new_profile_revision(ctx, body, expected_revision=ctx.expected_revision, reason=f"bind {cap} -> {rel.id}")
    if changed_metrics:
        ctx.event("comparison_group.started", cap, {"changed_metrics": changed_metrics})
    return {"profile_revision": pr.revision, "capability": cap, "bound": body["bindings"][cap],
            "replaced": [r for r in current if r != rel.id], "metric_identity_changed": bool(changed_metrics)}


@command("retire_component", "manage_profile")
def retire_component(ctx: Ctx, p: dict) -> dict:
    """Hide a release from new bindings. Blobs needed by live jobs, exports and evidence are kept;
    historical results are not invalidated."""
    rel = ctx.tx.require("component_release", p["component_ref"], lock=True)
    if rel.status != "active":
        raise Invalid(f"{rel.id} is already {rel.status}")
    prof = get_profile(ctx, lock=True)
    body = json.loads(json.dumps(prof.body))
    removed = []
    for cap, refs in list((body.get("bindings") or {}).items()):
        if rel.id in refs:
            body["bindings"][cap] = [r for r in refs if r != rel.id]
            removed.append(cap)
    for cap, ref in list((body.get("defaults") or {}).items()):
        if ref == rel.id:
            del body["defaults"][cap]
    ctx.tx.set_status("component_release", rel.id, "retired", labels={"retired_by": ctx.principal.id})
    pr = new_profile_revision(ctx, body, expected_revision=None, reason=f"retire {rel.id}") if removed else None
    live = [j["id"] for j in ctx.tx.list_jobs(ctx.project_id, statuses=["admitted", "queued", "running"])
            if j["lock"].get("component", {}).get("ref") == rel.id]
    ctx.event("component.retired", rel.id, {"unbound_capabilities": removed, "live_jobs_unaffected": live})
    return {"component_ref": rel.id, "status": "retired", "unbound": removed, "live_jobs_keep_pinned_lock": live,
            "profile_revision": pr.revision if pr else None}


@command("withdraw_component", "withdraw")
def withdraw_component(ctx: Ctx, p: dict) -> dict:
    """Scientific or security withdrawal: name the defect and affected-condition predicate,
    traverse dependencies, mark affected assessments, suspend affected approvals and queue
    reassessment. History is appended to, never rewritten."""
    rel = ctx.tx.require("component_release", p["component_ref"], lock=True)
    if not p.get("defect"):
        raise Invalid("a withdrawal must name the defect")
    severity = p.get("severity", "scientific")
    withdrawal = {"release_ref": rel.id, "release_digest": rel.body["digest"], "defect": p["defect"],
                  "condition": p.get("condition") or {}, "severity": severity, "by": ctx.principal.id}
    runs = [{"ref": j["id"], "component_ref": j["lock"].get("component", {}).get("ref"), "config": j["lock"].get("config", {})}
            for j in ctx.tx.list_jobs(ctx.project_id, limit=100000)]
    edges = [(e["src"], e["dst"]) for e in ctx.tx.lineage(ctx.project_id)]
    affected = support.affected_refs(withdrawal, runs, edges)
    wid = f"withdrawal:{rel.id}:{len(ctx.tx.list_records(ctx.project_id, 'withdrawal')) + 1}"
    withdrawal["affected"] = sorted(affected)
    ctx.tx.put_record("withdrawal", wid, ctx.project_id, withdrawal, created_by=ctx.principal.id, status="active")
    ctx.tx.set_status("component_release", rel.id, "withdrawn", labels={"withdrawal": wid})
    marked, suspended, reassess, stopped = [], [], [], []
    for a in ctx.tx.list_records(ctx.project_id, "assessment", limit=100000):
        deps = set(a.body.get("dependencies", [])) | {a.body.get("assessor", {}).get("ref")}
        if a.id in affected or deps & affected:
            ctx.tx.set_status("assessment", a.id, a.status, labels={"health": "affected", "affected_by": wid})
            marked.append(a.id)
            rid = f"reassess:{a.id}:{wid}"
            ctx.tx.put_record("reassessment_request", rid, ctx.project_id,
                              {"assessment_id": a.id, "withdrawal": wid, "plan_id": a.body.get("plan_id"),
                               "estimated_cost": a.body.get("cost", {})},
                              created_by=ctx.principal.id, status="queued")
            reassess.append(rid)
    for ap in ctx.tx.list_records(ctx.project_id, "approval", status="active", limit=100000):
        if ap.body.get("release_ref") == rel.id or ap.body.get("suite_digest") in affected:
            ctx.tx.set_status("approval", ap.id, "suspended", labels={"suspended_by": wid})
            suspended.append(ap.id)
    if severity == "security":
        for j in ctx.tx.list_jobs(ctx.project_id, statuses=["admitted", "queued", "running"]):
            if j["id"] in affected:
                ctx.tx.update_job(j["id"], cancel_requested=True)
                ctx.tx.enqueue("job.cancel", f"cancel:{j['id']}", {"job_id": j["id"], "policy": "cancel"})
                stopped.append(j["id"])
    ctx.event("component.withdrawn", rel.id, {"withdrawal": wid, "affected_assessments": marked,
                                             "suspended_approvals": suspended, "stopped_jobs": stopped})
    return {"withdrawal_id": wid, "affected": sorted(affected), "affected_assessments": marked,
            "suspended_approvals": suspended, "reassessment_requests": reassess, "stopped_jobs": stopped}
