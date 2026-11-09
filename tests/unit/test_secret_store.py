"""Provider keys in the OS secret store (ADR 0012): ``lab credentials set|status|delete`` and ``lab start``.

What is covered, in file order:

1. ``CredentialStore`` over ``MemoryBackend`` and a recording fake: set/status/delete/load_all, the item naming
   (service ``eidos/<lab id>/<provider>``, account = the env var), unknown providers, ``lab_id`` stability,
   ``check_value`` rejections, ``status`` never carrying the value, and ``openai`` / ``openai-compatible`` sharing
   OPENAI_API_KEY.
2. ``select_backend``: macOS -> Keychain, Linux with ``secret-tool`` -> libsecret, otherwise an explicit
   ``SecretStoreUnavailable`` (there is no plaintext fallback).
3. The ``lab credentials`` commands through typer's ``CliRunner``: the key is read from stdin only and is never
   echoed, and there is no option or argument that could carry it.
4. ``lab start`` reading stored keys when ``--credentials-stdin`` is not given: only names and sources are printed,
   stdin credentials bypass the store, an exported variable wins, an unavailable store is not fatal.
5. A leak test with a planted key value across success and error paths (stdout, stderr, logging), plus the
   ``MacKeychain`` / ``SecretTool`` argv checks: the value travels in ``input=`` only, never in an argv.
6. A real macOS Keychain round trip against a throwaway keychain file (select with ``-k real_keychain``).
7. A real libsecret round trip (select with ``-k real_secret_tool``; skipped unless a Secret Service is reachable,
   and a failure instead of a skip when ``EIDOS_REQUIRE_SECRET_TOOL=1``).

Safety: every test except the two ``real_*`` groups runs with the secret store hard-blocked (no ``select_backend``,
no ``subprocess``), so none of them can reach the owner's keychain. The real keychain test only ever passes the
path of a keychain it created under ``tmp_path``, and deletes it (removing it from the search list) afterwards.
No test uses the network."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import eidos.apps.cli as lab_cli  # noqa: F401  (importing registers `start` and `credentials` on the app)
import eidos.supervisor.ops as sup_ops
import eidos.supervisor.secret_store as SS
from eidos.cli.main import app

KEY = "sk-ant-api03-Test_Key-0123456789"
PLANTED = "sk-ant-PLANTED-canary-7f3a9c1e5b2d"
SECURITY = "/usr/bin/security"
REAL_SELECT_BACKEND = SS.select_backend  # the autouse guard below replaces the module attribute in most tests
_REAL_GROUPS = ("real_keychain", "real_secret_tool")


# --------------------------------------------------------------------------------------------------------------
# Test doubles and helpers
# --------------------------------------------------------------------------------------------------------------

class RecordingBackend(SS.MemoryBackend):
    """A MemoryBackend that records every call (never the value) so tests can see what was asked of the store."""

    name = "recording"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    def get(self, service, account):
        self.calls.append(("get", service, account))
        return super().get(service, account)

    def set(self, service, account, value, label):
        self.calls.append(("set", service, account, label))
        super().set(service, account, value, label)

    def delete(self, service, account):
        self.calls.append(("delete", service, account))
        return super().delete(service, account)


class ExistsBackend(RecordingBackend):
    """Like MacKeychain: it can answer "is there an item" without reading the secret."""

    name = "recording-exists"

    def exists(self, service, account):
        self.calls.append(("exists", service, account))
        return (service, account) in self.items


class LeakyBackend(SS.MemoryBackend):
    """A backend whose failure message echoes the secret, like a tool that prints the command it could not run."""

    name = "leaky"

    def set(self, service, account, value, label):
        raise SS.SecretStoreError(f"backend rejected {value!r} for {service}")


class _BlockedSubprocess:
    TimeoutExpired = subprocess.TimeoutExpired

    @staticmethod
    def run(*args, **kwargs):
        raise AssertionError(f"a unit test tried to run a real process: {args[:1]}")


@pytest.fixture(autouse=True)
def _never_touch_a_real_secret_store(request, monkeypatch):
    """Hard-block the OS secret store for every test that is not one of the explicit real_* tests."""
    if any(group in request.node.name for group in _REAL_GROUPS):
        return

    def blocked():
        raise SS.SecretStoreUnavailable("the OS secret store is blocked in unit tests")

    monkeypatch.delenv("EIDOS_KEYCHAIN", raising=False)
    monkeypatch.setattr(SS, "subprocess", _BlockedSubprocess)
    monkeypatch.setattr(SS, "select_backend", blocked)


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRun:
    """Stands in for ``subprocess.run`` inside secret_store: records (argv, kwargs), plays back responses."""

    def __init__(self, *responses):
        self.responses = list(responses) or [_proc()]
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            raise response
        return response


def _fake_run(monkeypatch, *responses) -> FakeRun:
    run = FakeRun(*responses)
    monkeypatch.setattr(SS, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return run


def _fake_platform(monkeypatch, platform, *, security=False, secret_tool=None, environ=None):
    """Make ``select_backend`` see another machine (only inside secret_store; the real ``os``/``sys`` are untouched)."""
    monkeypatch.setattr(SS, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(SS, "os", SimpleNamespace(
        path=SimpleNamespace(exists=lambda p: security and p == SS.SECURITY), environ=environ or {}))
    monkeypatch.setattr(SS, "shutil", SimpleNamespace(which=lambda name: secret_tool if name == "secret-tool" else None))


def _lab(*args, input=None):
    return CliRunner().invoke(app, list(args), input=input)


def _flat(text: str) -> str:
    """Collapse whitespace and rich's box-drawing characters so wrapped error panels can be searched."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # CI terminals get colour codes, which split words
    return re.sub(r"[\s│╭╮╰╯─]+", " ", text)


@pytest.fixture
def store(tmp_path):
    return SS.CredentialStore(RecordingBackend(), SS.lab_id(tmp_path / "lab"))


# --------------------------------------------------------------------------------------------------------------
# 1. CredentialStore with fake backends
# --------------------------------------------------------------------------------------------------------------

