import copy
import json
import math

import pytest

from study.exp.compare import RULES, main
from study.exp.compare import compare as compare_scorecards
from study.exp.scorecard import SCHEMA


def cards(delta=0, *, replicates=1, tier="T3"):
    rows = [dict(card_id=f"R{i:04d}", arm=4, replicate=rep, tier=tier,
                 vrg=i / 10 + delta, mrg=i / 10 + delta, error_ratio=math.exp(delta),
                 verified_success=True, null_card=False, incident=False, run_status="ok",
                 reference_se=0.1, false_claims=0, held_out_reads=False)
            for i in range(1, 9) for rep in range(1, replicates + 1)]
    return dict(schema=SCHEMA, pool={"id": "D", "commitment": "digest"}, provider={"id": "offline"},
                envelope={"wall_s": 100}, references_sha256="refs", replicates=replicates,
                treatment={"id": "a"}, build={"commit": "a" * 40}, individual=rows,
                per_card={r["card_id"]: {} for r in rows}, incomplete=False,
                aggregate=dict(false_claims=0, usd=1, heldout_identifier_matches=0,
                               live_runs=len(rows), time_seconds=1, quota_weekly_percent=1,
                               within_plan_cost_and_time=True, guardrails_clean=True))


def compare(before, after, rule, **kwargs):
    """Explicit registered identities and attestations for numeric rule fixtures."""
    tier = kwargs.get("tier", "T2")
    plan = dict(treatment={"incumbent": "a", "candidate": "a"}, budget={"usd": 10}, tiers=[])
    for selected in {r.get("tier", tier) for r in before["individual"]}:
        plan["tiers"].append(dict(tier=selected, pool="D",
                                  cards=sorted({r["card_id"] for r in before["individual"] if r.get("tier", tier) == selected}),
                                  replicates=before["replicates"], provider="offline"))
    choice = copy.deepcopy(kwargs.pop("rule_choice", {}))
    custom = choice.pop("plan", {})
    for declared in custom.pop("tiers", []):
        base = next((d for d in plan["tiers"] if d["tier"] == declared["tier"]), {})
        base.update(declared)
        if base not in plan["tiers"]:
            plan["tiers"].append(base)
    plan.update(custom)
    choice["plan"] = plan
    kwargs.setdefault("attest", {"checks": {"guardrails_clean": True}})
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from study.tests.test_exp_round2 import register
    for declaration in plan["tiers"]:
        if choice.get("extension"):
            declaration["extension"] = dict(cards=declaration["cards"], replicates=declaration["replicates"])
    with TemporaryDirectory(prefix="exp-compare-") as temporary:
        directory = Path(temporary) / "EXP-900-fixture"
        plan = register(directory, plan, rule, choice={k: choice[k] for k in ("extension", "overrides") if k in choice},
                        reviews=kwargs.pop("reviews", []))
        choice["plan"] = plan
        if kwargs.get("attest") is not None:
            kwargs["attest"] = dict(kwargs["attest"], experiment=plan["id"], commit=after["build"]["commit"])
        return compare_scorecards(before, after, rule, rule_choice=choice, experiment=directory, **kwargs)


def simple_rule(threshold=0.15):
    return dict(schema="eidos-exp-rule/1", id="R-behav", version=1, overridable={}, keep_when=[
        dict(metric="vrg", stat="mean_delta", op=">=", value=threshold, tier="T3"),
        dict(metric="vrg", stat="signflip_p", op="<=", value=0.1, tier="T3", method="exact_paired")])


def test_exp_compare_keep_and_exact_numbers():
    result = compare(cards(), cards(0.2), simple_rule(), tier="T3")
    assert result["decision"] == "keep"
    stat = result["stats"]["vrg"]
    assert stat["mean_delta"] == pytest.approx(0.2)
    assert stat["signflip_p"] == 1 / 256
    assert stat["mean_lower_bound"] == pytest.approx(0.2)
    assert result["stats"]["error_ratio"]["mean_delta"] == pytest.approx(0.2)
    assert len(stat["per_card"]) == 8


