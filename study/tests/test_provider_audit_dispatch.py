"""Offline launcher→installed worker→actual bridge audit, using only loopback."""
import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from study import launcher
from study.eidos_arms import common, provider_bridge
from study.evals.records import audited_requests

KNOWN = "a" * 64
CARD = "N01"
META = {"arm": 4, "card": CARD, "run_index": 1, "block_id": "N01-r1", "envelope": {"wall_s": 20},
        "charter_amendment_commit": "a" * 40, "charter_amendment_sha256": "b" * 64}


def audit_fixture(root, card=CARD, known=KNOWN, payload=None):
    root = root.resolve()
    out = root / "out"
    run_dir = out / "runs" / f"{card}-r1" / "arm4"
    history = root / "evals-history" / "fixture-build" / "fixture-attempt"
    history.mkdir(parents=True, mode=0o700)
    path = history / "audit-identifiers.json"
    raw = json.dumps({card: [known]}).encode() if payload is None else payload
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    (history / "attempt.json").write_text(json.dumps({
        "out": str(out), "cards": [card], "arms": [4], "seed_index": 1,
        "provider_audit_identifiers_sha256": sha,
    }))
    return run_dir, path, sha


# Only the fixture replaces setup and the investigation command. The real launcher,
# run_arm, installed worker, arm-4 adapter and bridge startup all execute.
WORKER_FIXTURE = r'''
import http.client
import json
import os
import socket
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

if sys.argv[2] != "study.eidos_arms.worker":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
sys.path.insert(0, str(Path.cwd()))
from study.eidos_arms import arm4, lab, provider_bridge, worker

assert "EIDOS_EVAL_AUDIT_IDENTIFIERS" not in os.environ
real_start, real_run = provider_bridge.start, arm4.run

class StopAfterProbe(Exception):
    pass

class Client:
    def command(self, name, payload, *, project):
        assert name == "start_investigation"
        text = json.dumps(payload)
        assert "audit-identifiers.json" not in text and "aaaaaaaaaaaaaaaa" not in text
        address = urlparse(payload["investigator"]["settings"]["base_url"])
        for content in ("clean transient body", "matched transient body " + "a" * 64):
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=4)
            try:
                connection.request("POST", "/v1/chat/completions",
                    body=json.dumps({"messages": [{"content": content}]}).encode(),
                    headers={"Authorization": "Bearer fixture-token", "Content-Type": "application/json"})
                # The audit happens before the deliberately unavailable loopback upstream.
                try:
                    connection.getresponse().read()
                except http.client.RemoteDisconnected:
                    pass
            finally:
                connection.close()
        raise StopAfterProbe
    def close(self):
        pass

def local_bridge(run_dir, upstream, deadline, timeout, **kwargs):
    assert "EIDOS_EVAL_AUDIT_IDENTIFIERS" not in lab.environment()
    return real_start(run_dir, f"https://127.0.0.1:{unused.getsockname()[1]}/v1", deadline, 1, **kwargs)

def run_with_probe(run_dir, meta, **kwargs):
    with patch.object(lab, "start"), patch.object(lab, "client", return_value=Client()), \
            patch.object(arm4, "prepare", return_value=("fixture", {
                "charter_id": "charter", "artifacts": {}, "task": {},
            })), patch.object(provider_bridge, "start", side_effect=local_bridge):
        try:
            return real_run(run_dir, meta, **kwargs)
        except StopAfterProbe:
            return {"status": "ok", "model_ids_seen": ["loopback-fixture"]}

with socket.socket() as unused:
    unused.bind(("127.0.0.1", 0))  # Reserved without a listener, so the upstream cannot receive anything.
    sys.argv = [sys.argv[2], *sys.argv[3:]]
    with patch.object(arm4, "run", side_effect=run_with_probe):
        raise SystemExit(worker.main())
'''


