"""MCP argument normalization (ADR 0017) against the real ledger: the record rides in the command receipt, it is not
part of the idempotency hash, and the lab's validators are exactly as strict as before (the original mangled config
is still refused; only the normalized one is admitted)."""

from __future__ import annotations

import copy
import json
import logging
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


def stored_row(lab, pid, principal, key):
    """The whole ``commands`` row (hash, status, receipt, delegation, created_at): replays must not change any of it."""
    with lab.ledger.transaction() as tx:
        return tx._one("SELECT * FROM commands WHERE project_id=:p AND principal_id=:u AND command_key=:k",
                       p=pid, u=principal.id, k=key)


def run_once(lab, pid, ref, key, record=None, config=None):
    """One ``run_component`` invocation through the command service with ``record`` as its normalized_arguments."""
    payload = {"component_ref": ref, "environment": "lab", "config": copy.deepcopy(NORMALIZED if config is None else config)}
    return lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key=key, payload=payload,
                           normalized_arguments=record)


def event_ids(lab, pid):
    return [e["seq"] for e in lab.q(lab.admin, "list_events", {"limit": 500}, project=pid)["events"]]


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
    assert again["admission_normalized_arguments"] == RECORD  # a replay always names the committed call's record
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
        # F2b: ``normalized_arguments`` is THIS invocation's; the committed call's record is reported separately
        assert r.get("normalized_arguments") == other
        assert r["admission_normalized_arguments"] == RECORD
    changed = {**payload, "config": {**NORMALIZED, "seed": 12}}
    with pytest.raises(CommandConflict):  # a different payload under the same key is still a conflict
        lab.svc.execute(lab.admin, "run_component", project_id=pid, command_key="k-h", payload=changed,
                        normalized_arguments=RECORD)


# -- (a2) F2b: a replay's ``normalized_arguments`` is THIS invocation's; the committed call's is separate ------------

OTHER_RECORD = [{"path": "config.seed", "rule": "numeric-string", "expected": "integer", "original": "11",
                 "normalized": 11}]


def test_native_first_call_then_a_normalized_retry_reports_the_retrys_record_only(lab):
    pid, ref = setup(lab)
    first = run_once(lab, pid, ref, "k-r1")
    assert first["replayed"] is False and "normalized_arguments" not in first
    retry = run_once(lab, pid, ref, "k-r1", RECORD)
    assert retry["replayed"] is True
    assert retry["normalized_arguments"] == RECORD  # what THIS call normalized
    assert "admission_normalized_arguments" not in retry  # the committed call had nothing normalized
    assert retry["result"] == first["result"] and retry["events"] == first["events"]
    assert admitted_runs(lab, pid)[0]["id"] == first["result"]["job_id"] and len(admitted_runs(lab, pid)) == 1
    # the retry's record is not written back: the stored receipt still says the first call was native
    stored = stored_receipt(lab, pid, lab.admin, "k-r1")
    assert "normalized_arguments" not in stored and "admission_normalized_arguments" not in stored
    assert stored["replayed"] is False


@pytest.mark.parametrize("retry_record", [None, []])
def test_normalized_first_call_then_a_native_retry_has_no_normalized_arguments_but_names_the_admissions(
        lab, retry_record):
    pid, ref = setup(lab)
    first = run_once(lab, pid, ref, "k-r2", RECORD)
    assert first["normalized_arguments"] == RECORD and "admission_normalized_arguments" not in first
    retry = run_once(lab, pid, ref, "k-r2", retry_record)
    assert retry["replayed"] is True
    assert "normalized_arguments" not in retry  # nothing was normalized in THIS invocation
    assert retry["admission_normalized_arguments"] == RECORD
    assert retry["result"] == first["result"] and retry["events"] == first["events"]
    assert len(admitted_runs(lab, pid)) == 1


