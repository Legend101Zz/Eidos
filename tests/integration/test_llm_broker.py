"""The LLM credential broker (ADR 0013) against real PostgreSQL, a real Unix-socket broker and a fake provider.

Covered: a component's calls end to end through the runner and the SDK client; refusal before any provider call
(grant request/token limits, the investigation ledger); expiry; wrong invocation (fenced or finished attempt, unknown
token); the provider key never reaching the component's environment, invocation, logs, outputs or the ledger; call
receipts and settlement; uncertain usage after a provider timeout; crash recovery of a reservation left open by a
dead broker; sealed runs get no grant unless allowed, and under Seatbelt reach only the broker socket.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from eidos.investigator.llm_broker import LlmBroker, ProviderResult, serve_unix

pytestmark = pytest.mark.pg

KEY = "sk-ant-planted-PROVIDER-KEY-" + uuid.uuid4().hex
POLICY = {"llm_broker": {"models": ["fake:echo", "fake:slow"], "max_requests": 3, "max_tokens": 600, "max_usd": 0.01,
                         "ttl_seconds": 600, "price_per_mtok": {"fake:echo": {"input": 1.0, "output": 2.0},
                                                                 "fake:slow": {"input": 1.0, "output": 2.0}}}}
MANIFEST = {"id": "gen.llm", "release": "1.0.0", "implements": "synthetic-text/1", "kind": "artifact-op",
            "entry": "m:run", "scientific_effects": ["generate-data"], "output": {"texts": "texts/1"},
            "llm": {"role": "generator", "prompt_revision": "p1", "max_calls": 3, "max_tokens": 600}}
CODE = '''
import hashlib, json, os
def run(inputs, config, *, writer=None, llm=None, context=None):
    texts = []
    for i in range(int(config.get("n", 2))):
        out = llm.complete([{"role": "user", "content": "item %d" % i}], max_tokens=50, request_key="k%d" % i)
        texts.append({"text": out.text, "call": out.call_id, "usage": out.usage})
    writer.stage_json("texts", texts, schema="texts/1")
    return {"env_names": sorted(os.environ), "same_object": context.llm is llm,
            "env_value_hashes": sorted(hashlib.sha256(v.encode()).hexdigest() for v in os.environ.values()),
            "token_in_repr": context.invocation.llm_grant["token"] in repr(llm)}
'''


class FakeProvider:
    def __init__(self, key: str, *, fail: Exception | None = None):
        self._key = key  # what a real adapter would send to the provider
        self.calls: list[tuple[str, list, int]] = []
        self.fail = fail

    def complete(self, model, messages, max_tokens):
        self.calls.append((model, messages, max_tokens))
        if self.fail is not None:
            raise self.fail
        text = "echo:" + messages[-1]["content"]
        return ProviderResult(text, input_tokens=7, output_tokens=5, request_id=f"req-{len(self.calls)}")


def _setup(lab, *, budgets=None, manifest=None):
    from test_command_path import _publish, _register_lab_env
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid, "budgets": budgets or {
        "compute_seconds": 10_000, "trials": 50, "model_tokens": 5000, "model_usd": 1.0}})
    _register_lab_env(lab, pid)
    lab.cmd(lab.admin, "update_profile_policy", {"policy": POLICY}, project=pid)
    ref = _publish(lab, pid, manifest or MANIFEST, {"m.py": CODE})["component_ref"]
    broker_pr = lab.principal(f"svc-llm-broker-{pid}", ["llm-broker"], [pid])
    launcher = lab.principal(f"svc-launcher-{pid}", ["launcher"], [pid])
    return pid, ref, broker_pr, launcher


def _socket_dir():
    d = Path(tempfile.mkdtemp(prefix="eb-", dir="/tmp"))  # AF_UNIX paths are short (104 bytes on macOS)
    return d


@pytest.fixture()
def broker_env(lab):
    from test_turn_recovery import ShimClient
    pid, ref, broker_pr, launcher = _setup(lab)
    provider = FakeProvider(KEY)
    d = _socket_dir()
    broker = LlmBroker(ShimClient(lab, broker_pr), {"fake": provider}, secrets=(KEY,))
    srv, stop = serve_unix(broker, d / "llm.sock")
    yield {"pid": pid, "ref": ref, "launcher": launcher, "provider": provider, "broker": broker, "sock": str(d / "llm.sock"),
           "broker_pr": broker_pr}
    stop()
    shutil.rmtree(d, ignore_errors=True)


def _grant(lab, env, *, run_extra=None):
    """Admit a job and issue its grant the way the runner does; returns (job, attempt, token)."""
    import hashlib
    pid = env["pid"]
    job = lab.cmd(lab.admin, "run_component", {"component_ref": env["ref"], "environment": "lab", "config": {},
                  **(run_extra or {})}, project=pid)["result"]["job_id"]
    st = lab.cmd(env["launcher"], "start_attempt", {"job_id": job}, project=pid)["result"]
    token = f"eidos_llmg_{pid}.{uuid.uuid4().hex}"
    g = lab.cmd(env["launcher"], "issue_llm_grant", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
                "token_sha256": hashlib.sha256(token.encode()).hexdigest()}, project=pid)["result"]
    return job, st, token, g


def _ask(env, token, n=1, max_tokens=50, model=None, request_key=None):
    return env["broker"].handle(token, {"messages": [{"role": "user", "content": "x" * n}], "max_tokens": max_tokens,
                                        "model": model, "request_key": request_key})


def _records(lab, pid, kind):
    r = lab.q(lab.admin, "list_records", {"kind": kind}, project=pid)
    return r.get("records") if isinstance(r, dict) else r


def test_component_calls_the_llm_through_the_broker_and_never_sees_the_key(lab, broker_env, monkeypatch, tmp_path):
    from test_turn_recovery import ShimClient

    from eidos.runtime.runner import AttemptRunner
    env, pid = broker_env, broker_env["pid"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)  # the runner's own environment holds a key: it must not leak
    job = lab.cmd(lab.admin, "run_component", {"component_ref": env["ref"], "environment": "lab",
                  "config": {"n": 2}}, project=pid)["result"]["job_id"]
    lock = lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)["lock"]
    assert lock["llm"]["models"] == ["fake:echo", "fake:slow"] and lock["llm"]["max_requests"] == 3
    root = tmp_path / "labroot"
    runner = AttemptRunner(ShimClient(lab, env["launcher"]), lab.store, lab_root=root, launcher_id=env["launcher"].id,
                           api_port=1, credential_path=str(root / "c"), broker_socket=env["sock"])
    res = runner.run_job(job, pid, activity_attempt=1)
    got = lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)
    assert res["job_status"] == "succeeded", got.get("terminal")
    receipt = got["terminal"]["receipt"]
    import hashlib
    assert hashlib.sha256(KEY.encode()).hexdigest() not in receipt["env_value_hashes"] and receipt["same_object"] is True
    assert not any("API_KEY" in n for n in receipt["env_names"]) and receipt["token_in_repr"] is False
    assert len(env["provider"].calls) == 2
    out = next(o for o in got["outputs"] if o["slot"] == "texts")
    texts = json.loads(Path(lab.store.path_for(out["digest"])).read_text())
    assert [t["text"] for t in texts] == ["echo:item 0", "echo:item 1"]
    calls = _records(lab, pid, "llm_call")
    assert sorted(c["status"] for c in calls) == ["ok", "ok"]
    body = lab.q(lab.admin, "get_record", {"kind": "llm_call", "id": texts[0]["call"]}, project=pid)["body"]
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 5} and body["provider_request_id"].startswith("req-")
    assert body["charged_tokens"] == 12 and body["response_digest"].startswith("sha256:")
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["model_tokens"]["used"] == 24 and b["model_tokens"]["reserved"] == 0 and b["model_tokens"]["uncertain"] == 0
    grant = _records(lab, pid, "llm_grant")[0]
    assert grant["status"] == "closed"  # the grant ended with its attempt
    # The key is nowhere: invocation, attempt files, outputs, records, events.
    for p in root.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(errors="ignore"), p
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        dump = json.dumps([tx.events_after(pid, 0, limit=100000)], default=str)
        dump += json.dumps(tx.c.execute(__import__("sqlalchemy").text(
            "SELECT body FROM records WHERE project_id=:p"), {"p": pid}).fetchall(), default=str)
    assert KEY not in dump
    inv = json.loads(next(root.rglob("invocation.json")).read_text())
    assert inv["llm_grant"]["socket"] == env["sock"] and inv["llm_grant"]["token"].startswith(f"eidos_llmg_{pid}.")


def test_requests_beyond_the_grant_or_the_ledger_are_refused_before_any_provider_call(lab, broker_env):
    env = broker_env
    job, st, token, g = _grant(lab, env)
    assert g["max_requests"] == 3 and g["max_tokens"] == 600
    for _ in range(3):
        assert _ask(env, token)[0] == 200
    status, err = _ask(env, token)
    assert status == 403 and err["error"]["code"] == "GRANT_EXHAUSTED" and len(env["provider"].calls) == 3
    # token limit: a single request whose maximum exceeds what is left
    job2, st2, token2, _ = _grant(lab, env)
    status, err = _ask(env, token2, max_tokens=700)
    assert status == 403 and err["error"]["code"] == "GRANT_EXHAUSTED" and len(env["provider"].calls) == 3
    status, err = _ask(env, token2, model="anthropic:claude-haiku-5-5")
    assert err["error"]["code"] == "MODEL_NOT_ALLOWED" and len(env["provider"].calls) == 3


def test_the_investigation_ledger_refuses_before_the_provider(lab):
    from test_turn_recovery import ShimClient
    pid, ref, broker_pr, launcher = _setup(lab, budgets={"compute_seconds": 10_000, "trials": 50,
                                                         "model_tokens": 80, "model_usd": 1.0})
    provider = FakeProvider(KEY)
    env = {"pid": pid, "ref": ref, "launcher": launcher, "provider": provider,
           "broker": LlmBroker(ShimClient(lab, broker_pr), {"fake": provider}, secrets=(KEY,))}
    job, st, token, _ = _grant(lab, env)
    assert _ask(env, token, max_tokens=50)[0] == 200  # reserves 51, settles 12
    status, err = _ask(env, token, max_tokens=70)  # 71 more would exceed the ledger's 80
    assert status == 403 and err["error"]["code"] == "BUDGET_EXHAUSTED" and len(provider.calls) == 1
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["model_tokens"]["used"] == 12 and b["model_tokens"]["reserved"] == 0


def test_expired_unknown_and_wrong_invocation_grants_are_refused(lab, broker_env):
    env, pid = broker_env, broker_env["pid"]
    job, st, token, _ = _grant(lab, env, run_extra={"llm": {"ttl_seconds": 1}})
    time.sleep(1.2)
    status, err = _ask(env, token)
    assert status == 403 and err["error"]["code"] == "GRANT_EXPIRED"
    assert _ask(env, f"eidos_llmg_{pid}.not-a-grant")[1]["error"]["code"] == "GRANT_INVALID"
    assert _ask(env, "sk-something-else")[0] == 401
    # a new attempt fences the old grant
    job, st, token, _ = _grant(lab, env)
    assert _ask(env, token)[0] == 200
    lab.cmd(env["launcher"], "start_attempt", {"job_id": job}, project=pid)
    status, err = _ask(env, token)
    assert err["error"]["code"] == "WRONG_INVOCATION"
    # a finished attempt closes its grant
    job, st, token, _ = _grant(lab, env)
    lab.cmd(env["launcher"], "finish_attempt", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
            "status": "failed", "final": True, "error": {"category": "user-code"}}, project=pid)
    assert _ask(env, token)[1]["error"]["code"] in ("GRANT_INVALID", "WRONG_INVOCATION")
    assert len(env["provider"].calls) == 1


def test_a_retried_request_key_returns_the_same_call(lab, broker_env):
    env = broker_env
    job, st, token, _ = _grant(lab, env)
    a = _ask(env, token, request_key="r1")
    b = _ask(env, token, request_key="r1")
    assert a[0] == b[0] == 200 and b[1].get("replayed") is True and b[1]["call_id"] == a[1]["call_id"]
    assert len(env["provider"].calls) == 1


def test_provider_timeout_is_charged_as_uncertain_with_the_reserved_maximum(lab, broker_env):
    env, pid = broker_env, broker_env["pid"]
    env["provider"].fail = TimeoutError(f"read timed out talking with key {KEY}")
    job, st, token, _ = _grant(lab, env)
    status, err = _ask(env, token, n=8, max_tokens=50)
    assert status == 504 and err["error"]["code"] == "OUTCOME_UNKNOWN" and KEY not in json.dumps(err)
    call = lab.q(lab.admin, "get_record", {"kind": "llm_call", "id": err["error"]["call_id"]}, project=pid)
    assert call["status"] == "uncertain" and call["body"]["uncertain"] and call["body"]["charged_tokens"] == 53
    assert KEY not in json.dumps(call)
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["model_tokens"]["uncertain"] == 53 and b["model_tokens"]["reserved"] == 0


def test_a_reservation_left_open_by_a_dead_broker_is_settled_as_uncertain_when_the_attempt_ends(lab, broker_env):
    import hashlib
    env, pid = broker_env, broker_env["pid"]
    job, st, token, _ = _grant(lab, env)
    # The broker reserved, then died before calling the provider or settling.
    r = lab.cmd(env["broker_pr"], "broker_reserve_call", {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "max_tokens": 100, "input_chars": 40, "model": "fake:echo"}, project=pid)["result"]
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["model_tokens"]["reserved"] == 111
    lab.cmd(env["launcher"], "finish_attempt", {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"],
            "status": "succeeded"}, project=pid)
    call = lab.q(lab.admin, "get_record", {"kind": "llm_call", "id": r["call_id"]}, project=pid)
    assert call["status"] == "uncertain" and call["body"]["settled_by"] == "attempt-finished"
    b = lab.q(lab.admin, "budgets", {"scope": f"project:{pid}"}, project=pid)["budgets"]
    assert b["model_tokens"]["reserved"] == 0 and b["model_tokens"]["uncertain"] == 111
    assert _records(lab, pid, "llm_grant")[0]["status"] == "closed"


def test_sealed_runs_get_no_grant_unless_allowed(lab, broker_env):
    env, pid = broker_env, broker_env["pid"]
    for profile in ("linux:bwrap-sealed", "macos:seatbelt-sealed"):
        job = lab.cmd(lab.admin, "run_component", {"component_ref": env["ref"], "environment": "lab",
                      "isolation_profile": profile, "config": {}}, project=pid)["result"]["job_id"]
        assert "llm" not in lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)["lock"]
        job = lab.cmd(lab.admin, "run_component", {"component_ref": env["ref"], "environment": "lab",
                      "isolation_profile": profile, "allow_llm_broker": True, "config": {}},
                      project=pid)["result"]["job_id"]
        assert lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)["lock"]["llm"]["sealed_allowed"] is True


def test_launch_specs_open_only_the_broker_socket():
    from eidos.runtime.launcher import LaunchSpec, bwrap_argv, seatbelt_policy
    s = LaunchSpec(argv=["py"], cwd="/w", attempt_dir="/a", profile="linux:bwrap-sealed", sockets=["/lab/run/b.sock"])
    argv = bwrap_argv(s)
    assert "--unshare-all" in argv and argv[argv.index("/lab/run/b.sock") - 1] == "--bind"
    assert "/lab/run/b.sock" not in bwrap_argv(LaunchSpec(argv=["py"], cwd="/w", attempt_dir="/a", profile="linux:bwrap-sealed"))
    text, params = seatbelt_policy(LaunchSpec(argv=["py"], cwd="/w", attempt_dir="/a", profile="macos:seatbelt-sealed",
                                              sockets=["/lab/run/b.sock"]), "/t")
    assert "(deny network*)" in text and "(allow network-outbound (literal (param" in text
    assert any(p.endswith("=/lab/run/b.sock") for p in params)
    text2, _ = seatbelt_policy(LaunchSpec(argv=["py"], cwd="/w", attempt_dir="/a", profile="macos:seatbelt-sealed"), "/t")
    assert "allow network-outbound" not in text2


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="needs macOS sandbox-exec")
def test_seatbelt_sealed_job_reaches_the_broker_and_nothing_else(lab, broker_env, tmp_path):
    from test_turn_recovery import ShimClient

    from eidos.runtime.runner import AttemptRunner
    env, pid = broker_env, broker_env["pid"]
    job = lab.cmd(lab.admin, "run_component", {"component_ref": env["ref"], "environment": "lab",
                  "isolation_profile": "macos:seatbelt-sealed", "allow_llm_broker": True,
                  "config": {"n": 1}}, project=pid)["result"]["job_id"]
    root = Path(tempfile.mkdtemp(prefix="ebl-", dir="/tmp"))
    try:
        runner = AttemptRunner(ShimClient(lab, env["launcher"]), lab.store, lab_root=root, launcher_id=env["launcher"].id,
                               api_port=1, credential_path=str(root / "c"), broker_socket=env["sock"],
                               allowed_profiles=("dev:same-user", "macos:seatbelt-sealed"))
        res = runner.run_job(job, pid, activity_attempt=1)
        got = lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)
        assert res["job_status"] == "succeeded", got.get("terminal")
        assert len(env["provider"].calls) == 1
        iso = lab.q(lab.admin, "get_record", {"kind": "isolation_receipt", "id": got["current_attempt"]}, project=pid)["body"]
        assert iso["controls"]["no-network-egress"] == "passed" and iso["reachable_sockets"] == {"llm-broker": [env["sock"]]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_example_component_generates_synthetic_data_through_the_broker(lab, broker_env, tmp_path):
    """components/eidos.synthetic.llm-examples, published from its directory and run through the runner."""
    from test_command_path import _publish
    from test_turn_recovery import ShimClient

    from eidos.runtime.runner import AttemptRunner
    env, pid = broker_env, broker_env["pid"]
    comp = Path(__file__).resolve().parents[2] / "components" / "eidos.synthetic.llm-examples"
    manifest = json.loads((comp / "eidos-component.json").read_text())
    files = {str(p.relative_to(comp)): p.read_text() for p in comp.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.name != "eidos-component.json"}
    ref = _publish(lab, pid, manifest, files)["component_ref"]
    job = lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": "lab",
                  "config": {"labels": ["refund", "praise"], "per_label": 1}}, project=pid)["result"]["job_id"]
    lock = lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)["lock"]
    assert lock["llm"]["max_requests"] == 3 and lock["llm"]["role"] == "synthetic-data-generator"  # policy narrows 20
    root = tmp_path / "labroot"
    runner = AttemptRunner(ShimClient(lab, env["launcher"]), lab.store, lab_root=root, launcher_id=env["launcher"].id,
                           api_port=1, credential_path=str(root / "c"), broker_socket=env["sock"])
    assert runner.run_job(job, pid, activity_attempt=1)["job_status"] == "succeeded"
    got = lab.q(lab.admin, "get_job", {"job_id": job}, project=pid)
    rec = got["terminal"]["receipt"]
    assert rec["generated"] == 2 and rec["calls"] == 2 and rec["models"] == ["fake:echo"] and rec["tokens"] == 24
    out = next(o for o in got["outputs"] if o["slot"] == "dataset")
    rows = json.loads(Path(lab.store.path_for(out["digest"])).read_text())
    assert [r["label"] for r in rows] == ["refund", "praise"]
    calls = {c["id"] for c in _records(lab, pid, "llm_call")}
    assert {r["call_id"] for r in rows} <= calls  # each row traces to its call receipt