class ProviderAuditDispatch(unittest.TestCase):
    def interpreter(self, root):
        python = os.environ.get("EIDOS_STUDY_PYTHON") or sys.executable
        wrapper = root / "fixture-venv/bin/python"
        wrapper.parent.mkdir(parents=True)
        helper = root / "worker-fixture.py"
        helper.write_text(WORKER_FIXTURE)
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(python)} {shlex.quote(str(helper))} \"$@\"\n")
        wrapper.chmod(0o700)
        (wrapper.parent.parent / "pilot-build.json").write_text(json.dumps({
            "eidos_commit_sha": "fixture", "python": str(wrapper),
        }))
        return str(wrapper)

    def test_actual_bridge_audits_both_requests_through_arm4_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, config, sha = audit_fixture(root)
            python = self.interpreter(root)
            env = {"EIDOS_STUDY_PYTHON": python, "EIDOS_RESEARCH_PYTHON": sys.executable,
                   "EIDOS_EVAL_AUDIT_IDENTIFIERS": "/untrusted/ambient-value",
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

            def setup(row, target):
                (target / "task").mkdir(parents=True)
                (target / "arm.json").write_text(json.dumps(META))
                launcher.record_prompt(target, META)

            rows = [{"arm": 4, "task": CARD, "run": 1, "block_id": META["block_id"]}]
            with patch.dict(os.environ, env), patch.object(launcher, "prepare", side_effect=setup):
                block = launcher.run_block(rows, run_dir.parents[1], eval_audit_config=config)
            self.assertEqual(block["status"], "ok")
            bridge = json.loads((run_dir / "provider_bridge.json").read_text())
            self.assertTrue(bridge["eval_provider_audit"])
            self.assertEqual(bridge["eval_provider_audit_config_sha256"], sha)
            requests, error = audited_requests(run_dir, {KNOWN}, audit_config_sha=sha)
            self.assertIsNone(error)
            self.assertEqual([row["matched_identifiers"] for row in requests], [[], [KNOWN]])
            expected = [hashlib.sha256(json.dumps({"messages": [{"content": body}]}).encode()).hexdigest()
                        for body in ("clean transient body", "matched transient body " + KNOWN)]
            self.assertEqual([row["request_sha256"] for row in requests], expected)
            self.assertNotIn("transient body", (run_dir / "eval_provider_audit.jsonl").read_text())
            self.assertNotIn("fixture-token", (run_dir / "provider_bridge.log").read_text())
            self.assertIsNotNone(audited_requests(run_dir, {KNOWN}, audit_config_sha="0" * 64)[1])

    def test_missing_malformed_stale_and_linked_configs_fail_before_worker(self):
        for mode in ("missing", "malformed", "stale", "linked"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                run_dir, config, _ = audit_fixture(root, payload=b"invalid" if mode == "malformed" else None)
                run_dir.mkdir(parents=True)
                (run_dir / "arm.json").write_text(json.dumps(META))
                if mode == "missing":
                    config.unlink()
                elif mode == "stale":
                    config.write_text(json.dumps({CARD: ["b" * 64]}))
                elif mode == "linked":
                    target = config.with_name("linked.json")
                    config.rename(target)
                    config.symlink_to(target)
                with patch.object(common.subprocess, "run") as process, self.assertRaisesRegex(
                    ValueError, "missing, malformed, or unbound"
                ):
                    common.dispatch(4, run_dir, eval_audit_config=config)
                process.assert_not_called()
                self.assertFalse((run_dir / "provider_bridge.json").exists())



    def test_bridge_rejects_malformed_config_before_opening_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, config, _ = audit_fixture(root, payload=b"invalid")
            run_dir.mkdir(parents=True)
            (run_dir / "arm.json").write_text(json.dumps(META))
            python = self.interpreter(root)
            with patch.dict(os.environ, {"EIDOS_STUDY_PYTHON": python}), self.assertRaisesRegex(
                RuntimeError, "provider deadline bridge failed"
            ):
                provider_bridge.start(run_dir, "https://127.0.0.1:1/v1", time.time() + 5, 1,
                                      eval_audit_config=config)
            self.assertFalse((run_dir / "provider_bridge.json").exists())
            self.assertFalse((run_dir / "eval_provider_audit.jsonl").exists())



    def test_audit_path_cannot_be_dispatched_to_arm3(self):
        with patch.object(common.subprocess, "run") as process, self.assertRaisesRegex(
            ValueError, "restricted to arm 4"
        ):
            common.dispatch(3, Path.cwd(), eval_audit_config=Path("/fixture/audit-identifiers.json"))
        process.assert_not_called()
