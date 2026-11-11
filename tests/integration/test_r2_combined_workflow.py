"""The R2 merge, end to end: E1 (ADR 0020), E2 (ADR 0021) and E3 (ADR 0022) working together.

The acceptance journey drives the real investigator activities (``begin_turn``, ``build_context``, ``decide``,
``execute_decision``, ``complete_turn``) against the real lab (PostgreSQL) and the real attempt runner. The model is the
real pydantic-ai runtime over a ``FunctionModel`` that plays a script: the n-th request is answered with the n-th decision,
written the way a model writes it (a tool call with JSON-text arguments). No provider or network is involved, and no Temporal
server: the activities run under ``ActivityEnvironment`` and ``Journey.turn`` repeats what ``TurnWorkflow`` does around them.

1. One decision authors a trainer, runs ``eidos.search.recipe-sweep`` on it, refits the winner and notes the sweep job, each
   later call naming an earlier one with ``{"$ref": ...}`` (the sweep really runs, in a subprocess of the runner).
2. A later decision records the ``candidate_lock`` and files the claim.
3. A concluding decision asks for disposition ``candidate`` with no held-out data: the claim state is derived from the
   evidence, so it is ``candidate-submitted``.
4. ``pack(context-policy/2)`` over the real view keeps what the investigator must not lose: the lock, the candidate state, the
   route, the sweep and the batch rules, also under a small character limit and a flood of failures, notes and jobs.

Focused tests follow: ``assess_candidate`` is compute for the batch's one-CPU gate; plans reached through a batch obey the
confirmation rules of ADR 0021; the published decision schema carries batch references together with ``control.claim``.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from test_assess_candidate_e2e import CLAIM, HELD_OUT, Scene, _pinned_draft
from test_cancel_through_acceptance import _env
from test_classification_accuracy_e2e import (
    BASELINE_RECIPE,
    CANDIDATE_RECIPE,
    CHARTER,
    TEMPLATE_REF,
    _builtin,
)
from test_command_path import _publish
from test_turn_recovery import ShimClient

import eidos_wire
from eidos.boundary import wire
from eidos.domain.assessment import validate_claim_block
from eidos.domain.turns import COMPUTE_TOOLS, JOB_TOOLS, PLAN_TOOLS, keyed_calls, validate_decision
from eidos.investigator.activities import InvestigatorActivities
from eidos.investigator.context import POLICY_ID, TOOL_GUIDE, pack

ROOT = Path(__file__).resolve().parents[2]
WIRE_V1 = ROOT / "tests" / "fixtures" / "wire" / f"v{eidos_wire.WIRE_MAJOR}"
DECISION_SCHEMA = ROOT / "schemas" / "eidos-wire" / f"v{eidos_wire.WIRE_MAJOR}" / "decision-proposal.schema.json"

SWEEP_REF = "eidos.search.recipe-sweep@1.1.0"
REFIT_REF = "proj.refit@1.0.0"
RUN = {"environment": "lab", "isolation_profile": "dev:same-user"}
# ADR 0023, R14: a lock needs the selection rule recorded first (scorer and selection data fixed before the results)
SELECTION_RULE = {"metric": "accuracy", "split": "validation", "direction": "higher_is_better",
                  "scorer": SWEEP_REF.split("@", 1)[0], "data": {"config": {"split": "validation"}}}
# The project's trainer, one file: validation accuracy peaks at lr 0.1. The sweep runs it once per grid point.
FIT_PY = ("import json, sys\n"
          "a = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
          "json.dump({'validation_accuracy': 1.0 - abs(float(a['--lr']) - 0.1)}, open(a['--out'] + '/receipt.json', 'w'))\n")
SWEEP_CONFIG = {"command": {"argv": [sys.executable, "fit.py", "--out", "{out}", "--lr", "{param.lr}"]},
                "space": {"mode": "grid", "parameters": {"lr": {"values": [0.5, 0.1, 0.01]}}},
                "objective": {"metric": "validation_accuracy", "direction": "maximize"}, "budget": {"max_trials": 10}}
# A project component that takes the sweep's winner as its input and refits with it.
REFIT = {"id": "proj.refit", "release": "1.0.0", "implements": "refit/1", "kind": "artifact-op", "entry": "refit:run",
         "scientific_effects": ["change-weights"], "input": {"recipe": "recipe-sweep-best/1"},
         "output": {"model": "fit-model/1"}}
REFIT_CODE = """
def run(inputs, config, *, reader=None, writer=None, context=None):
    best = reader.json("recipe")["recipe"]
    writer.stage_json("model", {"refit_with": best["parameters"]}, schema="fit-model/1")
    return {"refit_with": best["parameters"]}
