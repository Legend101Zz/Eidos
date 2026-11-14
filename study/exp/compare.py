"""Fail-closed paired comparison against registered experiment rule clauses.

The CLI accepts one pair of scorecards. A scorecard may carry individual rows
from several tiers/looks; absent required tiers remain pending. External checks
require explicit evidence and never become true from a favourable numeric score.

README: pass --plan FILE (or --rule-choice with adjacent plan.json) to bind
identities and complete card/replicate batches. Without a registered plan a keep
is impossible. --cards N is allowed only for the exact declared card list or a
plan tier with draw={method: "random", seed: INT, count: N}; that draw samples the
tier cards with Python Random(seed).sample. Plans never authorize hand picking.
Review verdicts come only from --review FILE, containing Verdict: ACCEPT/REJECT
and Commit: HASH lines (optional Kind: code). Self-reported CI/test booleans,
including tests_green, ci_green and other documented boolean checks, are accepted
only from explicit --attest FILE JSON (checks, optional tier_checks), recorded
with its SHA-256 digest. Scorecard review/check fields are ignored.
Use --previous decision.json ... to count prior extensions. Output is replaced
atomically; a pre-existing output is removed before reading inputs.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import operator
import os
import random
import re
import statistics
import tempfile
from pathlib import Path

from study.exp.scorecard import METRICS, SCHEMA, canonical, number, sha256
from study.exp.stats import mean_lower_bound, mean_se, signflip_p

RULES = Path(__file__).resolve().parents[2] / "experiments/rules"
BOOLEAN_CHECKS = {"tests_green", "ci_green", "default_treatment_golden_unchanged",
                  "practice_generators_no_harm", "guardrails_clean"}
CHECKS = BOOLEAN_CHECKS | {"code_review", "heldout_identifier_matches", "new_failure_class",
                          "cost_ratio", "within_plan_cost_and_time"}
OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq}


def _identity(row):
    return (row["card_id"], row.get("arm", 4), row["replicate"], row.get("tier"), row.get("look", "initial"))


def compatible(incumbent, candidate):
    for score in (incumbent, candidate):
        if not isinstance(score, dict):
            raise ValueError("scorecard must be an object")
        if score.get("schema") != SCHEMA:
            raise ValueError("expected eidos-exp-scorecard/1")
        for key in ("pool", "provider", "envelope", "references_sha256", "replicates"):
            if score.get(key) is None:
                raise ValueError(f"missing comparison identity: {key}")
        if not isinstance(score.get("individual"), list) or not score["individual"]:
            raise ValueError("empty paired rows")
        if not isinstance(score.get("aggregate"), dict) or not isinstance(score.get("per_card"), dict):
            raise ValueError("scorecard aggregate/per_card must be objects")
        if any(not isinstance(r, dict) or not isinstance(r.get("card_id"), str)
               or type(r.get("replicate")) is not int or r["replicate"] < 1 for r in score["individual"]):
            raise ValueError("invalid paired row identity")
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
    cost = aggregate.get("cost", {})
    if "usd" in cost and "usd" in aggregate and cost["usd"] != aggregate["usd"]:
        raise ValueError("contradictory cost evidence")
    return cost.get("usd", aggregate.get("usd"))


def _failure_classes(score):
    statuses = [r.get("run_status") for r in score["individual"] if not r.get("incident")]
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
        count = sum(reads) if reads and all(type(v) is bool for v in reads) else None
        if type(value) is bool:
            return int(value) if value or count is not None else None
        if type(value) is int and value >= 0:
            return max(value, sum(v is True for v in reads))
        return count
    if name == "cost_ratio":
        old, new = _cost(a), _cost(b)
        if not number(old) or not number(new) or min(old, new) < 0:
            return None
        return new / old if old else (1.0 if new == 0 else math.inf)
    if name == "within_plan_cost_and_time":
        budget = plan.get("budget", {})
        if not budget:
            return None
        actual = {"usd": _cost(b), "live_runs": agg.get("live_runs"),
                  "quota_weekly_percent": agg.get("quota_weekly_percent"),
                  "time_seconds": agg.get("time_seconds")}
        missing = False
        breached = False
        for key, limit in budget.items():
            if key not in actual or not number(limit) or limit < 0:
                raise ValueError("invalid plan budget")
            value = actual[key]
            if value is None:
                missing = True
            elif not number(value) or value < 0:
                raise ValueError("invalid budget evidence")
            else:
                breached |= value > limit
        return False if breached else None if missing else True
    if name == "code_review":
        commit = b.get("build", {}).get("commit")
        reviews = [r for r in choice.get("_reviews", []) if r.get("kind") == "code"
                   and isinstance(commit, str) and commit.strip()
                   and isinstance(r.get("commit"), str) and r["commit"].strip()
                   and r["commit"] == commit]
        return reviews[-1].get("verdict") if reviews else None
    if name not in CHECKS:
        raise ValueError(f"unknown check: {name}")
    attest = choice.get("_attest", {})
    tiers = {r.get("tier") for r in b["individual"]}
    checks = attest.get("checks", attest)
    if len(tiers) == 1:
        checks = attest.get("tier_checks", {}).get(next(iter(tiers)), checks)
    value = checks.get(name)
    return value if type(value) is bool else None


def _id(value):
    return value.get("id") if isinstance(value, dict) else value


def _bind_plan(a, b, plan, tier, count):
    if not plan:
        return {"matched": False, "reason": "no registered plan"}
    declarations = plan.get("tiers")
    if not isinstance(declarations, list) or not declarations:
        raise ValueError("plan requires tier declarations")
    available = {r.get("tier", tier) for r in b["individual"]}
    if tier not in available:
        raise ValueError("requested tier absent from scorecard")
    checked = []
    for selected in sorted(available):
        matching = [d for d in declarations if d.get("tier") == selected]
        if len(matching) != 1:
            raise ValueError("plan does not uniquely declare scorecard tier")
        declared = matching[0]
        cards = declared.get("cards")
        if not isinstance(cards, list) or not cards or not all(isinstance(c, str) for c in cards) or len(set(cards)) != len(cards):
            raise ValueError("plan requires an exact nonempty card set")
        draw = declared.get("draw")
        if draw is not None:
            n = draw.get("count")
            if (draw.get("method") != "random" or type(draw.get("seed")) is not int
                    or type(n) is not int or not 0 < n <= len(cards) or (count is not None and n != count)):
                raise ValueError("invalid plan card draw")
            cards = random.Random(draw["seed"]).sample(cards, n)
        elif count is not None and count != len(cards):
            raise ValueError("--cards disagrees with plan card set")
        reps = declared.get("replicates")
        if type(reps) is not int or reps < 1:
            raise ValueError("invalid plan replicates")
        for side, score in (("incumbent", a), ("candidate", b)):
            rows = [r for r in score["individual"] if r.get("tier", tier) == selected]
            if _id(score["pool"]) != _id(declared.get("pool")):
                raise ValueError("scorecard pool differs from plan")
            commitment = declared.get("commitment", declared.get("pool_commitment"))
            if isinstance(declared.get("pool"), dict):
                commitment = declared["pool"].get("commitment", commitment)
            if commitment is not None and score["pool"].get("commitment") != commitment:
                raise ValueError("scorecard commitment differs from plan")
            if "provider" not in declared or _id(score["provider"]) != _id(declared["provider"]):
                raise ValueError("scorecard provider differs from plan")
            if score["replicates"] != reps:
                raise ValueError("scorecard replicates differ from plan")
            if {r["card_id"] for r in rows} != set(cards):
                raise ValueError("scorecard card set differs from plan")
            if any(r.get("pool", _id(score["pool"])) != _id(declared.get("pool")) for r in rows):
                raise ValueError("scorecard row pool differs from plan")
            expected = plan.get("treatment", {}).get(side)
            if expected is not None and _id(score.get("treatment")) != expected:
                raise ValueError("scorecard treatment differs from plan")
            looks = {r.get("look", "initial") for r in rows}
            for card in cards:
                for look in looks:
                    found = [r for r in rows if r["card_id"] == card and r.get("look", "initial") == look]
                    if len(found) != reps or {r["replicate"] for r in found} != set(range(1, reps + 1)):
                        raise ValueError("scorecard pairs differ from plan")
            if any(r.get("look", "initial") not in {"initial", "extension"} for r in rows):
                raise ValueError("unknown plan look")
        checked.append(dict(tier=selected, cards=sorted(cards), replicates=reps, pool=declared["pool"]))
    return {"matched": True, "tiers": checked, "sha256": sha256(canonical(plan))}


def _registered_vetoes(rule, snapshot):
    supplied = rule.get("vetoes", snapshot)
    if not isinstance(supplied, dict) or not isinstance(supplied.get("keep_when"), list):
        raise ValueError("invalid veto snapshot")
    if any(c not in supplied["keep_when"] for c in snapshot["keep_when"]):
        raise ValueError("rule vetoes omit registered snapshot clauses")
    return supplied


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


def compare(incumbent, candidate, rule, *, tier="T2", rule_choice=None, aa=False, veto_rule=None,
            reviews=None, attest=None, previous=(), cards=None):
    compatible(incumbent, candidate)
    if aa and any(incumbent.get(k) != candidate.get(k) or candidate.get(k) is None for k in ("build", "treatment")):
        raise ValueError("A/A requires the same build and treatment")
    choice = dict(rule_choice or {})
    choice["_reviews"], choice["_attest"] = reviews or [], attest or {}
    plan = choice.get("plan", {})
    binding = _bind_plan(incumbent, candidate, plan, tier, cards)
    if any(c.get("check") not in CHECKS for c in rule["keep_when"] if "check" in c):
        raise ValueError("unknown check")
    if not rule["keep_when"]:
        raise ValueError("empty keep_when")
    if sum(p.get("decision") == "extend" for p in previous) > 1:
        raise ValueError("second extension refused")
    if (any(p.get("decision") == "extend" for p in previous)
            and choice.get("extension", rule.get("extension"))
            and all(r.get("look", "initial") == "initial" for r in candidate["individual"])):
        raise ValueError("second extension refused")
    novelty_clauses = any(c.get("metric") == "novelty_wins" for c in rule["keep_when"])
    if novelty_clauses:
        digests = [s.get("catalog", {}).get("sha256") for s in (incumbent, candidate)]
        if not all(digests) or digests[0] != digests[1]:
            raise ValueError("incompatible novelty catalog digests")
    rule = _effective(rule, choice, plan)
    if any(r.get("look") == "extension" for r in candidate["individual"]) and not rule["extension"]:
        raise ValueError("extension look was not declared")
    pairs = _pairs(incumbent, candidate, tier, default_tier=tier)
    stats = {m: paired_stats(pairs, m) for m in METRICS}
    veto_source = "explicit registered snapshot" if veto_rule is not None else "repository VETOES.json"
    snapshot = veto_rule if veto_rule is not None else json.loads((RULES / "VETOES.json").read_text())
    veto_rule = _registered_vetoes(rule, snapshot)
    if any(c.get("check") not in CHECKS for c in veto_rule["keep_when"] if "check" in c):
        raise ValueError("unknown veto check")
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
        contradiction = False
        if clause.get("check") == "heldout_identifier_matches":
            reported = candidate["aggregate"].get("heldout_identifier_matches")
            reads = [r.get("held_out_reads") for r in candidate["individual"]]
            if type(reported) is bool and all(type(v) is bool for v in reads):
                contradiction = reported != any(reads)
            elif type(reported) is int and all(type(v) is bool for v in reads):
                contradiction = reported != sum(reads)
            consistent &= not contradiction
        if clause.get("unless") == "plan_declared":
            exception = plan.get("cost_ratio_exception", {})
            if exception:
                if not number(exception.get("max_ratio")) or not exception.get("reason"):
                    raise ValueError("invalid cost ratio exception")
                threshold = exception["max_ratio"]
        passed = consistent and _passed(clause, value, threshold)
        vetoes.append(dict(clause=clause, value=value if value != math.inf else "infinity", passed=passed,
                           fired=(value is not None and not passed) or contradiction,
                           reason="contradictory evidence" if contradiction else None))
    positive_claims = any(type(v) is int and v > 0 for v in
                          [candidate["aggregate"].get("false_claims"),
                           *[r.get("false_claims") for r in candidate["individual"]]])
    positive_claims |= any(r.get("false_positive_claim") is True for r in candidate["individual"])
    if positive_claims:
        vetoes.append(dict(clause={"invariant": "false_claims > 0"}, value=True, passed=False, fired=True))
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
                    if stat == "count" and metric == "novelty_wins":
                        values = [b.get("novelty_win") for a, b in selected_pairs]
                        total = candidate["aggregate"].get(metric)
                        if all(type(v) is bool for v in values) and type(total) is int and sum(values) == total:
                            value = total
                    elif computed and stat in {"mean_delta", "signflip_p", "mean_lower_bound"}:
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
    failures = any(v["fired"] for v in vetoes)
    pending = any(c.get("status") == "pending" for c in clauses)
    failed = any(not c["passed"] and c.get("status") != "pending" for c in clauses)
    reasons = []
    if not plan:
        reasons.append("no registered plan")
    if pending:
        reasons.append("required tiers pending")
    if incumbent.get("incomplete") or candidate.get("incomplete"):
        reasons.append("incomplete scorecard")
    incident_pairs = sum(bool(a.get("incident") or b.get("incident")) for a, b in pairs)
    if any(r.get("incident") for score in (incumbent, candidate) for r in score["individual"]):
        reasons.append("incident in paired evidence")
    if any(c.get("value") is None for c in [*clauses, *vetoes]):
        reasons.append("missing clause or veto evidence")
    if failures:
        decision = "revert"
    elif reasons:
        decision = "incomplete"
    elif failed:
        decision = "revert"
    elif clauses and all(c["passed"] for c in clauses):
        decision = "keep"
    else:
        decision = "incomplete"
    # Only statistical shortfall at the initial look can trigger a declared
    # extension; a safety veto or a failed non-statistical clause cannot.
    failed_clauses = [c for c in clauses if not c["passed"] and c.get("status") != "pending"]
    primary = "mrg" if rule["id"] == "R-disc" else "vrg"
    first_look = all(r.get("look", "initial") == "initial" for r in candidate["individual"])
    if (rule["extension"] and first_look and not failures and not pending and failed_clauses
            and all(c["clause"].get("metric") == primary and c["clause"].get("stat") in {"mean_delta", "signflip_p"}
                    and c["clause"].get("tier") == tier for c in failed_clauses)
            and not reasons and not any(p.get("decision") == "extend" for p in previous)
            and stats[primary] and stats[primary]["mean_delta"] > 0
            and not incumbent.get("incomplete") and not candidate.get("incomplete")):
        decision = "extend"
    result = dict(schema="eidos-exp-decision/1", decision=decision, rule=rule["id"], tier=tier,
                  clauses=clauses, vetoes=vetoes, stats=stats, reasons=reasons, plan_binding=binding,
                  coverage={"paired_runs": len(pairs), "incident_pairs": incident_pairs},
                  extensions_used=sum(p.get("decision") == "extend" for p in previous),
                  inputs={"incumbent": sha256(canonical(incumbent)), "candidate": sha256(canonical(candidate)),
                          "rule": sha256(canonical(rule)), "vetoes": sha256(canonical(veto_rule)),
                          "reviews": sha256(canonical(reviews or [])), "attest": sha256(canonical(attest or {})),
                          "previous": sha256(canonical(previous))},
                  veto_source=veto_source)
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
        result["aa"]["rule_decision"] = "would_keep" if decision == "keep" else decision
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


def _review(path):
    text = path.read_text()
    verdict = re.search(r"(?im)^\s*(?:\*\*)?verdict\s*:\s*(ACCEPT|REJECT)\b", text)
    commit = re.search(r"(?im)^\s*(?:\*\*)?commit\s*:\s*`?([a-zA-Z0-9._-]+)", text)
    kind = re.search(r"(?im)^\s*kind\s*:\s*(code|result)\b", text)
    if not verdict or not commit:
        raise ValueError("review requires verdict line and nonempty commit")
    return dict(verdict=verdict[1], commit=commit[1], kind=kind[1] if kind else "code")


def _snapshot(directory):
    ledger = directory / "ledger.jsonl"
    if not ledger.is_file():
        return None
    for line in ledger.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("type") == "register":
            data = entry["data"]
            snapshot = data["vetoes_snapshot"]
            if sha256(canonical(snapshot)) != data["vetoes_sha256"]:
                raise ValueError("registered veto snapshot digest mismatch")
            return snapshot
    raise ValueError("ledger has no registered veto snapshot")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incumbent", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--rule", type=Path, required=True)
    parser.add_argument("--rule-choice", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--cards", type=int)
    parser.add_argument("--review", type=Path, action="append", default=[])
    parser.add_argument("--attest", type=Path)
    parser.add_argument("--previous", type=Path, nargs="+", default=[])
    parser.add_argument("--tier", choices=["T0", "T1", "T2", "T3"], default="T2")
    parser.add_argument("--out", type=Path, default=Path("decision.json"))
    parser.add_argument("--aa", action="store_true")
    args = parser.parse_args(argv)
    temporary = None
    plan_path = args.plan or (args.rule_choice or args.rule).with_name("plan.json")
    try:
        paths = {"incumbent": args.incumbent, "candidate": args.candidate, "rule": args.rule}
        if args.out.resolve() in {p.resolve() for p in [*paths.values(), args.rule_choice, plan_path, plan_path.parent / "ledger.jsonl", args.attest, *args.review, *args.previous] if p}:
            raise ValueError("output must differ from input files")
        args.out.unlink(missing_ok=True)
        data = {k: json.loads(p.read_text()) for k, p in paths.items()}
        choice = json.loads(args.rule_choice.read_text()) if args.rule_choice else {}
        if args.rule_choice:
            paths["rule_choice"] = args.rule_choice
        if plan_path.is_file():
            choice["plan"] = json.loads(plan_path.read_text())
            paths["plan"] = plan_path
        elif args.plan:
            raise ValueError("plan file missing")
        snapshot = _snapshot(plan_path.parent)
        if snapshot is not None:
            paths["vetoes/ledger"] = plan_path.parent / "ledger.jsonl"
            veto_source = "registered ledger snapshot"
        else:
            paths["vetoes"] = RULES / "VETOES.json"
            snapshot = json.loads(paths["vetoes"].read_text())
            veto_source = "repository VETOES.json"
        reviews = [_review(path) for path in args.review]
        attest = json.loads(args.attest.read_text()) if args.attest else None
        previous = [json.loads(p.read_text()) for p in args.previous]
        for i, path in enumerate(args.review):
            paths[f"review/{i}"] = path
        for i, path in enumerate(args.previous):
            paths[f"previous/{i}"] = path
        if args.attest:
            paths["attest"] = args.attest
        result = compare(data["incumbent"], data["candidate"], data["rule"], tier=args.tier,
                         rule_choice=choice, aa=args.aa, veto_rule=snapshot, reviews=reviews,
                         attest=attest, previous=previous, cards=args.cards)
        result["veto_source"] = veto_source
        if args.aa:
            decisions = {}
            for path in sorted(RULES.glob("R-*.json")):
                rule = json.loads(path.read_text())
                diagnostic = compare(data["incumbent"], data["candidate"], rule, tier=args.tier,
                                     rule_choice={"plan": choice.get("plan", {})}, aa=True,
                                     veto_rule=snapshot, reviews=reviews, attest=attest, previous=previous, cards=args.cards)
                decisions[rule["id"]] = diagnostic["aa"]["rule_decision"]
                paths["aa/" + path.name] = path
            result["aa"]["rules"] = decisions
        result["inputs"] = {k: sha256(p.read_bytes()) for k, p in paths.items()}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=args.out.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(args.out)
        print(markdown(result))
    except (ValueError, KeyError, TypeError, OSError, AttributeError, RecursionError, IndexError, OverflowError) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if isinstance(exc, OSError) and exc.filename:
            message = f"{exc.strerror}: {os.path.relpath(exc.filename, args.candidate.parent)}"
        else:
            message = str(exc)
            for path in paths.values():
                message = message.replace(str(path.resolve()), os.path.relpath(path, args.candidate.parent))
        parser.exit(2, f"comparison input error: {type(exc).__name__}: {message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
