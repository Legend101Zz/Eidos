"""Research commands of the ``lab`` CLI. Every command is an authenticated API call; there is
no local authoritative bypass (this module cannot import persistence or orchestration)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import typer

from eidos.client import LabClient, LabError
from eidos.settings import LabConfig

app = typer.Typer(no_args_is_help=True, help="Eidos local research lab")
project_app = typer.Typer(no_args_is_help=True, help="Projects and environments")
component_app = typer.Typer(no_args_is_help=True, help="Author, validate, publish, bind, retire, withdraw components")
inv_app = typer.Typer(no_args_is_help=True, help="Investigations")
charter_app = typer.Typer(no_args_is_help=True, help="Study charters")
job_app = typer.Typer(no_args_is_help=True, help="Jobs and trials")
checkpoint_app = typer.Typer(no_args_is_help=True, help="Checkpoint branches")
artifact_app = typer.Typer(no_args_is_help=True, help="Artifacts")
app.add_typer(project_app, name="project")
app.add_typer(component_app, name="component")
app.add_typer(inv_app, name="investigate")
app.add_typer(charter_app, name="charter")
app.add_typer(job_app, name="job")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(artifact_app, name="artifact")


def client() -> LabClient:
    url = os.environ.get("EIDOS_API_URL")
    token = os.environ.get("EIDOS_TOKEN")
    if not (url and token):
        cfg = LabConfig.load()
        url = url or cfg.api_url
        token = token or cfg.token("admin")
    return LabClient(url, token, client_name="lab-cli")


def out(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def fail(exc: LabError) -> None:
    typer.echo(json.dumps({"error": exc.error}, indent=2), err=True)
    raise typer.Exit(2)


def _kv(pairs: list[str]) -> dict[str, str]:
    d = {}
    for p in pairs:
        k, _, v = p.partition("=")
        d[k] = v
    return d


def _json_arg(s: str | None) -> dict:
    if not s:
        return {}
    if s.startswith("@"):
        return json.loads(Path(s[1:]).read_text())
    return json.loads(s)


@app.command("cmd")
def cmd(name: str, payload: str = typer.Option("{}", help="JSON or @file"), project: str = typer.Option(None, "-p"),
        key: str = typer.Option(None, help="command key (reuse to retry safely)"), expected_revision: int = None):
    """Run any domain command (escape hatch; same authorization as named commands)."""
    try:
        out(client().command(name, _json_arg(payload), project=project, key=key, expected_revision=expected_revision))
    except LabError as exc:
        fail(exc)


@app.command("query")
def query(name: str, params: str = typer.Option("{}"), project: str = typer.Option(None, "-p")):
    """Run any authorized query."""
    try:
        out(client().query(name, _json_arg(params), project=project))
    except LabError as exc:
        fail(exc)


# -- projects / environments ----------------------------------------------------------------

@project_app.command("create")
def project_create(project: str, budget: list[str] = typer.Option([], help="dimension=limit"),
                   builtins: bool = typer.Option(True, help="install the built-in component catalog")):
    c = client()
    try:
        out(c.command("create_project", {"project_id": project, "budgets": {k: float(v) for k, v in _kv(budget).items()}},
                      key=f"create-project:{project}"))
        _register_lab_env(c, project)
        if builtins:
            installed = []
            for d in builtin_component_dirs(lab_env_only=True):
                r = install_component(c, project, d, env="lab")
                installed.append({"ref": r.get("component_ref"), "validation": r.get("validation", "passed")})
            out({"builtin_components": installed,
                 "note": "project-environment components (eidos.pytorch.*, eidos.project.command, scorers) are installed "
                         "with `lab component install <project> <dir> --env default` after `lab project register-env`"})
    except LabError as exc:
        fail(exc)


LAB_ENV_COMPONENTS = ("eidos.assessors.paired-ci", "eidos.qualification-runner", "eidos.templates.improves-under-budget",
                      "lab.pairs.by_group")


def builtin_component_dirs(*, lab_env_only: bool = False) -> list[Path]:
    """Built-in components shipped in the wheel (or the repository checkout)."""
    import eidos
    for root in (Path(eidos.__file__).parent / "_components", Path(eidos.__file__).parents[2] / "components"):
        if root.is_dir():
            dirs = sorted(p for p in root.iterdir() if (p / "eidos-component.json").exists())
            return [d for d in dirs if not lab_env_only or d.name in LAB_ENV_COMPONENTS]
    return []


@component_app.command("builtins")
def component_builtins():
    """List the directories of the built-in components shipped with this installation."""
    out([str(p) for p in builtin_component_dirs()])


def _register_lab_env(c: LabClient, project: str) -> None:
    c.command("register_environment", {"name": "lab", "python_executable": sys.executable, "project_root": str(Path.cwd()),
                                       "python_version": sys.version.split()[0], "installed": {},
                                       "notes": "the lab's own interpreter; runs lab-environment components"},
              project=project, key=f"lab-env:{project}:{sys.executable}")


INSPECT = r"""
import json, sys, platform
from importlib import metadata
inst = {}
for d in metadata.distributions():
    n = d.metadata.get("Name")
    if n: inst[n.lower().replace("_", "-")] = d.version