"""
ESTIMATE = {"metric": "validation_accuracy", "baseline": 0.40, "candidate": 0.90, "split": "validation"}
STOP_REASONS = {"wait_for_jobs": "waiting", "request_input": "input-requested", "conclude": "concluded", "stop": "concluded"}
SMALL_LIMIT = 14_000  # room for the tool guide and the pinned facts, no more


def lab_test(fn):
    """A test against the real lab (PostgreSQL) with a fake model; the focused tests below that need neither carry no mark."""
    return pytest.mark.fixture_llm(pytest.mark.pg(fn))


# -- the call vocabulary --------------------------------------------------------------------------------------------------
def ref(index: int, field: str) -> dict:
    return {"$ref": f"call[{index}].{field}"}


def call(tool: str, **args) -> dict:
    return {"tool": tool, "args": args}


def decision(*calls: dict, action: str | None = None, **control) -> dict:
    """A decision proposal; its control only when ``action`` is given."""
    return {"rationale": "scripted", "purpose": "open-search", "calls": list(calls),
            **({"control": {"action": action, "summary": "scripted", **control}} if action else {})}


def sweep_batch() -> dict:
    """Author the trainer, sweep it, refit the winner and note the sweep job: three later calls, each naming an earlier one."""
    return decision(
        call("author_source", files={"fit.py": FIT_PY}),
        call("run_component", component_ref=SWEEP_REF, **RUN, inputs={"source": ref(0, "artifact_id")}, config=SWEEP_CONFIG),
        call("run_component", component_ref=REFIT_REF, **RUN, inputs={"recipe": ref(1, "outputs.best_recipe")}),
        call("record_note", kind="observation", text=ref(1, "job_id")),
        action="wait_for_jobs")


def lock_the_sweep(prompt: str) -> dict:
    """The model reads the sweep job's id from its context, locks it as the candidate and files the claim."""
    found = re.search(r"- Sweep (\S+) \(succeeded\)", prompt)
    assert found, "the sweep is not among the pinned facts of this context"
    return decision(call("record_note", kind="selection_rule", text=json.dumps(SELECTION_RULE)),
                    call("record_note", kind="candidate_lock", text=json.dumps({"job_id": found.group(1)})),
                    call("file_claim", statement=CLAIM["statement"], kind="improves", scope=CLAIM["scope"]))


def plan_batch(draft: dict, charter_id: str) -> dict:
    """File a claim, draft a plan for it, freeze and launch it: each step names the plan (or claim) of the one before."""
    return decision(
        call("file_claim", statement=CLAIM["statement"], kind="improves", scope=CLAIM["scope"]),
        call("draft_evidence_plan", claim_id=ref(0, "claim_id"), charter_id=charter_id, template_ref=TEMPLATE_REF, draft=draft),
        call("freeze_evidence_plan", plan_id=ref(1, "plan_id")),
        call("launch_evidence_plan", plan_id=ref(2, "plan_id")),
        action="request_input")


# -- the fake provider ------------------------------------------------------------------------------------------------------
def model_output(proposal: dict) -> dict:
    """A proposal as the runtime asks a model to write it: arguments, scope and claim as JSON text."""
    out = {**proposal, "calls": [{"tool": c["tool"], "args_json": json.dumps(c["args"])} for c in proposal["calls"]]}
    if "control" in proposal:
        control = dict(proposal["control"])
        out["control"] = {**{k: v for k, v in control.items() if k not in ("scope", "claim")},
                          **{f"{k}_json": json.dumps(control[k]) for k in ("scope", "claim") if k in control}}
    return out


class FakeProvider:
    """The n-th model request is answered with the n-th scripted decision (a dict, or a function of the prompt the model got).
    The prompts are kept: they are what the model saw."""

    def __init__(self, *script):
        self.script, self.prompts = list(script), []

    def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        self.prompts.append("\n".join(str(p.content) for m in messages for p in m.parts if p.part_kind == "user-prompt"))
        step = self.script[len(self.prompts) - 1]
        proposal = step(self.prompts[-1]) if callable(step) else step
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, model_output(proposal))],
                             usage=RequestUsage(input_tokens=1000, output_tokens=200))


# -- the real lab, activities and runner --------------------------------------------------------------------------------------
@pytest.fixture()
def scene(lab, tmp_path):
    s = Scene(lab, tmp_path)  # the Flow project: environment "lab", real templates and assessor, runner, attested held-out labels
    manifest, files = _builtin("eidos.search.recipe-sweep")
    _publish(lab, s.pid, manifest, files)
    _publish(lab, s.pid, REFIT, {"refit.py": REFIT_CODE})
    return s