@pytest.mark.parametrize("failure", ["false_claims", "heldout", "new_failure", "cost", "missing_check"])
def test_exp_compare_vetoes(failure):
    candidate = cards(0.2)
    if failure == "false_claims":
        candidate["aggregate"]["false_claims"] = 1
        candidate["individual"][0]["false_claims"] = 1
    elif failure == "heldout":
        candidate["aggregate"]["heldout_identifier_matches"] = 1
    elif failure == "new_failure":
        candidate["individual"][0]["run_status"] = "timeout"
    elif failure == "cost":
        candidate["aggregate"]["usd"] = 2
    else:
        del candidate["aggregate"]["usd"]
    assert compare(cards(), candidate, simple_rule(), tier="T3")["decision"] == ("incomplete" if failure == "missing_check" else "revert")


def test_exp_compare_clause_revert_extension_and_incomplete():
    assert compare(cards(), cards(0.1), simple_rule(), tier="T3")["decision"] == "revert"
    choice = {"extension": True}
    assert compare(cards(), cards(0.1), simple_rule(), tier="T3", rule_choice=choice)["decision"] == "extend"
    initial, extended = cards(), cards(0.1)
    for score in (initial, extended):
        for row in score["individual"]:
            row["look"] = "extension"
    assert compare(initial, extended, simple_rule(), tier="T3", rule_choice=choice)["decision"] == "revert"
    rule = json.loads((RULES / "R-behav.json").read_text())
    result = compare(cards(), cards(0.2), rule, tier="T3")
    assert result["decision"] == "incomplete"
    assert result["clauses"][0]["status"] == "pending"


@pytest.mark.parametrize("key", ["pool", "provider", "envelope", "references_sha256", "replicates"])
def test_exp_compare_incompatible(key):
    candidate = cards(0.2)
    candidate[key] = "different"
    with pytest.raises(ValueError):
        compare_scorecards(cards(), candidate, simple_rule(), tier="T3")


def test_exp_compare_pairs_cannot_be_dropped():
    candidate = cards(0.2)
    candidate["individual"].pop()
    with pytest.raises(ValueError):
        compare_scorecards(cards(), candidate, simple_rule(), tier="T3")
    candidate = cards(0.2)
    candidate["individual"][0]["incident"] = True
    assert compare(cards(), candidate, simple_rule(), tier="T3")["decision"] != "keep"


def test_exp_compare_cost_exception_and_false_claim_count_mismatch():
    candidate = cards(0.2)
    candidate["aggregate"]["usd"] = 2
    choice = {"plan": {"cost_ratio_exception": {"max_ratio": 2, "reason": "registered"}}}
    assert compare(cards(), candidate, simple_rule(), tier="T3", rule_choice=choice)["decision"] == "keep"
    candidate["individual"][0]["false_claims"] = 1
    assert compare(cards(), candidate, simple_rule(), tier="T3", rule_choice=choice)["decision"] == "revert"


def test_exp_compare_aa_spread_and_identity():
    result = compare(cards(replicates=2), cards(0.2, replicates=2), simple_rule(), tier="T3", aa=True)
    assert result["decision"] == "incomplete"
    assert result["aa"]["rule_decision"] == "would_keep"
    assert result["aa"]["paired_spread"]["vrg"] == pytest.approx(0, abs=1e-15)
    assert result["aa"]["between_replicate_sd"]["candidate"]["R0001"]["vrg"] == 0
    candidate = cards()
    candidate["treatment"]["id"] = "b"
    with pytest.raises(ValueError, match="A/A"):
        compare(cards(), candidate, simple_rule(), tier="T3", aa=True)