dev = {"cpu": True}
try:
    import torch
    dev["cuda"] = torch.cuda.is_available(); dev["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
except Exception:
    pass
print(json.dumps({"python_version": platform.python_version(), "installed": inst, "devices": dev,
                  "platform": platform.platform()}))
"""


@project_app.command("register-env")
def register_env(project: str, python: str = typer.Option(..., help="the project's existing interpreter"),
                 root: Path = typer.Option(..., help="project root (adapter importable from here)"),
                 name: str = "default", adapter: str = typer.Option(None, help="module:ATTR of the model adapter")):
    """Record an existing project environment without changing any of its packages."""
    r = subprocess.run(env=_job_env(), args=[python, "-c", INSPECT], capture_output=True, text=True, cwd=root)
    if r.returncode != 0:
        typer.echo(r.stderr, err=True)
        raise typer.Exit(2)
    info = json.loads(r.stdout)
    import hashlib
    lock = hashlib.sha256(json.dumps(info["installed"], sort_keys=True).encode()).hexdigest()
    try:
        out(client().command("register_environment", {"name": name, "python_executable": python,
                             "project_root": str(root.resolve()), "adapter": adapter, "lock_digest": "sha256:" + lock, **info},
                             project=project, key=f"env:{project}:{name}:{lock}"))
    except LabError as exc:
        fail(exc)


# -- artifacts --------------------------------------------------------------------------------

@artifact_app.command("upload")
def artifact_upload(project: str, path: Path, schema: str = None, media_type: str = "application/octet-stream",
                    role: str = "input", access_class: str = "project"):
    try:
        out(client().upload(project, path, schema=schema, media_type=media_type, role=role, access_class=access_class,
                            name=path.name))
    except LabError as exc:
        fail(exc)


@artifact_app.command("show")
def artifact_show(project: str, artifact_id: str, preview: bool = True):
    c = client()
    try:
        out(c.query("artifact_preview" if preview else "get_artifact", {"artifact_id": artifact_id}, project=project))
    except LabError as exc:
        fail(exc)


@artifact_app.command("download")
def artifact_download(project: str, artifact_id: str, dest: Path):
    try:
        client().download(project, artifact_id, dest)
        out({"saved": str(dest)})
    except LabError as exc:
        fail(exc)


# -- components -------------------------------------------------------------------------------

def bundle_dir(directory: Path) -> bytes:
    """Immutable source bundle: text files verbatim, binary files (e.g. fixture checkpoints) base64."""
    import base64
    files: dict[str, Any] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
            data = p.read_bytes()
            try:
                files[str(p.relative_to(directory))] = data.decode("utf-8")
            except UnicodeDecodeError:
                files[str(p.relative_to(directory))] = {"base64": base64.b64encode(data).decode()}
    return json.dumps({"files": files}, sort_keys=True).encode()


def wait_job(c: LabClient, project: str, job_id: str, timeout: float = 900) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = c.query("get_job", {"job_id": job_id, "include_lock": False}, project=project)
        if j["status"] in ("succeeded", "failed", "cancelled", "pruned"):
            return j
        time.sleep(1)
    raise TimeoutError(job_id)


def install_component(c: LabClient, project: str, directory: Path, env: str = "default", bind: bool = True) -> dict:
    up = c.upload(project, data=bundle_dir(directory), media_type="application/vnd.eidos.source-bundle+json",
                  schema="source-bundle/1", role="source", name=directory.name)
    aid = up["result"]["artifact_id"]
    v = c.command("validate_component", {"bundle_artifact_id": aid, "environment": env}, project=project,
                  key=f"validate:{aid}:{env}:{uuid.uuid4().hex[:8]}")["result"]
    job = wait_job(c, project, v["job_id"])
    if job["status"] != "succeeded":
        return {"component_ref": v["component_ref"], "validation": job["status"], "job_id": job["id"],
                "terminal": job.get("terminal")}
    pub = c.command("publish_component", {"bundle_artifact_id": aid, "validation_job_id": job["id"]}, project=project,
                    key=f"publish:{aid}:{job['id']}")["result"]  # the server dedupes identical releases
    if bind:
        try:
            c.command("bind_component", {"component_ref": pub["component_ref"], "mode": "add"}, project=project,
                      key=f"bind:{pub['component_ref']}")
        except LabError:
            pass
    return {**pub, "validation_job_id": job["id"], "bundle_artifact_id": aid}


@component_app.command("install")
def component_install(project: str, directory: Path, env: str = "default", bind: bool = True):
    """Upload → validate (build job) → publish → bind, through the normal command path."""
    try:
        out(install_component(client(), project, directory, env, bind))
    except LabError as exc:
        fail(exc)


@component_app.command("validate")
def component_validate(project: str, directory: Path, env: str = "default"):
    c = client()
    try:
        up = c.upload(project, data=bundle_dir(directory), media_type="application/vnd.eidos.source-bundle+json",
                      schema="source-bundle/1", role="source", name=directory.name)
        out(c.command("validate_component", {"bundle_artifact_id": up["result"]["artifact_id"], "environment": env},
                      project=project))
    except LabError as exc:
        fail(exc)


@component_app.command("publish")
def component_publish(project: str, bundle_artifact_id: str, validation_job_id: str):
    try:
        out(client().command("publish_component", {"bundle_artifact_id": bundle_artifact_id,
                             "validation_job_id": validation_job_id}, project=project))
    except LabError as exc:
        fail(exc)


@component_app.command("bind")
def component_bind(project: str, ref: str, capability: str = None, mode: str = "replace",
                   acknowledge_metric_change: bool = False, expected_revision: int = None):
    try:
        out(client().command("bind_component", {"component_ref": ref, "capability": capability, "mode": mode,
                             "acknowledge_metric_change": acknowledge_metric_change}, project=project,
                             expected_revision=expected_revision))
    except LabError as exc:
        fail(exc)


@component_app.command("retire")
def component_retire(project: str, ref: str):
    try:
        out(client().command("retire_component", {"component_ref": ref}, project=project))
    except LabError as exc:
        fail(exc)


@component_app.command("withdraw")
def component_withdraw(project: str, ref: str, defect: str = typer.Option(...), condition: str = "{}",
                       severity: str = "scientific"):
    try:
        out(client().command("withdraw_component", {"component_ref": ref, "defect": defect,
                             "condition": _json_arg(condition), "severity": severity}, project=project))
    except LabError as exc:
        fail(exc)


@component_app.command("list")
def component_list(project: str, text: str = "", capability: str = None):
    try:
        out(client().query("search_components", {"text": text, "capability": capability, "limit": 100}, project=project))
    except LabError as exc:
        fail(exc)


@component_app.command("describe")
def component_describe(project: str, ref: str):
    try:
        out(client().query("describe_component", {"component_ref": ref}, project=project))
    except LabError as exc:
        fail(exc)


# -- charters / investigations -----------------------------------------------------------------

@charter_app.command("commit")
def charter_commit(project: str, charter: Path, investigation: str = None):
    try:
        out(client().command("commit_study_charter", {"charter": json.loads(charter.read_text()),
                             "investigation_id": investigation}, project=project))
    except LabError as exc:
        fail(exc)


@charter_app.command("amend")
def charter_amend(project: str, charter_id: str, charter: Path, rationale: str = typer.Option(...),
                  expected_revision: int = typer.Option(...)):
    try:
        out(client().command("amend_study_charter", {"charter_id": charter_id, "charter": json.loads(charter.read_text()),
                             "rationale": rationale}, project=project, expected_revision=expected_revision))
    except LabError as exc:
        fail(exc)


@charter_app.command("show")
def charter_show(project: str, charter_id: str, history: bool = False):
    try:
        c = client()
        out(c.query("record_history" if history else "get_record", {"kind": "charter", "id": charter_id}, project=project))
    except LabError as exc:
        fail(exc)


@inv_app.command("start")
def inv_start(project: str, question: str = typer.Option(...), charter: str = None, model_artifact: str = None,
              environment: str = "default", limit: list[str] = typer.Option([], help="dimension=value"),
              runtime: str = "fixture", model: str = None, policy: str = None, max_turns: int = 40,
              investigation_id: str = None, key: str = None,
              input_artifact: list[str] = typer.Option([], "--input", help="declared input port=artifact_id"),
              settings: str = typer.Option(None, help="runtime settings JSON, e.g. max_requests_per_decision, price_per_mtok"),
              max_model_calls_per_turn: int = 4):
    inv = {"runtime": runtime, "model": model, "max_turns": max_turns, "max_model_calls_per_turn": max_model_calls_per_turn}
    if policy or settings:
        inv["settings"] = {**({"policy": policy} if policy else {}), **(json.loads(settings) if settings else {})}
    payload = {"question": question, "charter_id": charter, "model_artifact_id": model_artifact, "environment": environment,
               "inputs": _kv(input_artifact),
               "limits": {k: float(v) for k, v in _kv(limit).items()}, "investigator": inv,
               "investigation_id": investigation_id}
    try:
        out(client().command("start_investigation", payload, project=project, key=key or f"start:{uuid.uuid4().hex}"))
    except LabError as exc:
        fail(exc)


@inv_app.command("steer")
def inv_steer(project: str, investigation: str, instruction: str, expected_revision: int = None):
    try:
        out(client().command("steer_investigation", {"investigation_id": investigation, "instruction": instruction},
                             project=project, expected_revision=expected_revision))
    except LabError as exc:
        fail(exc)


def _control(name: str, project: str, investigation: str, policy: str):
    try:
        out(client().command(name, {"investigation_id": investigation, "running_job_policy": policy}, project=project))
    except LabError as exc:
        fail(exc)


@inv_app.command("pause")
def inv_pause(project: str, investigation: str, running_jobs: str = typer.Option("drain", help="drain|checkpoint-and-stop|cancel")):
    _control("pause_investigation", project, investigation, running_jobs)


@inv_app.command("resume")
def inv_resume(project: str, investigation: str):
    _control("resume_investigation", project, investigation, "drain")


@inv_app.command("stop")
def inv_stop(project: str, investigation: str, running_jobs: str = "drain"):
    _control("stop_investigation", project, investigation, running_jobs)


@inv_app.command("show")
def inv_show(project: str, investigation: str):
    try:
        out(client().query("get_investigation", {"investigation_id": investigation}, project=project))
    except LabError as exc:
        fail(exc)


@inv_app.command("events")
def inv_events(project: str, investigation: str = None, cursor: int = 0, follow: bool = False):
    """Stream events from a cursor; reattach later with the printed next cursor."""
    c = client()
    while True:
        r = c.query("list_events", {"investigation_id": investigation, "cursor": cursor, "limit": 200}, project=project)
        for e in r["events"]:
            typer.echo(json.dumps({"seq": e["seq"], "type": e["type"], "subject": e["subject"]}))
        cursor = r["next_cursor"]
        if not follow:
            typer.echo(json.dumps({"next_cursor": cursor}))
            return
        time.sleep(2)


# -- jobs / trials / branches --------------------------------------------------------------------

@app.command("run")
def run(project: str, component: str, input: list[str] = typer.Option([], help="port=artifact_id"),
        config: str = "{}", environment: str = None, purpose: str = "open-search", wait: bool = False,
        investigation: str = None, isolation_profile: str = None, wall_seconds: int = 900):
    c = client()
    try:
        r = c.command("run_component", {"component_ref": component, "inputs": _kv(input), "config": _json_arg(config),
                      "environment": environment, "purpose": purpose, "investigation_id": investigation,
                      "isolation_profile": isolation_profile, "wall_seconds": wall_seconds}, project=project)
        if wait:
            r["job"] = wait_job(c, project, r["result"]["job_id"])
        out(r)
    except LabError as exc:
        fail(exc)


@job_app.command("show")
def job_show(project: str, job_id: str):
    try:
        out(client().query("get_job", {"job_id": job_id}, project=project))
    except LabError as exc:
        fail(exc)


@job_app.command("wait")
def job_wait(project: str, job_id: str, timeout: float = 900):
    try:
        out(wait_job(client(), project, job_id, timeout))
    except LabError as exc:
        fail(exc)


@job_app.command("cancel")
def job_cancel(project: str, job_id: str):
    try:
        out(client().command("cancel_run", {"job_id": job_id}, project=project))
    except LabError as exc:
        fail(exc)


@checkpoint_app.command("branch")
def checkpoint_branch(project: str, parent: str = typer.Option(..., help="parent checkpoint artifact id"),
                      component: str = typer.Option(...), migration: str = typer.Option(..., help="JSON or @file"),
                      stream_policy: str = "paired", seed: int = None, intervention: str = '{"kind": "none"}',
                      config: str = "{}", input: list[str] = typer.Option([]), launch: bool = True,
                      wall_seconds: int = 900):
    c = client()
    try:
        plan = c.command("plan_branch", {"parent_checkpoint_id": parent, "component_ref": component,
                         "migration": _json_arg(migration), "stream_policy": stream_policy, "seed": seed,
                         "intervention": _json_arg(intervention), "config": _json_arg(config), "inputs": _kv(input),
                         "wall_seconds": wall_seconds}, project=project)
        if launch:
            plan["launch"] = c.command("launch_branch", {"trial_id": plan["result"]["trial_id"]}, project=project)
        out(plan)
    except LabError as exc:
        fail(exc)


@app.command("export")
def export(project: str, checkpoint: str = typer.Option(...), behavior_job: str = typer.Option(...),
           dest: Path = typer.Option(None), investigation: str = None):
    """Record a model export (requires a passing fresh-process behaviour check) and download it."""
    c = client()
    try:
        r = c.command("export_model", {"checkpoint_artifact_id": checkpoint, "behavior_check_job_id": behavior_job,
                      "investigation_id": investigation}, project=project)
        if dest:
            dest.mkdir(parents=True, exist_ok=True)
            c.download(project, checkpoint, dest / "checkpoint.pt")
            rec = c.query("get_record", {"kind": "export", "id": r["result"]["export_id"]}, project=project)
            (dest / "eidos-export.json").write_text(json.dumps(rec, indent=2, default=str))
        out(r)
    except LabError as exc:
        fail(exc)


task_app = typer.Typer(no_args_is_help=True, help="External task bundles (eidos-task-bundle/0.1) and common answers")
app.add_typer(task_app, name="task")


@task_app.command("import")
def task_import(project: str, bundle: Path, python: str = typer.Option(..., help="interpreter with the bundle's lock installed"),
                record: Path = typer.Option(None, help="write the import record here")):
    """Verify a task bundle's hashes and register it (inputs, environment, exploration-only charter)."""
    from eidos.tasks.bundle import import_bundle
    try:
        r = import_bundle(client(), project, bundle, python=python)
    except LabError as exc:
        fail(exc)
    if record:
        record.write_text(json.dumps(r, indent=2, sort_keys=True, default=str))
    out({k: r[k] for k in ("task_id", "artifacts", "charter_id", "bundle")})


@task_app.command("answer")
def task_answer(project: str, task_id: str = typer.Option(...), run_id: str = typer.Option(...),
                out_dir: Path = typer.Option(...), python: str = typer.Option(...),
                test_inputs: Path = typer.Option(None), candidate_job: str = typer.Option(None),
                baseline_score_job: str = None, candidate_score_job: str = None,
                status: str = typer.Option(...), claim_type: str = typer.Option(...), claim: str = typer.Option(...),
                scope: str = typer.Option(...), investigation: str = None,
                run_label: str = typer.Option(..., help="e.g. 'fixture-provider run' or 'builder-assisted run'"),
                validator: Path = typer.Option(None, help="path to the release's validate_answer.py")):
    """Export an eidos-answer/0.2 submission (candidate inference on test inputs in a separate process)."""
    from eidos.tasks.bundle import export_answer
    try:
        out(export_answer(client(), project, task_id=task_id, run_id=run_id, out=out_dir, python=python,
                          test_inputs=test_inputs, candidate_job=candidate_job, baseline_score_job=baseline_score_job,
                          candidate_score_job=candidate_score_job, status=status, claim_type=claim_type, claim=claim,
                          scope=scope, investigation_id=investigation, run_label=run_label, validator=validator))
    except LabError as exc:
        fail(exc)


@app.command("report")
def report(project: str, investigation: str = None, out_dir: Path = typer.Option(Path("reports"))):
    """Generate a static HTML report from authorized views (cannot change scientific state)."""
    from eidos.reporting.html import write_report
    try:
        path = write_report(client(), project, investigation, out_dir)
        out({"report": str(path)})
    except LabError as exc:
        fail(exc)


def _job_env() -> dict:
    """Project/candidate code never inherits provider keys or lab secrets from the operator's shell."""
    import os

    from eidos.runtime.launcher import job_environment
    return job_environment(os.environ)
