"""MCP argument normalization (ADR 0017) against the real ledger: the record rides in the command receipt, it is not
part of the idempotency hash, and the lab's validators are exactly as strict as before (the original mangled config
is still refused; only the normalized one is admitted)."""

from __future__ import annotations

import copy
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from eidos.api.app import create_app
from eidos.client import LabClient, LabError
from eidos.domain.errors import CommandConflict, DomainError, ResolutionError
from eidos.mcp.gateway import ToolFailure, call_tool

pytestmark = pytest.mark.pg

CONFIG_SCHEMA = {"type": "object", "properties": {
    "argv": {"type": "array", "items": {"type": "string"}},
    "seed": {"type": "integer"},
    "timeout_seconds": {"type": "number"}}}
ARGV = ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", "{seed}"]
MANGLED = {"argv": {"item": ARGV}, "seed": "11", "timeout_seconds": "180"}
NORMALIZED = {"argv": ARGV, "seed": 11, "timeout_seconds": 180}
PROBLEMS = ["config.argv: expected array, got dict", "config.seed: expected integer, got str",
            "config.timeout_seconds: expected number, got str"]
RECORD = [
    {"path": "config.argv", "rule": "item-wrapper", "expected": "array", "original": {"item": "<array of 9>"}},
    {"path": "config.seed", "rule": "numeric-string", "expected": "integer", "original": "11", "normalized": 11},
    {"path": "config.timeout_seconds", "rule": "numeric-string", "expected": "number", "original": "180",
     "normalized": 180},
]


def setup(lab, schema=CONFIG_SCHEMA):
    import sys

    from test_command_path import _publish, project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": sys.executable,
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}},
            project=pid)
    manifest = {"id": "demo.command", "release": "1.0.0", "implements": "demo-command/1", "kind": "artifact-op",
                "entry": "m:run", "scientific_effects": ["observe"]}
    if schema is not None:
        manifest["config_schema"] = schema
    ref = _publish(lab, pid, manifest, {"m.py": "def run(i, c):\n    return {}\n"})["component_ref"]
    return pid, ref


def admitted_runs(lab, pid):
    """Jobs other than the component's own validation build."""
    return [j for j in lab.q(lab.admin, "list_jobs", {}, project=pid) if j["kind"] != "build"]


def stored_receipt(lab, pid, principal, key):
    with lab.ledger.transaction() as tx:
        row = tx._one("SELECT receipt FROM commands WHERE project_id=:p AND principal_id=:u AND command_key=:k",
                      p=pid, u=principal.id, k=key)
    return row["receipt"]


class ShimClient:
    """LabClient-compatible adapter that calls the command service in-process (as in test_turn_recovery), passing
    ``normalized_arguments`` through exactly as LabClient does: only when there are some."""

    def __init__(self, lab, principal):
        self.lab, self.pr = lab, principal

    def command(self, name, payload=None, *, project=None, key=None, expected_revision=None,
                normalized_arguments=None):
        try:
            return self.lab.svc.execute(self.pr, name, project_id=project, command_key=key or uuid.uuid4().hex,
                                        payload=payload or {}, expected_revision=expected_revision,
                                        normalized_arguments=normalized_arguments or None)
        except DomainError as exc:
            raise LabError(exc.http_status, exc.to_dict()) from exc

    def query(self, name, params=None, *, project=None):
        try:
            return json.loads(json.dumps(self.lab.svc.query(self.pr, name, project_id=project, params=params or {}),
                                         default=str))
        except DomainError as exc:
            raise LabError(exc.http_status, exc.to_dict()) from exc


def lab_client_over_http(lab, principal_id="admin"):
    """The real LabClient speaking to the real FastAPI app (in-process transport)."""
    token = lab.tokens[principal_id]
    client = LabClient("http://testserver", token, client_name="eidos-mcp")
    client.http.close()
    client.http = TestClient(create_app(lab.svc), base_url="http://testserver",
                             headers={"authorization": f"Bearer {token}", "x-eidos-client": "eidos-mcp"})
    return client