def test_exp_compare_error_ratio_logs_card_means_and_replicate_spread():
    before, after = cards(replicates=2), cards(0.2, replicates=2)
    before["individual"][0]["error_ratio"] = 1
    before["individual"][1]["error_ratio"] = 3
    after["individual"][0]["error_ratio"] = 2
    after["individual"][1]["error_ratio"] = 2
    after["individual"][0]["vrg"] = 0.2
    after["individual"][1]["vrg"] = 0.4
    result = compare(before, after, simple_rule(), tier="T3", aa=True)
    assert result["stats"]["error_ratio"]["per_card"]["R0001"] == 0
    assert result["aa"]["between_replicate_sd"]["candidate"]["R0001"]["vrg"] == pytest.approx(math.sqrt(0.02))


def test_exp_compare_multitier_required_checks_are_separate():
    before, after = cards(), cards(0.2)
    for score in (before, after):
        extra = copy.deepcopy(score["individual"])
        for row in extra:
            row["tier"] = "T2"
        score["individual"] += extra
    rule = json.loads((RULES / "R-behav.json").read_text())
    assert compare(before, after, rule, tier="T3", attest={})["decision"] == "incomplete"
    after["tier_aggregates"] = {"T2": {"guardrails_clean": True}}
    assert compare(before, after, rule, tier="T3")["decision"] == "keep"
    after["tier_aggregates"]["T2"]["guardrails_clean"] = False
    assert compare(before, after, rule, tier="T3", attest={"tier_checks": {"T2": {"guardrails_clean": False}}})["decision"] == "revert"


def test_exp_compare_each_look_cannot_hide_a_failed_initial_look():
    before, after = cards(), cards(0.1)
    for score in (before, after):
        for row in score["individual"]:
            row["look"] = "initial"
        extra = copy.deepcopy(score["individual"])
        for row in extra:
            row["look"] = "extension"
            if score is after:
                row["vrg"] += 1
        score["individual"] += extra
    rule = simple_rule()
    rule["keep_when"][0]["each_look"] = True
    result = compare(before, after, rule, tier="T3", rule_choice={"extension": True})
    assert result["decision"] == "revert"
    assert result["clauses"][0]["look"] == "initial"
    assert not result["clauses"][0]["passed"]


def test_exp_compare_veto_evidence_contradictions_and_plan_limits():
    before, after = cards(), cards(0.2)
    after["individual"][0]["held_out_reads"] = True
    assert compare(before, after, simple_rule(), tier="T3")["decision"] == "revert"
    after["individual"][0]["held_out_reads"] = False
    choice = {"plan": {"budget": {"usd": 0.5}}}
    assert compare(before, after, simple_rule(), tier="T3", rule_choice=choice)["decision"] == "revert"


def test_exp_compare_declared_pairs_missing_on_both_sides():
    before, after = cards(replicates=2), cards(0.2, replicates=2)
    before["individual"].pop()
    after["individual"].pop()
    with pytest.raises(ValueError, match="replicates"):
        compare(before, after, simple_rule(), tier="T3")


def test_exp_compare_registered_override_and_target():
    rule = json.loads((RULES / "R-enable.json").read_text())
    before, after = cards(), cards(0.02)
    for row in before["individual"]:
        row["tokens"] = 100
    for row in after["individual"]:
        row["tokens"] = 80
    choice = {"overrides": {"ni_margin": 0.05}, "plan": {
        "tiers": [{"tier": "T3"}], "target": {"metric": "tokens", "direction": "down", "amount": 10, "tier": "T3"}}}
    assert compare(before, after, rule, tier="T3", rule_choice=choice)["decision"] == "keep"
    choice["overrides"]["ni_margin"] = 100
    with pytest.raises(ValueError, match="override"):
        compare(before, after, rule, tier="T3", rule_choice=choice)


def test_exp_compare_infra_requires_review_evidence():
    rule = json.loads((RULES / "R-infra.json").read_text())
    before, after = cards(), cards()
    after["aggregate"].update(tests_green=True, ci_green=True, default_treatment_golden_unchanged=True)
    assert compare(before, after, rule, tier="T3")["decision"] == "incomplete"
    choice = {"reviews": [{"kind": "code", "commit": "a" * 40, "verdict": "ACCEPT"}]}
    assert compare(before, after, rule, tier="T3", reviews=choice["reviews"],
                   attest={"checks": after["aggregate"]})["decision"] == "keep"