def test_set_status_delete_and_load_all_round_trip(store):
    assert store.status("anthropic")["stored"] is False
    assert store.load_all() == {}
    assert store.set("anthropic", KEY) == {
        "provider": "anthropic", "stored": True, "env_var": "ANTHROPIC_API_KEY",
        "service": f"eidos/{store.lab}/anthropic", "backend": "recording"}
    assert store.status("anthropic")["stored"] is True
    assert store.status("openai")["stored"] is False
    assert store.load_all() == {"ANTHROPIC_API_KEY": KEY}
    assert store.delete("anthropic") == {
        "provider": "anthropic", "deleted": True, "service": f"eidos/{store.lab}/anthropic", "backend": "recording"}
    assert store.status("anthropic")["stored"] is False
    assert store.delete("anthropic")["deleted"] is False  # nothing left to remove
    assert store.load_all() == {}


def test_set_strips_surrounding_whitespace_and_overwrites(store):
    store.set("anthropic", f"  {KEY}\n")
    assert store.load_all() == {"ANTHROPIC_API_KEY": KEY}
    store.set("anthropic", "sk-ant-second")
    assert store.load_all() == {"ANTHROPIC_API_KEY": "sk-ant-second"}


def test_item_names_are_service_per_lab_and_provider_with_the_env_var_as_account(store):
    store.set("anthropic", KEY)
    store.set("openai", "sk-openai-1")
    sets = [c for c in store.backend.calls if c[0] == "set"]
    assert [c[1:3] for c in sets] == [
        (f"eidos/{store.lab}/anthropic", "ANTHROPIC_API_KEY"),
        (f"eidos/{store.lab}/openai", "OPENAI_API_KEY"),
    ]
    assert sets[0][3] == f"Eidos {store.lab} anthropic key"
    assert store.service("openai-compatible") == f"eidos/{store.lab}/openai-compatible"
    assert list(store.backend.items) == [(f"eidos/{store.lab}/anthropic", "ANTHROPIC_API_KEY"),
                                         (f"eidos/{store.lab}/openai", "OPENAI_API_KEY")]


@pytest.mark.parametrize("operation", ["set", "status", "delete", "service"])
def test_unknown_provider_is_an_error_and_never_reaches_the_backend(store, operation):
    call = {"set": lambda p: store.set(p, KEY), "status": store.status, "delete": store.delete,
            "service": store.service}[operation]
    with pytest.raises(SS.SecretStoreError, match="unknown provider 'gemini'") as raised:
        call("gemini")
    assert "anthropic" in str(raised.value)  # lists the known providers
    assert store.backend.calls == []


def test_provider_to_env_var_mapping():
    assert SS.env_var_for("anthropic") == "ANTHROPIC_API_KEY"
    assert SS.env_var_for("openai") == "OPENAI_API_KEY"
    assert SS.env_var_for("openai-compatible") == "OPENAI_API_KEY"