# -- (a) the receipt carries the record; replays carry it too; it is not part of the payload hash ------------------

def test_receipt_stores_normalized_arguments_and_a_replay_returns_it(lab):
    pid, ref = setup(lab)
    payload = {"component_ref": ref, "environment": "lab", "config": copy.deepcopy(NORMALIZED)}
    first = lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-n1", payload=payload,
                            normalized_arguments=RECORD)
    assert first["status"] == "committed" and first["replayed"] is False
    assert first["normalized_arguments"] == RECORD
    again = lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-n1", payload=payload,
                            normalized_arguments=RECORD)
    assert again["replayed"] is True and again["normalized_arguments"] == RECORD
    assert again["result"] == first["result"]
    stored = stored_receipt(lab, pid, lab.admin, "k-n1")
    assert stored["normalized_arguments"] == RECORD and stored["replayed"] is False
    jobs = lab.q(lab.admin, "list_jobs", {}, project=pid)
    assert len([j for j in jobs if j["id"] == first["result"]["job_id"]]) == 1  # admitted once


def test_a_command_without_changes_has_no_normalized_arguments_in_its_receipt(lab):
    pid, ref = setup(lab)
    payload = {"component_ref": ref, "environment": "lab", "config": copy.deepcopy(NORMALIZED)}
    for changes in (None, []):
        r = lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key=f"k-{changes!r}",
                            payload=payload, normalized_arguments=changes)
        assert "normalized_arguments" not in r
        assert "normalized_arguments" not in stored_receipt(lab, pid, lab.admin, f"k-{changes!r}")


def test_the_record_is_not_part_of_the_idempotency_hash(lab):
    pid, ref = setup(lab)
    payload = {"component_ref": ref, "environment": "lab", "config": copy.deepcopy(NORMALIZED)}
    first = lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-h", payload=payload,
                            normalized_arguments=RECORD)
    # the same admission retried after the client got a correctly typed call (no record) or a different record
    for other in (None, [RECORD[1]]):
        r = lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-h", payload=payload,
                            normalized_arguments=other)
        assert r["replayed"] is True and r["result"] == first["result"]
        assert r["normalized_arguments"] == RECORD  # the original admission's record, not the retry's
    changed = {**payload, "config": {**NORMALIZED, "seed": 12}}
    with pytest.raises(CommandConflict):  # a different payload under the same key is still a conflict
        lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-h", payload=changed,
                        normalized_arguments=RECORD)


# -- (b) the lab validators are unchanged -----------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["run_component", "plan_trial"])
def test_the_original_mangled_config_is_still_refused_and_the_normalized_one_admitted(lab, command):
    pid, ref = setup(lab)
    base = {"component_ref": ref, "environment": "lab"}
    with pytest.raises(ResolutionError) as e:
        lab.cmd(lab.admin, command, {**base, "config": copy.deepcopy(MANGLED)}, project=pid)
    assert e.value.code == "RESOLUTION_ERROR" and e.value.http_status == 422
    assert e.value.details["problems"] == PROBLEMS
    ok = lab.cmd(lab.admin, command, {**base, "config": copy.deepcopy(NORMALIZED)}, project=pid)
    assert ok["status"] == "committed"


def test_claiming_a_normalization_cannot_launder_an_invalid_config(lab):
    """The lab validates ``payload`` exactly as for any caller; ``normalized_arguments`` is only recorded."""
    pid, ref = setup(lab)
    with pytest.raises(ResolutionError) as e:
        lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-launder",
                        payload={"component_ref": ref, "environment": "lab", "config": copy.deepcopy(MANGLED)},
                        normalized_arguments=RECORD)
    assert e.value.details["problems"] == PROBLEMS
    assert admitted_runs(lab, pid) == []  # nothing admitted, nothing recorded