def test_exp_compare_cli_digests_exit2_and_aa(tmp_path, capsys):
    paths = [tmp_path / name for name in ("a.json", "b.json", "rule.json")]
    for path, value in zip(paths, [cards(), cards(0.2), simple_rule()], strict=True):
        path.write_text(json.dumps(value))
    out = tmp_path / "decision.json"
    args = [str(paths[0]), str(paths[1]), "--rule", str(paths[2]), "--tier", "T3", "--out", str(out)]
    from study.tests.test_exp_round2 import register
    directory = tmp_path / "EXP-900-fixture"
    register(directory, registered_plan(), simple_rule())
    args += ["--experiment", str(directory)]
    assert main(args) == 0
    result = json.loads(out.read_text())
    from hashlib import sha256
    assert result["inputs"]["incumbent"] == sha256(paths[0].read_bytes()).hexdigest()
    assert "Decision: keep" in capsys.readouterr().out
    assert main(args + ["--aa"]) == 0
    assert json.loads(out.read_text())["decision"] != "keep"
    assert len(json.loads(out.read_text())["aa"]["rules"]) == 5
    bad = copy.deepcopy(cards())
    bad["provider"] = {"id": "other"}
    paths[1].write_text(json.dumps(bad))
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def registered_plan():
    return dict(treatment={"incumbent": "a", "candidate": "a"},
                tiers=[dict(tier="T3", pool="D", cards=list(cards()["per_card"]),
                            replicates=1, provider="offline")], budget={"usd": 10})


@pytest.mark.parametrize("n", [1, 4])
def test_exp_compare_m2_refuses_fraction_of_registered_plan(n):
    before, after = cards(), cards(0.2)
    for score in (before, after):
        score["individual"] = score["individual"][:n]
        score["per_card"] = {r["card_id"]: {} for r in score["individual"]}
    with pytest.raises(ValueError, match="plan"):
        compare_scorecards(before, after, simple_rule(), tier="T3", rule_choice={"plan": registered_plan()})


def test_exp_compare_m2_no_plan_cannot_keep():
    result = compare_scorecards(cards(), cards(0.2), simple_rule(), tier="T3")
    assert result["decision"] == "incomplete"
    assert "no registered plan" in result["reasons"]


@pytest.mark.parametrize("kind", ["flag", "incident", "missing"])
def test_exp_compare_m3_incomplete_evidence(kind):
    before, after = cards(), cards(0.2)
    if kind == "flag":
        after["incomplete"] = True
    elif kind == "incident":
        before["individual"][0]["incident"] = True
    else:
        after["individual"][0]["vrg"] = None
    result = compare_scorecards(before, after, simple_rule(), tier="T3", rule_choice={"plan": registered_plan()})
    assert result["decision"] == "incomplete"
    assert result["reasons"]


def test_exp_compare_extension_requires_positive_mean_and_no_veto():
    for delta in (0, -0.1):
        assert compare(cards(), cards(delta), simple_rule(), tier="T3", rule_choice={"extension": True})["decision"] == "revert"
    after = cards(0.1)
    after["aggregate"]["usd"] = 2
    assert compare(cards(), after, simple_rule(), tier="T3", rule_choice={"extension": True})["decision"] == "revert"


def test_exp_compare_inclusive_threshold():
    before, after = cards(), cards()
    for a, b in zip(before["individual"], after["individual"], strict=True):
        a["vrg"], b["vrg"] = 0, 0.25
    assert compare(before, after, simple_rule(0.25), tier="T3")["decision"] == "keep"


def test_exp_compare_null_pairs_excluded_and_membership_checked():
    from study.exp.compare import paired_stats
    before, after = cards(), cards(0.2)
    for score in (before, after):
        score["individual"][0].update(null_card=True, vrg=100)
    assert paired_stats(list(zip(before["individual"], after["individual"], strict=True)), "vrg")["n_cards"] == 7
    after["individual"][0]["null_card"] = False
    with pytest.raises(ValueError, match="null-card"):
        compare(before, after, simple_rule(), tier="T3")