def test_two_differently_normalized_calls_are_reported_separately(lab):
    pid, ref = setup(lab)
    first = run_once(lab, pid, ref, "k-r3", RECORD)
    retry = run_once(lab, pid, ref, "k-r3", OTHER_RECORD)
    assert retry["replayed"] is True
    assert retry["normalized_arguments"] == OTHER_RECORD
    assert retry["admission_normalized_arguments"] == RECORD
    assert [c["path"] for c in retry["normalized_arguments"]] == ["config.seed"]
    assert [c["path"] for c in retry["admission_normalized_arguments"]] == [
        "config.argv", "config.seed", "config.timeout_seconds"]
    assert first["normalized_arguments"] == RECORD  # the first call's own receipt is what it was
    # a third retry with yet another record: still the first call's record as the admission's, its own as its own
    third = run_once(lab, pid, ref, "k-r3", [RECORD[0]])
    assert third["normalized_arguments"] == [RECORD[0]] and third["admission_normalized_arguments"] == RECORD
    assert len(admitted_runs(lab, pid)) == 1


@pytest.mark.parametrize("first_record", [None, RECORD])
def test_a_later_plain_replay_returns_the_stored_first_call_record_as_the_admissions(lab, first_record):
    pid, ref = setup(lab)
    key = "k-r4"
    first = run_once(lab, pid, ref, key, first_record)
    run_once(lab, pid, ref, key, OTHER_RECORD)  # intermediate retries, normalized differently
    run_once(lab, pid, ref, key, RECORD)
    run_once(lab, pid, ref, key, None)
    for _ in range(2):
        plain = run_once(lab, pid, ref, key)  # a plain replay: nothing normalized in this invocation
        assert plain["replayed"] is True and plain["result"] == first["result"]
        assert "normalized_arguments" not in plain
        if first_record:  # the committed call's record, never an intermediate retry's
            assert plain["admission_normalized_arguments"] == RECORD
        else:
            assert "admission_normalized_arguments" not in plain
    assert len(admitted_runs(lab, pid)) == 1


def test_replays_never_modify_the_stored_receipt_or_the_command_row(lab):
    pid, ref = setup(lab)
    for key, first_record in (("k-s1", RECORD), ("k-s2", None)):
        run_once(lab, pid, ref, key, first_record)
        row = stored_row(lab, pid, lab.admin, key)
        events = event_ids(lab, pid)
        assert events  # the first call's events are in the ledger
        for record in (None, [], OTHER_RECORD, RECORD, [RECORD[0]], None):
            run_once(lab, pid, ref, key, record)
            assert stored_row(lab, pid, lab.admin, key) == row  # hash, status, receipt, delegation, created_at
            assert event_ids(lab, pid) == events  # and nothing new was emitted
        receipt = row["receipt"]
        assert receipt["replayed"] is False and "admission_normalized_arguments" not in receipt
        assert receipt.get("normalized_arguments") == first_record
    assert len(admitted_runs(lab, pid)) == 2


def test_a_replay_with_a_changed_payload_is_still_a_conflict_and_leaves_the_row_alone(lab):
    pid, ref = setup(lab)
    run_once(lab, pid, ref, "k-r5", RECORD)
    row = stored_row(lab, pid, lab.admin, "k-r5")
    with pytest.raises(CommandConflict):
        run_once(lab, pid, ref, "k-r5", OTHER_RECORD, config={**NORMALIZED, "seed": 12})
    assert stored_row(lab, pid, lab.admin, "k-r5") == row


