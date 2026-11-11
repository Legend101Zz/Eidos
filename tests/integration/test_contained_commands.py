"""Model-chosen commands (lab-free driver, scorer) run contained: no provider key, no home directory, no
network, writes only to their work dirs; refused without a sealed profile unless a waiver is given."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from eidos.runtime.launcher import (
    IsolationUnavailable,
    detect_sealed_profile,
    job_environment,
    run_contained,
    scrub_secrets,
)

ROOT = Path(__file__).resolve().parents[2]
sealed = pytest.mark.skipif(not detect_sealed_profile(), reason="no sealed profile on this host")

PROBE = r"""
import json, os, socket, sys
cfg = json.loads(sys.argv[1]); r = {}
r["key_visible"] = "ANTHROPIC_API_KEY" in os.environ or "EIDOS_TOKEN" in os.environ
try:
    open(cfg["planted"]).read(); r["home_read"] = True
except OSError:
    r["home_read"] = False
try:
    socket.create_connection(("127.0.0.1", cfg["port"]), timeout=2).close(); r["network"] = True
except OSError:
    r["network"] = False
try:
    open(cfg["outside"], "w").write("x"); r["outside_write"] = True
except OSError:
    r["outside_write"] = False
open(os.path.join(cfg["out"], "ok.txt"), "w").write("ok")
print(json.dumps(r))
"""


@sealed
def test_contained_command_sees_no_key_home_network_or_outside_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-planted")
    monkeypatch.setenv("EIDOS_TOKEN", "planted")
    planted = Path.home() / f".eidos-planted-{os.getpid()}"
    planted.write_text("secret")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    def _serve():  # macOS aborts a pending accept() with ECONNABORTED when the socket closes at teardown
        try:
            for _ in range(4):
                srv.accept()[0].close()
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir()
    out.mkdir()
    try:
        cfg = {"planted": str(planted), "port": srv.getsockname()[1], "out": str(out),
               "outside": str(tmp_path / "outside.txt")}
        r = run_contained([sys.executable, "-I", "-c", PROBE, json.dumps(cfg)], cwd=str(work),
                          writable=[str(work), str(out)], python=sys.executable, timeout=60,
                          attempt_dir=str(tmp_path / "proc"))
        assert r["returncode"] == 0, r["stderr"][-1000:]
        res = json.loads(r["stdout"].strip().splitlines()[-1])
        assert {k: res[k] for k in ("key_visible", "home_read", "network")} == \
            {"key_visible": False, "home_read": False, "network": False}, res
        assert not (tmp_path / "outside.txt").exists()  # judged on the host: a write into a private tmpfs is not one
        assert (out / "ok.txt").read_text() == "ok" and r["profile"] == detect_sealed_profile()
    finally:
        planted.unlink(missing_ok=True)
        srv.close()


def test_same_user_needs_an_explicit_waiver_and_still_drops_secrets(monkeypatch):
    import eidos.runtime.launcher as L
    monkeypatch.setattr(L, "detect_sealed_profile", lambda: None)
    with pytest.raises(IsolationUnavailable):
        L.run_contained([sys.executable, "-c", "pass"], cwd="/tmp", writable=[], timeout=10)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-planted")
    r = L.run_contained([sys.executable, "-c", "import os; print('ANTHROPIC_API_KEY' in os.environ)"], cwd="/tmp",
                        writable=[], timeout=30, allow_same_user=True)
    assert r["profile"] == "dev:same-user" and r["waiver"] and r["stdout"].strip() == "False"


def test_environment_filters():
    env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "k",
           "EIDOS_TOKEN": "t", "HOME": "/h", "LC_ALL": "C", "GITHUB_TOKEN": "g"}
    assert job_environment(env) == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C"}
    assert set(scrub_secrets(env)) == {"PATH", "HOME", "LC_ALL", "EIDOS_TOKEN"}


@sealed
def test_lab_free_driver_runs_the_d02_baseline_contained(tmp_path):
    py = os.environ.get("EIDOS_RESEARCH_PYTHON", "/workspace/.eidos-dev/lead/envs/research-dev/bin/python")
    if not Path(py).exists():
        pytest.skip("research env not available")
    import subprocess
    dev = ROOT / "research" / "public" / "development"
    bundle = tmp_path / "D02"
    subprocess.run([py, "package_task.py", "--task", "D02", "--out", str(bundle)], cwd=dev, check=True,
                   capture_output=True, timeout=300)
    sys.path.insert(0, str(ROOT / "scripts" / "qualify"))
    from live_bundle_investigation import LocalLab
    lab = LocalLab(bundle, py, tmp_path / "work", 600)
    r = lab.run_component({"component_ref": "eidos.project.command@1.0.4",
                           "inputs": {"train": "art_train", "validation": "art_validation", "task": "art_task"},
                           "config": {}})
    assert r["ok"], lab.jobs[-1]
    pred = next(o["artifact_id"] for o in r["outputs"] if "prediction" in o["slot"])
    s = lab.run_component({"component_ref": "eidos.task.score-classification@1.0.2",
                           "inputs": {"predictions": pred, "reference": "art_validation"}, "config": {}})
    assert s["ok"] and 0.5 < s["metrics"].get("accuracy", 0) <= 1.0, s
    assert {i["profile"] for i in lab.isolation} == {detect_sealed_profile()}


@sealed
def test_lab_free_driver_refuses_symlinked_outputs(tmp_path, monkeypatch):
    """Review 3 finding 1: a contained command left out/x -> /proc/self/environ; the host followed it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-planted")
    sys.path.insert(0, str(ROOT / "scripts" / "qualify"))
    from live_bundle_investigation import LocalLab
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for n in ("train.npz", "validation.npz"):
        (bundle / n).write_bytes(b"x")
    (bundle / "task.json").write_text("{}")
    lab = LocalLab(bundle, sys.executable, tmp_path / "work", 60)
    r = lab.run_component({"component_ref": "eidos.project.command@1.0.4", "inputs": {}, "config": {
        "argv": ["python", "-c", "import os; os.symlink('/proc/self/environ', '{out}/predictions.npz')"]}})
    assert not r["ok"] and "not a regular file" in r["error"]