def allow_investigator_to_freeze(scene: Scene) -> None:
    """An investigator freezes a plan only when the project's policy allows automatic freezing of approved templates."""
    scene.lab.cmd(scene.lab.admin, "update_profile_policy", {"policy": {"auto_freeze_approved_templates": True}}, project=scene.pid)


def charter_without_held_out(scene: Scene) -> str:
    """The flow's own charter minus its attested labels: this study has no held-out data."""
    charter = {**copy.deepcopy(CHARTER), "study_id": "r2-no-held-out"}
    assert "held_out" not in charter
    scene.cmd("commit_study_charter", {"charter": charter})
    return f"charter:{charter['study_id']}"


class Journey:
    """One investigation of the scene's project, run turn by turn against a :class:`FakeProvider`."""

    def __init__(self, scene: Scene, provider: FakeProvider, charter_id: str):
        self.scene, self.lab, self.pid = scene, scene.lab, scene.pid
        self.inv = scene.cmd("start_investigation", {
            "question": "which learning rate is best, and is the winner worth submitting?", "charter_id": charter_id,
            "environment": "lab", "limits": {"model_tokens": 1e6, "compute_seconds": 1e5, "trials": 100},
            "investigator": {"runtime": "pydantic-ai", "model": "openai-compatible:fake",
                             "settings": {"max_tokens_per_decision": 120_000}}})["investigation_id"]
        self.acts = InvestigatorActivities(
            ShimClient(self.lab, self.lab.principal(f"inv-{self.pid}", ["investigator"], [self.pid])),
            ShimClient(self.lab, self.lab.principal(f"wf-{self.pid}", ["workflow"], [self.pid])),
            runtime_overrides={"_model_object": FunctionModel(provider)}, batch_wait_slice=0.01, batch_poll=0.001)
        self.turns: list[str] = []
        self.decisions: list[str] = []  # the accepted decisions, in order
        self.slices: list[int] = []  # how many execute_decision activities each decision's calls took

    def turn(self) -> dict:
        """One turn, as TurnWorkflow runs it: context, then decisions until one ends the turn, then completion."""
        begun = _env(1).run(self.acts.begin_turn, {"investigation_id": self.inv, "project_id": self.pid,
                                                   "index": len(self.turns) + 1})
        base = {"turn_id": begun["turn_id"], "project_id": self.pid, "investigation_id": self.inv}
        self.turns.append(base["turn_id"])
        _env(1, activity_id="context").run(self.acts.build_context, base)
        receipts, control, stop = [], {}, "calls-exhausted"
        for slot in range(begun["max_model_calls"]):
            d = _env(1, activity_id=f"decide-{slot}").run(self.acts.decide, {**base, "slot": slot})
            assert d["accepted"], d
            self.decisions.append(d["decision_id"])
            if d["n_calls"]:
                done = self.execute(base, d["decision_id"])
                receipts.append({"decision_id": d["decision_id"], "summary": done["summary"]})
            control = d.get("control") or {}
            if control.get("action"):
                stop = STOP_REASONS[control["action"]]
                break
        _env(1, activity_id="complete").run(self.acts.complete_turn, {**base, "stop_reason": stop, "control": control,
                                                                      "receipts": receipts})
        self.run_admitted_jobs()  # what the investigator waited for has finished by the next turn
        return {"turn_id": base["turn_id"], "stop_reason": stop, "control": control}

    def execute(self, base: dict, decision_id: str) -> dict:
        """execute_decision, again and again while it hands the wait back; the real runner runs the admitted jobs meanwhile."""
        continuation, slices = 0, 0
        while True:
            out = _env(1, activity_id="execute").run(self.acts.execute_decision, {**base, "decision_id": decision_id,
                                                                                  "continuation": continuation})
            slices += 1
            if not out.get("pending"):
                self.slices.append(slices)
                return out
            self.run_admitted_jobs()
            continuation = out["continuation"]

    def run_admitted_jobs(self) -> None:
        for job in self.scene.q("list_jobs", {"investigation_id": self.inv}):
            if job["status"] in ("admitted", "queued"):
                self.scene.flow.runner.run_job(job["id"], self.pid, activity_attempt=1)

    # what the ledger holds
    def proposal(self, i: int) -> dict:
        """The i-th accepted decision as the lab persisted it."""
        for turn_id in self.turns:
            for d in self.scene.q("get_turn", {"turn_id": turn_id})["decisions"]:
                if d["id"] == self.decisions[i]:
                    return d["body"]
        raise AssertionError(self.decisions[i])

    def receipts(self, i: int) -> list[dict]:
        return self.scene.q("get_record", {"kind": "decision_receipts", "id": self.decisions[i]})["body"]["receipts"]

    def conclusion(self) -> dict:
        body = self.scene.q("get_investigation", {"investigation_id": self.inv})
        assert body["status"] == "concluded", body["status"]
        return body["body"]["conclusion"]

    def view(self) -> tuple[dict, list[dict]]:
        """The investigation view and the catalog as the context builder reads them, at the newest ledger position."""
        horizon = max(e["seq"] for e in self.scene.q("list_events", {"investigation_id": self.inv, "limit": 1000})["events"])
        return (self.scene.q("investigation_view", {"investigation_id": self.inv, "horizon": horizon}),
                self.scene.q("search_components", {"limit": 100})["components"])