def test_lab_id_is_stable_for_a_home_and_differs_between_homes(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    (tmp_path / "a").mkdir()
    assert SS.lab_id(a) == SS.lab_id(str(a)) == SS.lab_id(a / ".." / "a")  # same home, however spelled
    assert SS.lab_id(a) != SS.lab_id(b)
    assert re.fullmatch(r"lab-[0-9a-f]{12}", SS.lab_id(a))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert SS.lab_id("~/a") == SS.lab_id(a)  # ~ is expanded


def test_two_labs_keep_separate_keys_in_one_backend(tmp_path):
    backend = RecordingBackend()
    one = SS.CredentialStore(backend, SS.lab_id(tmp_path / "one"))
    two = SS.CredentialStore(backend, SS.lab_id(tmp_path / "two"))
    one.set("anthropic", "sk-one")
    assert two.status("anthropic")["stored"] is False
    assert two.load_all() == {}
    assert one.load_all() == {"ANTHROPIC_API_KEY": "sk-one"}


@pytest.mark.parametrize("bad", [
    "", "   ", "\n", "two words", "tab\tinside", "line\nbreak", 'dq"quote', "sq'quote", "back\\slash",
    "kéy-non-ascii", "x" * 1025])
def test_check_value_rejects_whitespace_quotes_empty_and_oversized(bad, store):
    with pytest.raises(SS.SecretStoreError, match="1-1024 printable ASCII") as raised:
        SS.check_value(bad)
    if bad.strip():
        assert bad not in str(raised.value)  # the message says nothing about the value
    with pytest.raises(SS.SecretStoreError):
        store.set("anthropic", bad)
    assert store.backend.calls == []  # an invalid key never reaches the backend


def test_check_value_accepts_printable_ascii_and_strips_ends():
    assert SS.check_value("  sk-1\n") == "sk-1"
    assert SS.check_value("x" * 1024) == "x" * 1024
    assert SS.check_value("k#!$%&()*+,-./:;<=>?@[]^_`{|}~") == "k#!$%&()*+,-./:;<=>?@[]^_`{|}~"


def test_redact_replaces_every_occurrence_and_ignores_empty_values():
    assert SS.redact(f"a {PLANTED} b {PLANTED}", PLANTED) == "a [redacted] b [redacted]"
    assert SS.redact("nothing here", None, "") == "nothing here"


def test_status_never_includes_the_value_and_only_checks_presence_when_it_can(tmp_path):
    plain = SS.CredentialStore(RecordingBackend(), "lab-x")
    plain.set("anthropic", PLANTED)
    assert PLANTED not in json.dumps(plain.status("anthropic")) and plain.status("anthropic")["stored"] is True
    with_exists = SS.CredentialStore(ExistsBackend(), "lab-x")
    with_exists.set("anthropic", PLANTED)
    with_exists.backend.calls.clear()
    status = with_exists.status("anthropic")
    assert status["stored"] is True and PLANTED not in json.dumps(status)
    assert with_exists.backend.calls == [("exists", "eidos/lab-x/anthropic", "ANTHROPIC_API_KEY")]  # never read the secret
    assert set(status) == {"provider", "stored", "env_var", "service", "backend"}


def test_set_and_delete_results_do_not_include_the_value(store):
    assert PLANTED not in json.dumps(store.set("anthropic", PLANTED))
    assert PLANTED not in json.dumps(store.delete("anthropic"))


def test_openai_and_openai_compatible_share_the_env_var_and_load_all_prefers_the_first_stored(store):
    store.set("openai-compatible", "sk-compat")
    assert store.load_all() == {"OPENAI_API_KEY": "sk-compat"}  # only the second is stored: it is used
    store.set("openai", "sk-openai")
    assert store.load_all() == {"OPENAI_API_KEY": "sk-openai"}  # both stored: openai (first in PROVIDERS) wins
    store.set("anthropic", KEY)
    assert store.load_all() == {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "sk-openai"}
    # they are still two separate items, each with its own status
    assert store.status("openai")["stored"] and store.status("openai-compatible")["stored"]
    store.delete("openai")
    assert store.load_all() == {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "sk-compat"}


def test_load_all_does_not_read_the_second_openai_item_once_the_first_provided_the_value(store):
    store.set("openai", "sk-openai")
    store.set("openai-compatible", "sk-compat")
    store.backend.calls.clear()
    store.load_all()
    read = [c[1] for c in store.backend.calls]
    assert f"eidos/{store.lab}/openai-compatible" not in read


def test_a_backend_failure_on_set_is_redacted_by_the_store(tmp_path):
    store = SS.CredentialStore(LeakyBackend(), "lab-x")
    with pytest.raises(SS.SecretStoreError) as raised:
        store.set("anthropic", PLANTED)
    assert PLANTED not in str(raised.value) and "[redacted]" in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__  # the leaky original is not chained


# --------------------------------------------------------------------------------------------------------------
# 2. select_backend
# --------------------------------------------------------------------------------------------------------------

def test_select_backend_picks_the_macos_keychain_on_darwin(monkeypatch):
    _fake_platform(monkeypatch, "darwin", security=True)
    backend = REAL_SELECT_BACKEND()
    assert isinstance(backend, SS.MacKeychain) and backend.keychain is None and backend.name == "macos-keychain"


def test_select_backend_honours_eidos_keychain_on_darwin(monkeypatch):
    _fake_platform(monkeypatch, "darwin", security=True, environ={"EIDOS_KEYCHAIN": "/somewhere/lab.keychain-db"})
    assert REAL_SELECT_BACKEND().keychain == "/somewhere/lab.keychain-db"


def test_select_backend_picks_libsecret_on_linux_with_secret_tool(monkeypatch):
    _fake_platform(monkeypatch, "linux", secret_tool="/usr/bin/secret-tool")
    backend = REAL_SELECT_BACKEND()
    assert isinstance(backend, SS.SecretTool) and backend.binary == "/usr/bin/secret-tool"
    assert backend.name == "libsecret"


@pytest.mark.parametrize("platform, security, secret_tool", [
    ("linux", False, None),                      # Linux without libsecret-tools
    ("linux", True, None),                       # /usr/bin/security only counts on macOS
    ("darwin", False, None),                     # macOS without /usr/bin/security
    ("darwin", False, "/usr/bin/secret-tool"),   # secret-tool is only used on Linux
    ("win32", True, "/usr/bin/secret-tool"),     # anything else
])
def test_select_backend_without_an_os_secret_store_fails_closed_with_no_plaintext_fallback(
        monkeypatch, platform, security, secret_tool):
    _fake_platform(monkeypatch, platform, security=security, secret_tool=secret_tool)
    with pytest.raises(SS.SecretStoreUnavailable) as raised:
        REAL_SELECT_BACKEND()
    message = str(raised.value)
    assert "no OS secret store" in message
    assert "never stores provider keys in plaintext" in message and "--credentials-stdin" in message
    assert isinstance(raised.value, SS.SecretStoreError)


# --------------------------------------------------------------------------------------------------------------
# 3. `lab credentials set|status|delete`
# --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A lab home, a shared in-memory secret store and a clean provider environment."""
    home = tmp_path / "lab-home"
    home.mkdir()
    backend = RecordingBackend()
    monkeypatch.setattr(lab_cli, "_cfg", lambda: SimpleNamespace(home=str(home)))
    monkeypatch.setattr(SS, "select_backend", lambda: backend)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return SimpleNamespace(home=home, backend=backend, service=lambda provider: f"eidos/{SS.lab_id(home)}/{provider}")


def test_cli_set_reads_the_key_from_stdin_and_never_echoes_it(lab):
    result = _lab("credentials", "set", "anthropic", input=KEY + "\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "provider": "anthropic", "stored": True, "env_var": "ANTHROPIC_API_KEY",
        "service": lab.service("anthropic"), "backend": "recording"}
    assert KEY not in result.output
    assert lab.backend.items == {(lab.service("anthropic"), "ANTHROPIC_API_KEY"): KEY}  # newline stripped


def test_cli_set_on_a_terminal_uses_a_hidden_prompt_instead_of_stdin(lab, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True, read=lambda: pytest.fail("read stdin")))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": prompts.append(prompt) or KEY)
    lab_cli.credentials_set("anthropic")
    assert len(prompts) == 1 and "anthropic" in prompts[0] and "hidden" in prompts[0] and KEY not in prompts[0]
    assert lab.backend.items == {(lab.service("anthropic"), "ANTHROPIC_API_KEY"): KEY}
    assert KEY not in capsys.readouterr().out


def test_cli_status_reports_presence_without_the_value(lab):
    before = _lab("credentials", "status", "anthropic")
    assert before.exit_code == 0 and json.loads(before.stdout)["stored"] is False
    _lab("credentials", "set", "anthropic", input=KEY)
    after = _lab("credentials", "status", "anthropic")
    assert after.exit_code == 0, after.output
    assert json.loads(after.stdout)["stored"] is True and json.loads(after.stdout)["env_var"] == "ANTHROPIC_API_KEY"
    assert KEY not in after.output


def test_cli_delete_removes_the_key_and_status_turns_false(lab):
    _lab("credentials", "set", "anthropic", input=KEY)
    deleted = _lab("credentials", "delete", "anthropic")
    assert deleted.exit_code == 0 and json.loads(deleted.stdout)["deleted"] is True and KEY not in deleted.output
    assert json.loads(_lab("credentials", "status", "anthropic").stdout)["stored"] is False
    again = _lab("credentials", "delete", "anthropic")
    assert again.exit_code == 0 and json.loads(again.stdout)["deleted"] is False
    assert lab.backend.items == {}