def test_a_component_without_a_config_schema_has_nothing_to_refuse_or_normalize(lab):
    pid, ref = setup(lab, schema=None)
    r = lab.cmd(lab.admin, "run_component", {"component_ref": ref, "environment": "lab",
                                             "config": copy.deepcopy(MANGLED)}, project=pid)
    assert r["status"] == "committed"  # no schema: the lab never had a type to enforce (unchanged behaviour)
    described = lab.q(lab.admin, "describe_component", {"component_ref": ref}, project=pid)
    assert "config_schema" not in described["body"]["manifest"]


# -- (c) the real gateway against the real service --------------------------------------------------------------------

def test_gateway_reads_the_real_config_schema_normalizes_and_the_receipt_carries_the_record(lab):
    pid, ref = setup(lab)
    client = ShimClient(lab, lab.admin)
    schema = client.query("describe_component", {"component_ref": ref}, project=pid)["body"]["manifest"]["config_schema"]
    assert schema == CONFIG_SCHEMA  # the record view the adapter reads
    args = {"project": pid, "command_key": "k-gw", "component_ref": ref, "environment": "lab",
            "config": copy.deepcopy(MANGLED), "seed": "7"}
    receipt = call_tool(client, "run_component", args)
    assert receipt["status"] == "committed"
    assert receipt["normalized_arguments"] == [
        {"path": "seed", "rule": "numeric-string", "expected": "integer", "original": "7", "normalized": 7}, *RECORD]
    assert stored_receipt(lab, pid, lab.admin, "k-gw")["normalized_arguments"] == receipt["normalized_arguments"]
    job = lab.q(lab.admin, "get_job", {"job_id": receipt["result"]["job_id"], "include_lock": True}, project=pid)
    lock_text = json.dumps(job["lock"])
    assert '"seed": 11' in lock_text and '"timeout_seconds": 180' in lock_text and '"item"' not in lock_text
    assert args["config"] == MANGLED and args["seed"] == "7"  # the caller's arguments were not touched
    # a retry of the same MCP call (same key, same arguments) replays the same admission, record included
    retry = call_tool(client, "run_component", {**args, "config": copy.deepcopy(MANGLED)})
    assert retry["replayed"] is True and retry["result"] == receipt["result"]
    assert retry["normalized_arguments"] == receipt["normalized_arguments"]


@pytest.mark.parametrize("config", [
    {"argv": {"item": "python"}},                       # scalar item
    {"argv": {"item": ARGV, "other": 1}},               # extra key
    {"seed": " 11"}, {"seed": "1.0"}, {"timeout_seconds": "NaN"},
])
def test_gateway_leaves_ambiguous_shapes_for_the_lab_to_refuse(lab, config):
    pid, ref = setup(lab)
    client = ShimClient(lab, lab.admin)
    with pytest.raises(ToolFailure) as e:
        call_tool(client, "run_component", {"project": pid, "command_key": "k-amb", "component_ref": ref,
                                            "environment": "lab", "config": copy.deepcopy(config)})
    assert e.value.code == "RESOLUTION_ERROR" and e.value.status == 422
    assert e.value.details["problems"]
    assert admitted_runs(lab, pid) == []


def test_gateway_with_a_partly_ambiguous_config_normalizes_only_the_unambiguous_part(lab):
    pid, ref = setup(lab)
    with pytest.raises(ToolFailure) as e:
        call_tool(ShimClient(lab, lab.admin), "plan_trial", {
            "project": pid, "command_key": "k-part", "component_ref": ref, "environment": "lab",
            "config": {"argv": {"item": "python"}, "seed": "11", "timeout_seconds": "180"}})
    # seed and timeout_seconds were normalized, so the lab only complains about the field left ambiguous
    assert e.value.details["problems"] == ["config.argv: expected array, got dict"]


def test_gateway_without_changes_sends_a_plain_command(lab):
    pid, ref = setup(lab)
    receipt = call_tool(ShimClient(lab, lab.admin), "run_component", {
        "project": pid, "command_key": "k-plain", "component_ref": ref, "environment": "lab",
        "config": copy.deepcopy(NORMALIZED)})
    assert receipt["status"] == "committed" and "normalized_arguments" not in receipt
    assert "normalized_arguments" not in stored_receipt(lab, pid, lab.admin, "k-plain")