# -- the pinned facts -----------------------------------------------------------------------------------------------------------
def pinned_facts(text: str) -> str:
    return text.split("## Pinned facts", 1)[1].split("## Tools", 1)[0]


def assert_pinned_facts(text: str, lock_id: str, sweep_id: str, best_id: str) -> None:
    facts = pinned_facts(text)
    lock = next(line for line in facts.splitlines() if line.startswith("- Current lock"))
    assert lock.startswith(f"- Current lock ({lock_id}, 1 recorded): ") and sweep_id in lock  # the lock names the sweep job
    assert "- Candidate state: no assessment recorded" in facts and "candidate submitted for confirmation" in facts
    assert "- Route: no held-out data is attested in the charter, so assessment stays exploratory" in facts
    assert f"- Sweep {sweep_id} (succeeded): " in facts and best_id in facts and "lock the winner before confirming" in facts
    assert '- Batches: one decision can chain calls with {"$ref": "call[i].field"}' in facts


def flood(view: dict) -> dict:
    """The same investigation with a long, noisy history: 150 newer failed jobs (the sweep leaves the job window), a huge
    note and a page of failed call results."""
    noisy = copy.deepcopy(view)
    noisy["investigation"]["body"]["notes"] = "unrelated working notes. " * 4000
    noisy["jobs"] += [{"id": f"job_{i:04x}", "component": f"c{i % 7}@1", "environment": "lab", "status": "failed",
                       "purpose": "open-search", "inputs": {}, "config": {f"k{n}": "v" * 40 for n in range(30)}, "receipt": None,
                       "outputs": [], "error": {"category": "execution", "message": f"ModuleNotFoundError: No module named 'pkg{i}'"}}
                      for i in range(150)]
    noisy["call_results"] = [{"turn": 1, "tool": "run_component", "ok": False, "code": "DEPENDENCY_FAILED",
                             "error": {"message": "x" * 300}} for _ in range(30)]
    return noisy


