"""ADR 0022: dependent decision batches, without a lab or a database.

A decision's later call may name an earlier call's result in its arguments as ``{"$ref": "call[<i>].<field>"}`` or
``{"$ref": "call[<i>].outputs.<slot>"}``. Three layers are tested here with plain fakes:

* the validator (``validate_decision``: grammar, range, direction, the fields a tool produces, literal arguments, the
  decision's resource policy) and ``resolve_refs`` (what a call's receipt records as provenance);
* ``InvestigatorActivities.execute_decision`` against a fake ``LabClient`` under ``temporalio.testing.ActivityEnvironment``:
  references resolved against earlier receipts and jobs, waiting for jobs in slices (``pending`` and ``continuation``),
  sequential jobs unless the decision asks for more, failed dependencies, the final slice and a stop while waiting;
* ``wait_for_jobs`` after a batch (``nothing_outstanding``).

The fake lab stores a decision exactly as the real validator accepted it (``validate_decision`` then ``keyed_calls``),
replays any command or upload by its key (``replayed: True`` on a repeat, no second effect), and lets a test decide when
each job it admitted finishes (``plan``: after that many ``get_job`` polls; ``finish``: now)."""

from __future__ import annotations

import copy
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from eidos.client import LabError
from eidos.domain import turns
from eidos.domain.errors import Invalid
from eidos.domain.turns import iter_refs, keyed_calls, parse_ref, resolve_refs, validate_decision
from eidos.investigator.activities import InvestigatorActivities
from eidos_wire import command_key

P, INV, TURN, DEC = "proj", "inv-1", "turn-1", "dec-1"
EXECUTION = "run-A/exec-1"
RECEIPTS_KEY = f"receipts:{DEC}"


# -- call builders ---------------------------------------------------------------------------------------------------
def ref(index: int, field: str) -> dict:
    return {"$ref": f"call[{index}].{field}"}


def src() -> dict:
    return {"tool": "author_source", "args": {"files": {"project.py": "print('train')"}}}


def train(**inputs: Any) -> dict:
    return {"tool": "run_component", "args": {"component_ref": "eidos.project.command@1.0.0", "inputs": inputs}}


def score(**inputs: Any) -> dict:
    return {"tool": "run_component", "args": {"component_ref": "eidos.score@1.0.0", "inputs": inputs}}


def note(text: Any = "independent note") -> dict:
    return {"tool": "record_note", "args": {"text": text}}


def pipeline() -> list[dict]:
    """source -> train -> score: the train call names the authored source, the score call names the training job's
    ``predictions`` output (and the source again, to depend on two calls)."""
    return [src(), train(source=ref(0, "artifact_id")),
            score(predictions=ref(1, "outputs.predictions"), tags=[ref(0, "artifact_id")])]


def minimal(tool: str) -> dict:
    """A call of ``tool`` with just its required arguments."""
    return {"tool": tool, "args": {a: ({"f": "x"} if a == "files" else "x") for a in turns.REQUIRED_ARGS.get(tool, ())}}


def accept(calls: list[dict], **proposal: Any) -> dict:
    return validate_decision({"rationale": "r", "calls": calls, **proposal}, max_calls=16)


# =====================================================================================================================
# a) the validator
# =====================================================================================================================
def test_artifact_reference_is_accepted_and_the_dependency_recorded():
    d = accept([src(), train(source=ref(0, "artifact_id"))])
    assert "depends_on" not in d["calls"][0]
    assert d["calls"][1]["depends_on"] == [0]
    # the persisted call keeps the reference as written; it is resolved only when the call executes
    assert d["calls"][1]["args"]["inputs"]["source"] == {"$ref": "call[0].artifact_id"}


def test_job_output_reference_is_accepted_and_depends_on_is_sorted_and_unique():
    d = accept([src(), train(source=ref(0, "artifact_id")),
                score(predictions=ref(1, "outputs.predictions"), source=ref(0, "artifact_id"),
                      again=ref(0, "artifact_id"))])
    # the first reference found names call 1, the next ones call 0 (twice): sorted, no duplicates
    assert d["calls"][2]["depends_on"] == [0, 1]
    assert d["calls"][2]["args"]["inputs"]["predictions"] == {"$ref": "call[1].outputs.predictions"}


def test_a_proposal_supplied_depends_on_is_overwritten():
    # a stated dependency with no reference behind it is dropped ...
    stated = train()
    stated["depends_on"] = [0]
    # ... and a stated one that disagrees with the references is replaced by what they say
    wrong = score(predictions=ref(1, "outputs.predictions"))
    wrong["depends_on"] = [0, 7]
    junk = note()
    junk["depends_on"] = "nonsense"
    d = accept([src(), stated, wrong, junk])
    assert "depends_on" not in d["calls"][1]
    assert d["calls"][2]["depends_on"] == [1]
    assert "depends_on" not in d["calls"][3]


def test_references_nested_in_lists_and_objects_are_found():
    args = {"component_ref": "c@1.0.0",
            "config": {"argv": ["python", ref(0, "artifact_id")], "deep": {"x": [{"y": ref(1, "job_id")}]}}}
    assert list(iter_refs(args)) == [("config.argv[1]", "call[0].artifact_id"), ("config.deep.x[0].y", "call[1].job_id")]
    d = accept([src(), train(), {"tool": "run_component", "args": args}])
    assert d["calls"][2]["depends_on"] == [0, 1]


def test_a_decision_without_references_is_unchanged():
    d = accept([src(), train(), note()])
    assert all("depends_on" not in c for c in d["calls"])
    assert list(iter_refs({"a": [1, {"b": "call[0].artifact_id"}], "c": None})) == []  # a plain string is not a reference


GOOD_GRAMMAR = [("call[0].artifact_id", (0, "artifact_id")), ("call[12].job_id", (12, "job_id")),
                ("call[3].outputs.predictions", (3, "outputs.predictions")),
                ("call[0].outputs.pred-1_A", (0, "outputs.pred-1_A")), ("call[0].outputs", (0, "outputs"))]


@pytest.mark.parametrize("text, parsed", GOOD_GRAMMAR)
def test_parse_ref_grammar(text, parsed):
    assert parse_ref(text) == parsed