@pytest.mark.parametrize("bad", ["sk with spaces", "sk-with-'quote", 'sk-with-"dq', "sk-back\\slash", "", "  \n"])
def test_cli_set_with_an_invalid_key_fails_without_echoing_it_or_storing_it(lab, bad):
    marked = bad.replace("sk", PLANTED) if bad.strip() else bad
    result = _lab("credentials", "set", "anthropic", input=marked)
    assert result.exit_code != 0
    assert PLANTED not in result.output
    assert "printable ASCII" in _flat(result.output)
    assert lab.backend.items == {}


def test_cli_has_no_option_or_argument_that_accepts_the_key(lab):
    credentials = get_command(app).commands["credentials"]
    for name in ("set", "status", "delete"):
        assert [p.name for p in credentials.commands[name].params if p.name != "help"] == ["provider"]
    assert credentials.commands["set"].params[0].param_type_name == "argument"
    attempts = [["credentials", "set", "anthropic", "sk-xyz-arg"]] + [
        ["credentials", "set", "anthropic", flag, "sk-xyz-opt"] for flag in ("--key", "--value", "--api-key", "--password")]
    for args in attempts:
        result = _lab(*args, input=KEY)
        assert result.exit_code == 2, (args, result.output)  # a usage error, nothing was stored
        assert re.search(r"No such option|unexpected extra argument", result.output), result.output
    assert lab.backend.items == {}


def test_cli_unknown_provider_is_rejected(lab):
    result = _lab("credentials", "set", "gemini", input=KEY)
    assert result.exit_code != 0 and "unknown provider" in _flat(result.output) and KEY not in result.output
    assert lab.backend.items == {}


@pytest.mark.parametrize("args", [("set", "anthropic"), ("status", "anthropic"), ("delete", "anthropic")])
def test_cli_unavailable_store_gives_a_clear_error_and_a_non_zero_exit(lab, monkeypatch, args):
    _fake_platform(monkeypatch, "linux")  # no secret-tool: the real select_backend refuses
    monkeypatch.setattr(SS, "select_backend", REAL_SELECT_BACKEND)
    result = _lab("credentials", *args, input=KEY)
    assert result.exit_code != 0
    text = _flat(result.output)
    assert "no OS secret store" in text and "--credentials-stdin" in text
    assert KEY not in result.output
    assert lab.backend.items == {}


def test_cli_two_lab_homes_keep_separate_keys(lab, tmp_path, monkeypatch):
    _lab("credentials", "set", "anthropic", input=KEY)
    other = tmp_path / "other-lab"
    other.mkdir()
    monkeypatch.setattr(lab_cli, "_cfg", lambda: SimpleNamespace(home=str(other)))
    status = json.loads(_lab("credentials", "status", "anthropic").stdout)
    assert status["stored"] is False and status["service"] == f"eidos/{SS.lab_id(other)}/anthropic"


def test_typer_does_not_print_local_variables_in_tracebacks():
    # An unexpected error while the key is in a local variable must not print the key (older typer defaulted to True).
    assert app.pretty_exceptions_show_locals is False


# --------------------------------------------------------------------------------------------------------------
# 4. `lab start` and stored keys
# --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def start_lab(lab, monkeypatch):
    """``lab`` plus a fake ``eidos.supervisor.ops.start`` that records the ``credentials`` it was given."""
    lab.starts = []

    def fake_start(cfg, *, wait=120.0, credentials=None):
        lab.starts.append(SimpleNamespace(cfg=cfg, credentials=dict(credentials or {}), passed=credentials))
        return {"phase": "running", "pid": 4242}

    monkeypatch.setattr(sup_ops, "start", fake_start)
    return lab


def _store_for(lab) -> SS.CredentialStore:
    return SS.CredentialStore(lab.backend, SS.lab_id(lab.home))


def test_start_passes_the_stored_key_through_and_prints_only_names_and_sources(start_lab):
    _store_for(start_lab).set("anthropic", KEY)
    result = _lab("start")
    assert result.exit_code == 0, result.output
    [call] = start_lab.starts
    assert call.credentials == {"ANTHROPIC_API_KEY": KEY}
    assert call.cfg.home == str(start_lab.home)
    assert json.loads(result.stdout) == {"phase": "running", "pid": 4242,
                                        "credentials": {"ANTHROPIC_API_KEY": "os-secret-store"}}
    assert KEY not in result.output
    assert call.passed == {}  # the cli empties its copy once the supervisor has been handed the keys