# =====================================================================================================================
# the acceptance journey: sweep in a batch, lock, conclude a submitted candidate, pinned facts
# =====================================================================================================================
@lab_test
def test_the_combined_workflow_sweeps_locks_concludes_a_submitted_candidate_and_pins_its_facts(scene):
    conclude = decision(action="conclude", scope={"split": "validation", "population": "visible validation only"},
                        claim={"disposition": "candidate", "point_estimate": ESTIMATE})
    provider = FakeProvider(sweep_batch(), lock_the_sweep, conclude)
    journey = Journey(scene, provider, charter_without_held_out(scene))

    # 1. the batch: the dependent calls wait for the sweep, which really runs, and are resolved from its real outputs
    assert journey.turn()["stop_reason"] == "waiting"
    assert journey.slices == [2]  # the refit waited for the sweep: the first slice handed the wait back
    persisted = journey.proposal(0)["calls"]
    assert persisted[1]["args"]["inputs"]["source"] == ref(0, "artifact_id")  # the reference is kept, not rewritten
    assert persisted[2]["args"]["inputs"]["recipe"] == ref(1, "outputs.best_recipe")
    assert persisted[3]["args"]["text"] == ref(1, "job_id")
    assert [c.get("depends_on") for c in persisted] == [None, [0], [1], [1]]
    wire.validate_wire("decision-proposal", journey.proposal(0))  # as persisted, it still satisfies the published schema

    receipts = journey.receipts(0)
    assert [r["ok"] for r in receipts] == [True] * 4
    source_id = receipts[0]["result"]["artifact_id"]
    sweep = scene.flow.job(receipts[1]["result"]["job_id"])
    assert sweep["status"] == "succeeded" and sweep["lock"]["component"]["ref"] == SWEEP_REF
    assert sweep["lock"]["inputs"]["source"]["artifact_id"] == source_id
    best_id, best = scene.flow.output(sweep, "best_recipe")
    assert best["exploratory"] is True and best["recipe"]["parameters"] == {"lr": 0.1}  # the sweep found the peak
    refit = scene.flow.job(receipts[2]["result"]["job_id"])
    assert refit["status"] == "succeeded" and refit["lock"]["inputs"]["recipe"]["artifact_id"] == best_id
    assert scene.flow.output(refit, "model")[1] == {"refit_with": {"lr": 0.1}}

    assert "depends_on" not in receipts[0] and "resolved" not in receipts[0]  # a call that names nothing has no provenance
    assert (receipts[1]["depends_on"], receipts[1]["resolved"]) == (
        [0], [{"path": "inputs.source", "ref": "call[0].artifact_id", "value": source_id}])
    assert (receipts[2]["depends_on"], receipts[2]["resolved"]) == (
        [1], [{"path": "inputs.recipe", "ref": "call[1].outputs.best_recipe", "value": best_id}])
    assert (receipts[3]["depends_on"], receipts[3]["resolved"]) == (
        [1], [{"path": "text", "ref": "call[1].job_id", "value": sweep["id"]}])
    assert len({r["command_key"] for r in receipts[1:]}) == 3
    assert scene.record("note", receipts[3]["result"]["note_id"])["body"]["text"] == sweep["id"]

    # 2. the lock and the claim, then 3. the conclusion: a candidate with no held-out data is submitted, not confirmed
    assert journey.turn()["stop_reason"] == "concluded"
    lock_note = scene.record("note", journey.receipts(1)[1]["result"]["note_id"])  # after the selection rule
    assert lock_note["body"]["kind"] == "candidate_lock" and lock_note["body"]["investigation_id"] == journey.inv
    assert json.loads(lock_note["body"]["text"]) == {"job_id": sweep["id"]}
    state = journey.conclusion()["claim_state"]
    assert state["state"] == "candidate-submitted" and state["confirmed"] is False, state
    assert state["label"] == "candidate submitted for confirmation"
    assert state["requested_disposition"] == "candidate" and "downgraded_from" not in state
    assert state["claim_id"] == journey.receipts(1)[2]["result"]["claim_id"]
    assert state["assessment_id"] is None and state["plan_id"] is None
    assert state["point_estimate"]["source"] == "investigator" and state["point_estimate"]["value"] == pytest.approx(0.5)
    assert any("held-out" in o for o in state["unfulfilled_obligations"]), state["unfulfilled_obligations"]
    wire.validate_wire("conclusion", journey.conclusion())  # the recorded conclusion satisfies the published schema
    assert len(provider.prompts) == 3  # one model request per decision: every scripted decision was accepted as written
    # the model saw the sweep and the route of its charter among the pinned facts of its own context
    assert "- Sweep " in provider.prompts[1] and "no held-out data is attested" in provider.prompts[1]

    # 4. the pinned facts of the real view, and again under a small limit with a flood of failures, notes and jobs
    view, catalog = journey.view()
    assert_pinned_facts(pack(view, catalog, policy=POLICY_ID)["text"], lock_note["id"], sweep["id"], best_id)
    packed = pack(flood(view), catalog, char_limit=SMALL_LIMIT, policy=POLICY_ID)
    assert_pinned_facts(packed["text"], lock_note["id"], sweep["id"], best_id)
    assert packed["truncations"]["context_truncated"] is True and TOOL_GUIDE in packed["text"]
    assert packed["chars"] == len(packed["text"]) <= SMALL_LIMIT
    assert sweep["id"] in packed["omissions"]["jobs"]  # the job window dropped the sweep; its pinned line did not


# =====================================================================================================================
# assess_candidate is compute for the batch's one-CPU gate (ADR 0022 addendum R11)
# =====================================================================================================================
P, INV, TURN, DEC = "proj", "inv-1", "turn-1", "dec-1"
ASSESS = call("assess_candidate", success_rule_id="rule", claim=CLAIM, baseline={"job_id": "job-b"},
              candidate={"job_id": "job-c"}, seeds=[21], labels="art-labels", data_provenance=HELD_OUT)
TRAIN = call("run_component", component_ref="proj.train@1.0.0", **RUN)
LAUNCHED = {"status": "launched", "plan_id": "plan-1", "claim_id": "claim-1", "request_id": "req-1", "plan_digest": "sha256:1"}
REFUSED = {"status": "refused", "request_id": "req-1", "exploratory": True,
           "reasons": [{"code": "selection-data", "message": "candidate submitted for confirmation instead"}]}
WAIT = {"action": "wait_for_jobs"}