BAD_GRAMMAR = ["", "artifact_id", "call[0]", "call[0].", "call0.artifact_id", "call[0]artifact_id", "call[-1].artifact_id",
               "call[01].artifact_id", "call[x].artifact_id", "call[].artifact_id", "Call[0].artifact_id",
               " call[0].artifact_id", "call[0].artifact_id ", "call[0].Artifact_Id", "call[0].artifact-id",
               "call[0].outputs.", "call[0].outputs.a b", "call[0].outputs.x.y", "call[0].outputs.a/b"]


@pytest.mark.parametrize("text", BAD_GRAMMAR)
def test_malformed_reference_is_refused(text):
    assert parse_ref(text) is None
    with pytest.raises(Invalid) as e:
        accept([src(), train(source={"$ref": text})])
    assert e.value.details["call"] == 1 and e.value.details["path"] == "inputs.source"
    assert e.value.details["ref"] == text


def test_a_trailing_newline_does_not_make_a_malformed_reference_well_formed():
    """REF_PATTERN is ``^...$`` and Python's ``$`` also matches before a final newline, so ``"call[0].artifact_id\\n"``
    parses (and passes the ``BatchResultRef`` wire model, which uses the same Python engine). The published JSON Schema
    carries the same ``pattern``, which an ECMA-262 validator (``$`` is strict there) refuses: the grammars disagree.
    Expected: refused here too."""
    assert parse_ref("call[0].artifact_id\n") is None
    with pytest.raises(Invalid):
        accept([src(), train(source={"$ref": "call[0].artifact_id\n"})])


@pytest.mark.parametrize("extra", [{"default": "a"}, {"x": 1}, {"$ref2": "call[0].artifact_id"}])
def test_extra_keys_beside_a_reference_are_refused(extra):
    with pytest.raises(Invalid, match="a reference is exactly") as e:
        accept([src(), train(source={"$ref": "call[0].artifact_id", **extra})])
    assert e.value.details["path"] == "inputs.source"


@pytest.mark.parametrize("value", [3, None, True, ["call[0].artifact_id"], {"call": 0}])
def test_a_non_string_reference_is_refused(value):
    with pytest.raises(Invalid, match="a reference is exactly"):
        accept([src(), train(source={"$ref": value})])


def test_a_reference_object_nested_anywhere_is_checked():
    with pytest.raises(Invalid):
        accept([src(), train(a=[{"b": {"$ref": 3}}])])
    with pytest.raises(Invalid):
        accept([src(), train(a=[[{"$ref": "call[0].artifact_id", "x": 1}]])])


@pytest.mark.parametrize("index", [2, 3, 99])
def test_out_of_range_index_is_refused(index):
    with pytest.raises(Invalid, match="names call") as e:
        accept([src(), train(source=ref(index, "artifact_id"))])
    assert e.value.details["call"] == 1 and e.value.details["ref"] == f"call[{index}].artifact_id"


def test_forward_reference_is_refused():
    with pytest.raises(Invalid, match="forward or self"):
        accept([train(source=ref(1, "artifact_id")), src()])


def test_self_reference_is_refused():
    with pytest.raises(Invalid, match="forward or self"):
        accept([src(), train(source=ref(1, "job_id"))])


def test_a_cycle_cannot_be_written():
    with pytest.raises(Invalid, match="forward or self"):
        accept([train(a=ref(1, "job_id")), train(b=ref(0, "job_id"))])


FIELD_NOT_PRODUCED = [
    pytest.param([src(), train(x=ref(0, "job_id"))], id="job_id-of-author_source"),
    pytest.param([src(), train(x=ref(0, "trial_id"))], id="trial_id-of-author_source"),
    pytest.param([train(), train(x=ref(0, "artifact_id"))], id="artifact_id-of-run_component"),
    pytest.param([train(), train(x=ref(0, "outputs"))], id="bare-outputs"),
    pytest.param([train(), train(x=ref(0, "nonsense"))], id="unknown-field"),
    pytest.param([note(), train(x=ref(0, "note_id"))], id="tool-without-result-fields"),
    pytest.param([minimal("bind_component"), train(x=ref(0, "component_ref"))], id="bind_component-has-none"),
    pytest.param([minimal("file_claim"), train(x=ref(0, "job_id"))], id="job_id-of-file_claim"),
]


@pytest.mark.parametrize("calls", FIELD_NOT_PRODUCED)
def test_a_field_the_target_tool_does_not_produce_is_refused(calls):
    with pytest.raises(Invalid, match="names no result of") as e:
        accept(calls)
    assert e.value.details["call"] == 1 and "allowed" in e.value.details


def test_the_refusal_lists_what_the_tool_does_produce():
    with pytest.raises(Invalid) as e:
        accept([src(), train(x=ref(0, "job_id"))])
    assert e.value.details["allowed"] == ["artifact_id"]
    with pytest.raises(Invalid) as e:
        accept([train(), train(x=ref(0, "artifact_id"))])
    assert e.value.details["allowed"] == [*turns.RESULT_FIELDS["run_component"], "outputs.<slot>"]


@pytest.mark.parametrize("tool", [t for t in turns.EFFECT_TOOLS if t not in turns.JOB_TOOLS])
def test_output_slots_are_refused_on_a_tool_that_admits_no_job(tool):
    with pytest.raises(Invalid, match="names no result of"):
        turns.validate_decision({"calls": [minimal(tool), note(ref(0, "outputs.predictions"))]},
                                max_calls=6, allow_prepare_candidate=tool == "prepare_candidate")


@pytest.mark.parametrize("tool", turns.JOB_TOOLS)
def test_output_slots_are_accepted_on_every_tool_that_admits_a_job(tool):
    d = accept([minimal(tool), note(ref(0, "outputs.predictions"))])
    assert d["calls"][1]["depends_on"] == [0]


@pytest.mark.parametrize("tool, field", [(t, f) for t, fields in turns.RESULT_FIELDS.items() for f in fields])
def test_every_declared_result_field_can_be_referenced(tool, field):
    d = accept([minimal(tool), note(ref(0, field))])
    assert d["calls"][1]["depends_on"] == [0]


@pytest.mark.parametrize("name", ["component_ref", "environment", "investigation_id", "purpose"])
def test_a_reference_in_a_literal_argument_is_refused(name):
    args = {"component_ref": "eidos.project.command@1.0.0", name: ref(0, "artifact_id")}
    with pytest.raises(Invalid, match="must be literal") as e:
        accept([src(), {"tool": "run_component", "args": args}])
    assert e.value.details["path"] == name


