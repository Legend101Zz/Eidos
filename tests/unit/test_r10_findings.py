"""Review R10 of ADR 0020: redaction of structured secrets before any cut, pinned-section budgets and the final limit, the
turn boundary, and the study's wall deadline. Requirement and probe rules are in test_environment_capabilities and test_env_probe."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from eidos.domain.environments import CAPABILITIES_SCHEMA, assess, describe_problems
from eidos.domain.redaction import REDACTED, redact_text, redact_value
from eidos.domain.turns import MODEL_CONTEXT_CHAR_LIMIT, completion_state, turn_boundary, turn_seconds
from eidos.investigator.context import TOOL_GUIDE, _error_view, pack
from eidos.runtime.env_probe import declared_requirements
from eidos.runtime.runner import _log_tail


def make_view(**over):
    return {"investigation": {"id": "inv1", "status": "active", "body": {"question": "Q?", "notes": over.pop("notes", "")}},
            "horizon": 3, "charter": None, "jobs": [], "budgets": {}, **over}


# -- findings 3 and 4: details of what preflight reports ------------------------------------------------------------------

def test_an_extras_version_mismatch_names_the_package_the_extra_needs():
    problems = [{"owner": "c", "package": "pysocks", "requirement": "requests[socks]", "problem": "version-mismatch",
                 "found": "1.5.7", "via_extra": "socks"}]
    assert describe_problems(problems) == "pysocks (extra socks, found 1.5.7)"


def test_a_partial_marker_environment_is_not_completed_from_the_running_interpreter():
    env = {"installed": {"numpy": "2.1"}, "capabilities": {
        "schema": CAPABILITIES_SCHEMA, "status": "ok", "marker_environment": {"python_version": "2.7"}, "import_names": {}}}
    result = assess({"c": ["absent-package; python_version >= '3'"]}, env)
    assert result["problems"] == []
    assert [i["inconclusive"] for i in result["inconclusive"]] == ["marker-not-evaluated"]


def test_a_requirements_file_symlinked_out_of_the_project_is_not_read(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("leaked-requirement\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").symlink_to(outside)
    assert declared_requirements(project) == []


# -- finding 5: structured secrets, before any cut ---------------------------------------------------------------------

@pytest.mark.parametrize("text, secret", [
    ('{"password": "sensitive-value"}', "sensitive-value"),
    ('{"api_key":"abc-123"}', "abc-123"),
    ("{'token': 'two words'}", "two words"),
    ('{"client_secret": "abc\\"def"}', 'def"'),
    (r'{\"password\": \"nested value\"}', "nested value"),
    ('"Authorization": "Bearer abcdefghijklmnop"', "abcdefghijklmnop"),
    ('password=hunter2', "hunter2"),
    ('"password": "cut off at the end of the line', "cut off"),
    ('"secrets": {"a": "inner-value"}', "inner-value"),
    ('"password": 123456', "123456"),
    ("postgres://user:pa55word@db/x", "pa55word"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----", "abc"),
])
def test_structured_and_textual_secrets_are_redacted(text, secret):
    assert secret not in redact_text(text)
    assert REDACTED in redact_text(text)


@pytest.mark.parametrize("text", ['"max_tokens": 64,', "max_tokens=64", '"tokenizer": "bert"', '"token": null', '"password": ""'])
def test_values_that_are_not_secrets_stay(text):
    assert redact_text(text) == text


def test_redaction_keeps_the_scan_linear_on_long_word_runs():
    started = time.monotonic()
    redact_text("A" * 300_000 + "\n" + "x-" * 150_000)
    assert time.monotonic() - started < 2


def test_redact_value_covers_keys_nested_values_and_shape():
    out = redact_value({"category": "user-code", "password": "p1", "max_tokens": 5, "nested": [{"api_key": "k1"}],
                        "message": 'failed {"token": "t1"}'})
    assert out["password"] == REDACTED and out["max_tokens"] == 5 and out["category"] == "user-code"
    assert out["nested"][0]["api_key"] == REDACTED
    assert "t1" not in out["message"]


def test_the_model_view_redacts_a_quoted_secret_in_stderr():
    view = _error_view({"category": "user-code", "message": '{"password": "sensitive-value"}',
                        "stderr_tail": 'boom {"api_key": "sensitive-key"}\n'})
    assert "sensitive-value" not in json.dumps(view) and "sensitive-key" not in json.dumps(view)


def test_a_log_tail_is_redacted_before_it_is_cut(tmp_path):
    # the value is longer than the tail, so a cut made first would remove the name that identifies it
    log = tmp_path / "stderr.log"
    log.write_text('prefix\n{"password": "' + "S" * 5000 + '"}\n')
    tail = _log_tail(log, 3000)
    assert "S" * 20 not in tail and "password" in tail and REDACTED in tail


def test_a_log_tail_scan_starts_at_a_line_boundary(tmp_path):
    log = tmp_path / "stderr.log"
    log.write_text('{"password": "first-line-secret"}' + "z" * (70 << 10) + "\nlast line\n")
    assert "first-line-secret" not in _log_tail(log) and "last line" in _log_tail(log)


def test_a_missing_log_has_an_empty_tail(tmp_path):
    assert _log_tail(tmp_path / "absent.log") == ""


# -- finding 6: every pinned section is budgeted, and the final limit holds ---------------------------------------------

def refusals(n):
    return [{"component": f"comp{i}", "environment": "e", "problems": [{"package": f"p{j}"} for j in range(40)]} for i in range(n)]


def environments(n):
    modules = {f"mod{m}": "1.0" for m in range(30)}
    return {f"env{i}": {"probed": True, "python_version": "3.12", "interpreter_digest": "sha256:ab", "modules": modules} for i in range(n)}


def overloaded(**over):
    wall = {"remaining_seconds": 30.0, "conclude_threshold_seconds": 60.0, "conclude_now": True}
    return make_view(notes="N" * 90_000, admission_refusals=refusals(500), environment_details=environments(500),
                     completion=wall, **over)


def test_a_context_with_every_pinned_section_oversized_stays_under_the_limit():
    out = pack(overloaded(), [])
    assert len(out["text"]) <= MODEL_CONTEXT_CHAR_LIMIT and out["chars"] == len(out["text"])
    assert "CONCLUDE NOW" in out["text"] and TOOL_GUIDE in out["text"]
    assert out["truncations"]["context_truncated"] is True


def test_refusal_groups_are_capped_and_the_rest_recorded():
    out = pack(make_view(admission_refusals=refusals(40)), [])
    text = out["text"]
    assert text.count("refused 1× before launch") == 12
    assert len([o for o in out["omissions"]["known_failures"] if o.startswith("refused|")]) == 28
    assert " and 30 more" in text


def test_large_notes_are_cut_and_recorded():
    out = pack(make_view(notes="N" * 90_000), [])
    assert "N" * 3100 not in out["text"]
    assert any(o.startswith("notes:") for o in out["omissions"]["sections"])


def test_many_environments_are_budgeted_and_recorded():
    out = pack(make_view(environment_details=environments(200)), [])
    assert sum(len(ln) + 1 for ln in out["text"].splitlines() if ln.startswith("- Environment ")) <= 6000
    assert any(o.startswith("environments:") for o in out["omissions"]["sections"])


def test_steering_keeps_the_newest_and_records_what_it_drops():
    steering = [{"by": "op", "instruction": f"instruction-{i}-" + "x" * 2000} for i in range(9)]
    view = make_view()
    view["investigation"]["body"]["steering"] = steering
    out = pack(view, [])
    assert "instruction-8-" in out["text"] and "instruction-0-" not in out["text"]
    assert any(o.startswith("steering:") for o in out["omissions"]["sections"])


@pytest.mark.parametrize("limit", [6000, 1200, 50])
def test_the_final_limit_holds_at_any_size(limit):
    out = pack(overloaded(), [], char_limit=limit)
    assert len(out["text"]) <= limit and out["chars"] == len(out["text"])
    assert out["omissions"]["sections"]


# -- finding 7: the turn boundary ----------------------------------------------------------------------------------------

def test_a_turn_before_the_reserve_ends_where_the_reserve_begins():
    state = completion_state(1000.0, 500.0, 60.0)
    assert state["conclude_now"] is False and turn_boundary(state) == 940.0


def test_the_concluding_turn_ends_at_the_wall_deadline():
    state = completion_state(1000.0, 950.0, 60.0)
    assert state["conclude_now"] is True and turn_boundary(state) == 1000.0
    assert turn_seconds(600, state) <= 50


# -- finding 1: the study arm passes the wall deadline to the lab --------------------------------------------------------

@pytest.fixture()
def arm4(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2]))  # the study tree is not an installed package
    import study.eidos_arms.arm4 as module
    return module


class _Stop(Exception):
    pass


class _Client:
    def __init__(self):
        self.calls = []

    def command(self, name, payload, **kw):
        self.calls.append((name, payload))
        raise _Stop

    def close(self):
        pass


def test_the_arm_registers_the_wall_time_actually_remaining(tmp_path, monkeypatch, arm4):
    clock = SimpleNamespace(now=10_000.0)
    fake_time = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now, sleep=lambda s: None)
    client = _Client()
    bridge = SimpleNamespace(poll=lambda: 0, pid=1, returncode=0)

    def start_bridge(*a, eval_audit_config=None):
        assert eval_audit_config is None
        clock.now += 120.0  # setup after the envelope started: the deadline must not be counted again from here
        return bridge, {"base_url": "http://127.0.0.1:1", "upstream": "u"}

    write_json = arm4.write_json

    def delayed_setup_record(path, body):
        if path.name == "setup.json":
            clock.now += 9.0
        return write_json(path, body)

    monkeypatch.setattr(arm4, "time", fake_time)
    monkeypatch.setattr(arm4, "write_json", delayed_setup_record)
    monkeypatch.setattr(arm4.lab, "start", lambda: None)
    monkeypatch.setattr(arm4.lab, "client", lambda: client)
    monkeypatch.setattr(arm4, "prepare", lambda c, d, n: ("proj", {"charter_id": "c", "artifacts": {}, "task": {}}))
    monkeypatch.setattr(arm4, "envelope", lambda meta: (3600, 1000))
    monkeypatch.setattr(arm4, "prompt", lambda n, d: "question")
    monkeypatch.setattr("study.eidos_arms.provider_bridge.start", start_bridge)
    (tmp_path / "task").mkdir()
    with pytest.raises(_Stop):
        arm4.run(tmp_path, {})
    name, payload = client.calls[0]
    assert name == "start_investigation"
    expected = 3600 - 129 - arm4.WATCHDOG_MARGIN_SECONDS
    assert payload["investigator"]["wall_seconds"] == pytest.approx(expected)
    assert payload["investigator"]["wall_deadline_unix"] == 10_000 + 3600 - arm4.WATCHDOG_MARGIN_SECONDS
    assert payload["investigator"]["wall_seconds"] < 3600 - 120  # strictly inside the watchdog's envelope


def test_remaining_wall_seconds_never_reaches_zero(arm4):
    assert arm4.remaining_wall_seconds(100.0, now=500.0) == 1.0
    assert arm4.remaining_wall_seconds(1000.0, now=500.0) == 495.0