def test_start_passes_every_stored_provider_key(start_lab):
    store = _store_for(start_lab)
    store.set("anthropic", KEY)
    store.set("openai-compatible", "sk-compat")
    result = _lab("start")
    assert start_lab.starts[0].credentials == {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": "sk-compat"}
    assert json.loads(result.stdout)["credentials"] == {"ANTHROPIC_API_KEY": "os-secret-store",
                                                        "OPENAI_API_KEY": "os-secret-store"}


def test_start_without_stored_keys_passes_nothing(start_lab):
    result = _lab("start")
    assert result.exit_code == 0 and start_lab.starts[0].credentials == {}
    assert json.loads(result.stdout)["credentials"] == {}


def test_start_with_credentials_stdin_does_not_consult_the_store(start_lab, monkeypatch):
    _store_for(start_lab).set("anthropic", "sk-stored-should-not-be-used")
    start_lab.backend.calls.clear()
    selected = []
    monkeypatch.setattr(SS, "select_backend", lambda: selected.append(1) or start_lab.backend)
    result = _lab("start", "--credentials-stdin", input="ANTHROPIC_API_KEY=sk-from-stdin\nEXTRA_API_KEY=extra\n")
    assert result.exit_code == 0, result.output
    assert start_lab.starts[0].credentials == {"ANTHROPIC_API_KEY": "sk-from-stdin", "EXTRA_API_KEY": "extra"}
    assert selected == [] and start_lab.backend.calls == []
    assert json.loads(result.stdout)["credentials"] == {"ANTHROPIC_API_KEY": "stdin", "EXTRA_API_KEY": "stdin"}
    assert "sk-from-stdin" not in result.output and "extra" not in json.loads(result.stdout)["credentials"].values()


def test_start_with_empty_credentials_stdin_still_ignores_the_store(start_lab):
    _store_for(start_lab).set("anthropic", KEY)
    result = _lab("start", "--credentials-stdin", input="")
    assert result.exit_code == 0 and start_lab.starts[0].credentials == {}


@pytest.mark.parametrize("line", ["no-equals-sign", f"lower_case={PLANTED}", f"1BAD={PLANTED}", f"HAS SPACE={PLANTED}"])
def test_start_rejects_malformed_credentials_stdin_without_echoing_the_value(start_lab, line):
    result = _lab("start", "--credentials-stdin", input=line + "\n")
    assert result.exit_code != 0 and start_lab.starts == []
    assert PLANTED not in result.output and "NAME=VALUE" in _flat(result.output)


def test_an_exported_variable_wins_over_the_stored_key(start_lab, monkeypatch):
    store = _store_for(start_lab)
    store.set("anthropic", KEY)
    store.set("openai", "sk-openai-stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exported")
    result = _lab("start")
    assert result.exit_code == 0, result.output
    # The exported key is not passed as a credential (ops.start picks it up from the environment itself);
    # the stored anthropic key is not used; the other stored key still is.
    assert start_lab.starts[0].credentials == {"OPENAI_API_KEY": "sk-openai-stored"}
    assert json.loads(result.stdout)["credentials"] == {"OPENAI_API_KEY": "os-secret-store"}
    assert KEY not in result.output and "sk-exported" not in result.output


def test_start_proceeds_without_stored_credentials_when_no_store_is_available(start_lab, monkeypatch):
    _fake_platform(monkeypatch, "linux")
    monkeypatch.setattr(SS, "select_backend", REAL_SELECT_BACKEND)
    result = _lab("start")
    assert result.exit_code == 0, result.output
    assert start_lab.starts[0].credentials == {}
    assert json.loads(result.stdout)["credentials"] == {}
    assert "could not be read" not in result.output  # an absent store is normal, not a warning


def test_start_proceeds_with_a_note_when_the_store_cannot_be_read(start_lab, monkeypatch):
    class Unreadable(SS.MemoryBackend):
        def get(self, service, account):
            raise SS.SecretStoreError("keychain read failed for eidos/lab-x/anthropic (36): denied")

    monkeypatch.setattr(SS, "select_backend", lambda: Unreadable())
    result = _lab("start")
    assert result.exit_code == 0, result.output
    assert start_lab.starts[0].credentials == {}
    assert "stored provider keys could not be read" in result.stderr and "denied" in result.stderr
    assert json.loads(result.stdout)["credentials"] == {}


# --------------------------------------------------------------------------------------------------------------
# 5. The value never leaks
# --------------------------------------------------------------------------------------------------------------

def test_planted_key_never_reaches_stdout_stderr_or_logs_on_the_cli_paths(start_lab, caplog, capfd, monkeypatch):
    caplog.set_level(logging.DEBUG)
    steps = [
        (["credentials", "set", "anthropic"], PLANTED + "\n"),
        (["credentials", "status", "anthropic"], None),
        (["start"], None),
        (["start", "--credentials-stdin"], f"ANTHROPIC_API_KEY={PLANTED}\n"),
        (["start", "--credentials-stdin"], f"lower_case={PLANTED}\n"),
        (["start", "--credentials-stdin"], PLANTED + "\n"),
        (["credentials", "set", "anthropic"], PLANTED + " with spaces"),
        (["credentials", "set", "anthropic"], PLANTED + "'"),
        (["credentials", "set", "gemini"], PLANTED),
        (["credentials", "delete", "anthropic"], None),
        (["credentials", "status", "anthropic"], None),
    ]
    seen = []
    for args, stdin in steps:
        result = _lab(*args, input=stdin)
        seen += [result.output, result.stdout, result.stderr, repr(result.exception)]
    # the key really did flow through the stored-key start (so the absence below is not vacuous)
    assert any(call.credentials.get("ANTHROPIC_API_KEY") == PLANTED for call in start_lab.starts)
    captured = capfd.readouterr()
    for blob in (*seen, captured.out, captured.err, caplog.text):
        assert PLANTED not in blob


def test_planted_key_never_leaks_when_the_backend_fails_loudly_with_the_value_in_its_message(lab, caplog, capfd, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(SS, "select_backend", lambda: LeakyBackend())
    result = _lab("credentials", "set", "anthropic", input=PLANTED)
    assert result.exit_code != 0
    captured = capfd.readouterr()
    for blob in (result.output, result.stdout, result.stderr, repr(result.exception), captured.out, captured.err,
                 caplog.text):
        assert PLANTED not in blob
    assert "[redacted]" in _flat(result.output)


@pytest.mark.parametrize("make_backend, set_failure", [
    (lambda: SS.MacKeychain(keychain="/tmp/t.keychain-db"), _proc(1, "", f"security: could not add password {PLANTED}")),
    (lambda: SS.MacKeychain(), _proc(0, "", f'error: add-generic-password -w "{PLANTED}" failed')),
    (lambda: SS.SecretTool(), _proc(1, "", f"secret-tool: cannot store {PLANTED}")),
    (lambda: SS.SecretTool(), OSError(f"[Errno 2] cannot execute with {PLANTED}")),
])
def test_tool_failure_messages_that_echo_the_value_are_redacted(monkeypatch, make_backend, set_failure):
    _fake_run(monkeypatch, set_failure)
    backend = make_backend()
    with pytest.raises(SS.SecretStoreError) as direct:
        backend.set("eidos/lab-x/anthropic", "ANTHROPIC_API_KEY", PLANTED, "Eidos label")
    assert PLANTED not in str(direct.value)
    with pytest.raises(SS.SecretStoreError) as via_store:
        SS.CredentialStore(backend, "lab-x").set("anthropic", PLANTED)
    assert PLANTED not in str(via_store.value) and PLANTED not in repr(via_store.value)


def test_a_failure_near_the_truncation_limit_is_redacted_before_truncating(monkeypatch):
    _fake_run(monkeypatch, _proc(1, "", "x" * 290 + PLANTED))
    with pytest.raises(SS.SecretStoreError) as raised:
        SS.MacKeychain().set("eidos/lab-x/anthropic", "ANTHROPIC_API_KEY", PLANTED, "Eidos label")
    assert PLANTED not in str(raised.value) and PLANTED[:8] not in str(raised.value)


@pytest.mark.parametrize("backend", [SS.MacKeychain(), SS.SecretTool()], ids=["macos", "libsecret"])
def test_read_errors_do_not_include_stdout_that_may_hold_the_secret(monkeypatch, backend):
    _fake_run(monkeypatch, _proc(36, PLANTED, f"locked; partial output {PLANTED}"))
    with pytest.raises(SS.SecretStoreError) as raised:
        backend.get("eidos/lab-x/anthropic", "ANTHROPIC_API_KEY")
    assert PLANTED not in str(raised.value)


def test_mac_keychain_set_keeps_the_value_out_of_argv_and_env(monkeypatch, tmp_path):
    run = _fake_run(monkeypatch)
    keychain = str(tmp_path / "my keychain.keychain-db")
    SS.MacKeychain(keychain=keychain).set("eidos/lab-x/anthropic", "ANTHROPIC_API_KEY", PLANTED, "Eidos lab-x key")
    [(argv, kwargs)] = run.calls
    assert argv == [SECURITY, "-i"]
    assert all(PLANTED not in part for part in argv) and PLANTED not in json.dumps(kwargs["env"])
    command = kwargs["input"]  # `security -i` reads its command, and so the value, from stdin
    assert PLANTED in command and command.endswith("\n")
    assert command.startswith("add-generic-password -U -s \"eidos/lab-x/anthropic\" -a \"ANTHROPIC_API_KEY\" -l ")
    assert f'-w "{PLANTED}"' in command and command.rstrip("\n").endswith(f'"{keychain}"')
    assert kwargs["capture_output"] is True and kwargs["text"] is True


def test_secret_tool_set_keeps_the_value_out_of_argv_and_env(monkeypatch):
    run = _fake_run(monkeypatch)
    SS.SecretTool("/usr/bin/secret-tool").set("eidos/lab-x/anthropic", "ANTHROPIC_API_KEY", PLANTED, "Eidos lab-x key")
    [(argv, kwargs)] = run.calls
    assert argv == ["/usr/bin/secret-tool", "store", "--label=Eidos lab-x key",
                    "service", "eidos/lab-x/anthropic", "account", "ANTHROPIC_API_KEY"]
    assert all(PLANTED not in part for part in argv) and PLANTED not in json.dumps(kwargs["env"])
    assert kwargs["input"] == PLANTED


def test_the_value_is_in_no_argv_through_the_whole_credential_store_lifecycle(monkeypatch):
    run = _fake_run(monkeypatch, _proc(0, PLANTED + "\n", ""))  # every read returns the value; writes succeed
    for backend in (SS.MacKeychain(keychain="/tmp/t.keychain-db"), SS.SecretTool()):
        store = SS.CredentialStore(backend, "lab-x")
        store.set("anthropic", PLANTED)
        store.status("anthropic")
        store.load_all()
        store.delete("anthropic")
    assert run.calls
    for argv, _kwargs in run.calls:
        assert all(PLANTED not in part for part in argv), argv


def test_subprocess_environment_is_scrubbed_of_provider_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLANTED)
    run = _fake_run(monkeypatch)
    SS.MacKeychain().exists("eidos/lab-x/anthropic", "ANTHROPIC_API_KEY")
    [(_argv, kwargs)] = run.calls
    assert "ANTHROPIC_API_KEY" not in kwargs["env"] and PLANTED not in json.dumps(kwargs["env"])


# MacKeychain / SecretTool command semantics (fake subprocess)

def test_mac_keychain_get_exists_delete_follow_the_security_exit_codes(monkeypatch):
    kc = SS.MacKeychain(keychain="/tmp/t.keychain-db")
    run = _fake_run(monkeypatch, _proc(0, "the-value\n"), _proc(44), _proc(0), _proc(44), _proc(0), _proc(44),
                    _proc(45, "", "denied"))
    assert kc.get("svc", "ACC") == "the-value"
    assert kc.get("svc", "ACC") is None                    # 44: errSecItemNotFound
    assert kc.exists("svc", "ACC") is True
    assert kc.exists("svc", "ACC") is False
    assert kc.delete("svc", "ACC") is True
    assert kc.delete("svc", "ACC") is False
    with pytest.raises(SS.SecretStoreError, match="keychain delete failed"):
        kc.delete("svc", "ACC")
    assert run.calls[0][0] == [SECURITY, "find-generic-password", "-s", "svc", "-a", "ACC", "-w", "/tmp/t.keychain-db"]
    assert run.calls[2][0] == [SECURITY, "find-generic-password", "-s", "svc", "-a", "ACC", "/tmp/t.keychain-db"]  # no -w
    assert run.calls[4][0] == [SECURITY, "delete-generic-password", "-s", "svc", "-a", "ACC", "/tmp/t.keychain-db"]


def test_mac_keychain_get_raises_for_other_failures(monkeypatch):
    _fake_run(monkeypatch, _proc(36, "", "User interaction is not allowed."))
    with pytest.raises(SS.SecretStoreError, match=r"keychain read failed .*\(36\).*not allowed"):
        SS.MacKeychain().get("svc", "ACC")


def test_mac_keychain_set_fails_when_security_exits_zero_but_reports_an_error(monkeypatch):
    _fake_run(monkeypatch, _proc(0, "", "security: SecKeychainItemCreateFromContent: Error: no such keychain"))
    with pytest.raises(SS.SecretStoreError, match="keychain write failed"):
        SS.MacKeychain().set("svc", "ACC", KEY, "label")


@pytest.mark.parametrize("field", ["service", "account", "label", "keychain"])
@pytest.mark.parametrize("bad", ['quote"inside', "back\\slash", "new\nline"])
def test_mac_keychain_set_refuses_names_that_could_break_the_command_line(monkeypatch, field, bad):
    run = _fake_run(monkeypatch)
    names = {"service": "svc", "account": "ACC", "label": "label", "keychain": "/tmp/t.keychain-db"}
    names[field] = bad
    backend = SS.MacKeychain(keychain=names["keychain"])
    with pytest.raises(SS.SecretStoreError, match="must not contain quotes or newlines"):
        backend.set(names["service"], names["account"], KEY, names["label"])
    assert run.calls == []  # nothing was sent to security


def test_secret_tool_get_and_delete_semantics(monkeypatch):
    tool = SS.SecretTool("/usr/bin/secret-tool")
    run = _fake_run(monkeypatch, _proc(1), _proc(0, "the-value"), _proc(1, "", "no Secret Service"))
    assert tool.get("svc", "ACC") is None                  # exit 1, no output: nothing matches
    assert tool.get("svc", "ACC") == "the-value"
    with pytest.raises(SS.SecretStoreError, match="lookup failed.*no Secret Service"):
        tool.get("svc", "ACC")
    assert run.calls[0][0] == ["/usr/bin/secret-tool", "lookup", "service", "svc", "account", "ACC"]
    # delete: item exists -> clear and report True; item missing -> False even if `clear` complains
    _fake_run(monkeypatch, _proc(0, "v"), _proc(0))
    assert tool.delete("svc", "ACC") is True
    _fake_run(monkeypatch, _proc(1), _proc(1, "", "nothing to clear"))
    assert tool.delete("svc", "ACC") is False
    _fake_run(monkeypatch, _proc(0, "v"), _proc(1, "", "clear exploded"))
    with pytest.raises(SS.SecretStoreError, match="clear failed"):
        tool.delete("svc", "ACC")


def test_secret_tool_protocol_against_a_fake_binary(tmp_path, monkeypatch):
    """The real ``_run`` and a stand-in ``secret-tool`` script: lookup/store/clear with the value on stdin."""
    script = tmp_path / "bin" / "secret-tool"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        'd="$(dirname "$0")/items"; mkdir -p "$d"\n'
        'cmd=$1; shift\n'
        '[ "$cmd" = store ] && shift\n'                     # drop --label=...
        'f="$d/$(printf "%s" "$2|$4" | tr "/ " "__")"\n'    # $2 = service value, $4 = account value
        'case "$cmd" in\n'
        '  lookup) [ -f "$f" ] || exit 1; cat "$f";;\n'
        '  store) cat > "$f";;\n'
        '  clear) rm -f "$f";;\n'
        'esac\n')
    script.chmod(0o755)
    monkeypatch.setattr(SS, "subprocess", subprocess)  # this one test uses a real process: our own script
    store = SS.CredentialStore(SS.SecretTool(str(script)), SS.lab_id(tmp_path / "lab"))
    assert store.status("anthropic")["stored"] is False
    store.set("anthropic", KEY)
    assert store.status("anthropic")["stored"] is True
    assert store.load_all() == {"ANTHROPIC_API_KEY": KEY}
    [stored_file] = (script.parent / "items").iterdir()
    assert stored_file.read_text() == KEY  # arrived on stdin
    assert store.delete("anthropic")["deleted"] is True
    assert store.delete("anthropic")["deleted"] is False
    assert store.load_all() == {}