class GateLab:
    """The lab as ``execute_decision`` sees it, reduced from FakeLab of tests/unit/test_dependent_batches.py: one stored
    decision, commands replayed by key, jobs that stay running until the test ends them. ``assess`` is what the lab answers
    ``assess_candidate`` with; a launched assessment starts a plan job."""

    def __init__(self, calls: list[dict], assess: dict, *, control: dict | None = None, plan_job: str = "running"):
        decided = validate_decision({"rationale": "r", "calls": calls, "control": control or {}}, max_calls=16)
        self.body = {**decided, "calls": keyed_calls(P, TURN, DEC, decided["calls"])}
        self.assess, self.plan_job = assess, plan_job
        self.jobs: dict[str, dict] = {}
        self.responses: dict[str, dict] = {}  # command key -> first answer, replayed on a repeat
        self.effects: list[str] = []  # the commands the lab really executed (not replayed), in order
        self.receipts: list[dict] | None = None  # what the activity recorded for the decision

    def add_job(self, job_id: str, status: str = "running", **extra) -> None:
        self.jobs[job_id] = {"id": job_id, "status": status, "outputs": [], "terminal": None, **extra}

    def query(self, name: str, params: dict | None = None, *, project: str | None = None):
        params = params or {}
        if name == "get_turn":
            return {"id": TURN, "status": "open",
                    "decisions": [{"id": DEC, "accepted": True, "revoked": False, "body": copy.deepcopy(self.body)}]}
        if name == "get_job":
            return copy.deepcopy(self.jobs[params["job_id"]])
        if name == "list_jobs":
            return [copy.deepcopy(j) for j in self.jobs.values()
                    if j["status"] in params["statuses"] and params.get("plan_id") in (None, j.get("plan_id"))]
        if name == "get_record":  # is the investigation still active? (asked while a wait runs)
            return {"id": params["id"], "status": "active"}
        raise AssertionError(f"unexpected query {name}")

    def command(self, name: str, payload: dict | None = None, *, project: str | None = None, key: str | None = None, **_):
        if key in self.responses:
            return {**copy.deepcopy(self.responses[key]), "replayed": True}
        result: dict = {}
        if name == "hand_off_decision":
            result = {"handed_off": True}
        elif name == "record_decision_receipts":
            self.receipts = payload["receipts"]
        else:
            self.effects.append(name)
            if name == "assess_candidate":
                result = dict(self.assess)
                if result["status"] == "launched":
                    self.add_job("plan-job-1", self.plan_job, plan_id=result["plan_id"])
            else:
                result = {"job_id": f"job-{len(self.jobs) + 1}", "trial_id": "trial-1", "lock_digest": "sha256:2",
                          "component": payload["component_ref"]}
                self.add_job(result["job_id"])
        self.responses[key] = {"result": result}
        return {"result": copy.deepcopy(result), "replayed": False}


def run_gate(lab: GateLab, **extra) -> dict:
    """One execute_decision activity (slice) of the lab's decision."""
    acts = InvestigatorActivities(lab, lab, batch_wait_slice=0.05, batch_poll=0.001)  # type: ignore[arg-type]
    return _env(1, activity_id="execute").run(acts.execute_decision, {
        "turn_id": TURN, "project_id": P, "investigation_id": INV, "decision_id": DEC, **extra})


def test_assess_candidate_is_plan_compute_not_a_job_tool():
    assert "assess_candidate" in PLAN_TOOLS and "assess_candidate" in COMPUTE_TOOLS and "assess_candidate" not in JOB_TOOLS


def test_a_job_after_a_launched_assessment_waits_until_the_plans_jobs_have_ended():
    lab = GateLab([ASSESS, TRAIN], LAUNCHED)
    assert run_gate(lab) == {"pending": True, "next_call": 1, "continuation": 1}
    assert lab.effects == ["assess_candidate"]  # the plan holds the one CPU: the job is not admitted
    # the plan's next step takes over in the transaction that ends the previous one: still the plan's allowance
    lab.jobs["plan-job-1"]["status"] = "succeeded"
    lab.add_job("plan-job-2", plan_id="plan-1")
    assert run_gate(lab, continuation=1) == {"pending": True, "next_call": 1, "continuation": 2}
    assert lab.effects == ["assess_candidate"]
    lab.jobs["plan-job-2"]["status"] = "succeeded"  # the plan's last job ended: the allowance is free
    done = run_gate(lab, continuation=2)
    assert "pending" not in done and lab.effects == ["assess_candidate", "run_component"]
    assert [(s["tool"], s["replayed"]) for s in done["summary"]] == [("assess_candidate", True), ("run_component", False)]