def test_a_reference_nested_in_a_literal_argument_is_refused():
    args = {"component_ref": "eidos.project.command@1.0.0", "environment": {"name": ref(0, "artifact_id")}}
    with pytest.raises(Invalid, match="must be literal") as e:
        accept([src(), {"tool": "run_component", "args": args}])
    assert e.value.details["path"] == "environment.name"


def test_a_reference_in_other_arguments_is_allowed():
    d = accept([src(), {"tool": "run_component", "args": {"component_ref": "c@1.0.0", "environment": "default",
                                                          "config": {"seed": 1}, "inputs": {"s": ref(0, "artifact_id")}}}])
    assert d["calls"][1]["depends_on"] == [0]


def test_resource_policy_accepts_a_concurrency_request():
    d = accept([train(), train()], resource_policy={"max_concurrent_jobs": 2})
    assert d["resource_policy"] == {"max_concurrent_jobs": 2}


@pytest.mark.parametrize("policy", [None, {}])
def test_no_resource_policy_means_none_is_recorded(policy):
    assert "resource_policy" not in accept([train()], resource_policy=policy)
    assert "resource_policy" not in accept([train()])


@pytest.mark.parametrize("policy", [
    {"max_concurrent_jobs": 2, "extra": 1}, {"other": 2}, {"max_concurrent_jobs": 0}, {"max_concurrent_jobs": -1},
    {"max_concurrent_jobs": True}, {"max_concurrent_jobs": False}, {"max_concurrent_jobs": "2"},
    {"max_concurrent_jobs": 2.0}, {"max_concurrent_jobs": None}, {"max_concurrent_jobs": [2]},
    "serial", 2, ["max_concurrent_jobs"]])
def test_other_resource_policy_shapes_are_refused(policy):
    with pytest.raises(Invalid, match="resource_policy"):
        accept([train()], resource_policy=policy)


# =====================================================================================================================
# b) resolve_refs
# =====================================================================================================================
def test_resolve_refs_substitutes_nested_values_and_returns_provenance():
    known = {(0, "artifact_id"): "art-1", (1, "outputs.predictions"): "art-pred", (1, "job_id"): "job-1"}
    seen: list[tuple[int, str]] = []

    def lookup(index: int, field: str) -> str:
        seen.append((index, field))
        return known[(index, field)]

    args = {"component_ref": "eidos.score@1.0.0", "n": 3, "flag": None,
            "inputs": {"source": ref(0, "artifact_id"), "preds": [ref(1, "outputs.predictions"), "literal", 7]},
            "config": {"deep": [{"job": ref(1, "job_id")}]}}
    before = copy.deepcopy(args)
    value, used = resolve_refs(args, lookup)
    assert value == {"component_ref": "eidos.score@1.0.0", "n": 3, "flag": None,
                     "inputs": {"source": "art-1", "preds": ["art-pred", "literal", 7]},
                     "config": {"deep": [{"job": "job-1"}]}}
    assert used == [{"path": "inputs.source", "ref": "call[0].artifact_id", "value": "art-1"},
                    {"path": "inputs.preds[0]", "ref": "call[1].outputs.predictions", "value": "art-pred"},
                    {"path": "config.deep[0].job", "ref": "call[1].job_id", "value": "job-1"}]
    assert seen == [(0, "artifact_id"), (1, "outputs.predictions"), (1, "job_id")]
    assert args == before  # the persisted arguments are never rewritten


def test_resolve_refs_with_no_reference_returns_the_value_and_no_provenance():
    args = {"a": [1, {"b": "call[0].artifact_id"}], "c": None}
    value, used = resolve_refs(args, lambda i, f: pytest.fail("nothing to look up"))
    assert value == args and used == []


def test_resolve_refs_of_a_bare_reference_has_an_empty_path():
    value, used = resolve_refs({"$ref": "call[2].job_id"}, lambda i, f: f"{f}@{i}")
    assert value == "job_id@2"
    assert used == [{"path": "", "ref": "call[2].job_id", "value": "job_id@2"}]


def test_resolve_refs_refuses_a_malformed_reference_and_lets_lookup_errors_through():
    with pytest.raises(Invalid):
        resolve_refs({"x": {"$ref": "nonsense"}}, lambda i, f: "v")

    class Gone(Exception):
        pass

    def lookup(i: int, f: str) -> Any:
        raise Gone(f)

    with pytest.raises(Gone):
        resolve_refs({"x": ref(0, "artifact_id")}, lookup)


# =====================================================================================================================
# c) execute_decision against a fake lab
# =====================================================================================================================
JOB_COMMANDS = ("run_component", "launch_trial", "launch_branch", "validate_component")


