"""The CLI's JSON options accept `@file`, so large payloads never need argv.

Linux caps one argv string at 128 KiB (MAX_ARG_STRLEN): a control suite passed inline to `lab cmd --payload`
failed with `OSError: [Errno 7] Argument list too long` before the CLI even ran. Journeys and scripts pass `@path`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eidos.cli import main as cli

BIG = 200 * 1024  # larger than MAX_ARG_STRLEN (128 KiB)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    def command(self, name: str, payload: dict, **kw: Any) -> dict:
        self.calls.append(("command", name, payload, kw))
        return {"result": {}}

    def query(self, name: str, params: dict, **kw: Any) -> dict:
        self.calls.append(("query", name, params, kw))
        return {}


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    r = _Recorder()
    monkeypatch.setattr(cli, "client", lambda: r)
    return r


def _big_suite() -> dict:
    return {"name": "suite", "controls": [{"id": f"c{i}", "pad": "x" * 1024} for i in range(BIG // 1024)]}


def _write(tmp_path: Path, obj: Any) -> str:
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(obj))
    assert f.stat().st_size > 128 * 1024 or len(json.dumps(obj)) < 1024
    return f"@{f}"


def test_json_arg_inline_file_and_empty(tmp_path: Path) -> None:
    f = tmp_path / "p.json"
    f.write_text('{"a": [1, 2]}')
    assert cli._json_arg('{"a": 1}') == {"a": 1}
    assert cli._json_arg(f"@{f}") == {"a": [1, 2]}
    assert cli._json_arg(None) == {}
    assert cli._json_arg("") == {}


def test_json_arg_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cli._json_arg(f"@{tmp_path / 'absent.json'}")


def test_cmd_payload_from_file_larger_than_argv_limit(rec: _Recorder, tmp_path: Path) -> None:
    suite = _big_suite()
    payload = {"suite": suite, "suite_id": "suite:big"}
    r = CliRunner().invoke(cli.app, ["cmd", "register_control_suite", "--payload", _write(tmp_path, payload),
                                     "-p", "sw1", "--key", "k1"])
    assert r.exit_code == 0, r.output
    kind, name, got, kw = rec.calls[-1]
    assert (kind, name) == ("command", "register_control_suite")
    assert got == payload
    assert kw["project"] == "sw1" and kw["key"] == "k1"


def test_query_params_from_file(rec: _Recorder, tmp_path: Path) -> None:
    params = {"kind": "qualification", "parent_id": "x" * BIG, "limit": 500}
    r = CliRunner().invoke(cli.app, ["query", "list_records", "-p", "sw1", "--params", _write(tmp_path, params)])
    assert r.exit_code == 0, r.output
    assert rec.calls[-1][:3] == ("query", "list_records", params)


def test_investigate_start_settings_from_file(rec: _Recorder, tmp_path: Path) -> None:
    settings = {"max_requests_per_decision": 3, "price_per_mtok": {"in": 1.0, "out": 2.0}}
    r = CliRunner().invoke(cli.app, ["investigate", "start", "p1", "--question", "q", "--policy", "pol",
                                     "--settings", _write(tmp_path, settings)])
    assert r.exit_code == 0, r.output
    kind, name, payload, _ = rec.calls[-1]
    assert (kind, name) == ("command", "start_investigation")
    assert payload["investigator"]["settings"] == {"policy": "pol", **settings}


def test_investigate_start_settings_inline_still_works(rec: _Recorder) -> None:
    r = CliRunner().invoke(cli.app, ["investigate", "start", "p1", "--question", "q",
                                     "--settings", '{"max_requests_per_decision": 2}'])
    assert r.exit_code == 0, r.output
    assert rec.calls[-1][2]["investigator"]["settings"] == {"max_requests_per_decision": 2}