def test_a_refused_assessment_holds_no_allowance_so_the_next_job_starts_at_once():
    lab = GateLab([ASSESS, TRAIN], REFUSED)
    done = run_gate(lab)
    assert "pending" not in done and lab.effects == ["assess_candidate", "run_component"]  # nothing to wait for
    refusal = lab.receipts[0]
    assert refusal["ok"] is True and refusal["result"]["status"] == "refused"  # an answer, not an error
    assert [s["ok"] for s in done["summary"]] == [True, True]


def test_waiting_after_only_a_refused_assessment_has_nothing_outstanding_but_a_launched_one_has():
    refused = run_gate(GateLab([ASSESS], REFUSED, control=WAIT))
    assert refused["nothing_outstanding"] is True and refused["failed_calls"] == []  # it started no compute
    running = run_gate(GateLab([ASSESS], LAUNCHED, control=WAIT))
    assert "nothing_outstanding" not in running  # its plan's jobs are what the wait is for
    ended = run_gate(GateLab([ASSESS], LAUNCHED, control=WAIT, plan_job="succeeded"))
    assert "nothing_outstanding" not in ended  # and when they have all ended already, their events still are


# =====================================================================================================================
# plans reached through a batch obey the confirmation rules (ADR 0021)
#
# The real path: ``execute_decision`` against the real lab with ``$ref`` plan ids, driven by the same Journey. The plans use
# the scene's charter, which attests its held-out labels.
# =====================================================================================================================
@lab_test
def test_a_batch_plan_with_no_held_out_provenance_freezes_exploratory_and_selection_data_is_refused(scene):
    allow_investigator_to_freeze(scene)
    baseline, candidate = scene.job(BASELINE_RECIPE), scene.job(CANDIDATE_RECIPE)
    undeclared = scene.fresh_draft(baseline, candidate)
    del undeclared["data_provenance"]  # the plan declares nothing about its data
    selection = {**scene.fresh_draft(baseline, candidate), "data_provenance": {"split": "selection"}}
    journey = Journey(scene, FakeProvider(plan_batch(undeclared, scene.charter_id), plan_batch(selection, scene.charter_id)),
                      scene.charter_id)

    assert journey.turn()["stop_reason"] == "input-requested"
    claim, drafted, frozen, launched = journey.receipts(0)
    plan_id = drafted["result"]["plan_id"]
    assert [r["ok"] for r in (claim, drafted, frozen, launched)] == [True] * 4
    assert drafted["resolved"] == [{"path": "claim_id", "ref": "call[0].claim_id", "value": claim["result"]["claim_id"]}]
    assert frozen["resolved"] == [{"path": "plan_id", "ref": "call[1].plan_id", "value": plan_id}]
    assert launched["resolved"] == [{"path": "plan_id", "ref": "call[2].plan_id", "value": plan_id}]
    assert frozen["depends_on"] == [1] and launched["depends_on"] == [2]
    plan = scene.record("evidence_plan", plan_id)
    assert plan["status"] == "executing"  # it froze and launched ...
    confirmation = plan["body"]["confirmation"]
    assert confirmation["eligible"] is False and confirmation["data_keys"] == [] and confirmation["verified"] == {}
    assert any("held-out" in reason for reason in confirmation["reasons"]), confirmation  # ... but it can never confirm

    # a draft that says its data is selection data cannot be frozen at all; the launch that names it is skipped
    assert journey.turn()["stop_reason"] == "input-requested"
    claim, drafted, frozen, launched = journey.receipts(1)
    assert frozen["ok"] is False and "selection" in frozen["error"]["message"]
    assert frozen["error"]["details"]["data_provenance"] == {"split": "selection"}
    assert (launched["code"], launched["skipped"], launched["depends_on"]) == ("DEPENDENCY_FAILED", True, [2])
    assert scene.record("evidence_plan", drafted["result"]["plan_id"])["status"] == "draft"
    assert sorted(p["status"] for p in scene.plans()) == ["draft", "executing"]


@lab_test
def test_a_batch_plan_pinning_results_that_already_exist_is_refused_at_freeze_as_observed_evidence(scene):
    allow_investigator_to_freeze(scene)
    published = scene.flow.output(scene.flow.job(scene.job(BASELINE_RECIPE)), "predictions")[0]  # a result that exists
    jobs_before = len(scene.q("list_jobs"))
    journey = Journey(scene, FakeProvider(plan_batch(_pinned_draft(published, scene.labels), scene.charter_id)), scene.charter_id)

    assert journey.turn()["stop_reason"] == "input-requested"
    claim, drafted, frozen, launched = journey.receipts(0)
    plan_id = drafted["result"]["plan_id"]
    assert drafted["ok"] is True  # drafting only records the plan ...
    assert frozen["ok"] is False and frozen["resolved"] == [{"path": "plan_id", "ref": "call[1].plan_id", "value": plan_id}]
    assert frozen["error"]["details"]["reason_code"] == "observed-evidence"  # ... freezing is where it is refused
    pinned = {(i["arm"], i["port"], i["artifact_id"]) for i in frozen["error"]["details"]["inputs"]}
    assert pinned == {(arm, port, published) for arm in ("baseline", "candidate") for port in ("baseline", "candidate")}
    assert (launched["code"], launched["skipped"]) == ("DEPENDENCY_FAILED", True)  # nothing launches what did not freeze
    assert scene.record("evidence_plan", plan_id)["status"] == "draft"
    assert len(scene.q("list_jobs")) == jobs_before  # and nothing was run for it


