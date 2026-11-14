import copy
import json
import math

import pytest

from study.exp.compare import RULES, compare, main
from study.exp.scorecard import SCHEMA


def cards(delta=0, *, replicates=1, tier="T3"):
    rows = [dict(card_id=f"R{i:04d}", arm=4, replicate=rep, tier=tier,
                 vrg=i / 10 + delta, mrg=i / 10 + delta, error_ratio=math.exp(delta),
                 verified_success=True, null_card=False, incident=False, run_status="ok",
                 reference_se=0.1, false_claims=0, held_out_reads=False)
            for i in range(1, 9) for rep in range(1, replicates + 1)]
    return dict(schema=SCHEMA, pool={"id": "D", "commitment": "digest"}, provider={"id": "offline"},
                envelope={"wall_s": 100}, references_sha256="refs", replicates=replicates,
                treatment={"id": "a"}, build={"commit": "abc"}, individual=rows,
                per_card={r["card_id"]: {} for r in rows}, incomplete=False,
                aggregate=dict(false_claims=0, usd=1, heldout_identifier_matches=0,
                               within_plan_cost_and_time=True, guardrails_clean=True))


def simple_rule(threshold=0.15):
    return dict(id="test", version=1, overridable={}, keep_when=[
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
        del candidate["aggregate"]["within_plan_cost_and_time"]
    assert compare(cards(), candidate, simple_rule(), tier="T3")["decision"] == "revert"


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
        compare(cards(), candidate, simple_rule())


def test_exp_compare_pairs_cannot_be_dropped():
    candidate = cards(0.2)
    candidate["individual"].pop()
    with pytest.raises(ValueError):
        compare(cards(), candidate, simple_rule())
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
    assert result["aa"]["rule_decision"] == "keep"
    assert result["aa"]["paired_spread"]["vrg"] == pytest.approx(0, abs=1e-15)
    assert result["aa"]["between_replicate_sd"]["candidate"]["R0001"]["vrg"] == 0
    candidate = cards()
    candidate["treatment"]["id"] = "b"
    with pytest.raises(ValueError, match="A/A"):
        compare(cards(), candidate, simple_rule(), aa=True)


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
    assert compare(before, after, rule, tier="T3")["decision"] == "revert"
    after["tier_aggregates"] = {"T2": {"guardrails_clean": True}}
    assert compare(before, after, rule, tier="T3")["decision"] == "keep"
    after["tier_aggregates"]["T2"]["guardrails_clean"] = False
    assert compare(before, after, rule, tier="T3")["decision"] == "revert"


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
    assert compare(before, after, rule, tier="T3")["decision"] == "revert"
    choice = {"reviews": [{"kind": "code", "commit": "abc", "verdict": "ACCEPT"}]}
    assert compare(before, after, rule, tier="T3", rule_choice=choice)["decision"] == "keep"


def test_exp_compare_cli_digests_exit2_and_aa(tmp_path, capsys):
    paths = [tmp_path / name for name in ("a.json", "b.json", "rule.json")]
    for path, value in zip(paths, [cards(), cards(0.2), simple_rule()]):
        path.write_text(json.dumps(value))
    out = tmp_path / "decision.json"
    args = [str(paths[0]), str(paths[1]), "--rule", str(paths[2]), "--tier", "T3", "--out", str(out)]
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