# --------------------------------------------------------------------------------------------------------------
# 6. Real macOS Keychain (throwaway keychain file)
# --------------------------------------------------------------------------------------------------------------

_HAVE_SECURITY = sys.platform == "darwin" and os.path.exists(SECURITY)
needs_security = pytest.mark.skipif(not _HAVE_SECURITY, reason="needs macOS and /usr/bin/security")


@pytest.fixture
def throwaway_keychain(tmp_path):
    """A brand-new keychain file under tmp_path. Only this path is ever handed to ``security``; it is deleted
    (which also removes it from the keychain search list) when the test ends."""
    path = str(tmp_path / f"eidos-test-{uuid.uuid4().hex[:8]}.keychain-db")
    password = uuid.uuid4().hex
    try:
        for argv in ([SECURITY, "create-keychain", "-p", password, path],
                     [SECURITY, "unlock-keychain", "-p", password, path]):
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, f"{argv[1]} failed: {done.stderr.strip()}"
        yield path
    finally:
        deleted = subprocess.run([SECURITY, "delete-keychain", path], capture_output=True, text=True, timeout=60)
        leftover = os.path.exists(path)
        if leftover:
            os.unlink(path)
        assert deleted.returncode == 0 and not leftover, f"could not delete the throwaway keychain: {deleted.stderr.strip()}"


