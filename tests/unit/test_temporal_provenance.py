"""lab init records where the Temporal server came from; operator-supplied (e.g. source-built) binaries are
recorded as such with their sha256s and the declared provenance, never as the pinned release."""

from __future__ import annotations

import json

from eidos.settings import LabConfig
from eidos.supervisor import services as svc


def test_operator_supplied_server_is_recorded_with_declared_provenance(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for n in ("temporal-server", "temporal-sql-tool"):
        (src / n).write_text("bin")
    (src / "provenance.json").write_text(json.dumps({"kind": "source-built", "version": "v1.32.2"}))
    cfg = LabConfig(home=str(tmp_path / "lab"))
    svc.install_temporal(cfg, str(src))
    prov = json.loads((tmp_path / "lab/services/temporal/provenance.json").read_text())
    assert prov["kind"] == "operator-supplied" and prov["declared"]["kind"] == "source-built"
    assert set(prov["sha256"]) == {"temporal-server", "temporal-sql-tool"}


def test_cached_archive_with_wrong_digest_is_rejected(tmp_path, monkeypatch):
    import pytest
    key = svc.temporal_platform()
    if key not in svc.TEMPORAL_RELEASES:
        pytest.skip("no pinned release for this platform")
    pinned = svc.TEMPORAL_RELEASES[key][1]
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"temporal-server-{pinned}.tgz").write_bytes(b"poisoned")
    monkeypatch.setenv("EIDOS_TEMPORAL_ARCHIVE_CACHE", str(cache))
    calls = []
    monkeypatch.setattr(svc, "_download", lambda url, dest: (calls.append(url), dest.write_bytes(b"still bad")))
    with pytest.raises(svc.ServiceError, match="digest"):
        svc.install_temporal(LabConfig(home=str(tmp_path / "lab")))
    assert calls and not (cache / f"temporal-server-{pinned}.tgz").exists()  # bad entry dropped, download tried