def test_the_normalization_of_every_invocation_is_logged_replays_included(lab, caplog):
    pid, ref = setup(lab)
    with caplog.at_level(logging.INFO, logger="eidos.commands"):
        run_once(lab, pid, ref, "k-log", RECORD)
        run_once(lab, pid, ref, "k-log", OTHER_RECORD)
        run_once(lab, pid, ref, "k-log", None)  # nothing normalized: nothing to log
        run_once(lab, pid, ref, "k-log-native")
    logged = [r for r in caplog.records if r.getMessage() == "command arguments normalized"]
    assert [(r.command_key, r.replayed, r.normalized_arguments) for r in logged] == [
        ("k-log", False, RECORD), ("k-log", True, OTHER_RECORD)]
    assert all(r.command == "run_component" and r.principal == "admin" and r.project == pid for r in logged)
    committed = [r for r in caplog.records if r.getMessage() == "command committed"]
    assert [r.command_key for r in committed] == ["k-log", "k-log-native"]  # replays are not committed again


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
    {"timeout_seconds": "1e-400"}, {"timeout_seconds": "9007199254740993.0"}, {"timeout_seconds": "1e400"},
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


@pytest.mark.parametrize("command", ["run_component", "plan_trial"])
@pytest.mark.parametrize("text", ["1e-400", "-1e-400", "1e-324", "9007199254740993.0"])
def test_gateway_does_not_normalize_a_number_the_double_cannot_denote_and_the_lab_refuses_it(lab, command, text):
    """F2b end to end: ``"1e-400"`` must not silently become 0.0 (a zero timeout). The adapter leaves the string, the
    lab refuses it, nothing is admitted, and the value is not claimed as normalized anywhere."""
    pid, ref = setup(lab)
    client = ShimClient(lab, lab.admin)
    config = {"argv": ARGV, "seed": "11", "timeout_seconds": text}
    with pytest.raises(ToolFailure) as e:
        call_tool(client, command, {"project": pid, "command_key": "k-uf", "component_ref": ref,
                                    "environment": "lab", "config": config})
    assert e.value.code == "RESOLUTION_ERROR" and e.value.status == 422
    # seed was normalized (so the lab is not told about it); only the unconvertible number is refused, as a string
    assert e.value.details["problems"] == ["config.timeout_seconds: expected number, got str"]
    assert admitted_runs(lab, pid) == []
    assert config["timeout_seconds"] == text  # the caller's arguments were not touched
    with lab.ledger.transaction() as tx:  # a refused command leaves no receipt (and so no normalization record)
        assert tx._one("SELECT receipt FROM commands WHERE project_id=:p AND command_key=:k", p=pid, k="k-uf") is None


def test_gateway_underflow_refusal_is_reported_without_a_normalization_record_while_a_neighbour_is_normalized(lab):
    pid, ref = setup(lab)
    seen = {}

    class Spy(ShimClient):
        def command(self, name, payload=None, **kw):
            seen.update(payload=payload, kw=kw)
            return super().command(name, payload, **kw)

    with pytest.raises(ToolFailure):
        call_tool(Spy(lab, lab.admin), "run_component", {
            "project": pid, "command_key": "k-uf2", "component_ref": ref, "environment": "lab",
            "config": {"seed": "11", "timeout_seconds": "1e-400"}})
    assert seen["payload"]["config"] == {"seed": 11, "timeout_seconds": "1e-400"}
    assert [c["path"] for c in seen["kw"]["normalized_arguments"]] == ["config.seed"]  # not config.timeout_seconds


@pytest.mark.parametrize("text, value", [("0.1", 0.1), ("1e-3", 0.001), ("5e-324", 5e-324)])
def test_gateway_admits_a_number_the_double_denotes_exactly_and_the_lock_holds_that_double(lab, text, value):
    pid, ref = setup(lab)
    receipt = call_tool(ShimClient(lab, lab.admin), "run_component", {
        "project": pid, "command_key": "k-ex", "component_ref": ref, "environment": "lab",
        "config": {"timeout_seconds": text}})
    assert receipt["status"] == "committed"
    assert receipt["normalized_arguments"] == [{"path": "config.timeout_seconds", "rule": "numeric-string",
                                                "expected": "number", "original": text, "normalized": value}]
    job = lab.q(lab.admin, "get_job", {"job_id": receipt["result"]["job_id"], "include_lock": True}, project=pid)
    assert f'"timeout_seconds": {value!r}' in json.dumps(job["lock"])


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