class FakeLab:
    """One object standing for both the ``investigator`` and the ``workflow`` ``LabClient`` of the activities."""

    def __init__(self, calls: list[dict], *, control: dict | None = None, resource_policy: dict | None = None):
        proposal: dict[str, Any] = {"rationale": "r", "calls": calls, "control": control or {}}
        if resource_policy:
            proposal["resource_policy"] = resource_policy
        decided = validate_decision(proposal, max_calls=16)
        self.body = {**decided, "calls": keyed_calls(P, TURN, DEC, decided["calls"])}
        self.investigation_status = "active"
        self.jobs: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.polls: Counter[str] = Counter()
        self.queries: Counter[str] = Counter()
        self.sent: list[dict] = []  # every command and upload sent, replays included
        self.effects: list[dict] = []  # the ones the lab executed (not replayed): name, args, key
        self.recorded: dict[str, list[dict]] = {}  # record_decision_receipts: key -> receipts
        self.responses: dict[str, dict] = {}  # command key -> first response (a repeat replays it)
        self.on_query: dict[str, Callable[[dict], None]] = {}
        self.fail: dict[str, LabError] = {}  # command name -> the error the lab refuses it with
        self.omit: set[str] = set()  # result fields the lab does not return

    # -- the client surface the activities use ---------------------------------------------------------------
    def query(self, name: str, params: dict | None = None, *, project: str | None = None) -> Any:
        assert project == P
        params = params or {}
        self.queries[name] += 1
        if name in self.on_query:
            self.on_query[name](params)
        if name == "get_turn":
            return {"id": TURN, "status": "open",
                    "decisions": [{"id": DEC, "accepted": True, "revoked": False, "body": copy.deepcopy(self.body)}]}
        if name == "get_job":
            return self._poll(params["job_id"])
        if name == "list_jobs":
            return [copy.deepcopy(j) for j in self.jobs.values() if j["status"] in params["statuses"]
                    and params.get("plan_id") in (None, j.get("plan_id"))]
        if name == "get_record":
            assert params["kind"] == "investigation"
            return {"id": params["id"], "status": self.investigation_status}
        raise AssertionError(f"unexpected query {name}")

    def command(self, name: str, payload: dict | None = None, *, project: str | None = None, key: str | None = None,
                expected_revision: int | None = None, normalized_arguments: Any = None,
                admission: dict | None = None) -> dict:
        assert project == P and key
        self.sent.append({"name": name, "payload": copy.deepcopy(payload), "key": key, "admission": admission})
        if key in self.responses:
            return {**copy.deepcopy(self.responses[key]), "replayed": True}
        if name in self.fail:
            raise self.fail[name]
        result = self._apply(name, payload or {}, key)
        self.responses[key] = {"result": result}
        return {"result": copy.deepcopy(result), "replayed": False}

    def upload(self, project: str, path: Any = None, *, data: bytes | None = None, media_type: str = "",
               schema: str | None = None, role: str = "input", access_class: str = "project", name: str | None = None,
               key: str | None = None, investigation: str | None = None, admission: dict | None = None) -> dict:
        assert project == P and key
        self.sent.append({"name": "upload", "payload": {"data": data, "name": name, "schema": schema, "role": role},
                          "key": key, "admission": admission})
        if key in self.responses:
            return {**copy.deepcopy(self.responses[key]), "replayed": True}
        n = 1 + sum(1 for e in self.effects if e["name"] == "upload")
        self.effects.append({"name": "upload", "args": {"data": data}, "key": key})
        self.responses[key] = {"result": {"artifact_id": f"art-src-{n}"}}
        return {**copy.deepcopy(self.responses[key]), "replayed": False}

    # -- the lab's behaviour ----------------------------------------------------------------------------------
    def _apply(self, name: str, payload: dict, key: str) -> dict:
        if name == "hand_off_decision":
            return {"handed_off": True}
        if name == "record_decision_receipts":
            self.recorded[key] = copy.deepcopy(payload["receipts"])
            return {"recorded": True}
        self.effects.append({"name": name, "args": copy.deepcopy(payload), "key": key})
        n = len(self.effects)
        out: dict[str, Any] = {}
        if name == "launch_evidence_plan":
            plan_id = payload["plan_id"]
            job_id = f"plan-job-{len(self.jobs) + 1}"
            self.jobs[job_id] = {"id": job_id, "status": "running", "outputs": [], "terminal": None, "plan_id": plan_id}
            return {"plan_id": plan_id, "jobs": {"baseline": [job_id]}, "pending_steps": 1}
        if name in JOB_COMMANDS:
            job_id = f"job-{len(self.jobs) + 1}"
            self.jobs[job_id] = {"id": job_id, "status": "running", "outputs": [], "terminal": None}
            if self.plans.get(job_id, {}).get("after") == 0:
                self._finish(job_id)
            out = {"job_id": job_id, "trial_id": f"trial-{n}", "lock_digest": f"sha256:{n}",
                   "component": payload.get("component_ref")}
        elif name == "file_claim":
            out = {"claim_id": f"claim-{n}"}
        for f in self.omit:
            out.pop(f, None)
        return out

    def _poll(self, job_id: str) -> dict:
        self.polls[job_id] += 1
        plan = self.plans.get(job_id)
        if plan and self.jobs[job_id]["status"] == "running" and self.polls[job_id] > plan["after"]:
            self._finish(job_id)
        return copy.deepcopy(self.jobs[job_id])

    def _finish(self, job_id: str) -> None:
        plan, job = self.plans[job_id], self.jobs[job_id]
        job["status"] = plan["status"]
        job["outputs"] = [{"slot": s, "artifact_id": a} for s, a in plan["outputs"].items()]
        if plan["status"] == "failed":
            job["terminal"] = {"error": {"category": "user-code", "message": plan["error"]}}

    # -- what a test says about jobs ---------------------------------------------------------------------------
    def plan(self, job_id: str, *, after: int, status: str = "succeeded", outputs: dict | None = None,
             error: str = "boom") -> None:
        """``job_id`` ends ``status`` once ``get_job`` has been asked about it ``after`` times (0: when admitted)."""
        self.plans[job_id] = {"after": after, "status": status, "outputs": outputs or {}, "error": error}

    def finish(self, job_id: str, status: str = "succeeded", outputs: dict | None = None, error: str = "boom") -> None:
        self.plan(job_id, after=0, status=status, outputs=outputs, error=error)
        self._finish(job_id)

    # -- what a test asks about the outcome --------------------------------------------------------------------
    def executed(self, name: str) -> list[dict]:
        return [e for e in self.effects if e["name"] == name]

    def sent_named(self, name: str) -> list[dict]:
        return [s for s in self.sent if s["name"] == name]

    @property
    def receipts(self) -> list[dict]:
        assert len(self.recorded) == 1, f"expected one receipt record, got {sorted(self.recorded)}"
        return next(iter(self.recorded.values()))


def activities(lab: FakeLab, *, wait_slice: float = 0.05, poll: float = 0.001) -> InvestigatorActivities:
    return InvestigatorActivities(lab, lab, batch_wait_slice=wait_slice, batch_poll=poll)  # type: ignore[arg-type]


def execute(acts: InvestigatorActivities, *, attempt: int = 1, **extra: Any) -> dict:
    """One execute_decision activity attempt, under a Temporal activity context (run ``run-A``, activity ``exec-1``)."""
    env = ActivityEnvironment()
    env.info = env.info.__class__(**{**env.info.__dict__, "attempt": attempt, "workflow_run_id": "run-A",
                                     "activity_id": "exec-1"})
    return env.run(acts.execute_decision, {"turn_id": TURN, "project_id": P, "investigation_id": INV,
                                           "decision_id": DEC, **extra})


def codes(receipts: list[dict]) -> list[str | None]:
    return [r.get("code") for r in receipts]


