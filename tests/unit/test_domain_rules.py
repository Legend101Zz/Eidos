"""Pure domain rule tests (no database, no frameworks)."""

from __future__ import annotations

import copy

import pytest

from eidos.domain import branching, charter, evidence, isolation, qualification, resolver, support, turns
from eidos.domain.errors import (
    IncompatibleHook,
    Invalid,
    PayloadTooLarge,
    QualificationMissing,
    ResolutionError,
    ThresholdOverride,
)
from eidos_wire import command_key


def make_charter(**over):
    body = {
        "study_id": "order-study",
        "question": "Can the switch-order model be repaired at step 45?",
        "population": "switch-generator/1 opposite-order pairs",
        "comparator": "parent-unedited",
        "metrics": {
            "both_correct": {"direction": "increase", "denominator": "pairs", "unit": "proportion"},
            "reference_correct": {"direction": "increase", "denominator": "pairs", "unit": "proportion"},
        },
        "success_rules": [{
            "id": "early-repair", "metric": "both_correct", "min_effect": 0.10,
            "comparison": {"baseline_arm_role": "baseline", "candidate_arm_role": "candidate"},
            "decision_standard": {"kind": "paired-difference-ci", "confidence": 0.95},
            "guardrails": [{"metric": "reference_correct", "max_regression": 0.02}],
        }],
    }
    body.update(over)
    return charter.validate_charter(body)


def test_charter_requires_success_rules_unless_exploration_only():
    with pytest.raises(Invalid):
        charter.validate_charter({"study_id": "s", "question": "q", "metrics": {}})
    ok = charter.validate_charter({"study_id": "s", "question": "q", "exploration_only": True})
    assert ok["exploration_only"] is True


def test_threshold_override_rejected_anywhere_in_plan():
    with pytest.raises(ThresholdOverride):
        charter.reject_threshold_overrides("plan", {"arms": [{"id": "a", "min_effect": 0.01}]})
    with pytest.raises(ThresholdOverride):
        charter.reject_threshold_overrides("plan", {"allow_plan_threshold_override": True})
    charter.reject_threshold_overrides("plan", {"arms": [{"id": "a"}]})


def test_amendment_after_exposure_is_exposure_informed_and_survives_new_study_id():
    old = make_charter()
    new = copy.deepcopy(old)
    new["success_rules"][0]["min_effect"] = 0.05
    new["study_id"] = "order-study-v2"
    body, amend = charter.amend_charter(old, new, rationale="pilot looked smaller", author="human:ana",
                                        exposure_refs=[], exposures_since_charter=3)
    assert amend["exposure_informed"] and amend["changes_success_rule"] and amend["study_id_changed"]
    assert amend["changes"]["success_rules"]["before"][0]["min_effect"] == 0.10
    assert body["exposure_informed_lineage"] is True
    # A further amendment without new exposure keeps the lineage flag.
    again = copy.deepcopy(body)
    again["question"] = "reworded"
    body2, amend2 = charter.amend_charter(body, again, rationale="wording", author="h", exposure_refs=[],
                                          exposures_since_charter=0)
    assert body2["exposure_informed_lineage"] is True and not amend2["exposure_informed"]


def _plan(ch=None, **draft_over):
    ch = ch or make_charter()
    ref = {"id": "charter:order-study", "revision": 1, "digest": charter.charter_digest(ch)}
    draft = {"success_rule_id": "early-repair", "assessor": {"ref": "a@1.0.0"},
             "arms": [{"id": "parent", "role": "baseline"}, {"id": "edit", "role": "candidate"}],
             "analysis_unit": "pair", "cluster_by": "seed", "seed_policy": {"seeds": [41, 53, 67]}}
    draft.update(draft_over)
    claim = evidence.validate_claim({"statement": "edit improves", "kind": "improves", "scope": {"builder": "X"}})
    return evidence.compile_plan(draft=draft, claim=claim, claim_ref={"id": "c1", "revision": 1},
                                 charter=ch, charter_ref=ref, template_ref={"ref": "t@1.0.0"})


def test_plan_inherits_charter_rule_and_rejects_override():
    plan = _plan()
    assert plan["success_rule"]["min_effect"] == 0.10
    assert plan["success_rule_source"] == "immutable-charter"
    with pytest.raises(ThresholdOverride):
        _plan(min_effect=0.0)


def test_plan_rejects_exploration_only_charter():
    ch = charter.validate_charter({"study_id": "s", "question": "q", "exploration_only": True})
    with pytest.raises(Invalid):
        _plan(ch)


