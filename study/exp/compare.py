"""Fail-closed paired comparison against registered experiment rule clauses.

The CLI accepts one pair of scorecards. A scorecard may carry individual rows
from several tiers/looks; absent required tiers remain pending. External checks
require explicit evidence and never become true from a favourable numeric score.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import operator
import statistics
from pathlib import Path

from study.exp.scorecard import METRICS, SCHEMA, canonical, number, sha256
from study.exp.stats import mean_lower_bound, mean_se, signflip_p

RULES = Path(__file__).resolve().parents[2] / "experiments/rules"
OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq}


def _identity(row):
    return (row["card_id"], row.get("arm", 4), row["replicate"], row.get("tier"), row.get("look", "initial"))


def compatible(incumbent, candidate):
    for score in (incumbent, candidate):
        if score.get("schema") != SCHEMA:
            raise ValueError("expected eidos-exp-scorecard/1")
        for key in ("pool", "provider", "envelope", "references_sha256", "replicates"):
            if score.get(key) is None:
                raise ValueError(f"missing comparison identity: {key}")
        if not isinstance(score.get("individual"), list) or not score["individual"]:
            raise ValueError("empty paired rows")
        ids = [_identity(r) for r in score["individual"]]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate paired row")
        if set(score.get("per_card", {})) != {r["card_id"] for r in score["individual"]}:
            raise ValueError("per-card table and rows disagree")
        if type(score["replicates"]) is not int or score["replicates"] < 1:
            raise ValueError("invalid replicate declaration")
        groups = {}
        for row in score["individual"]:
            key = (row["card_id"], row.get("arm", 4), row.get("tier"), row.get("look", "initial"))
            groups.setdefault(key, set()).add(row["replicate"])
        if any(len(reps) != score["replicates"] for reps in groups.values()):
            raise ValueError("incomplete declared replicates")
    for key in ("pool", "replicates", "provider", "envelope", "references_sha256"):
        if incumbent[key] != candidate[key]:
            raise ValueError(f"incompatible {key}")
    if set(incumbent["per_card"]) != set(candidate["per_card"]):
        raise ValueError("incompatible card set")
    if {_identity(r) for r in incumbent["individual"]} != {_identity(r) for r in candidate["individual"]}:
        raise ValueError("incomplete or incompatible card/arm/replicate/tier/look pairs")
    old = {_identity(r): r for r in incumbent["individual"]}
    if any(bool(old[_identity(r)].get("null_card")) != bool(r.get("null_card")) for r in candidate["individual"]):
        raise ValueError("incompatible null-card membership")


def _pairs(a, b, tier, *, default_tier, mode=None, pool=None, look=None):
    old = {_identity(r): r for r in a["individual"]}
    pairs = []
    for row in b["individual"]:
        before = old[_identity(row)]
        if row.get("tier", default_tier) != tier or before.get("tier", default_tier) != tier:
            continue
        if look is not None and row.get("look", "initial") != look:
            continue
        if mode is not None and (row.get("mode") != mode or before.get("mode") != mode):
            continue
        if pool is not None and (row.get("pool", b["pool"]["id"]) != pool or before.get("pool", a["pool"]["id"]) != pool):
            continue
        pairs.append((before, row))
    return pairs


def _value(row, metric):
    if metric == "verified_rate":
        return float(row["verified_success"]) if type(row.get("verified_success")) is bool else None
    value = row.get(metric)
    return value if number(value) else None


def paired_stats(pairs, metric):
    cards = {}
    for old, new in pairs:
        if old.get("null_card") or new.get("null_card"):
            continue
        if old.get("incident") or new.get("incident"):
            return None
        a, b = _value(old, metric), _value(new, metric)
        if a is None or b is None:
            return None
        if metric == "error_ratio" and min(a, b) <= 0:
            return None
        cards.setdefault(new["card_id"], []).append((a, b))
    if not cards:
        return None
    table = {}
    for card, values in sorted(cards.items()):
        old_mean = statistics.mean(a for a, b in values)
        new_mean = statistics.mean(b for a, b in values)
        table[card] = math.log(new_mean / old_mean) if metric == "error_ratio" else new_mean - old_mean
    values = list(table.values())
    mean, se = mean_se(values)
    return dict(mean_delta=mean, se=se, mean_lower_bound=mean_lower_bound(values),
                signflip_p=signflip_p(values), per_card=table, n_cards=len(values),
                method="exact_paired" if len(values) <= 20 else "seeded_monte_carlo")


def _get(data, dotted):
    for key in dotted.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _resolve(value, plan):
    return _get(plan, value["plan"]) if isinstance(value, dict) and set(value) == {"plan"} else value


def _cost(score):
    aggregate = score["aggregate"]
    return aggregate.get("cost", {}).get("usd", aggregate.get("usd"))


def _failure_classes(score):
    statuses = [r.get("run_status") for r in score["individual"]]
    if any(not isinstance(s, str) for s in statuses):
        return None
    return set(statuses) - {"ok", "valid", "succeeded", "completed"}


def _check(name, a, b, plan, choice):
    agg = b["aggregate"]
    if name == "new_failure_class":
        old, new = _failure_classes(a), _failure_classes(b)
        return bool(new - old) if old is not None and new is not None else None
    if name == "heldout_identifier_matches":
        value = agg.get(name)
        reads = [r.get("held_out_reads") for r in b["individual"]]
        if type(value) is int and value >= 0:
            return max(value, sum(v is True for v in reads))
        return sum(reads) if all(type(v) is bool for v in reads) else None
    if name == "cost_ratio":
        old, new = _cost(a), _cost(b)
        if not number(old) or not number(new) or min(old, new) < 0:
            return None
        return new / old if old else (1.0 if new == 0 else math.inf)
    if name == "within_plan_cost_and_time":
        if agg.get(name) is False:
            return False
        budget = plan.get("budget", {})
        if not budget:
            return agg[name] if type(agg.get(name)) is bool else None
        actual = {"usd": _cost(b), "live_runs": agg.get("live_runs"),
                  "quota_weekly_percent": agg.get("quota_weekly_percent"),
                  "time_seconds": agg.get("time_seconds")}
        if any(k not in actual or not number(actual[k]) or not number(v) for k, v in budget.items()):
            return None
        return all(actual[k] <= limit for k, limit in budget.items())
    if name == "code_review":
        reviews = choice.get("reviews", b.get("reviews", []))
        reviews = [r for r in reviews if r.get("kind") == "code" and r.get("commit") == b.get("build", {}).get("commit")]
        return reviews[-1].get("verdict") if reviews else None
    value = agg.get(name)
    return value if type(value) is bool else None


def _passed(clause, value, threshold):
    if value is None or threshold is None:
        return False
    if "equals" in clause:
        return type(value) is type(threshold) and value == threshold
    if clause.get("op") not in OPS or not number(threshold) or isinstance(value, bool):
        return False
    return OPS[clause["op"]](value, threshold)


def _effective(rule, choice, plan):
    rule = copy.deepcopy(rule)
    if choice and (choice.get("rule", rule["id"]) != rule["id"] or choice.get("version", rule.get("version")) != rule.get("version")):
        raise ValueError("rule choice does not match rule id/version")
    overrides = choice.get("overrides", rule.get("overrides", {}))
    extension = choice.get("extension", rule.get("extension", False))
    if type(extension) is not bool:
        raise ValueError("extension must be boolean")
    for key, value in overrides.items():
        bounds = rule.get("overridable", {}).get(key)
        if not bounds or not number(value) or not bounds["min"] <= value <= bounds["max"]:
            raise ValueError(f"invalid registered override: {key}")
    for clause in rule["keep_when"]:
        key = clause.get("override")
        if key in overrides:
            clause["value"] = overrides[key] * clause.get("override_scale", 1)
        if rule["id"] == "R-enable" and clause.get("metric") == "vrg" and plan.get("model_free_suffices") is True:
            clause.update(value=-0.01, tier="T1", mode="autopilot")
    rule["extension"] = extension
    return rule


def compare(incumbent, candidate, rule, *, tier="T2", rule_choice=None, aa=False, veto_rule=None):
    compatible(incumbent, candidate)
    choice = rule_choice or {}
    plan = choice.get("plan", {})
    rule = _effective(rule, choice, plan)
    if aa and any(incumbent.get(k) != candidate.get(k) or candidate.get(k) is None for k in ("build", "treatment")):
        raise ValueError("A/A requires the same build and treatment")
    pairs = _pairs(incumbent, candidate, tier, default_tier=tier)
    stats = {m: paired_stats(pairs, m) for m in METRICS}
    veto_rule = veto_rule or rule.get("vetoes") or json.loads((RULES / "VETOES.json").read_text())
    vetoes = []
    for clause in veto_rule["keep_when"]:
        threshold = clause.get("equals", clause.get("value"))
        consistent = True
        if clause.get("metric") == "false_claims":
            counts = [r.get("false_claims", int(r["false_positive_claim"]) if type(r.get("false_positive_claim")) is bool else None)
                      for r in candidate["individual"]]
            value = sum(counts) if all(type(v) is int and v >= 0 for v in counts) else None
            total = candidate["aggregate"].get("false_claims")
            consistent = value == total
            if not consistent:
                value = max(value or 0, total) if type(total) is int and total > 0 else None
        else:
            value = _check(clause.get("check"), incumbent, candidate, plan, choice)
        if clause.get("unless") == "plan_declared":
            exception = plan.get("cost_ratio_exception", {})
            if exception:
                if not number(exception.get("max_ratio")) or not exception.get("reason"):
                    raise ValueError("invalid cost ratio exception")
                threshold = exception["max_ratio"]
        passed = consistent and _passed(clause, value, threshold)
        vetoes.append(dict(clause=clause, value=value if value != math.inf else "infinity", passed=passed,
                           fired=value is not None and not passed))
    clauses = []
    available = {r.get("tier", tier) for r in candidate["individual"]}
    for clause in rule["keep_when"]:
        when = clause.get("when", {})
        if any((rule["extension"] if k == "extension" else plan.get(k, False)) != v for k, v in when.items()):
            continue
        requested = _resolve(clause.get("tier", tier), plan)
        tiers = sorted(available) if requested == "all" else [requested]
        if requested == "planned":
            tiers = [t["tier"] if isinstance(t, dict) else t for t in plan.get("tiers", [])] or [None]
        for selected in tiers:
            looks = ["initial", "extension"] if clause.get("each_look") and any(r.get("look") == "extension" for r in candidate["individual"]) else [None]
            for look in looks:
                item = dict(clause=clause, tier=selected, look=look, value=None, passed=False)
                if selected not in available:
                    item["status"] = "pending"
                    clauses.append(item)
                    continue
                selected_pairs = _pairs(incumbent, candidate, selected, default_tier=tier,
                                        mode=clause.get("mode"), pool=clause.get("pool"), look=look)
                value = None
                if clause.get("check") and selected_pairs:
                    selected_scores = []
                    for score, index in ((incumbent, 0), (candidate, 1)):
                        aggregate = score["aggregate"] if len(available) == 1 else score.get("tier_aggregates", {}).get(selected, {})
                        selected_scores.append(dict(score, aggregate=aggregate, individual=[p[index] for p in selected_pairs]))
                    value = _check(clause["check"], *selected_scores, plan, choice)
                elif selected_pairs:
                    metric = clause.get("metric")
                    if metric == "target":
                        metric = _resolve(clause.get("target_metric", {"plan": "target.metric"}), plan)
                    stat = clause.get("stat")
                    computed = paired_stats(selected_pairs, metric) if isinstance(metric, str) else None
                    if computed and stat in {"mean_delta", "signflip_p", "mean_lower_bound"}:
                        if clause.get("method") != "exact_paired" or computed["method"] == "exact_paired":
                            value = computed[stat]
                    elif computed and stat == "improvement" and _get(plan, "target.direction") in {"up", "down"}:
                        value = computed["mean_delta"] * (1 if _get(plan, "target.direction") == "up" else -1)
                    elif stat == "success_count_delta":
                        values = [(a.get("verified_success"), b.get("verified_success")) for a, b in selected_pairs if not a.get("null_card")]
                        if values and all(type(a) is bool and type(b) is bool for a, b in values):
                            value = sum(int(b) - int(a) for a, b in values)
                    elif computed and stat == "min_card_delta_reference_se":
                        ratios = []
                        for card, delta in computed["per_card"].items():
                            if clause.get("except_plan_named_cards") and card in plan.get("named_cards", []):
                                continue
                            ses = {a.get("reference_se") for a, b in selected_pairs if a["card_id"] == card}
                            if len(ses) != 1 or not all(number(se) and se > 0 for se in ses):
                                ratios = []
                                break
                            ratios.append(delta / next(iter(ses)))
                        value = min(ratios) if ratios else None
                threshold = _resolve(clause.get("equals", clause.get("value")), plan)
                item.update(value=value, passed=_passed(clause, value, threshold), status="evaluated")
                clauses.append(item)
    failures = any(not v["passed"] for v in vetoes)
    pending = any(c.get("status") == "pending" for c in clauses)
    failed = any(not c["passed"] and c.get("status") != "pending" for c in clauses)
    if failures or failed:
        decision = "revert"
    elif pending or incumbent.get("incomplete") or candidate.get("incomplete"):
        decision = "incomplete"
    elif clauses and all(c["passed"] for c in clauses):
        decision = "keep"
    else:
        decision = "revert"
    # Only statistical shortfall at the initial look can trigger a declared
    # extension; a safety veto or a failed non-statistical clause cannot.
    failed_clauses = [c for c in clauses if not c["passed"] and c.get("status") != "pending"]
    primary = "mrg" if rule["id"] == "R-disc" else "vrg"
    first_look = all(r.get("look", "initial") == "initial" for r in candidate["individual"])
    if (rule["extension"] and first_look and not failures and not pending and failed_clauses
            and all(c["clause"].get("stat") in {"mean_delta", "signflip_p"} and c["clause"].get("tier") == tier for c in failed_clauses)
            and stats[primary] and stats[primary]["mean_delta"] > 0
            and not incumbent.get("incomplete") and not candidate.get("incomplete")):
        decision = "extend"
    result = dict(schema="eidos-exp-decision/1", decision=decision, rule=rule["id"], tier=tier,
                  clauses=clauses, vetoes=vetoes, stats=stats,
                  inputs={"incumbent": sha256(canonical(incumbent)), "candidate": sha256(canonical(candidate)),
                          "rule": sha256(canonical(rule))})
    if aa:
        result["aa"] = dict(paired_spread={
            metric: statistics.stdev(s["per_card"].values()) if s and s["n_cards"] > 1 else None
            for metric, s in stats.items()
        }, between_replicate_sd={})
        for score_name, score in (("incumbent", incumbent), ("candidate", candidate)):
            result["aa"]["between_replicate_sd"][score_name] = {
                card: {metric: statistics.stdev(values) if len(values) > 1 else None
                       for metric in METRICS
                       for values in [[_value(r, metric) for r in score["individual"]
                                       if r["card_id"] == card and not r.get("incident") and not r.get("null_card") and _value(r, metric) is not None]]}
                for card in score["per_card"]
            }
        result["aa"]["rule_decision"] = decision
        result["decision"] = "incomplete"  # diagnostic evidence can never authorize keep
    return result


def markdown(result):
    lines = [f"Decision: {result['decision']}", "", "| Metric | Mean delta | Lower bound | p |", "|---|---:|---:|---:|"]
    for metric, stats in result["stats"].items():
        if stats:
            values = ["—" if stats[k] is None else f"{stats[k]:.6g}" for k in ("mean_delta", "mean_lower_bound", "signflip_p")]
            lines.append("| " + " | ".join([metric, *values]) + " |")
    if "aa" in result:
        lines += ["", "A/A diagnostics: " + json.dumps(result["aa"], sort_keys=True)]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incumbent", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--rule-choice", type=Path)
    parser.add_argument("--tier", choices=["T0", "T1", "T2", "T3"], default="T2")
    parser.add_argument("--out", type=Path, default=Path("decision.json"))
    parser.add_argument("--aa", action="store_true")
    args = parser.parse_args(argv)
    try:
        paths = {"incumbent": args.incumbent, "candidate": args.candidate, "rule": args.rule}
        data = {k: json.loads(p.read_text()) for k, p in paths.items()}
        choice = json.loads(args.rule_choice.read_text()) if args.rule_choice else {}
        if args.rule_choice:
            paths["rule_choice"] = args.rule_choice
            plan_path = args.rule_choice.with_name("plan.json")
            if plan_path.is_file():
                choice["plan"] = json.loads(plan_path.read_text())
                paths["plan"] = plan_path
        result = compare(data["incumbent"], data["candidate"], data["rule"], tier=args.tier,
                         rule_choice=choice, aa=args.aa)
        if args.aa:
            decisions = {}
            for path in sorted(RULES.glob("R-*.json")):
                rule = json.loads(path.read_text())
                diagnostic = compare(data["incumbent"], data["candidate"], rule, tier=args.tier,
                                     rule_choice={"plan": choice.get("plan", {})}, aa=True)
                decisions[rule["id"]] = diagnostic["aa"]["rule_decision"]
                paths["aa/" + path.name] = path
            result["aa"]["rules"] = decisions
        veto_path = RULES / "VETOES.json"
        if "vetoes" not in data["rule"]:
            paths["vetoes"] = veto_path
        result["inputs"] = {k: sha256(p.read_bytes()) for k, p in paths.items()}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        print(markdown(result))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