def test_exp_compare_rdisc_primary_and_floor():
    before, after = cards(), cards(0.04)
    for score in (before, after):
        extra = copy.deepcopy(score["individual"])
        for row in extra:
            row["tier"] = "T2"
        score["individual"] += extra
    for row in after["individual"]:
        row["vrg"] -= 0.04
    rule = json.loads((RULES / "R-disc.json").read_text())
    assert compare(before, after, rule, tier="T3", rule_choice={"extension": True})["decision"] == "extend"
    for row in after["individual"]:
        row["vrg"] -= 0.5
    assert compare(before, after, rule, tier="T3", rule_choice={"extension": True})["decision"] == "revert"


def test_exp_compare_mc_method_cannot_satisfy_exact_clause():
    from study.exp.compare import paired_stats
    pairs = [(dict(card_id=str(i), vrg=0), dict(card_id=str(i), vrg=1)) for i in range(21)]
    assert paired_stats(pairs, "vrg")["method"] == "seeded_monte_carlo"


def test_exp_compare_success_count_and_min_card_orientation():
    before, after = cards(), cards(0.2)
    rule = simple_rule()
    rule["keep_when"] = [dict(metric="verified_rate", stat="success_count_delta", op=">=", value=-1, tier="T3")]
    after["individual"][0]["verified_success"] = False
    result = compare(before, after, rule, tier="T3")
    assert result["decision"] == "keep"
    assert result["clauses"][0]["value"] == -1
    after["individual"][1]["verified_success"] = False
    assert compare(before, after, rule, tier="T3")["decision"] == "revert"
    rule["keep_when"] = [dict(metric="vrg", stat="min_card_delta_reference_se", op=">=", value=0, tier="T3")]
    assert compare(before, after, rule, tier="T3")["decision"] == "keep"


def test_exp_compare_latest_review_nonempty_commits_and_explicit_attestation():
    before, after = cards(), cards()
    rule = json.loads((RULES / "R-infra.json").read_text())
    after["aggregate"].update(tests_green=True, ci_green=True, default_treatment_golden_unchanged=True)
    reviews = [dict(kind="code", commit="a" * 40, verdict="ACCEPT"), dict(kind="code", commit="a" * 40, verdict="REJECT")]
    attest = {"checks": after["aggregate"]}
    assert compare(before, after, rule, tier="T3", reviews=reviews, attest=attest)["decision"] == "revert"
    assert compare(before, after, rule, tier="T3", reviews=reviews[:1], attest=attest)["decision"] == "keep"
    after["reviews"] = reviews[:1]
    assert compare(before, after, rule, tier="T3", attest=attest)["decision"] == "incomplete"
    assert compare(before, after, rule, tier="T3", reviews=reviews[:1])["decision"] == "incomplete"
    for commit in (None, "", " "):
        after["build"]["commit"] = commit
        with pytest.raises(ValueError, match="attestation"):
            compare(before, after, rule, tier="T3", reviews=[dict(kind="code", commit=commit, verdict="ACCEPT")], attest=attest)


def test_exp_compare_model_free_uses_t1_autopilot():
    before, after = cards(), cards()
    for score in (before, after):
        extra = copy.deepcopy(score["individual"])
        for row in extra:
            row.update(tier="T1", mode="autopilot")
        score["individual"] += extra
        for row in score["individual"]:
            row["tokens"] = 100 if score is before else 80
            if score is after:
                row["vrg"] -= 0.005 if row["tier"] == "T1" else 0.5
    rule = json.loads((RULES / "R-enable.json").read_text())
    choice = {"plan": {"model_free_suffices": True, "target": {"metric": "tokens", "direction": "down", "amount": 10, "tier": "T3"}}}
    assert compare(before, after, rule, tier="T3", rule_choice=choice)["decision"] == "keep"