REAL_KEYCHAIN_VALUES = {
    "typical": "sk-ant-api03-AbC_123-xyz",
    "leading-dash": "-leading-dash-key",
    "leading-hash": "#not-a-comment",
    "punctuation": "k#!$%&()*+,-./:;<=>?@[]^_`{|}~",
    "digits-only": "1234567890",
    "long-1024": ("Ab1_" * 256),
}


@needs_security
@pytest.mark.parametrize("value", REAL_KEYCHAIN_VALUES.values(), ids=REAL_KEYCHAIN_VALUES.keys())
def test_real_keychain_round_trip_through_credential_store(throwaway_keychain, tmp_path, value):
    backend = SS.MacKeychain(keychain=throwaway_keychain)
    store = SS.CredentialStore(backend, SS.lab_id(tmp_path / "lab"))
    service, env = store.service("anthropic"), "ANTHROPIC_API_KEY"

    assert backend.get(service, env) is None  # a missing item reads as None, not an error
    assert backend.exists(service, env) is False
    assert store.status("anthropic")["stored"] is False and store.load_all() == {}
    assert store.delete("anthropic")["deleted"] is False

    store.set("anthropic", value)
    assert backend.exists(service, env) is True
    assert backend.get(service, env) == value
    status = store.status("anthropic")
    assert status["stored"] is True and value not in json.dumps(status)
    assert store.load_all() == {env: value}

    store.set("anthropic", "sk-replacement")  # `add-generic-password -U` updates in place
    assert backend.get(service, env) == "sk-replacement"

    assert store.delete("anthropic")["deleted"] is True
    assert backend.get(service, env) is None and backend.exists(service, env) is False
    assert store.delete("anthropic")["deleted"] is False
    assert store.load_all() == {}