def _units(cand_rate, base_rate, seeds=(41, 53, 67), n=400):
    out = {"candidate": [], "baseline": []}
    for s in seeds:
        for i in range(n):
            out["candidate"].append({"unit": i, "cluster": s, "both_correct": int(i < cand_rate * n),
                                     "reference_correct": 1})
            out["baseline"].append({"unit": i, "cluster": s, "both_correct": int(i < base_rate * n),
                                    "reference_correct": 1})
    return out


def test_success_rule_pass_fail_inconclusive():
    plan = _plan()
    assert evidence.evaluate_success_rule(plan, _units(0.9, 0.3))["outcome"] == "protocol-passed"
    assert evidence.evaluate_success_rule(plan, _units(0.31, 0.3))["outcome"] == "protocol-failed"
    # Effect near the bar with only 3 clusters of varied effect -> inconclusive.
    u = _units(0.5, 0.3)
    for r in u["candidate"]:
        if r["cluster"] == 41:
            r["both_correct"] = int(r["unit"] < 0.3 * 400)
    assert evidence.evaluate_success_rule(plan, u)["outcome"] == "inconclusive"


def test_guardrail_violation_blocks_pass():
    plan = _plan()
    u = _units(0.9, 0.3)
    for r in u["candidate"]:
        r["reference_correct"] = int(r["unit"] % 2)
    assert evidence.evaluate_success_rule(plan, u)["outcome"] != "protocol-passed"


def test_nonconformance_forces_inconclusive():
    plan = _plan()
    conf = evidence.check_conformance(plan, {"arms": {"parent": {"status": "succeeded"}}, "isolation_ok": True})
    assert conf["conformance"] == "incomplete"
    res = evidence.finalize_assessment(plan=plan, plan_digest="p", evidence_set_digest="e", assessor_ref={},
                                       conformance=conf, evaluation={"outcome": "protocol-passed"})
    assert res["outcome"] == "inconclusive"


SUITE = {"target": {"kind": "assessor"}, "scope": ["improves-under-budget"],
         "controls": [{"id": "planted", "type": "planted", "input": "f1", "expected_outcome": "protocol-passed"},
                      {"id": "null", "type": "null", "input": "f2"}]}


def test_qualification_gate_deterministic():
    qualification.validate_control_suite(SUITE)
    good = {"planted": [{"detected": True, "outcome": "protocol-passed"}], "null": [{"detected": False}]}
    assert qualification.evaluate_qualification(SUITE, good)["status"] == "passed"
    noisy = {"planted": [{"detected": True, "outcome": "protocol-passed"}], "null": [{"detected": True}]}
    assert qualification.evaluate_qualification(SUITE, noisy)["status"] == "failed"
    missing = {"planted": [{"detected": True, "outcome": "protocol-passed"}]}
    assert qualification.evaluate_qualification(SUITE, missing)["status"] == "inconclusive"


def test_qualification_requires_both_control_types_and_stochastic_bounds():
    with pytest.raises(Invalid):
        qualification.validate_control_suite({**SUITE, "controls": SUITE["controls"][:1]})
    suite = {"target": {"kind": "assessor"}, "scope": ["x"], "acceptance": {"min_power": 0.8, "max_false_positive": 0.1},
             "controls": [{"id": "p", "type": "planted", "deterministic": False, "repetitions": 30, "input": "a"},
                          {"id": "n", "type": "null", "deterministic": False, "repetitions": 30, "input": "b"}]}
    qualification.validate_control_suite(suite)
    lucky = {"p": [{"detected": i < 25} for i in range(30)], "n": [{"detected": i < 1} for i in range(30)]}
    # 25/30 detection: Wilson lower bound ~0.66 < 0.8 -> fails even though the point estimate is 0.83.
    assert qualification.evaluate_qualification(suite, lucky)["status"] == "failed"
    # 0/30 nulls still cannot certify FPR <= 0.10 at 95% (Wilson upper ~0.113): need more repetitions.
    silent30 = {"p": [{"detected": True} for _ in range(30)], "n": [{"detected": False} for _ in range(30)]}
    assert qualification.evaluate_qualification(suite, silent30)["status"] == "failed"
    for c in suite["controls"]:
        c["repetitions"] = 40
    strong = {"p": [{"detected": True} for _ in range(40)], "n": [{"detected": False} for _ in range(40)]}
    assert qualification.evaluate_qualification(suite, strong)["status"] == "passed"