@pytest.mark.parametrize("field,value", [("pool", "E"), ("replicates", 2), ("provider", "other"), ("tier", "T2"),
                                         ("commitment", "other")])
def test_exp_compare_plan_identity_mismatch(field, value):
    plan = registered_plan()
    plan["tiers"][0][field] = value
    with pytest.raises(ValueError, match="plan"):
        compare_scorecards(cards(), cards(0.2), simple_rule(), tier="T3", rule_choice={"plan": plan})


def test_exp_compare_plan_treatment_and_declared_random_draw():
    plan = registered_plan()
    plan["treatment"]["candidate"] = "other"
    with pytest.raises(ValueError, match="treatment"):
        compare_scorecards(cards(), cards(0.2), simple_rule(), tier="T3", rule_choice={"plan": plan})
    import random
    plan = registered_plan()
    plan["tiers"][0]["draw"] = dict(method="random", seed=12, count=4)
    chosen = set(random.Random(12).sample(list(cards()["per_card"]), 4))
    before, after = cards(), cards(0.2)
    for score in (before, after):
        score["individual"] = [r for r in score["individual"] if r["card_id"] in chosen]
        score["per_card"] = {c: {} for c in chosen}
    assert compare(before, after, simple_rule(), tier="T3", rule_choice={"plan": plan}, cards=4)["decision"] == "keep"
    with pytest.raises(ValueError, match="draw"):
        compare_scorecards(before, after, simple_rule(), tier="T3", rule_choice={"plan": plan}, cards=3)


def test_exp_compare_veto_snapshot_and_contradictory_heldout():
    rule = simple_rule()
    rule["vetoes"] = {"keep_when": []}
    with pytest.raises(ValueError, match="vetoes"):
        compare(cards(), cards(0.2), rule, tier="T3")
    after = cards(0.2)
    after["aggregate"]["heldout_identifier_matches"] = True
    result = compare(cards(), after, simple_rule(), tier="T3")
    assert result["decision"] == "revert"
    assert any(v.get("reason") == "contradictory evidence" for v in result["vetoes"])


def test_exp_compare_unknown_check_and_second_extension_refused():
    rule = simple_rule()
    rule["keep_when"] = [{"check": "invented", "equals": True}]
    after = cards(0.2)
    after["aggregate"]["invented"] = True
    with pytest.raises(ValueError, match="unknown"):
        compare(cards(), after, rule, tier="T3")
    with pytest.raises(ValueError, match="second extension"):
        compare(cards(), cards(0.1), simple_rule(), tier="T3", rule_choice={"extension": True}, previous=[{"decision": "extend"}])


def test_exp_compare_budget_computed_and_missing_evidence_precedes_failure():
    after = cards(0.2)
    after["aggregate"].update(within_plan_cost_and_time=True, usd=20)
    assert compare(cards(), after, simple_rule(), tier="T3")["decision"] == "revert"
    after["aggregate"].update(usd=1, within_plan_cost_and_time=False)
    assert compare(cards(), after, simple_rule(), tier="T3")["decision"] == "keep"
    after["incomplete"] = True
    after["individual"][0]["false_claims"] = 1
    after["aggregate"]["false_claims"] = 1
    assert compare(cards(), after, simple_rule(), tier="T3")["decision"] == "revert"


@pytest.mark.parametrize("bad", [[], None, 1, {"schema": "bad"}])
def test_exp_compare_cli_malformed_removes_stale_output(tmp_path, bad):
    a, b, rule, out = [tmp_path / n for n in ("a.json", "b.json", "rule.json", "decision.json")]
    for p, data in ((a, cards()), (b, bad), (rule, simple_rule()), (out, {"decision": "keep"})):
        p.write_text(json.dumps(data))
    with pytest.raises(SystemExit) as error:
        main([str(a), str(b), "--rule", str(rule), "--out", str(out)])
    assert error.value.code == 2
    assert not out.exists()