# -- the real HTTP API and the real LabClient ---------------------------------------------------------------------------

def test_http_api_records_normalized_arguments_in_the_receipt(lab):
    pid, ref = setup(lab)
    c = TestClient(create_app(lab.svc))
    h = {"authorization": f"Bearer {lab.tokens['admin']}"}
    body = {"project_id": pid, "command_key": "k-http", "payload": {
        "component_ref": ref, "environment": "lab", "config": copy.deepcopy(NORMALIZED)},
        "normalized_arguments": RECORD}
    r = c.post("/v1/commands/run_component", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["normalized_arguments"] == RECORD and r.json()["replayed"] is False
    again = c.post("/v1/commands/run_component", json=body, headers=h)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["normalized_arguments"] == RECORD
    assert stored_receipt(lab, pid, lab.admin, "k-http")["normalized_arguments"] == RECORD
    # omitted: nothing is recorded
    plain = {**body, "command_key": "k-http-plain"}
    plain.pop("normalized_arguments")
    r = c.post("/v1/commands/run_component", json=plain, headers=h)
    assert r.status_code == 200 and "normalized_arguments" not in r.json()


def test_http_api_does_not_relax_validation_for_a_request_that_claims_normalization(lab):
    pid, ref = setup(lab)
    c = TestClient(create_app(lab.svc))
    h = {"authorization": f"Bearer {lab.tokens['admin']}"}
    r = c.post("/v1/commands/run_component", headers=h, json={
        "project_id": pid, "command_key": "k-http-bad",
        "payload": {"component_ref": ref, "environment": "lab", "config": copy.deepcopy(MANGLED)},
        "normalized_arguments": RECORD})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RESOLUTION_ERROR"
    assert r.json()["error"]["details"]["problems"] == PROBLEMS


def test_http_api_bounds_the_record_and_requires_a_list_of_objects(lab):
    pid, ref = setup(lab)
    c = TestClient(create_app(lab.svc))
    h = {"authorization": f"Bearer {lab.tokens['admin']}"}
    base = {"project_id": pid, "payload": {"component_ref": ref, "environment": "lab"}}
    too_many = c.post("/v1/commands/run_component", headers=h, json={
        **base, "command_key": "k-64", "normalized_arguments": [{"path": f"p{i}"} for i in range(65)]})
    assert too_many.status_code == 422
    not_a_list = c.post("/v1/commands/run_component", headers=h, json={
        **base, "command_key": "k-str", "normalized_arguments": "config.seed"})
    assert not_a_list.status_code == 422
    at_limit = c.post("/v1/commands/run_component", headers=h, json={
        **base, "command_key": "k-64-ok", "normalized_arguments": [{"path": f"p{i}"} for i in range(64)]})
    assert at_limit.status_code == 200 and len(at_limit.json()["normalized_arguments"]) == 64


def test_real_lab_client_and_gateway_end_to_end_over_http(lab):
    pid, ref = setup(lab)
    client = lab_client_over_http(lab)
    receipt = call_tool(client, "run_component", {
        "project": pid, "command_key": "k-e2e", "component_ref": ref, "environment": "lab",
        "config": copy.deepcopy(MANGLED)})
    assert receipt["status"] == "committed" and receipt["normalized_arguments"] == RECORD
    assert stored_receipt(lab, pid, lab.admin, "k-e2e")["normalized_arguments"] == RECORD
    # the same call bypassing the adapter's normalization is refused by the lab with the original error
    with pytest.raises(LabError) as e:
        client.command("run_component", {"component_ref": ref, "environment": "lab", "config": copy.deepcopy(MANGLED)},
                       project=pid, key="k-e2e-raw")
    assert e.value.status == 422 and e.value.code == "RESOLUTION_ERROR"
    assert e.value.error["details"]["problems"] == PROBLEMS
    # and a read tool reports its own normalization in its result
    events = call_tool(client, "list_events", {"project": pid, "limit": "5"})
    assert events["normalized_arguments"][0]["path"] == "limit"
