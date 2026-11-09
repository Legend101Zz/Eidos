"""Hostile-access probes: they must pass under the sealed profile and *fail* without it, otherwise
they would not be measuring anything."""

from __future__ import annotations

import os
import sys

import pytest

from eidos.domain.isolation import SEALED_CONTROLS, evaluate_receipt
from eidos.runtime.launcher import LaunchSpec, bwrap_available, run_probes


def _probe(tmp_path, profile, cwd=None):
    if os.geteuid() == 0:
        # As root with the eidos-cand user, the probe runs as that user (ADR 0011): its attempt directory must be
        # reachable, and pytest's root tmp_path is 0700. A real lab home is 0711 for the same reason.
        import pathlib
        import tempfile
        fresh = pathlib.Path(tempfile.mkdtemp(dir="/tmp"))
        os.chmod(fresh, 0o755)
        cwd = fresh if cwd == tmp_path else cwd
        tmp_path = fresh
    secret = tmp_path / "secrets" / "admin.token"
    secret.parent.mkdir()
    secret.write_text("token")
    label = tmp_path / "labels.csv"
    label.write_text("case,label\n1,0\n")
    from eidos.runtime.runner import python_prefixes
    proj = cwd or (tmp_path / "project")
    proj.mkdir(exist_ok=True)
    spec = LaunchSpec(argv=[sys.executable], cwd=str(proj), attempt_dir=str(tmp_path / "attempt"), profile=profile,
                      python_prefixes=python_prefixes(sys.executable) if profile != "dev:same-user" else [])
    return run_probes(spec, label_path=str(label), credential_path=str(secret),
                      host_write_path=str(tmp_path / "host-write"), api_port=1, net_host="127.0.0.1", net_port=1)


def test_same_user_profile_fails_access_probes(tmp_path):
    r = _probe(tmp_path, "dev:same-user")
    assert r["controls"]["no-label-read"] == "failed"
    assert r["controls"]["no-credential-read"] == "failed"
    assert r["controls"]["no-host-write"] == "failed"
    receipt = {"profile": "dev:same-user", "launcher": "svc", "controls": r["controls"]}
    assert not evaluate_receipt({"sealed": True}, receipt, trusted_launchers={"svc"})["ok"]


@pytest.mark.skipif(not bwrap_available(), reason="bubblewrap unavailable")
def test_bwrap_profile_passes_access_probes(tmp_path):
    r = _probe(tmp_path, "linux:bwrap-sealed")
    for c in SEALED_CONTROLS:
        assert r["controls"][c] == "passed", (c, r)
    assert not (tmp_path / "host-write").exists()
    receipt = {"profile": "linux:bwrap-sealed", "launcher": "svc", "controls": r["controls"]}
    assert evaluate_receipt({"sealed": True}, receipt, trusted_launchers={"svc"})["ok"]
    # A plan that additionally demands a distinct OS identity is only satisfied when one existed.
    strict = evaluate_receipt({"controls": list(SEALED_CONTROLS) + ["separate-identity"]}, receipt, trusted_launchers={"svc"})
    assert strict["ok"] == (r["controls"]["separate-identity"] == "passed")


@pytest.mark.skipif(not bwrap_available(), reason="bubblewrap unavailable")
def test_probe_detects_labels_exposed_through_a_bound_directory(tmp_path):
    """Misconfiguration (labels inside the project root, which is bound read-only) is caught."""
    r = _probe(tmp_path, "linux:bwrap-sealed", cwd=tmp_path)
    assert r["controls"]["no-label-read"] == "failed"


CAND_PY = os.environ.get("EIDOS_CANDIDATE_PYTHON", "/opt/eidos/envs/sealed-py312/bin/python")


@pytest.mark.skipif(not (bwrap_available() and os.path.exists(CAND_PY)), reason="no candidate-readable interpreter")
def test_distinct_candidate_identity_with_world_readable_interpreter(tmp_path):
    """With an interpreter the candidate OS user can execute (e.g. under /opt), the sealed profile
    runs candidate code as that user and the separate-identity control passes."""
    from eidos.runtime.launcher import candidate_uid
    from eidos.runtime.runner import python_prefixes
    if candidate_uid(CAND_PY) is None:
        pytest.skip("candidate user absent or cannot run the interpreter")
    import pathlib
    import tempfile
    tmp_path = pathlib.Path(tempfile.mkdtemp(dir="/tmp"))  # every parent must be traversable by the candidate
    os.chmod(tmp_path, 0o755)
    secret = tmp_path / "secret.token"
    secret.write_text("t")
    proj = tmp_path / "project"
    proj.mkdir()
    spec = LaunchSpec(argv=[CAND_PY], cwd=str(proj), attempt_dir=str(tmp_path / "attempt"), profile="linux:bwrap-sealed",
                      python_prefixes=python_prefixes(CAND_PY))
    r = run_probes(spec, label_path=str(secret), credential_path=str(secret), host_write_path=str(tmp_path / "hw"),
                   api_port=1, net_host="127.0.0.1", net_port=1)
    assert r["controls"]["separate-identity"] == "passed", r
    for c in SEALED_CONTROLS:
        assert r["controls"][c] == "passed", (c, r)