def _distinctive_value(length: int) -> str:
    """Printable, no whitespace or quotes, and every 20-character window is unique (so fragments are detectable)."""
    return "".join(f"{i:04d}x" for i in range(length))[:length]


@needs_security
def test_real_keychain_stores_a_value_of_the_documented_maximum_length(throwaway_keychain, tmp_path):
    backend = SS.MacKeychain(keychain=throwaway_keychain)
    store = SS.CredentialStore(backend, SS.lab_id(tmp_path / "lab"))
    value = _distinctive_value(SS.MAX_VALUE)  # was 4096 (fixed: capped well below security's line limit)
    store.set("anthropic", value)
    assert backend.get(store.service("anthropic"), "ANTHROPIC_API_KEY") == value


@needs_security
def test_real_keychain_error_for_an_oversized_value_does_not_echo_fragments_of_it(throwaway_keychain, tmp_path):
    backend = SS.MacKeychain(keychain=throwaway_keychain)
    value = _distinctive_value(4096)  # bypasses check_value: the backend itself must not echo fragments
    try:
        backend.set("eidos/x/anthropic", "ANTHROPIC_API_KEY", value, label="t")
    except SS.SecretStoreError as exc:
        message = str(exc)
        fragments = [value[i:i + 20] for i in range(0, len(value) - 20)]
        assert not any(fragment in message for fragment in fragments), message[:200]


@needs_security
def test_real_keychain_keeps_labs_and_providers_apart(throwaway_keychain, tmp_path):
    backend = SS.MacKeychain(keychain=throwaway_keychain)
    one = SS.CredentialStore(backend, SS.lab_id(tmp_path / "one"))
    two = SS.CredentialStore(backend, SS.lab_id(tmp_path / "two"))
    one.set("anthropic", "sk-one")
    one.set("openai-compatible", "sk-compat")
    assert two.load_all() == {} and one.load_all() == {"ANTHROPIC_API_KEY": "sk-one", "OPENAI_API_KEY": "sk-compat"}
    assert one.status("openai")["stored"] is False and one.status("openai-compatible")["stored"] is True
    one.delete("anthropic")
    one.delete("openai-compatible")


@needs_security
def test_real_keychain_cli_set_status_start_delete(throwaway_keychain, tmp_path, monkeypatch):
    home = tmp_path / "lab-home"
    home.mkdir()
    started = []
    monkeypatch.setattr(lab_cli, "_cfg", lambda: SimpleNamespace(home=str(home)))
    monkeypatch.setattr(SS, "select_backend", lambda: SS.MacKeychain(keychain=throwaway_keychain))
    monkeypatch.setattr(sup_ops, "start", lambda cfg, *, credentials=None, **kw: started.append(dict(credentials)) or {"phase": "running"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = _lab("credentials", "set", "anthropic", input=PLANTED + "\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["backend"] == "macos-keychain" and PLANTED not in result.output
    status = _lab("credentials", "status", "anthropic")
    assert json.loads(status.stdout)["stored"] is True and PLANTED not in status.output
    started_result = _lab("start")
    assert started == [{"ANTHROPIC_API_KEY": PLANTED}]  # read back from the real keychain
    assert json.loads(started_result.stdout)["credentials"] == {"ANTHROPIC_API_KEY": "os-secret-store"}
    assert PLANTED not in started_result.output
    assert json.loads(_lab("credentials", "delete", "anthropic").stdout)["deleted"] is True
    assert json.loads(_lab("credentials", "status", "anthropic").stdout)["stored"] is False
    _lab("start")
    assert started[-1] == {}


# --------------------------------------------------------------------------------------------------------------
# 7. Real libsecret (Linux with a Secret Service)
# --------------------------------------------------------------------------------------------------------------

def _require_real_secret_tool() -> str:
    problems = []
    if not sys.platform.startswith("linux"):
        problems.append("not Linux")
    tool = shutil.which("secret-tool")
    if not tool:
        problems.append("`secret-tool` is not on PATH")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        problems.append("DBUS_SESSION_BUS_ADDRESS is not set (no session bus for the Secret Service)")
    if problems:
        message = "the real libsecret test needs a Secret Service: " + "; ".join(problems)
        if os.environ.get("EIDOS_REQUIRE_SECRET_TOOL") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return tool


def test_real_secret_tool_round_trip():
    tool = _require_real_secret_tool()
    backend = SS.SecretTool(tool)
    service, account = f"eidos-test/{uuid.uuid4().hex}/anthropic", "ANTHROPIC_API_KEY"
    try:
        assert backend.get(service, account) is None
        backend.set(service, account, KEY, "Eidos test item (safe to delete)")
        assert backend.get(service, account) == KEY
        backend.set(service, account, "sk-replacement", "Eidos test item (safe to delete)")
        assert backend.get(service, account) == "sk-replacement"
        assert backend.delete(service, account) is True
        assert backend.get(service, account) is None
        assert backend.delete(service, account) is False
    finally:
        with contextlib.suppress(SS.SecretStoreError):
            backend.delete(service, account)


def test_real_secret_tool_credential_store_round_trip(tmp_path):
    tool = _require_real_secret_tool()
    store = SS.CredentialStore(SS.SecretTool(tool), SS.lab_id(tmp_path / f"lab-{uuid.uuid4().hex}"))
    try:
        assert store.status("anthropic")["stored"] is False
        store.set("anthropic", KEY)
        assert store.status("anthropic")["stored"] is True
        assert store.load_all() == {"ANTHROPIC_API_KEY": KEY}
        assert store.delete("anthropic")["deleted"] is True
        assert store.load_all() == {}
    finally:
        with contextlib.suppress(SS.SecretStoreError):
            store.delete("anthropic")