def test_exp_compare_cli_review_attest_and_registered_snapshot(tmp_path):
    from study.tests.test_exp_round2 import register
    a, b, rule, attest, review, out = [tmp_path / n for n in ("a.json", "b.json", "R-infra.json", "attest.json", "review.md", "decision.json")]
    definition = json.loads((RULES / "R-infra.json").read_text())
    directory = tmp_path / "EXP-900-fixture"
    plan = register(directory, registered_plan(), definition,
                    reviews=[dict(kind="code", commit="a" * 40, verdict="ACCEPT")])
    for path, data in ((a, cards()), (b, cards()), (rule, definition),
                       (attest, {"experiment": plan["id"], "commit": "a" * 40,
                                 "checks": dict(tests_green=True, ci_green=True, default_treatment_golden_unchanged=True)})):
        path.write_text(json.dumps(data))
    review.write_text("VERDICT: ACCEPT — commit aaaaaaa\n")
    assert main([str(a), str(b), "--rule", str(rule), "--tier", "T3", "--out", str(out),
                 "--attest", str(attest), "--review", str(review), "--experiment", str(directory)]) == 0
    result = json.loads(out.read_text())
    from study.exp.scorecard import sha256
    assert result["decision"] == "keep"
    assert result["inputs"]["attest"] == sha256(attest.read_bytes())
    assert result["inputs"]["review/0"] == sha256(review.read_bytes())
    assert result["veto_source"] == "registered ledger snapshot"
    assert result["vetoes"][3]["clause"]["value"] == 1.5


def test_exp_compare_novelty_requires_identical_catalogs():
    before, after = cards(), cards()
    rule = simple_rule()
    rule["keep_when"] = [dict(metric="novelty_wins", stat="count", op=">=", value=0)]
    before["catalog"], after["catalog"] = {"sha256": "a"}, {"sha256": "b"}
    with pytest.raises(ValueError, match="catalog"):
        compare(before, after, rule, tier="T3")



def test_exp_compare_candidate_only_incident_and_incomplete_look():
    before, after = cards(), cards(0.2)
    after["individual"][0].update(incident=True, run_status="incident", vrg=None)
    assert compare(before, after, simple_rule(), tier="T3")["decision"] == "incomplete"
    before, after = cards(), cards(0.2)
    for score in (before, after):
        score["individual"][0]["look"] = "extension"
    with pytest.raises(ValueError, match="plan"):
        compare(before, after, simple_rule(), tier="T3", rule_choice={"extension": True})


def test_exp_compare_false_claim_invariant_survives_empty_external_snapshot():
    after = cards(0.2)
    after["aggregate"]["false_claims"] = 1
    after["individual"][0]["false_claims"] = 1
    assert compare(cards(), after, simple_rule(), tier="T3", veto_rule={"keep_when": []})["decision"] == "revert"


def test_exp_compare_output_cannot_overwrite_adjacent_plan(tmp_path):
    a, b, rule, plan = [tmp_path / n for n in ("a.json", "b.json", "rule.json", "plan.json")]
    for path, value in ((a, cards()), (b, cards(0.2)), (rule, simple_rule()), (plan, registered_plan())):
        path.write_text(json.dumps(value))
    original = plan.read_bytes()
    with pytest.raises(SystemExit) as error:
        main([str(a), str(b), "--rule", str(rule), "--out", str(plan)])
    assert error.value.code == 2
    assert plan.read_bytes() == original



def test_exp_compare_missing_and_incident_clauses_never_pass():
    from study.exp.compare import paired_stats
    before, after = cards(), cards(0.2)
    after["individual"][0]["vrg"] = None
    result = compare(before, after, simple_rule(), tier="T3")
    assert not any(c["passed"] for c in result["clauses"])
    after = cards(0.2)
    after["individual"][0]["incident"] = True
    assert paired_stats(list(zip(before["individual"], after["individual"], strict=True)), "vrg") is None


