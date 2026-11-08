"""The local supervisor: owns startup order, health, restart and shutdown of lab services.

Research job processes are *not* children of the supervisor: they run in their own sessions and
outlive a supervisor or runner restart; the runner reattaches to them (or reconciles them as
lost) using their recorded handles.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from eidos.settings import LabConfig
from eidos.supervisor import services as svc

WORKER_ROLES = ("workflow-worker", "investigator-worker", "training-runner", "assessor-worker")


@dataclass
class Child:
    name: str
    argv: list[str]
    proc: subprocess.Popen | None = None
    started_at: float = 0.0
    restarts: list[float] = field(default_factory=list)
    state: str = "stopped"
    last_exit: int | None = None


class Supervisor:
    def __init__(self, cfg: LabConfig, credentials: dict[str, str] | None = None):
        self.cfg = cfg
        # Provider credentials, held in memory only (never in this process's environment): added to the
        # investigator worker's environment at each (re)spawn and to no other service.
        self._credentials = dict(credentials or {})
        self.children: dict[str, Child] = {}
        self.stopping = False
        py = cfg.lab_python or sys.executable
        self.children["temporal"] = Child("temporal", svc.temporal_argv(cfg))
        self.children["api"] = Child("api", [py, "-m", "eidos.apps.role", "api"])
        roles = list(WORKER_ROLES) + (["sealed-runner"] if cfg.sealed_available else [])
        for r in roles:
            self.children[r] = Child(r, [py, "-m", "eidos.apps.role", r])

    # -- lifecycle -----------------------------------------------------------------------------
    def spawn(self, ch: Child) -> None:
        log = open(self.cfg.p("logs", f"{ch.name}.log"), "ab")
        from eidos.runtime.launcher import scrub_secrets
        base = scrub_secrets(os.environ)
        if ch.name == "investigator-worker":  # only the model-calling investigator worker holds provider credentials
            base = {**dict(os.environ), **self._credentials}
        env = {**base, "EIDOS_HOME": str(self.cfg.root), "PYTHONUNBUFFERED": "1"}
        cwd = str(self.cfg.p("services", "temporal")) if ch.name == "temporal" else str(self.cfg.root)
        ch.proc = subprocess.Popen(ch.argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                                   start_new_session=True)
        log.close()
        ch.started_at = time.time()
        ch.state = "running"

    def write_status(self, phase: str) -> None:
        st = {"phase": phase, "supervisor_pid": os.getpid(), "updated_at": time.time(),
              "services": {"postgres": {"state": "running" if svc.pg_running(self.cfg) else "stopped"}}}
        for n, ch in self.children.items():
            st["services"][n] = {"state": ch.state, "pid": ch.proc.pid if ch.proc else None,
                                 "started_at": ch.started_at, "restarts": len(ch.restarts), "last_exit": ch.last_exit}
        p = self.cfg.p("run", "status.json")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(p)

    async def start(self) -> None:
        self.cfg.p("run").mkdir(parents=True, exist_ok=True)
        self.cfg.p("logs").mkdir(parents=True, exist_ok=True)
        self.write_status("starting-postgres")
        svc.pg_start(self.cfg)
        self.write_status("starting-temporal")
        self.spawn(self.children["temporal"])
        if not await svc.temporal_ready(self.cfg, 90):
            raise svc.ServiceError("temporal server did not become healthy; see logs/temporal.log")
        await svc.ensure_namespace(self.cfg)
        self.write_status("starting-api")
        self.spawn(self.children["api"])
        await self._wait_api(60)
        for n, ch in self.children.items():
            if n not in ("temporal", "api"):
                self.spawn(ch)
        self.write_status("running")

    async def _wait_api(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        async with httpx.AsyncClient() as c:
            while time.monotonic() < end:
                try:
                    if (await c.get(self.cfg.api_url + "/v1/health", timeout=2)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise svc.ServiceError("API did not become healthy; see logs/api.log")

    async def monitor(self) -> None:
        while not self.stopping:
            for ch in self.children.values():
                if ch.proc and ch.proc.poll() is not None and ch.state == "running":
                    ch.last_exit = ch.proc.returncode
                    now = time.time()
                    ch.restarts = [t for t in ch.restarts if now - t < 300] + [now]
                    if len(ch.restarts) > 5:
                        ch.state = "failed"
                        continue
                    ch.state = "restarting"
                    await asyncio.sleep(min(30, 2 ** len(ch.restarts)))
                    if not self.stopping:
                        self.spawn(ch)
            self.write_status("running")
            await asyncio.sleep(2)

    def stop(self, *, stop_postgres: bool = True) -> None:
        self.stopping = True
        order = [n for n in self.children if n not in ("api", "temporal")] + ["api", "temporal"]
        for n in order:
            ch = self.children[n]
            if ch.proc and ch.proc.poll() is None:
                try:
                    os.killpg(ch.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    ch.proc.wait(20)
                except subprocess.TimeoutExpired:
                    os.killpg(ch.proc.pid, signal.SIGKILL)
            ch.state = "stopped"
        if stop_postgres:
            svc.pg_stop(self.cfg)
        self.write_status("stopped")


def read_credentials(stream) -> dict[str, str]:
    """Credentials handed over by ``ops.start`` on stdin (a JSON object); empty for a terminal or /dev/null."""
    try:
        if stream is None or stream.isatty():
            return {}
        raw = stream.read()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def run_forever(cfg: LabConfig) -> None:
    from eidos.runtime.launcher import scrub_secrets
    creds = read_credentials(sys.stdin)
    # also take any credential the supervisor was (still) started with out of its environment
    for k in [k for k in os.environ if k not in scrub_secrets(os.environ)]:
        creds.setdefault(k, os.environ.pop(k))
    sup = Supervisor(cfg, credentials=creds)
    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

    def on_signal(*_):
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    Path(cfg.p("run", "supervisor.pid")).parent.mkdir(parents=True, exist_ok=True)
    cfg.p("run", "supervisor.pid").write_text(str(os.getpid()))

    async def main():
        try:
            await sup.start()
        except Exception as exc:
            sup.write_status(f"failed: {exc}")
            sup.stop()
            raise
        mon = asyncio.create_task(sup.monitor())
        await stop_event.wait()
        mon.cancel()
        sup.stop()

    try:
        loop.run_until_complete(main())
    finally:
        cfg.p("run", "supervisor.pid").unlink(missing_ok=True)
