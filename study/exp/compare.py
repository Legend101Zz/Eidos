"""Fail-closed paired comparison against registered experiment rule clauses.

The CLI accepts INCUMBENT.json CANDIDATE.json, or repeated --incumbent FILE
--candidate FILE pairs in matching order, one per tier/look. The API accepts
one dict or ordered lists of dicts on each side. Tier/look declarations supply
the exact card set, replicate count, pool role and provider. The header pool id
remains the manifest id (E1/E2/E3 for evaluation_rotation). Row pool tags are
roles. Omitted tiers remain pending. tiers[].extension declares the sole extra
look with its own cards and replicates (and optional pool/provider/draw).

README: --experiment DIR selects the registered experiment; otherwise infer DIR
from --plan FILE or the adjacent plan.json of --rule-choice/--rule. A keep requires
a registered ledger, unchanged registered files and the effective frozen rule
computed by study.exp.ledger.effective_rule. An inline plan in a rule-choice file
is refused. The Python API's optional experiment parameter names the same DIR;
its rule_choice['plan'] remains supported and must equal the registered plan.
Without registration the best result is incomplete (no registered experiment).
--cards N requires the exact declaration or draw={method: "random", seed: INT,
count: N}, sampling with Python Random(seed).sample; no hand picking.

--usage FILE / API usage=dict optionally accepts eidos-exp-usage/1 runner JSON:
runs, usd (number|null), quota_weekly_percent (number|null), time_seconds and
per_variant. runs and budget totals cover BOTH arms and all supplied tiers/looks.
per_variant may use incumbent/candidate keys or distinct treatment ids, with
{runs, usd} entries to supply cost-ratio evidence when run billing is unknown.
Unknown billing without variant accounting leaves cost_ratio unknown.
The CLI records the usage file SHA-256 in decision.inputs.usage.

With registration, reviews come from ledger review entries; latest sequence for
code wins, even when its commit does not match. --review FILE is a diagnostic
fallback without registration. Accept VERDICT: ACCEPT|FIX FIRST|REJECT — commit
SHA, or separate Verdict:/Commit: lines (optional Kind: code/result). A 7-to-40
hex prefix must match the full build commit. Full build commits within a bundle
must agree, so the prefix identifies one candidate build unambiguously.
Boolean checks require --attest FILE / API attest=dict with experiment (plan id),
commit (full build SHA), checks and optional tier_checks, recorded with its digest.
Scorecard review/check fields are ignored. External checks never become true
from a favourable numeric score. All new API parameters are optional.
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

from study.exp import ledger
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
        for key in ("pool", "envelope", "references_sha256", "replicates"):
            if score.get(key) is None:
                raise ValueError(f"missing comparison identity: {key}")
        if "provider" not in score:
            raise ValueError("missing comparison identity: provider")
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
        if len({(r.get("tier"), r.get("look", "initial")) for r in score["individual"]}) == 1 and any(
                reps != set(range(1, score["replicates"] + 1)) for reps in groups.values()):
            raise ValueError("incomplete declared replicates")
    for key in ("pool", "replicates", "provider", "envelope", "references_sha256"):
        equivalent = (_provider(incumbent.get(key)) is None and _provider(candidate.get(key)) is None
                      if key == "provider" else False)
        if not equivalent and incumbent[key] != candidate[key]:
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
        known_matches = sum(v is True for v in reads)
        if type(value) is bool:
            return max(int(value), known_matches) if value or known_matches or count is not None else None
        if type(value) is int and value >= 0:
            return max(value, known_matches) if value > 0 or known_matches > 0 or count is not None else None
        return known_matches if known_matches else count
    if name == "cost_ratio":
        old, new = _cost(a), _cost(b)
        if not number(old) or not number(new) or min(old, new) < 0:
            return None
        return new / old if old else (1.0 if new == 0 else math.inf)
    if name == "within_plan_cost_and_time":
        budget = plan.get("budget", {})
        if not budget:
            return None
        actual = choice.get("_usage") or {
            "usd": _sum_known([_cost(a), _cost(b)]),
            "live_runs": len(a["individual"]) + len(b["individual"]),
            "quota_weekly_percent": _sum_known([a["aggregate"].get("quota_weekly_percent"), agg.get("quota_weekly_percent")]),
            "time_seconds": _sum_known([a["aggregate"].get("time_seconds"), agg.get("time_seconds")])}
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
        reviews = [r for r in choice.get("_reviews", []) if r.get("kind") == "code"]
        latest = max(reviews, key=lambda r: r.get("sequence", 0)) if reviews and any("sequence" in r for r in reviews) else (reviews[-1] if reviews else {})
        return latest.get("verdict") if _commit_matches(latest.get("commit"), commit) else None
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


def _provider(value):
    value = _id(value)
    return None if value in {None, "none (fixture runtime)", "model-free"} else value


def _commit_matches(prefix, commit):
    return (isinstance(prefix, str) and isinstance(commit, str)
            and re.fullmatch(r"[0-9a-fA-F]{7,40}", prefix) is not None
            and re.fullmatch(r"[0-9a-fA-F]{40}", commit) is not None
            and commit.lower().startswith(prefix.lower()))


def _sum_known(values):
    return sum(values) if all(number(v) and v >= 0 for v in values) else None


def _bind_plan(a, b, plan, tier, count):
    if not plan:
        return {"matched": False, "reason": "no registered plan"}
    declarations = plan.get("tiers")
    if not isinstance(declarations, list) or not declarations:
        raise ValueError("plan requires tier declarations")
    checked = []
    available = {(r.get("tier", tier), r.get("look", "initial")) for r in b["individual"]}
    for selected, look in sorted(available):
        matching = [d for d in declarations if d.get("tier") == selected]
        if len(matching) != 1:
            raise ValueError("plan does not uniquely declare scorecard tier")
        declared = matching[0]
        if look not in {"initial", "extension"}:
            raise ValueError("unknown plan look")
        if look == "extension":
            extension = declared.get("extension")
            if not isinstance(extension, dict) or not {"cards", "replicates"} <= extension.keys():
                raise ValueError("plan requires declared extension cards and replicates")
            declared = dict(declared, **extension)
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
        role = _id(declared.get("pool"))
        for side, score in (("incumbent", a), ("candidate", b)):
            rows = [r for r in score["individual"] if r.get("tier", tier) == selected and r.get("look", "initial") == look]
            manifest = _id(score["pool"])
            if (manifest not in {"E1", "E2", "E3"} if role == "evaluation_rotation" else manifest != role):
                raise ValueError("scorecard pool differs from plan")
            commitment = declared.get("commitment", declared.get("pool_commitment"))
            if isinstance(declared.get("pool"), dict):
                commitment = declared["pool"].get("commitment", commitment)
            if commitment is not None and score["pool"].get("commitment") != commitment:
                raise ValueError("scorecard commitment differs from plan")
            if "provider" not in declared or _provider(score.get("provider")) != _provider(declared["provider"]):
                raise ValueError("scorecard provider differs from plan")
            if len(available) == 1 and score["replicates"] != reps:
                raise ValueError("scorecard replicates differ from plan")
            if {r["card_id"] for r in rows} != set(cards):
                raise ValueError("scorecard card set differs from plan")
            if any(r.get("pool", role) != role for r in rows):
                raise ValueError("scorecard row pool differs from plan")
            treatments = plan.get("treatment")
            if not isinstance(treatments, dict):
                raise ValueError("plan treatment must be an object")
            expected = treatments.get(side)
            if expected is not None and _id(score.get("treatment")) != expected:
                raise ValueError("scorecard treatment differs from plan")
            for card in cards:
                found = [r for r in rows if r["card_id"] == card]
                if len(found) != reps or {r["replicate"] for r in found} != set(range(1, reps + 1)):
                    raise ValueError("scorecard pairs differ from plan")
            for row in rows:
                row.setdefault("pool", role)
        checked.append(dict(tier=selected, look=look, cards=sorted(cards), replicates=reps, pool=role))
    return {"matched": True, "tiers": checked, "sha256": sha256(canonical(plan))}


def _combine(scores):
    result = copy.deepcopy(scores[0])
    if len(scores) == 1:
        return result
    for key in ("build", "treatment", "catalog"):
        if any(s.get(key) != result.get(key) for s in scores):
            raise ValueError(f"scorecard bundle has incompatible {key}")
    result["individual"] = [r for s in scores for r in s["individual"]]
    identities = [_identity(r) for r in result["individual"]]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate paired row across scorecards")
    result["per_card"] = {r["card_id"]: {} for r in result["individual"]}
    result["incomplete"] = any(s.get("incomplete") for s in scores)
    result["aggregate"] = {k: _sum_known([s["aggregate"].get(k) for s in scores]) for k in
                           ("false_claims", "usd", "live_runs", "time_seconds", "quota_weekly_percent",
                            "heldout_identifier_matches", "novelty_wins")}
    return result


def _registration(directory, rule, choice, plan):
    if directory is None or not (Path(directory) / "ledger.jsonl").is_file():
        return None
    directory = Path(directory)
    entries = ledger.read_chain(directory / "ledger.jsonl")
    if not entries or entries[0]["type"] != "register":
        raise ValueError("ledger requires register")
    ledger.check_registered(directory, entries)
    registered_plan = ledger.read_json(directory / "plan.json")
    if plan and plan != registered_plan:
        raise ValueError("plan differs from registered plan")
    expected = ledger.effective_rule(RULES.parents[1], directory, entries)
    registered_choice = ledger.read_json(directory / "rule.json")
    if any(key in choice and choice[key] != registered_choice[key]
           for key in ("rule", "version", "overrides", "extension")):
        raise ValueError("rule choice differs from registered rule choice")
    _registered_vetoes(rule, entries[0]["data"]["vetoes_snapshot"])
    if "vetoes" in rule and rule["vetoes"] != expected["vetoes"]:
        raise ValueError("effective rule vetoes differ from registered snapshot")
    # Apply exactly the ledger's merge to the caller's rule as well. No second
    # implementation of registered override/model-free semantics lives here.
    supplied_entries = copy.deepcopy(entries)
    supplied_entries[0]["data"]["rule_snapshot"] = rule
    effective = ledger.effective_rule(RULES.parents[1], directory, supplied_entries)
    if effective != expected:
        raise ValueError("effective rule differs from registered rule snapshot")
    snapshot = entries[0]["data"]["vetoes_snapshot"]
    _registered_vetoes({"vetoes": snapshot}, ledger.read_json(RULES / "VETOES.json"))
    return registered_plan, expected, entries


def _usage_evidence(usage, a, b):
    if usage is None:
        return None
    if usage.get("schema") != "eidos-exp-usage/1":
        raise ValueError("invalid usage schema")
    runs = len(a["individual"]) + len(b["individual"])
    if type(usage.get("runs")) is not int or usage["runs"] != runs:
        raise ValueError("usage runs must count both arms")
    for key in ("usd", "quota_weekly_percent", "time_seconds"):
        value = usage.get(key)
        if (value is None and key == "time_seconds") or (value is not None and (not number(value) or value < 0)):
            raise ValueError("invalid usage accounting")
    variants = usage.get("per_variant")
    if not isinstance(variants, dict):
        raise ValueError("usage requires per_variant object")
    # Explicit side names allow cost-ratio evidence even when run billing is unknown.
    for side, score in (("incumbent", a), ("candidate", b)):
        evidence = variants.get(side, variants.get(_id(score.get("treatment"))))
        if evidence is not None:
            if not isinstance(evidence, dict) or evidence.get("runs") != len(score["individual"]):
                raise ValueError("usage variant runs mismatch")
            usd = evidence.get("usd")
            if usd is not None and (not number(usd) or usd < 0):
                raise ValueError("invalid variant cost")
            score["aggregate"].pop("cost", None)
            score["aggregate"]["usd"] = usd
    costs = [_cost(a), _cost(b)]
    if all(number(v) for v in costs) and usage.get("usd") is not None and not math.isclose(sum(costs), usage["usd"]):
        raise ValueError("usage total contradicts variant costs")
    return dict(usage, live_runs=runs)


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
    rule["overrides"] = overrides
    return rule


def compare(incumbent, candidate, rule, *, tier="T2", rule_choice=None, aa=False, veto_rule=None,
            reviews=None, attest=None, previous=(), cards=None, experiment=None, usage=None):
    """Compare dict scorecards or ordered lists of dicts (one per tier/look).

    experiment optionally names a registered directory; it is required for keep.
    usage optionally supplies runner accounting (eidos-exp-usage/1) for both arms.
    All inputs are copied; defaults retain single-pair and A/A calling conventions.
    """
    input_digests = {"incumbent": sha256(canonical(incumbent)), "candidate": sha256(canonical(candidate))}
    choice = dict(rule_choice or {})
    plan = choice.get("plan", {})
    registration = _registration(experiment, rule, choice, plan)
    if registration:
        plan, rule, entries = registration
        choice = dict(choice, plan=plan, overrides=rule["overrides"], extension=rule["extension"])
        veto_rule = rule["vetoes"]
        reviews = [dict(e["data"], sequence=e["seq"]) for e in entries if e["type"] == "review"]
    elif experiment is not None and Path(experiment).joinpath("ledger.jsonl").exists():
        raise ValueError("no registered experiment")
    left = copy.deepcopy(incumbent if isinstance(incumbent, list) else [incumbent])
    right = copy.deepcopy(candidate if isinstance(candidate, list) else [candidate])
    if not left or len(left) != len(right):
        raise ValueError("scorecard bundles require paired files")
    if aa and any(a.get(k) != b.get(k) or b.get(k) is None
                  for a, b in zip(left, right, strict=True) for k in ("build", "treatment")):
        raise ValueError("A/A requires the same build and treatment")
    bindings = []
    for a, b in zip(left, right, strict=True):
        compatible(a, b)
        bindings.append(_bind_plan(a, b, plan, tier, cards))
    incumbent, candidate = _combine(left), _combine(right)
    if tier not in {r.get("tier", tier) for r in candidate["individual"]}:
        raise ValueError("requested tier absent from scorecard")
    usage_actual = _usage_evidence(usage, incumbent, candidate)
    if attest is not None and (
            not isinstance(attest.get("experiment"), str) or not attest["experiment"]
            or not _commit_matches(attest.get("commit"), candidate.get("build", {}).get("commit"))
            or len(attest["commit"]) != 40 or attest["experiment"] != plan.get("id")):
        raise ValueError("attestation experiment/commit mismatch")
    choice["_reviews"], choice["_attest"], choice["_usage"] = reviews or [], attest or {}, usage_actual
    binding = dict(matched=all(b["matched"] for b in bindings), batches=bindings)
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
    if not registration:
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
                elif selected_pairs and not any(a.get("incident") or b.get("incident") for a, b in selected_pairs):
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
    failed = any(c["value"] is not None and not c["passed"] and c.get("status") != "pending" for c in clauses)
    reasons = []
    if not plan:
        reasons.append("no registered plan")
    if not registration:
        reasons.append("no registered experiment")
    if pending:
        reasons.append("required tiers pending")
    if incumbent.get("incomplete") or candidate.get("incomplete"):
        reasons.append("incomplete scorecard")
    incident_pairs = sum(bool(a.get("incident") or b.get("incident")) for a, b in pairs)
    if any(r.get("incident") for score in (incumbent, candidate) for r in score["individual"]):
        reasons.append("incident in paired evidence")
    if any(c.get("value") is None for c in [*clauses, *vetoes]):
        reasons.append("missing clause or veto evidence")
    if failures or failed:
        decision = "revert"
    elif reasons:
        decision = "incomplete"
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
                  coverage={"paired_runs": len(pairs), "incident_pairs": incident_pairs,
                            "total_paired_runs": len(candidate["individual"])},
                  extensions_used=sum(p.get("decision") == "extend" for p in previous),
                  inputs={**input_digests, "rule": sha256(canonical(rule)), "vetoes": sha256(canonical(veto_rule)),
                          "reviews": sha256(canonical(reviews or [])), "attest": sha256(canonical(attest or {})),
                          "previous": sha256(canonical(previous)), "usage": sha256(canonical(usage)) if usage is not None else None},
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
    verdict = re.search(r"(?im)^\s*(?:\*\*)?verdict\s*:\s*(ACCEPT|FIX FIRST|REJECT)\b", text)
    commit = re.search(r"(?im)^\s*(?:\*\*)?commit\s*:\s*`?([a-zA-Z0-9._-]+)", text)
    kind = re.search(r"(?im)^\s*kind\s*:\s*(code|result)\b", text)
    if not commit:
        commit = re.search(r"(?im)^\s*(?:\*\*)?verdict\s*:.*?\bcommit\s+`?([0-9a-fA-F]{7,40})\b", text)
    if not verdict or not commit:
        raise ValueError("review requires verdict line and nonempty commit")
    return dict(verdict=verdict[1], commit=commit[1], kind=kind[1] if kind else "code")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incumbent", type=Path, nargs="?")
    parser.add_argument("candidate", type=Path, nargs="?")
    parser.add_argument("--incumbent", dest="incumbents", type=Path, action="append", default=[])
    parser.add_argument("--candidate", dest="candidates", type=Path, action="append", default=[])
    parser.add_argument("--experiment", type=Path)
    parser.add_argument("--usage", type=Path)
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
    plan_path = args.plan or (args.experiment / "plan.json" if args.experiment else
                              (args.rule_choice or args.rule).with_name("plan.json"))
    experiment = args.experiment or plan_path.parent
    base = (args.candidate or (args.candidates[0] if args.candidates else args.out)).parent
    paths = {}
    try:
        left, right = args.incumbents, args.candidates
        if args.incumbent or args.candidate:
            if left or right or not args.incumbent or not args.candidate:
                raise ValueError("use one positional pair or repeated --incumbent/--candidate pairs")
            left, right = [args.incumbent], [args.candidate]
        if not left or len(left) != len(right):
            raise ValueError("supply equal nonzero incumbent/candidate scorecards")
        paths = {"rule": args.rule}
        for side, files in (("incumbent", left), ("candidate", right)):
            for i, path in enumerate(files):
                paths[side if len(files) == 1 else f"{side}/{i}"] = path
        # Protect all implicit inputs before stale-output cleanup, including A/A rules.
        protected = [*paths.values(), args.rule_choice, plan_path, experiment / "ledger.jsonl",
                     experiment / "rule.json", experiment / "HYPOTHESIS.md", args.attest, args.usage,
                     *args.review, *args.previous, *RULES.glob("*.json")]
        if args.out.resolve() in {p.resolve() for p in protected if p}:
            raise ValueError("output must differ from input files")
        args.out.unlink(missing_ok=True)
        incumbent = [ledger.read_json(p) for p in left]
        candidate = [ledger.read_json(p) for p in right]
        rule = ledger.read_json(args.rule)
        choice_path = args.rule_choice or (experiment / "rule.json" if (experiment / "ledger.jsonl").is_file() else None)
        choice = ledger.read_json(choice_path) if choice_path else {}
        if "plan" in choice:
            raise ValueError("inline plan key refused; use registered plan.json")
        if choice_path:
            paths["rule_choice"] = choice_path
        if plan_path.is_file():
            choice["plan"] = ledger.read_json(plan_path)
            paths["plan"] = plan_path
        elif args.plan or args.experiment:
            raise ValueError("plan file missing")
        if (experiment / "ledger.jsonl").is_file():
            paths["ledger"] = experiment / "ledger.jsonl"
            paths["registered_rule_choice"] = experiment / "rule.json"
            veto_source = "registered ledger snapshot"
        else:
            veto_source = "repository VETOES.json"
        paths["vetoes"] = RULES / "VETOES.json"
        snapshot = ledger.read_json(paths["vetoes"])
        reviews = [_review(path) for path in args.review]
        attest = ledger.read_json(args.attest) if args.attest else None
        usage = ledger.read_json(args.usage) if args.usage else None
        previous = [ledger.read_json(p) for p in args.previous]
        for i, path in enumerate(args.review):
            paths[f"review/{i}"] = path
        for i, path in enumerate(args.previous):
            paths[f"previous/{i}"] = path
        for name, path in (("attest", args.attest), ("usage", args.usage)):
            if path:
                paths[name] = path
        result = compare(incumbent, candidate, rule, tier=args.tier,
                         rule_choice=choice, aa=args.aa, reviews=reviews,
                         attest=attest, previous=previous, cards=args.cards,
                         experiment=experiment, usage=usage)
        result["veto_source"] = veto_source
        if args.aa:
            decisions = {}
            for path in sorted(RULES.glob("R-*.json")):
                diagnostic = compare(incumbent, candidate, ledger.read_json(path), tier=args.tier,
                                     rule_choice={"plan": choice.get("plan", {})}, aa=True,
                                     veto_rule=snapshot, reviews=reviews, attest=attest, previous=previous, cards=args.cards,
                                     usage=usage)
                decisions[ledger.read_json(path)["id"]] = diagnostic["aa"]["rule_decision"]
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
            message = f"{exc.strerror}: {os.path.relpath(exc.filename, base)}"
        else:
            message = str(exc)
            for path in paths.values():
                message = message.replace(str(path.resolve()), os.path.relpath(path, base))
        parser.exit(2, f"comparison input error: {type(exc).__name__}: {message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