# -- the batch that works --------------------------------------------------------------------------------------------
def test_source_train_score_resolves_references_and_records_provenance():
    lab = FakeLab(pipeline())
    lab.plan("job-1", after=2, outputs={"predictions": "art-pred"})  # training takes a few polls
    lab.plan("job-2", after=0)
    out = execute(activities(lab, wait_slice=10.0))

    assert "pending" not in out and "stopped" not in out
    assert out["summary"] == [{"tool": "author_source", "ok": True, "replayed": False, "code": None},
                              {"tool": "run_component", "ok": True, "replayed": False, "code": None},
                              {"tool": "run_component", "ok": True, "replayed": False, "code": None}]
    # the effects ran in order, the dependent ones with the dependency's value substituted, never the reference
    assert [e["name"] for e in lab.effects] == ["upload", "run_component", "run_component"]
    train_args, score_args = (e["args"] for e in lab.executed("run_component"))
    assert train_args["inputs"] == {"source": "art-src-1"}
    assert score_args["inputs"] == {"predictions": "art-pred", "tags": ["art-src-1"]}
    assert train_args["investigation_id"] == INV and score_args["component_ref"] == "eidos.score@1.0.0"
    assert lab.polls["job-1"] >= 3  # the score call waited for the training job (it succeeded on the third look)
    # every effect, the upload included, was admitted against this decision and execution
    admission = {"decision_id": DEC, "execution": EXECUTION}
    effects = [s for s in lab.sent if s["name"] not in ("hand_off_decision", "record_decision_receipts")]
    assert len(effects) == 3 and all(s["admission"] == admission for s in effects)
    assert lab.sent_named("hand_off_decision")[0]["payload"] == {"decision_id": DEC, "execution": EXECUTION}

    receipts = lab.receipts
    assert list(lab.recorded) == [RECEIPTS_KEY] and [r["index"] for r in receipts] == [0, 1, 2]
    r0, r1, r2 = receipts
    assert "depends_on" not in r0 and "resolved" not in r0  # a call that names nothing has no provenance
    assert r1["depends_on"] == [0] and r1["command_key"] == command_key(P, TURN, DEC, 1)
    assert r1["resolved"] == [{"path": "inputs.source", "ref": "call[0].artifact_id", "value": "art-src-1"}]
    assert r1["result"]["job_id"] == "job-1"
    assert r2["depends_on"] == [0, 1] and r2["command_key"] == command_key(P, TURN, DEC, 2)
    assert r2["resolved"] == [
        {"path": "inputs.predictions", "ref": "call[1].outputs.predictions", "value": "art-pred"},
        {"path": "inputs.tags[0]", "ref": "call[0].artifact_id", "value": "art-src-1"}]
    # the recorded receipts are the replay-invariant ones: no replay marker, no cached job record
    assert all("_job" not in r and "replayed" not in r for r in receipts)


def test_a_reference_to_a_call_that_admits_no_job_does_not_wait_or_poll():
    lab = FakeLab([src(), note(ref(0, "artifact_id")), {"tool": "file_claim", "args": {
        "statement": "s", "kind": "improves", "scope": {"artifact": ref(0, "artifact_id")}}}])
    out = execute(activities(lab, wait_slice=10.0))
    assert [s["ok"] for s in out["summary"]] == [True, True, True]
    assert lab.queries["get_job"] == 0 and lab.queries["get_record"] == 0  # nothing to wait for
    assert lab.executed("record_note")[0]["args"]["text"] == "art-src-1"
    assert lab.executed("file_claim")[0]["args"]["scope"] == {"artifact": "art-src-1"}


# -- a job that is still running: pending and continuation -------------------------------------------------------------
def test_a_running_job_returns_pending_and_a_continuation_does_not_admit_again():
    lab = FakeLab(pipeline())  # job-1 never finishes on its own
    acts = activities(lab)  # a 50 ms slice
    first = execute(acts)
    assert first == {"pending": True, "next_call": 2, "continuation": 1}
    assert lab.recorded == {}  # a decision that is still waiting records no receipts yet
    assert [e["name"] for e in lab.effects] == ["upload", "run_component"]
    admitted = [(s["name"], s["key"]) for s in lab.sent if s["name"] in ("upload", "run_component")]

    # still running: a later slice goes on waiting and hands the wait back again, with the next number
    again = execute(acts, continuation=1)
    assert again == {"pending": True, "next_call": 2, "continuation": 2}
    assert len(lab.effects) == 2

    # the job succeeds: the next slice finishes the batch. Committed calls replay by key; only call 2 is new.
    lab.plan("job-2", after=0)
    lab.finish("job-1", outputs={"predictions": "art-pred"})
    done = execute(acts, continuation=2)
    assert "pending" not in done
    assert [s["replayed"] for s in done["summary"]] == [True, True, False]
    assert [e["name"] for e in lab.effects] == ["upload", "run_component", "run_component"]  # no duplicate effect
    # the committed calls were sent again with the very same keys (three slices, and a new call only in the last one)
    k0, k1, k2 = (command_key(P, TURN, DEC, i) for i in range(3))
    assert [s["key"] for s in lab.sent_named("upload")] == [k0, k0, k0]
    assert [s["key"] for s in lab.sent_named("run_component")] == [k1, k1, k1, k2]
    assert admitted == [("upload", k0), ("run_component", k1)]
    assert [r["index"] for r in lab.receipts] == [0, 1, 2]
    assert lab.receipts[2]["resolved"][0] == {"path": "inputs.predictions", "ref": "call[1].outputs.predictions",
                                              "value": "art-pred"}
    # every slice was handed off under its own key, so a run that lost the turn meanwhile is refused at once
    handoffs = lab.sent_named("hand_off_decision")
    assert len(handoffs) == 3 and len({h["key"] for h in handoffs}) == 3


def test_the_pending_result_is_small_and_names_where_it_stopped():
    lab = FakeLab(pipeline())
    out = execute(activities(lab))
    assert set(out) == {"pending", "next_call", "continuation"}


