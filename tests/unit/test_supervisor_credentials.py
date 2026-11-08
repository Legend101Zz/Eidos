"""Provider credentials reach only the investigator worker (found on a researcher Mac, 2026-11-08).

Before the fix, ``lab start`` launched the supervisor with its full environment, so the provider key
was in the supervisor's environment and, through ``pg_ctl``, in the PostgreSQL postmaster's
(``ps eww`` showed it in three processes: supervisor, postgres, investigator worker). Now the supervisor
runs with a scrubbed environment, receives credentials over a stdin pipe, keeps them in memory and adds
them only to the investigator worker's environment; service subprocesses get a scrubbed environment."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import eidos.supervisor.daemon as D
import eidos.supervisor.ops as O
import eidos.supervisor.services as SV

KEY = "planted-provider-key-value"


def _cfg(tmp_path: Path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    return SimpleNamespace(p=lambda *a: tmp_path.joinpath(*a), root=tmp_path, lab_python="/usr/bin/python3",
                           sealed_available=False, temporal_bin=str(tmp_path), pg_bin="/x", api_url="http://127.0.0.1:1")


def test_only_the_investigator_worker_gets_credentials(tmp_path, monkeypatch):
    envs = {}

    class P:
        def __init__(self, argv, env, **kw):
            envs[argv[-1]] = env

    monkeypatch.setattr(D.subprocess, "Popen", P)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sup = D.Supervisor(_cfg(tmp_path), credentials={"ANTHROPIC_API_KEY": KEY})
    for ch in sup.children.values():
        sup.spawn(ch)
    assert envs["investigator-worker"]["ANTHROPIC_API_KEY"] == KEY
    others = {n: e for n, e in envs.items() if n != "investigator-worker"}
    assert others and all(KEY not in json.dumps(e) for e in others.values()), sorted(others)
    assert KEY not in json.dumps(dict(os.environ))  # never placed in the supervisor's own environment


def test_supervisor_reads_credentials_from_stdin_and_drops_env_copies(monkeypatch):
    assert D.read_credentials(io.StringIO(json.dumps({"ANTHROPIC_API_KEY": KEY}))) == {"ANTHROPIC_API_KEY": KEY}
    assert D.read_credentials(io.StringIO("")) == {}
    assert D.read_credentials(io.StringIO("not json")) == {}


def test_lab_start_scrubs_the_supervisor_env_and_pipes_credentials(tmp_path, monkeypatch):
    seen = {}

    class Stdin:
        def write(self, b):
            seen["stdin"] = seen.get("stdin", b"") + b

        def close(self):
            seen["closed"] = True

    class P:
        def __init__(self, argv, env, stdin, **kw):
            seen["env"], seen["stdin_kind"] = env, stdin
            self.stdin = Stdin()

    monkeypatch.setattr(O.subprocess, "Popen", P)
    monkeypatch.setattr(O, "supervisor_pid", lambda cfg: None)
    monkeypatch.setattr(O, "status_file", lambda cfg: {"phase": "running"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "other-key")
    O.start(_cfg(tmp_path), credentials={"EXTRA_API_KEY": "from-stdin"})
    assert "ANTHROPIC_API_KEY" not in seen["env"] and "OPENAI_API_KEY" not in seen["env"]
    assert KEY not in json.dumps(seen["env"]) and seen["stdin_kind"] == O.subprocess.PIPE and seen["closed"]
    piped = json.loads(seen["stdin"])
    assert {k: piped.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "EXTRA_API_KEY")} == \
        {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "other-key", "EXTRA_API_KEY": "from-stdin"}


def test_postgres_control_runs_without_credentials(tmp_path, monkeypatch):
    envs = []
    monkeypatch.setattr(SV.subprocess, "run", lambda *a, **k: envs.append(k.get("env")) or SimpleNamespace(
        returncode=3, stdout="", stderr=""))
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    (tmp_path / "services" / "pg" / "data").mkdir(parents=True)
    (tmp_path / "services" / "pg" / "data" / "postmaster.pid").write_text("1")
    SV.pg_running(_cfg(tmp_path))
    SV.pg_stop(_cfg(tmp_path))
    assert envs and all(e is not None and "ANTHROPIC_API_KEY" not in e for e in envs)
