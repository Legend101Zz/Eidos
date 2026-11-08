"""``lab`` composition root: research commands (API client) + local operator commands."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from eidos.cli.main import app, out
from eidos.settings import LabConfig


def _cfg() -> LabConfig:
    return LabConfig.load()


@app.command("init")
def init(home: Path = typer.Option(None, help="lab data root (default $EIDOS_HOME or ~/.eidos)"),
         api_port: int = 8765, pg_port: int = 5544, temporal_port: int = 7633, pg_bin: str = "",
         temporal_source: str = typer.Option(None, help="directory with temporal-server binaries (else download pinned)")):
    """Create a lab: PostgreSQL cluster + roles, ledger migration, Temporal server + schema, tokens."""
    from eidos.supervisor.ops import init_lab
    home = home or Path(os.environ.get("EIDOS_HOME") or Path.home() / ".eidos")
    cfg = init_lab(str(home), api_port=api_port, pg_port=pg_port, temporal_port=temporal_port, pg_bin=pg_bin,
                   temporal_source=temporal_source)
    out({"home": cfg.home, "api_url": cfg.api_url, "sealed_available": cfg.sealed_available,
         "sealed_profile": cfg.sealed_profile or None, "admin_token_file": str(cfg.secret_path("admin.token")), "next": f"EIDOS_HOME={cfg.home} lab start"})


@app.command("start")
def start():
    from eidos.supervisor.ops import start as _start
    out(_start(_cfg()))


@app.command("stop")
def stop(running_jobs: str = typer.Option("detach", help="detach|drain|cancel: what to do with admitted research jobs")):
    """Stop lab services. 'detach' leaves research processes running for reattachment on restart;
    'cancel' requests cancellation of every running job first; 'drain' waits for them."""
    from eidos.supervisor.ops import stop as _stop
    cfg = _cfg()
    if running_jobs in ("cancel", "drain"):
        from eidos.cli.main import client, wait_job
        c = client()
        for pr in c.query("list_projects"):
            for j in c.query("list_jobs", {"statuses": ["admitted", "queued", "running"]}, project=pr["id"]):
                if running_jobs == "cancel":
                    c.command("cancel_run", {"job_id": j["id"]}, project=pr["id"])
                wait_job(c, pr["id"], j["id"], timeout=3600)
    out(_stop(cfg))


@app.command("status")
def status(as_json: bool = typer.Option(False, "--json")):
    from eidos.supervisor.ops import status as _status
    st = _status(_cfg())
    if as_json:
        out(st)
        return
    typer.echo(f"lab {st['home']}  phase={st.get('phase')}  api={st.get('api', {}).get('status')}")
    for n, s in (st.get("services") or {}).items():
        typer.echo(f"  {n:22s} {s.get('state'):10s} pid={s.get('pid')} restarts={s.get('restarts')}")
    for p in st.get("projects", []):
        typer.echo(f"  project {p['project_id']}: jobs={p['jobs']} records={p['records']}")


@app.command("doctor")
def doctor(as_json: bool = typer.Option(False, "--json")):
    from eidos.supervisor.ops import doctor as _doctor
    checks = _doctor(_cfg())
    if as_json:
        out(checks)
    else:
        for c in checks:
            typer.echo(f"[{c['status']:4s}] {c['check']}: {c['detail']}")
    if any(c["status"] == "fail" for c in checks):
        raise typer.Exit(1)


@app.command("logs")
def logs(service: str = typer.Argument(None), lines: int = 100):
    from eidos.supervisor.ops import logs as _logs
    typer.echo(_logs(_cfg(), service, lines))


@app.command("backup")
def backup(dest: Path = typer.Option(None)):
    from eidos.supervisor.maintenance import backup as _backup
    out(_backup(_cfg(), dest))


@app.command("restore")
def restore(bundle: Path, home: Path = typer.Option(..., help="fresh lab directory to restore into"),
            api_port: int = 8765, pg_port: int = 5544, temporal_port: int = 7633):
    from eidos.supervisor.maintenance import restore as _restore
    out(_restore(bundle, str(home), api_port=api_port, pg_port=pg_port, temporal_port=temporal_port))


@app.command("upgrade")
def upgrade():
    """Apply ledger migrations and restart services; in-flight workflows replay on new workers."""
    from eidos.supervisor.maintenance import upgrade as _upgrade
    out(_upgrade(_cfg()))


@app.command("mcp")
def mcp(transport: str = typer.Option("stdio", help="stdio | http"), port: int = 8766):
    """Run the MCP adapter (stdio for local clients; streamable HTTP for authenticated remote operators)."""
    from eidos.mcp.server import serve
    serve(transport=transport, port=port)


@app.command("token")
def token():
    """Print the path of the operator token file (for MCP client configuration)."""
    cfg = _cfg()
    out({"token_file": str(cfg.secret_path("admin.token")), "api_url": cfg.api_url})


def main() -> None:
    app()


if __name__ == "__main__":
    main()