def test_exp_compare_veto_evidence_values_and_consistency_are_recorded():
    after = cards(0.2)
    after["individual"][0]["held_out_reads"] = True
    result = compare(cards(), after, simple_rule(), tier="T3")
    assert result["vetoes"][1]["value"] == 1
    after = cards(0.2)
    after["individual"][0]["false_claims"] = 1
    result = compare(cards(), after, simple_rule(), tier="T3")
    assert not result["vetoes"][0]["passed"]


def test_exp_compare_known_budget_breach_fires_even_with_other_missing_budget_evidence():
    before, after = cards(), cards(0.2)
    plan = registered_plan()
    plan["budget"] = {"usd": 0.5, "quota_weekly_percent": 10}
    result = compare_scorecards(before, after, simple_rule(), tier="T3", rule_choice={"plan": plan})
    assert result["decision"] == "revert"
    assert result["vetoes"][4]["value"] is False


def test_exp_compare_r2_n03_infra_incident_is_the_only_missing_evidence():
    before, after = cards(), cards()
    after['individual'][0]['incident'] = True
    result = compare(before, after, json.loads((RULES / 'R-infra.json').read_text()), tier='T3',
                     reviews=[dict(kind='code', commit='a' * 40, verdict='ACCEPT')],
                     attest={'checks': dict(tests_green=True, ci_green=True, default_treatment_golden_unchanged=True)})
    assert result['decision'] == 'incomplete'
    assert result['reasons'] == ['incident in paired evidence']


def test_exp_compare_r2_n09_wrong_nonempty_review_commit():
    result = compare(cards(), cards(), json.loads((RULES / 'R-infra.json').read_text()), tier='T3',
                     reviews=[dict(kind='code', commit='b' * 40, verdict='ACCEPT')],
                     attest={'checks': dict(tests_green=True, ci_green=True, default_treatment_golden_unchanged=True)})
    assert result['decision'] == 'incomplete'
    assert result['clauses'][-1]['value'] is None


def test_exp_compare_r2_n10_two_extensions_with_extension_rows():
    before, after = cards(), cards(.2)
    for score in [before, after]:
        for row in score['individual']:
            row['look'] = 'extension'
    with pytest.raises(ValueError, match='second extension'):
        compare(before, after, simple_rule(), tier='T3', rule_choice={'extension': True},
                previous=[{'decision': 'extend'}, {'decision': 'extend'}])


def test_exp_compare_r2_n20_superset_card_set():
    before, after = cards(), cards(.2)
    for score in [before, after]:
        score['individual'].append(dict(score['individual'][0], card_id='R0009'))
        score['per_card']['R0009'] = {}
    with pytest.raises(ValueError, match='card set differs from plan'):
        compare_scorecards(before, after, simple_rule(), tier='T3', rule_choice={'plan': registered_plan()})


def test_exp_compare_r2_registered_incomplete_flag_alone():
    after = cards(.2)
    after['incomplete'] = True
    result = compare(cards(), after, simple_rule(), tier='T3')
    assert result['decision'] == 'incomplete'
    assert result['reasons'] == ['incomplete scorecard']


def test_exp_compare_r2_exact_card_count_argument():
    with pytest.raises(ValueError, match='--cards disagrees'):
        compare(cards(), cards(.2), simple_rule(), tier='T3', cards=7)


def test_exp_compare_r2_mixed_tier_missing_replicate():
    before, after = cards(replicates=2), cards(.2, replicates=2)
    for score in [before, after]:
        extra = [dict(r, tier='T2') for r in score['individual'] if r['replicate'] == 1][:4]
        score['individual'].pop()
        score['individual'] += extra
    with pytest.raises(ValueError, match='pairs differ from plan'):
        compare(before, after, simple_rule(), tier='T3', rule_choice={'plan': {'tiers': [dict(tier='T2', replicates=1)]}})


def test_exp_compare_r2_known_heldout_read_despite_other_unknown_rows():
    after = cards(.2)
    after['individual'][0]['held_out_reads'] = True
    after['individual'][1]['held_out_reads'] = None
    result = compare(cards(), after, simple_rule(), tier='T3')
    assert result['decision'] == 'revert'
    assert result['vetoes'][1]['value'] == 1