# -- failed jobs and dependencies --------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["failed", "cancelled", "pruned"])
def test_a_job_that_cannot_succeed_stops_its_dependents_transitively_but_not_independent_calls(status):
    lab = FakeLab([
        src(),                                              # 0
        train(source=ref(0, "artifact_id")),                # 1: job-1, which will not succeed
        score(predictions=ref(1, "outputs.predictions")),   # 2: names job-1's output
        {"tool": "file_claim", "args": {"statement": "s", "kind": "improves",
                                        "scope": {"job": ref(2, "job_id")}}},  # 3: names call 2 only
        note(),                                             # 4: independent
        train(),                                            # 5: independent job, queued behind job-1
    ])
    lab.plan("job-1", after=2, status=status, error="boom")
    out = execute(activities(lab, wait_slice=10.0))

    receipts = lab.receipts
    assert codes(receipts) == [None, None, "DEPENDENCY_FAILED", "DEPENDENCY_FAILED", None, None]
    assert [r["ok"] for r in receipts] == [True, True, False, False, True, True]
    r2, r3 = receipts[2], receipts[3]
    assert r2["skipped"] is True and r2["command_key"] == command_key(P, TURN, DEC, 2) and r2["depends_on"] == [1]
    assert r2["error"]["details"]["depends_on"] == 1 and r2["error"]["details"]["job_id"] == "job-1"
    assert r2["error"]["details"]["job_status"] == status
    assert r2["error"]["details"]["error"] == ({"category": "user-code", "message": "boom"} if status == "failed" else None)
    assert "job-1" in r2["error"]["message"]
    # the claim is skipped because call 2 did not run, without asking the lab about any job for it
    assert r3["skipped"] is True and r3["depends_on"] == [2]
    assert r3["error"]["details"]["depends_on"] == 2 and r3["error"]["details"]["dependency_code"] == "DEPENDENCY_FAILED"
    # what was independent still ran, and the skipped calls were never sent to the lab
    assert [e["name"] for e in lab.effects] == ["upload", "run_component", "record_note", "run_component"]
    assert lab.executed("record_note")[0]["args"]["text"] == "independent note"
    assert not lab.executed("file_claim")
    assert [s["code"] for s in out["summary"]] == codes(receipts)


def test_a_dependency_refused_at_admission_stops_its_dependents_without_polling():
    lab = FakeLab([train(), score(predictions=ref(0, "outputs.predictions"))])
    lab.fail["run_component"] = LabError(409, {"code": "COMPONENT_NOT_RUNNABLE", "message": "no"})
    out = execute(activities(lab, wait_slice=10.0))
    r0, r1 = lab.receipts
    assert r0["ok"] is False and r0["code"] == "COMPONENT_NOT_RUNNABLE"
    assert r1["code"] == "DEPENDENCY_FAILED" and r1["error"]["details"]["dependency_code"] == "COMPONENT_NOT_RUNNABLE"
    assert lab.queries["get_job"] == 0 and "pending" not in out


def test_a_missing_output_slot_is_ref_unresolved():
    lab = FakeLab([src(), train(source=ref(0, "artifact_id")), score(predictions=ref(1, "outputs.predictions")),
                   note(ref(2, "job_id"))])
    lab.plan("job-1", after=1, outputs={"metrics": "art-metrics", "log": "art-log"})
    execute(activities(lab, wait_slice=10.0))
    r2, r3 = lab.receipts[2], lab.receipts[3]
    assert r2["ok"] is False and r2["code"] == "REF_UNRESOLVED" and r2["skipped"] is True
    assert "predictions" in r2["error"]["message"] and r2["command_key"] == command_key(P, TURN, DEC, 2)
    assert r2["error"]["details"] == {"depends_on": 1, "job_id": "job-1", "published": ["log", "metrics"]}
    assert len(lab.executed("run_component")) == 1  # the score call never reached the lab
    assert r3["code"] == "DEPENDENCY_FAILED"  # and what named it is skipped in turn


def test_a_result_field_the_lab_did_not_return_is_ref_unresolved():
    lab = FakeLab([train(), note(ref(0, "lock_digest"))])
    lab.omit = {"lock_digest"}
    lab.plan("job-1", after=0)  # the job succeeds; it is the call's own result that lacks the field
    execute(activities(lab, wait_slice=10.0))
    r1 = lab.receipts[1]
    assert r1["code"] == "REF_UNRESOLVED" and "lock_digest" in r1["error"]["message"]
    assert r1["error"]["details"] == {"depends_on": 0}
    assert not lab.executed("record_note")


# -- job calls are sequential unless the decision asks for more --------------------------------------------------------
def test_job_calls_are_sequential_by_default():
    lab = FakeLab([train(), score(), note()])  # two independent jobs and a note
    acts = activities(lab)
    first = execute(acts)
    assert first == {"pending": True, "next_call": 1, "continuation": 1}  # the second job waits for the first
    assert [e["name"] for e in lab.executed("run_component")] == ["run_component"]
    assert lab.polls["job-1"] >= 1 and not lab.recorded

    lab.finish("job-1")
    done = execute(acts, continuation=1)
    assert "pending" not in done and [s["ok"] for s in done["summary"]] == [True, True, True]
    assert len(lab.executed("run_component")) == 2  # admitted once the first had finished
    assert [s["replayed"] for s in done["summary"]] == [True, False, False]
    assert [r["index"] for r in lab.receipts] == [0, 1, 2]


def test_a_failed_job_frees_its_slot_for_the_next_job_call():
    lab = FakeLab([train(), score()])
    lab.plan("job-1", after=1, status="failed")
    out = execute(activities(lab, wait_slice=10.0))
    assert [s["ok"] for s in out["summary"]] == [True, True] and len(lab.executed("run_component")) == 2


def test_a_resource_policy_admits_jobs_up_to_its_limit():
    lab = FakeLab([train(), train(), train()], resource_policy={"max_concurrent_jobs": 2})
    acts = activities(lab)
    first = execute(acts)
    assert first == {"pending": True, "next_call": 2, "continuation": 1}
    assert len(lab.executed("run_component")) == 2  # both of the first two were admitted at once, the third waits
    lab.finish("job-1")
    done = execute(acts, continuation=1)
    assert "pending" not in done and len(lab.executed("run_component")) == 3
    assert [s["replayed"] for s in done["summary"]] == [True, True, False]


def test_with_a_resource_policy_both_jobs_are_admitted_without_waiting():
    lab = FakeLab([train(), score()], resource_policy={"max_concurrent_jobs": 2})
    out = execute(activities(lab))
    assert "pending" not in out and [s["ok"] for s in out["summary"]] == [True, True]
    assert len(lab.executed("run_component")) == 2
    assert lab.queries["get_job"] == 0  # the second job did not wait for the first