# =====================================================================================================================
# the published decision schema carries batch references together with control.scope and control.claim
# =====================================================================================================================
def batch_conclusion() -> dict:
    """One decision that sweeps, locks the sweep and concludes: references, scope and claim in the same proposal."""
    return {"rationale": "sweep, lock the winner and submit it", "purpose": "open-search", "hypotheses": [],
            "calls": [call("author_source", files={"fit.py": FIT_PY}),
                      call("run_component", component_ref=SWEEP_REF, **RUN, inputs={"source": ref(0, "artifact_id")},
                           config=SWEEP_CONFIG),
                      call("record_note", kind="candidate_lock", text=ref(1, "job_id"))],
            "control": {"action": "conclude", "summary": "candidate submitted for confirmation",
                        "scope": {"split": "validation", "answer": {"status": "improvement_claimed"}},
                        "claim": {"disposition": "candidate", "point_estimate": ESTIMATE, "evidence_refs": ["job_0123"],
                                  "obligations": ["independent confirmation"]}},
            "resource_policy": {"max_concurrent_jobs": 1}}


def published_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(DECISION_SCHEMA.read_text()))


def assert_published(proposal: dict) -> wire.DecisionProposal:
    """Valid against the published JSON Schema and the server's boundary model, and read back without losing a field."""
    assert not [e.message for e in published_validator().iter_errors(proposal)]
    wire.validate_wire("decision-proposal", proposal)
    parsed = wire.DecisionProposal.model_validate(proposal)
    assert parsed.model_dump(by_alias=True, exclude_none=True, mode="json") == proposal
    return parsed


def test_a_decision_with_batch_references_scope_and_claim_validates_against_the_wire_model():
    proposal = batch_conclusion()
    parsed = assert_published(proposal)
    assert parsed.calls[1].args.root["inputs"].root["source"].ref == "call[0].artifact_id"
    assert parsed.control.scope["split"] == "validation" and parsed.control.claim.disposition == "candidate"
    # and the lab's own validator accepts it: references kept as written, dependencies derived, control untouched
    accepted = validate_decision(copy.deepcopy(proposal), max_calls=16)
    assert [c.get("depends_on") for c in accepted["calls"]] == [None, [0], [1]]
    assert accepted["calls"][1]["args"]["inputs"]["source"] == ref(0, "artifact_id")
    assert accepted["calls"][2]["args"]["text"] == ref(1, "job_id") and accepted["control"] == proposal["control"]
    validate_claim_block(proposal["control"]["claim"])


def test_the_published_conclude_claim_fixture_round_trips():
    data = json.loads((WIRE_V1 / "decision-proposal-conclude-claim.json").read_text())
    parsed = assert_published(data)
    assert parsed.control.claim.disposition == "candidate" and parsed.control.claim.point_estimate["metric"] == "accuracy"
    assert validate_decision(copy.deepcopy(data), max_calls=16)["control"] == data["control"]
    again = json.loads(eidos_wire.canonical_json(parsed.model_dump(by_alias=True, exclude_none=True, mode="json")))
    assert again == data  # one more trip through the canonical wire encoding


@pytest.mark.parametrize("damage", [
    pytest.param(lambda d: d["calls"][1]["args"]["inputs"].update(source={"$ref": "call[0]"}), id="reference-without-field"),
    pytest.param(lambda d: d["calls"][1]["args"]["inputs"].update(source={"$ref": "call[0].artifact_id", "or": "x"}),
                 id="reference-with-another-key"),
    pytest.param(lambda d: d["control"]["claim"].update(disposition="proven"), id="unknown-disposition"),
    pytest.param(lambda d: d["control"]["claim"].update(point_estimate=0.5), id="point-estimate-not-an-object"),
])
def test_the_published_schema_refuses_a_malformed_reference_or_claim(damage):
    proposal = batch_conclusion()
    damage(proposal)
    assert list(published_validator().iter_errors(proposal))
    with pytest.raises(wire.WireSchemaError):
        wire.validate_wire("decision-proposal", proposal)