def test_promotion_requires_passing_receipt_not_signature():
    with pytest.raises(QualificationMissing):
        qualification.check_promotion(release_digest="d1", requested_scope=["x"], qualifications=[], withdrawn_refs=set())
    q = {"release_digest": "d1", "suite_digest": "s1", "status": "passed", "scope": ["x"]}
    assert qualification.check_promotion(release_digest="d1", requested_scope=["x"], qualifications=[q], withdrawn_refs=set())
    with pytest.raises(QualificationMissing):
        qualification.check_promotion(release_digest="d1", requested_scope=["x"], qualifications=[q], withdrawn_refs={"s1"})


def test_withdrawal_affects_dependent_support_but_alternate_justification_survives():
    runs = [{"ref": "run:1", "component_ref": "scorer@1.0.0", "config": {"group_by": "stratum"}},
            {"ref": "run:2", "component_ref": "other@1.0.0", "config": {}}]
    edges = [("run:1", "obs:1"), ("obs:1", "assess:1"), ("run:2", "obs:2"), ("obs:2", "assess:2")]
    hit = support.affected_refs({"release_ref": "scorer@1.0.0", "condition": {"config.group_by": "stratum"}}, runs, edges)
    assert {"run:1", "obs:1", "assess:1"} <= hit and "assess:2" not in hit
    st = support.support_status([{"id": "j1", "requires": ["assess:1"]}, {"id": "j2", "requires": ["assess:2"]}], hit)
    assert st["status"] == "supported-with-affected-justifications"
    st2 = support.support_status([{"id": "j1", "requires": ["assess:1"]}], hit)
    assert st2["status"] == "unsupported-pending-reassessment"


def rel(ref, implements, status="active", **man):
    cid, release = ref.split("@")
    m = {"id": cid, "release": release, "implements": implements, "kind": "artifact-op", "entry": "m:f",
         "scientific_effects": ["observe"], "input": {}, "output": {}}
    m.update(man)
    return {"ref": ref, "digest": "sha256:" + ref, "status": status, "manifest": m, "environment": "lab"}


def test_resolver_precedence_missing_and_grants():
    scorer_a, scorer_b = rel("a@1.0.0", "scorer/1"), rel("b@1.0.0", "scorer/1")
    target = rel("t@1.0.0", "report/1", requires=["scorer/1", "artifact.reader/1"],
                 input={"preds": "pairs/1"})
    inputs = {"preds": {"artifact_id": "x", "digest": "d", "schema": "pairs/1"}}
    kw = dict(target=target, catalog=[scorer_a, scorer_b, target], inputs=inputs, config={}, grants=set(), installed={})
    lock = resolver.resolve_invocation(profile={"bindings": {"scorer/1": ["a@1.0.0"]}}, overrides={}, **kw)
    assert lock["ports"]["scorer/1"]["ref"] == "a@1.0.0"
    lock2 = resolver.resolve_invocation(profile={"bindings": {"scorer/1": ["a@1.0.0"]}}, overrides={"scorer/1": "b@1.0.0"}, **kw)
    assert lock2["ports"]["scorer/1"]["selected_by"] == "trial-override" and lock2["digest"] != lock["digest"]
    with pytest.raises(ResolutionError):
        resolver.resolve_invocation(profile={}, overrides={}, **kw)
    with pytest.raises(ResolutionError):
        resolver.resolve_invocation(profile={"bindings": {"scorer/1": ["a@1.0.0", "b@1.0.0"]}}, overrides={}, **kw)
    needy = rel("n@1.0.0", "x/1", grants=["model.client"])
    with pytest.raises(ResolutionError) as e:
        resolver.resolve_invocation(target=needy, catalog=[needy], profile={}, overrides={}, inputs={}, config={},
                                    grants=set(), installed={})
    assert e.value.details["missing_grants"] == ["model.client"]


def test_resolver_schema_mismatch_names_consumer_and_adapter():
    target = rel("t@1.0.0", "report/1", input={"preds": "paired-labels/1"})
    adapter = rel("ad@1.0.0", "adapter/1", input={"x": "pairs/1"}, output={"y": "paired-labels/1"})
    with pytest.raises(ResolutionError) as e:
        resolver.resolve_invocation(target=target, catalog=[target], profile={}, overrides={},
                                    inputs={"preds": {"artifact_id": "a", "digest": "d", "schema": "pairs/1"}},
                                    config={}, grants=set(), installed={}, adapters=[adapter])
    d = e.value.details
    assert d["consumer"] == "t@1.0.0" and d["approved_adapters"] == ["ad@1.0.0"]


def test_dependency_conflicts_never_upgrade_project():
    c = resolver.dependency_conflicts({"a": ["torch>=3.0"]}, {"torch": "2.15.0"})
    assert c and c[0]["installed"] == "2.15.0"
    c2 = resolver.dependency_conflicts({"a": ["numpy<2"], "b": ["numpy>=2"]}, {})
    assert c2 and c2[0]["package"] == "numpy"
    assert not resolver.dependency_conflicts({"a": ["numpy>=1.20"], "b": ["numpy==1.26"]}, {})