def test_gateway_retries_report_each_calls_own_normalization_and_the_admissions_separately(lab):
    pid, ref = setup(lab)
    client = ShimClient(lab, lab.admin)

    def go(config):
        return call_tool(client, "run_component", {"project": pid, "command_key": "k-gwr", "component_ref": ref,
                                                   "environment": "lab", "config": copy.deepcopy(config)})
    first = go(MANGLED)  # normalized in three places
    assert first["normalized_arguments"] == RECORD and "admission_normalized_arguments" not in first
    native = go(NORMALIZED)  # the same admission retried with correctly typed arguments
    assert native["replayed"] is True and native["result"] == first["result"]
    assert "normalized_arguments" not in native and native["admission_normalized_arguments"] == RECORD
    partly = go({"argv": ARGV, "seed": "11", "timeout_seconds": 180})  # only config.seed needs normalizing
    assert partly["replayed"] is True
    assert [c["path"] for c in partly["normalized_arguments"]] == ["config.seed"]
    assert partly["admission_normalized_arguments"] == RECORD
    assert stored_receipt(lab, pid, lab.admin, "k-gwr")["normalized_arguments"] == RECORD  # unchanged by any of it
    assert len(admitted_runs(lab, pid)) == 1


def test_gateway_native_first_call_then_a_normalized_retry(lab):
    pid, ref = setup(lab)
    client = ShimClient(lab, lab.admin)
    base = {"project": pid, "command_key": "k-gwn", "component_ref": ref, "environment": "lab"}
    first = call_tool(client, "run_component", {**base, "config": copy.deepcopy(NORMALIZED)})
    assert "normalized_arguments" not in first
    retry = call_tool(client, "run_component", {**base, "config": copy.deepcopy(MANGLED)})
    assert retry["replayed"] is True and retry["normalized_arguments"] == RECORD
    assert "admission_normalized_arguments" not in retry
    assert "normalized_arguments" not in stored_receipt(lab, pid, lab.admin, "k-gwn")


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


def test_http_api_replays_return_this_requests_record_and_the_admissions_separately(lab):
    pid, ref = setup(lab)
    c = TestClient(create_app(lab.svc))
    h = {"authorization": f"Bearer {lab.tokens['admin']}"}
    body = {"project_id": pid, "command_key": "k-http-r", "payload": {
        "component_ref": ref, "environment": "lab", "config": copy.deepcopy(NORMALIZED)},
        "normalized_arguments": RECORD}
    first = c.post("/v1/commands/run_component", json=body, headers=h).json()
    assert first["normalized_arguments"] == RECORD and "admission_normalized_arguments" not in first
    plain = {k: v for k, v in body.items() if k != "normalized_arguments"}
    r = c.post("/v1/commands/run_component", json=plain, headers=h)
    assert r.status_code == 200 and r.json()["replayed"] is True
    assert "normalized_arguments" not in r.json() and r.json()["admission_normalized_arguments"] == RECORD
    other = c.post("/v1/commands/run_component", json={**body, "normalized_arguments": OTHER_RECORD}, headers=h).json()
    assert other["normalized_arguments"] == OTHER_RECORD and other["admission_normalized_arguments"] == RECORD
    assert other["result"] == first["result"]
    assert stored_receipt(lab, pid, lab.admin, "k-http-r")["normalized_arguments"] == RECORD
    # and the other way round: a native first call, a normalized retry
    native = {**plain, "command_key": "k-http-r2"}
    assert "normalized_arguments" not in c.post("/v1/commands/run_component", json=native, headers=h).json()
    retry = c.post("/v1/commands/run_component", json={**native, "normalized_arguments": RECORD}, headers=h).json()
    assert retry["replayed"] is True and retry["normalized_arguments"] == RECORD
    assert "admission_normalized_arguments" not in retry


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
