"""G6 (A15 service failures): PostgreSQL is daemonized by pg_ctl, not a supervised child. A cluster that
died is started again by the supervisor (PostgreSQL crash recovery replays the WAL); repeated deaths
leave it failed and visible in run/status.json instead of a restart loop."""

from __future__ import annotations

import asyncio
import json

import eidos.supervisor.daemon as D
from test_supervisor_credentials import _cfg


def test_dead_postgres_is_restarted_and_a_crash_loop_is_reported(tmp_path, monkeypatch):
    state = {"running": False, "starts": 0}
    monkeypatch.setattr(D.svc, "pg_running", lambda cfg: state["running"])

    def start(cfg):
        state["starts"] += 1
        state["running"] = True

    monkeypatch.setattr(D.svc, "pg_start", start)
    sup = D.Supervisor(_cfg(tmp_path))
    asyncio.run(sup._check_postgres())
    assert state["starts"] == 1 and len(sup._pg_restarts) == 1
    asyncio.run(sup._check_postgres())  # running again: nothing to do
    assert state["starts"] == 1
    # a crash loop: it keeps dying; after five restarts in five minutes it is left failed
    monkeypatch.setattr(D.svc, "pg_start", lambda cfg: state.update(starts=state["starts"] + 1))
    state["running"] = False
    for _ in range(8):
        asyncio.run(sup._check_postgres())
    assert sup._pg_state == "failed" and state["starts"] == 5
    sup.cfg.p("run").mkdir(parents=True, exist_ok=True)
    sup.write_status("running")
    pg = json.loads(sup.cfg.p("run", "status.json").read_text())["services"]["postgres"]
    assert pg["state"] == "failed" and pg["restarts"] == 6


def test_pg_start_error_is_recorded_and_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(D.svc, "pg_running", lambda cfg: False)

    def boom(cfg):
        raise D.svc.ServiceError("pg_ctl start failed: shared memory still in use")

    monkeypatch.setattr(D.svc, "pg_start", boom)
    sup = D.Supervisor(_cfg(tmp_path))
    asyncio.run(sup._check_postgres())
    assert "shared memory" in sup._pg_last_error and sup._pg_state == "running"
