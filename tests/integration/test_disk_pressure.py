"""Disk exhaustion: the store refuses new bytes below its reserve; nothing unverifiable is published
and committed evidence stays readable."""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.pg


def test_upload_refused_under_disk_pressure_and_existing_evidence_intact(lab):
    from test_command_path import project

    from eidos.api.app import create_app
    pid = project(lab)
    app = create_app(lab.svc)
    c = TestClient(app)
    h = {"authorization": f"Bearer {lab.tokens['admin']}", "x-eidos-command-key": "u1"}
    ok = c.post(f"/v1/projects/{pid}/artifacts", content=b"before", headers=h)
    assert ok.status_code == 200, ok.text
    aid = ok.json()["result"]["artifact_id"]
    lab.store.min_free_bytes = lab.store.free_bytes() + (1 << 40)  # simulate a full disk
    r = c.post(f"/v1/projects/{pid}/artifacts", content=b"after", headers={**h, "x-eidos-command-key": "u2"})
    assert r.status_code == 507 and r.json()["error"]["code"] == "DISK_PRESSURE"
    lab.store.min_free_bytes = 0
    got = c.get(f"/v1/projects/{pid}/artifacts/{aid}/content", headers=h)
    assert got.status_code == 200 and got.content == b"before"
