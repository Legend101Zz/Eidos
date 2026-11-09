"""The command service validates received worker manifests against the published wire schema (design
"The worker compatibility promise"): a violation is refused with a clear, field-level error and
nothing is published; a newer-minor manifest with unknown fields is accepted."""

from __future__ import annotations

import uuid

import pytest

from eidos.application.handlers.runs import admit_job
from eidos.application.service import Ctx
from eidos.domain.errors import Fenced, Invalid

pytestmark = pytest.mark.pg


def _attempt(lab):
    pid = f"p{uuid.uuid4().hex[:8]}"
    lab.cmd(lab.admin, "create_project", {"project_id": pid, "budgets": {"compute_seconds": 10_000, "trials": 50}})
    with lab.ledger.transaction() as tx:
        tx.project_id = pid
        ctx = Ctx(tx, lab.admin, pid, None, lab.store, {})
        job = admit_job(ctx, kind="artifact-op", lock={"component": {"ref": "x@1"}, "inputs": {}, "outputs": {}},
                        investigation_id=None, purpose="open-search", wall_seconds=100)
    launcher = lab.principal(f"L-{pid}", ["launcher"], [pid])
    st = lab.cmd(launcher, "start_attempt", {"job_id": job}, project=pid)["result"]
    return pid, launcher, {"job_id": job, "attempt_id": st["attempt_id"], "epoch": st["epoch"]}


def test_publish_refuses_a_manifest_that_violates_the_schema(lab):
    pid, launcher, base = _attempt(lab)
    digest, size = lab.store.put_bytes(b"{}")
    manifest = {"wire": "eidos-wire/1", **base, "status": "succeeded", "receipt": ["not", "an", "object"],
                "outputs": [{"slot": "o", "filename": "o.json", "digest": digest, "size": size}]}
    with pytest.raises(Invalid) as exc:
        lab.cmd(launcher, "publish_outputs", {**base, "manifest": manifest}, project=pid)
    assert "violates the eidos-wire/1 schema" in str(exc.value) and "receipt" in str(exc.value)
    assert lab.q(lab.admin, "get_job", {"job_id": base["job_id"]}, project=pid)["status"] != "succeeded"


def test_publish_accepts_a_newer_minor_manifest_with_unknown_fields(lab):
    pid, launcher, base = _attempt(lab)
    digest, size = lab.store.put_bytes(b"{}")
    manifest = {"wire": "eidos-wire/1", **base, "status": "succeeded", "provenance_note": "newer minor",
                "outputs": [{"slot": "o", "filename": "o.json", "digest": digest, "size": size,
                             "checksum_algorithm": "sha256"}]}
    pub = lab.cmd(launcher, "publish_outputs", {**base, "manifest": manifest}, project=pid)["result"]
    assert pub["published"]


def test_a_stale_attempt_is_fenced_before_its_manifest_is_schema_checked(lab):
    """Fencing comes first: a superseded attempt gets Fenced (and the stale-publication event), not Invalid."""
    pid, launcher, old = _attempt(lab)
    new = lab.cmd(launcher, "start_attempt", {"job_id": old["job_id"]}, project=pid, key=f"start:{old['job_id']}:run2:1")["result"]
    assert new["epoch"] > old["epoch"]
    manifest = {"wire": "eidos-wire/1", **old, "status": "succeeded", "receipt": ["not", "an", "object"]}
    with pytest.raises(Fenced):
        lab.cmd(launcher, "publish_outputs", {**old, "manifest": manifest}, project=pid)