def test_hook_phase_absent_and_conflicting_writers():
    e1 = {"ref": "rule@1", "manifest": {"phases": ["before-optimizer-step"], "writes": ["grad"]}}
    e2 = {"ref": "clip@1", "manifest": {"phases": ["before-optimizer-step"], "writes": ["grad"]}}
    with pytest.raises(IncompatibleHook):
        resolver.order_hooks([e1], ["after-optimizer-step"])
    with pytest.raises(IncompatibleHook):
        resolver.order_hooks([e1, e2], ["before-optimizer-step"])
    e2c = {"ref": "clip@1", "manifest": {"phases": ["before-optimizer-step"], "writes": ["grad"], "compose": ["grad"]}}
    e1c = {"ref": "rule@1", "manifest": {**e1["manifest"], "compose": ["grad"]}}
    assert len(resolver.order_hooks([e1c, e2c], ["before-optimizer-step"])) == 2


def test_metric_identity_changes_with_numerator():
    a = {"metrics": {"both": {"numerator": "both correct", "denominator": "pairs"}}}
    b = {"metrics": {"both": {"numerator": "either correct", "denominator": "pairs"}}}
    assert resolver.metric_identity(a, "both") != resolver.metric_identity(b, "both")


PARENT = {"checkpoint_id": "ck1", "trial_id": "t1", "step": 45, "complete": True, "trajectory_id": "traj-41",
          "state_entries": ["model", "optimizer", "rng.torch", "data_cursor"], "cumulative_cost": {"steps": 45}}


def test_branch_requires_every_state_declared_and_derives_restore_class():
    with pytest.raises(Invalid):
        branching.validate_branch(PARENT, {"migration": {"model": "copy"}, "stream_policy": "paired"})
    same = branching.validate_branch(PARENT, {"migration": {k: "copy" for k in PARENT["state_entries"]},
                                              "stream_policy": "paired"})
    assert same["no_change_continuation"] and same["restore_class"] == "exact"
    edit = branching.validate_branch(PARENT, {"intervention": {"kind": "change-weights"},
                                              "migration": {"model": "copy", "optimizer": "drop", "rng.torch": "copy",
                                                            "data_cursor": "copy"}, "stream_policy": "paired"})
    assert edit["restore_class"] == "warm-start" and edit["independence_unit"] == "traj-41"
    with pytest.raises(Invalid):
        branching.validate_branch({**PARENT, "complete": False}, {"migration": {}, "stream_policy": "paired"})
    with pytest.raises(Invalid):
        branching.validate_branch(PARENT, {"intervention": {"kind": "change-weights"},
                                           "migration": {"model": "copy", "optimizer": "drop", "rng.torch": "copy",
                                                         "data_cursor": "copy"}, "stream_policy": "paired",
                                           "claimed_restore_class": "exact"})


def test_same_user_receipt_cannot_satisfy_sealed_plan():
    need = {"sealed": True}
    dev = {"profile": "dev:same-user", "launcher": "launcher:local", "controls": {}}
    assert not isolation.evaluate_receipt(need, dev, trusted_launchers={"launcher:local"})["ok"]
    sealed = {"profile": "linux:bwrap-sealed", "launcher": "launcher:local",
              "controls": {c: "passed" for c in isolation.SEALED_CONTROLS}}
    assert isolation.evaluate_receipt(need, sealed, trusted_launchers={"launcher:local"})["ok"]
    assert not isolation.evaluate_receipt(need, sealed, trusted_launchers={"other"})["ok"]
    assert not isolation.evaluate_receipt(need, None, trusted_launchers={"launcher:local"})["ok"]


def test_command_key_stable_and_decision_validation():
    k1 = command_key("p", "t", "d", 0)
    assert k1 == command_key("p", "t", "d", 0) and k1 != command_key("p", "t", "d", 1)
    calls = turns.keyed_calls("p", "t", "d", [{"tool": "run_component", "args": {}}, {"tool": "file_claim", "args": {}}])
    assert [c["command_key"] for c in calls] == [command_key("p", "t", "d", 0), command_key("p", "t", "d", 1)]
    with pytest.raises(Invalid):
        turns.validate_decision({"calls": [{"tool": "drop_database", "args": {}}]}, max_calls=4)
    with pytest.raises(PayloadTooLarge):
        turns.check_payload({"x": "a" * 200_000})