def test_a_policy_of_one_is_the_default():
    lab = FakeLab([train(), score()], resource_policy={"max_concurrent_jobs": 1})
    assert execute(activities(lab))["pending"] is True and len(lab.executed("run_component")) == 1


def test_a_dependent_job_call_waits_for_its_dependency_even_with_a_resource_policy():
    lab = FakeLab([train(), score(predictions=ref(0, "outputs.predictions"))], resource_policy={"max_concurrent_jobs": 4})
    out = execute(activities(lab))
    assert out == {"pending": True, "next_call": 1, "continuation": 1}
    assert len(lab.executed("run_component")) == 1


# -- the final slice, a stop while waiting -------------------------------------------------------------------------------
FINAL_SCENARIOS = [
    pytest.param(lambda: [src(), train(source=ref(0, "artifact_id")), score(predictions=ref(1, "outputs.predictions")),
                          note()], 2, id="waits-for-a-dependencys-job"),
    pytest.param(lambda: [train(), score(), note()], 1, id="queued-behind-a-running-job"),
]


@pytest.mark.parametrize("calls, blocked", FINAL_SCENARIOS)
def test_the_final_slice_records_batch_deadline_without_waiting(calls, blocked):
    lab = FakeLab(calls())  # job-1 keeps running
    started = time.monotonic()
    out = execute(activities(lab, wait_slice=30.0, poll=2.0), final=True)
    assert time.monotonic() - started < 5.0  # it did not sit out a slice or a poll interval
    assert "pending" not in out and "stopped" not in out
    receipts = lab.receipts
    r = receipts[blocked]
    assert r["ok"] is False and r["code"] == "BATCH_DEADLINE" and r["skipped"] is True
    assert r["command_key"] == command_key(P, TURN, DEC, blocked)
    assert r["error"]["details"]["running_jobs"] == ["job-1"]
    assert out["summary"][blocked]["code"] == "BATCH_DEADLINE"
    assert len(lab.executed("run_component")) == 1  # the blocked call was never admitted
    assert receipts[-1]["ok"] is True and receipts[-1]["tool"] == "record_note"  # an independent call still runs
    assert not any(c == "BATCH_DEADLINE" for c in codes(receipts[:blocked]))


def test_the_final_slice_still_runs_whatever_is_ready():
    lab = FakeLab(pipeline())
    lab.plan("job-1", after=0, outputs={"predictions": "art-pred"})
    lab.plan("job-2", after=0)
    out = execute(activities(lab, wait_slice=30.0, poll=2.0), final=True)
    assert [s["ok"] for s in out["summary"]] == [True, True, True]
    assert lab.executed("run_component")[1]["args"]["inputs"]["predictions"] == "art-pred"


def test_a_call_that_names_a_deadline_skipped_call_is_dependency_failed():
    lab = FakeLab([src(), train(source=ref(0, "artifact_id")), score(predictions=ref(1, "outputs.predictions")),
                   note(ref(2, "job_id"))])
    execute(activities(lab, wait_slice=30.0, poll=2.0), final=True)
    r2, r3 = lab.receipts[2], lab.receipts[3]
    assert r2["code"] == "BATCH_DEADLINE" and r3["code"] == "DEPENDENCY_FAILED"
    assert r3["error"]["details"]["dependency_code"] == "BATCH_DEADLINE"


STOP_SCENARIOS = [
    pytest.param(lambda: [src(), train(source=ref(0, "artifact_id")), score(predictions=ref(1, "outputs.predictions"))], 2,
                 id="waiting-for-a-dependency"),
    pytest.param(lambda: [train(), score()], 1, id="queued-behind-a-running-job"),
]


@pytest.mark.parametrize("calls, blocked", STOP_SCENARIOS)
def test_a_stop_while_waiting_returns_stopped_with_an_effect_not_admitted_receipt(calls, blocked):
    lab = FakeLab(calls())
    # the operator stops the investigation while the batch is waiting for the running job
    lab.on_query["get_job"] = lambda params: setattr(lab, "investigation_status", "stopped")
    out = execute(activities(lab, wait_slice=10.0))

    assert out["stopped"] == "investigation-stopped" and "pending" not in out
    assert out["summary"][-1] == {"tool": lab.body["calls"][blocked]["tool"], "ok": False, "replayed": None,
                                  "code": "EFFECT_NOT_ADMITTED"}
    assert len(out["summary"]) == blocked + 1
    # the receipts of a stopped decision have their own key (per refused call), and name the reason
    assert list(lab.recorded) == [f"{RECEIPTS_KEY}:stopped:{blocked}"]
    last = lab.receipts[-1]
    assert last["index"] == blocked and last["ok"] is False and last["code"] == "EFFECT_NOT_ADMITTED"
    assert last["error"]["details"] == {"reason": "investigation-stopped"} and "not started" in last["error"]["message"]
    assert len(lab.executed("run_component")) == 1  # the job admitted before the stop is untouched; nothing after it runs


def test_a_pause_while_waiting_is_a_stop_too():
    lab = FakeLab(pipeline())
    lab.on_query["get_job"] = lambda params: setattr(lab, "investigation_status", "paused")
    out = execute(activities(lab, wait_slice=10.0))
    assert out["stopped"] == "investigation-paused"
    assert lab.receipts[-1]["error"]["details"] == {"reason": "investigation-paused"}


# -- evidence plans use the same CPU allowance (ADR 0022 addendum R11) ----------------------------------------------------
def launch_plan(**args: Any) -> dict:
    return {"tool": "launch_evidence_plan", "args": {"plan_id": "plan-1", **args}}


def test_a_plan_waits_for_a_running_job_of_the_batch():
    lab = FakeLab([train(), launch_plan()])
    acts = activities(lab)
    out = execute(acts)
    assert out["pending"] is True and out["next_call"] == 1
    assert lab.executed("launch_evidence_plan") == []
    lab.finish("job-1")
    assert "pending" not in execute(acts, continuation=out["continuation"])
    assert lab.executed("launch_evidence_plan")[0]["args"]["max_concurrent_jobs"] == 1


def test_compute_after_a_plan_waits_for_the_plans_last_job():
    lab = FakeLab([launch_plan(), train()])
    acts = activities(lab)
    out = execute(acts)
    assert out["pending"] is True and out["next_call"] == 1 and lab.executed("run_component") == []
    # the plan's next step takes over in the same transaction that ended the previous one: still the plan's allowance
    lab.jobs["plan-job-1"]["status"] = "succeeded"
    lab.jobs["plan-job-2"] = {"id": "plan-job-2", "status": "running", "outputs": [], "terminal": None, "plan_id": "plan-1"}
    assert execute(acts, continuation=1)["pending"] is True and lab.executed("run_component") == []
    lab.jobs["plan-job-2"]["status"] = "failed"  # a failed plan job ends the plan's claim like any other end
    assert "pending" not in execute(acts, continuation=2)
    assert len(lab.executed("run_component")) == 1


def test_a_plan_is_not_a_job_dependency_and_its_plan_id_needs_no_wait():
    lab = FakeLab([launch_plan(), note(ref(0, "plan_id"))])
    out = execute(activities(lab, wait_slice=10.0))
    assert [s["ok"] for s in out["summary"]] == [True, True] and lab.executed("record_note")[0]["args"]["text"] == "plan-1"


def test_a_plan_has_no_output_slots_to_reference():
    with pytest.raises(Invalid, match="names no result"):
        accept([launch_plan(), train(x=ref(0, "outputs.table"))])


@pytest.mark.parametrize("policy, launched", [(None, 1), ({"max_concurrent_jobs": 2}, 2)])
def test_a_plan_launch_carries_the_decisions_allowance_not_the_models_own_argument(policy, launched):
    lab = FakeLab([launch_plan(max_concurrent_jobs=99)], resource_policy=policy)
    assert "pending" not in execute(activities(lab, wait_slice=10.0))
    assert lab.executed("launch_evidence_plan")[0]["args"]["max_concurrent_jobs"] == launched


def test_under_concurrency_a_plan_still_starts_only_on_an_idle_batch_and_holds_it():
    lab = FakeLab([train(), launch_plan(), train()], resource_policy={"max_concurrent_jobs": 2})
    acts = activities(lab)
    out = execute(acts)
    assert out["next_call"] == 1 and lab.executed("launch_evidence_plan") == []  # one job of two running: the plan waits
    lab.finish("job-1")
    out = execute(acts, continuation=1)
    assert out["next_call"] == 2 and len(lab.executed("run_component")) == 1  # the plan holds both slots
    lab.jobs["plan-job-2"]["status"] = "succeeded"
    assert "pending" not in execute(acts, continuation=2)


def test_the_final_slice_names_the_plans_running_jobs():
    lab = FakeLab([launch_plan(), train()])
    out = execute(activities(lab), final=True)
    assert "pending" not in out
    last = lab.receipts[-1]
    assert last["code"] == "BATCH_DEADLINE" and last["error"]["details"]["running_jobs"] == ["plan-job-1"]


def test_wait_for_jobs_after_a_plan_whose_jobs_all_finished_leaves_something_to_wait_for():
    lab = FakeLab([launch_plan()], control={"action": "wait_for_jobs"})
    lab.on_query["list_jobs"] = lambda params: lab.jobs["plan-job-1"].update(status="succeeded")  # done before the wait
    out = execute(activities(lab, wait_slice=10.0))
    assert lab.queries["list_jobs"] == 1 and "nothing_outstanding" not in out


# -- wait_for_jobs after a batch ----------------------------------------------------------------------------------------
WAIT = {"action": "wait_for_jobs"}


def test_wait_for_jobs_after_a_batch_whose_jobs_all_finished_leaves_something_to_wait_for():
    """Sequential jobs may all have finished by the time the decision's calls are done; their events are the news the
    wait is for, so the turn must not be told that nothing is outstanding."""
    lab = FakeLab(pipeline(), control=WAIT)
    lab.plan("job-1", after=1, outputs={"predictions": "art-pred"})
    lab.plan("job-2", after=0)
    out = execute(activities(lab, wait_slice=10.0))
    assert all(j["status"] == "succeeded" for j in lab.jobs.values()) and len(lab.jobs) == 2
    assert lab.queries["list_jobs"] == 1  # nothing active, yet ...
    assert "nothing_outstanding" not in out and "failed_calls" not in out  # ... a job succeeded: the wait stands
    assert [s["ok"] for s in out["summary"]] == [True, True, True]


def test_wait_for_jobs_while_a_job_is_still_running_leaves_something_to_wait_for():
    lab = FakeLab([train()], control=WAIT)
    out = execute(activities(lab))
    assert "nothing_outstanding" not in out


def test_wait_for_jobs_after_calls_that_all_failed_has_nothing_outstanding():
    lab = FakeLab([train(), score(predictions=ref(0, "outputs.predictions"))], control=WAIT)
    lab.fail["run_component"] = LabError(409, {"code": "COMPONENT_NOT_RUNNABLE", "message": "the component is not runnable"})
    out = execute(activities(lab, wait_slice=10.0))
    assert out["nothing_outstanding"] is True
    assert out["failed_calls"] == [
        {"tool": "run_component", "code": "COMPONENT_NOT_RUNNABLE", "message": "the component is not runnable"},
        {"tool": "run_component", "code": "DEPENDENCY_FAILED", "message": "call 0 (run_component) did not succeed"}]


def test_wait_for_jobs_after_a_job_that_failed_but_did_start_does_not_say_nothing_outstanding():
    """The batch's job was admitted (its call succeeded) and then ended failed; the lab holds its failure event, which
    is what the wait is for. Only a decision that started nothing is reported as having nothing to wait for."""
    lab = FakeLab([train(), score(predictions=ref(0, "outputs.predictions"))], control=WAIT)
    lab.plan("job-1", after=1, status="failed")
    out = execute(activities(lab, wait_slice=10.0))
    assert [s["code"] for s in out["summary"]] == [None, "DEPENDENCY_FAILED"]
    assert "nothing_outstanding" not in out


def test_wait_for_jobs_after_effects_that_started_no_job_has_nothing_outstanding():
    lab = FakeLab([note(), src()], control=WAIT)
    out = execute(activities(lab))
    assert out["nothing_outstanding"] is True and out["failed_calls"] == []


def test_without_a_wait_control_nothing_outstanding_is_never_reported():
    lab = FakeLab([train()], control={})
    lab.fail["run_component"] = LabError(409, {"code": "X", "message": "m"})
    out = execute(activities(lab))
    assert "nothing_outstanding" not in out and lab.queries["list_jobs"] == 0
