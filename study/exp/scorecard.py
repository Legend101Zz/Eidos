"""Build reproducible development evidence from local graded run exports.

build(..., tier=None, pool_role=None, look=None, mode=None) optionally stamps
all rows with tier, pool (the plan role, not the manifest id), look and mode.
The CLI exposes --tier/--pool-role/--look/--mode. Existing row tags must agree;
omitted keywords preserve recorded row tags. Use one scorecard per tier/look
when pools, providers or replicate counts differ. provider=None supports a
model-free tier. Header pool.id always records the actual pool manifest id.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import statistics
from datetime import UTC, datetime
from pathlib import Path

from study.exp import novelty, traces
from study.exp.stats import geomean

SCHEMA = "eidos-exp-scorecard/1"
METRICS = ("vrg", "mrg", "verified_rate", "error_ratio")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _mean(values):
    values = list(values)
    return statistics.mean(values) if values else None


def _sum_known(values):
    values = list(values)
    return sum(values) if values and all(number(v) for v in values) else None


def _median(values):
    values = list(values)
    return statistics.median(values) if values and all(number(v) for v in values) else None


def _tokens(value):
    if isinstance(value, dict):
        return value.get("total") if number(value.get("total")) else None
    return value if number(value) else None


def _observations(rows, record, answer):
    states = [r["data"] for r in rows if r.get("type") == "investigation"]
    events = [r["data"] for r in rows if r.get("type") == "ledger_event"]
    state = states[-1] if states else {}
    body = state.get("body", {})
    attribution = body.get("controller_abstention")
    for event in events:
        if event.get("type") == "investigation.controller_abstained":
            attribution = event.get("body")
        elif event.get("type") == "investigation.resumed":
            attribution = None
    concluded = bool(body.get("conclusion") or any(e.get("type") == "investigation.concluded" for e in events)) and attribution is None
    if attribution is not None:
        disposition = "controller_abstention" if attribution.get("schema") == "controller-abstention/1" else "unknown_controller_attribution"
    elif concluded:
        status = body.get("conclusion", {}).get("scope", {}).get("answer", {}).get("status")
        disposition = "investigator_abstention" if status == "abstained" else "investigator_conclusion"
    else:
        disposition = "no_conclusion"
    if record.get("stub") or record.get("arm") == 2:
        concluded = record.get("status") == "ok" and answer is not None
        disposition = ("stub_answer" if record.get("stub") else "coding_answer") if concluded else "no_conclusion"
    locked = any(r.get("type") == "note" and r["data"].get("body", {}).get("kind") == "candidate_lock" for r in rows)
    locked |= any(e.get("type") == "incumbent.changed" for e in events)
    dead_end = True if locked and not concluded and attribution is None else None
    if record.get("stub") or (states and any(r.get("type") == "turn" for r in rows)):
        dead_end = bool(locked and not concluded and attribution is None)
    return dict(concluded=concluded, disposition=disposition, dead_end=dead_end,
                held_out_reads=False if record.get("stub") else None)


def _claims(answer, grade):
    if answer is None:
        status, claim = grade.get("answer_status"), grade.get("claim_type")
    else:
        status, claim = answer.get("status"), answer.get("claim_type")
    if status is None and claim is None:
        return None
    return status == "improvement_claimed" or claim in {
        "checkpoint_improvement", "recipe_improvement", "architecture_improvement", "transfer"
    }


def _run_error(row, grade, ref, gain, verified):
    if not verified:
        return ref["err_base"]
    numeric = grade.get("numeric") or {}
    metric = ref.get("metric", numeric.get("metric"))
    candidate = numeric.get("candidate", row.get("test_score"))
    if not number(candidate) and number(gain):
        candidate = ref["base_score"] + gain if metric == "accuracy" else ref["base_score"] - gain
    if not number(candidate):
        raise ValueError("verified run lacks an independent candidate score")
    if metric == "accuracy":
        error = 1 - candidate
    elif metric == "rmse":
        # reference.py stores err_base = base_score / max(SD(y_test), 1e-12).
        # Recover that scale without reading sealed labels or executing code.
        if ref["err_base"] <= 0 or ref["base_score"] <= 0:
            raise ValueError("cannot recover regression test SD from a zero baseline")
        error = candidate * ref["err_base"] / ref["base_score"]
    else:
        raise ValueError("reference metric must be accuracy or rmse")
    if not number(error) or error < -1e-12:
        raise ValueError("invalid candidate error")
    return max(0.0, error)


def build(run_dir, pool_root=None, references=None, *, treatment, provider, build_record,
          replicates, generated_utc=None, autopilot=None, arm=None, catalog_record=None,
          tier=None, pool_role=None, look=None, mode=None):
    """One individual per (card, arm, replicate), equal card weights in aggregate.

    Incident rows retain accounting/claim evidence but have no primary metrics.
    Input paths in the output are logical relative labels, never local paths.
    Optional tier, pool_role, look and mode stamp every row; defaults preserve
    recorded tags. pool_role is written as row.pool; header.pool is the manifest.
    Conflicting recorded tags are errors.
    """
    tags = {"tier": tier, "pool": pool_role, "look": look, "mode": mode}
    if tier is not None and tier not in {"T0", "T1", "T2", "T3"}:
        raise ValueError("unknown tier")
    if look is not None and look not in {"initial", "extension"}:
        raise ValueError("unknown look")
    root = Path(run_dir)
    inputs = {}

    def read(path, label, *, optional=False):
        if optional and not path.is_file():
            return None
        data = path.read_bytes()
        inputs[label] = sha256(data)
        return json.loads(data)

    rows = read(root / "results.json", "run/results.json")
    if not isinstance(rows, list) or not rows:
        raise ValueError("results.json must be a nonempty list of grade rows")
    if type(replicates) is not int or replicates < 1:
        raise ValueError("replicates must be a positive integer")
    arms = {row.get("arm") for row in rows}
    if arm is None and len(arms) > 1:
        raise ValueError("multiple arms require --arm N")
    if arm is not None:
        rows = [row for row in rows if row.get("arm") == arm]
        if not rows:
            raise ValueError("selected arm has no runs")
    pool = {"id": "legacy-development", "commitment": None}
    if pool_root is not None:
        pool_path = Path(pool_root)
        manifest = read(pool_path / "pool.json", "pool/pool.json")
        pool = {"id": manifest["pool_id"], "commitment": manifest["commitment"]}
    if isinstance(references, (str, Path)):
        references = read(Path(references), "references.json")
    references = dict(references or {})
    identities = []
    for row in rows:
        card = row.get("card_id", row.get("task", row.get("card")))
        rep = row.get("replicate", row.get("run", row.get("seed_index")))
        arm = row.get("arm")
        if not isinstance(card, str) or not card.isalnum() or type(rep) is not int or rep < 1 or type(arm) is not int:
            raise ValueError("row requires a safe card id, positive replicate and integer arm")
        identity = (card, arm, rep)
        if identity in identities:
            raise ValueError(f"duplicate run: {identity}")
        identities.append(identity)
        if card not in references and pool_root is not None:
            references[card] = read(Path(pool_root) / "controller" / card / "reference.json",
                                    f"pool/controller/{card}/reference.json")
        if card not in references:
            raise ValueError(f"missing reference for {card}")
    used_references = {card: references[card] for card in sorted({i[0] for i in identities})}
    for card, ref in used_references.items():
        if (not all(number(ref.get(k)) for k in ("headroom", "err_base", "err_ref", "se_paired", "base_score"))
                or ref["headroom"] < 0 or (ref["headroom"] == 0 and ref.get("null_card") is not True)
                or ref["err_base"] < 0 or ref["err_ref"] < 0
                or type(ref.get("null_card")) is not bool):
            raise ValueError(f"invalid reference for {card}")
    records = {}
    for card, selected_arm, rep in identities:
        suffix = f"{card}-r{rep}/arm{selected_arm}"
        records[(card, selected_arm, rep)] = read(root / "runs" / suffix / "run_record.json",
                                                f"run/runs/{suffix}/run_record.json")
    commits = {r.get("eidos_commit_sha") for r in records.values()}
    if len(commits) != 1 or not all(isinstance(c, str) and c.strip() for c in commits):
        raise ValueError("run records require one nonempty build commit")
    commit = next(iter(commits))
    if build_record.get("commit") != commit:
        raise ValueError("recorded build commit differs from header")
    if isinstance(catalog_record, (str, Path)):
        cat = read(Path(catalog_record), "catalog.json")
    elif catalog_record is not None:
        cat = copy.deepcopy(catalog_record)
    else:
        cat = novelty.catalog(Path(__file__).resolve().parents[2], commit=commit)
    if cat.get("commit") != commit:
        raise ValueError("catalog commit differs from recorded build commit")
    contents = {k: v for k, v in cat.items() if k != "sha256"}
    if cat.get("sha256") != sha256(canonical(contents)):
        raise ValueError("catalog digest mismatch")
    inputs.update({"catalog/" + k: v for k, v in cat["inputs"].items()})
    individuals, envelopes = [], []
    for identity, row in sorted(zip(identities, rows, strict=True), key=lambda pair: pair[0]):
        card, arm, rep = identity
        suffix = f"{card}-r{rep}/arm{arm}"
        run = root / "runs" / suffix
        record = records[identity]
        if (record.get("eidos_commit_sha") is not None and build_record.get("commit") is not None
                and record["eidos_commit_sha"] != build_record["commit"]):
            raise ValueError(f"{card}: recorded build commit differs from header")
        grade = read(root / "grading" / suffix / "grade.json", f"run/grading/{suffix}/grade.json")
        for key in ("status", "false_positive_claim"):
            if key in row and key in grade and row[key] != grade[key]:
                raise ValueError(f"{card}: results and grade {key} disagree")
        measured_gain = (grade.get("numeric") or {}).get("gain")
        if number(row.get("test_gain")) and number(measured_gain) and row["test_gain"] != measured_gain:
            raise ValueError(f"{card}: results and grade gain disagree")
        meta = read(run / "arm.json", f"run/runs/{suffix}/arm.json", optional=True) or {}
        for name, expected in (("treatment", treatment), ("provider", provider)):
            if name in meta and meta[name] != expected:
                raise ValueError(f"{card}: mixed {name} metadata")
        envelope = meta.get("envelope", record.get("envelope"))
        if envelope is not None:
            envelopes.append(envelope)
        answer = read(run / "out/answer.json", f"run/runs/{suffix}/out/answer.json", optional=True)
        transcript = run / "transcript.jsonl"
        trace_rows = []
        if transcript.is_file():
            inputs[f"run/runs/{suffix}/transcript.jsonl"] = sha256(transcript.read_bytes())
            trace_rows = traces.read_rows(run)
        if not any(r.get("type") == "investigation" for r in trace_rows):
            state = read(run / "investigation_latest.json", f"run/runs/{suffix}/investigation_latest.json", optional=True)
            if state is not None:
                trace_rows.append({"type": "investigation", "data": state})
        observations = _observations(trace_rows, record, answer)
        for field in ("concluded", "disposition", "dead_end", "held_out_reads"):
            if field in row:
                observations[field] = row[field]
        flags = row.get("invariant_flags", {})
        for field in ("dead_end", "held_out_reads"):
            if field in flags:
                observations[field] = flags[field]
        mechanism = traces.from_records(trace_rows, record, transcript_present=transcript.is_file())
        fp = novelty.fingerprint(run / "out", cat)
        for key, value in fp["inputs"].items():
            inputs[f"run/runs/{suffix}/out/{key}"] = value
        if "provenance_sha256" in fp:
            stamp_name = "out/candidate_provenance.json" if (run / "out/candidate_provenance.json").is_file() else "candidate_provenance.json"
            inputs[f"run/runs/{suffix}/{stamp_name}"] = fp["provenance_sha256"]
        ref = used_references[card]
        status = grade["status"]
        gain = row.get("test_gain", (grade.get("numeric") or {}).get("gain"))
        gain = gain if number(gain) else None
        verified = row.get("verified_success", grade.get("verified_success")) is True
        if verified != (grade.get("verified_success") is True):
            raise ValueError(f"{card}: results and grade verification disagree")
        run_status = record["status"]
        incident = status == "incident" or run_status == "incident"
        excluded = incident or ref["null_card"]
        vrg = min(gain / ref["headroom"], 2.0) if not excluded and verified and gain is not None else 0.0
        if verified and gain is None:
            raise ValueError(f"{card}: verified run lacks gain")
        mrg = max(-0.5, min(gain / ref["headroom"], 2.0)) if not excluded and status == "valid" and gain is not None else 0.0
        err_run = None if incident else _run_error(row, grade, ref, gain, verified)
        claims = _claims(answer, grade)
        individuals.append(dict(
            card_id=card, card=card, arm=arm, replicate=rep, seed_index=rep,
            null_card=ref["null_card"], incident=incident, test_gain=gain, gain=gain,
            headroom=ref["headroom"], reference_se=ref["se_paired"],
            verified=verified, verified_success=verified, verified_rate=None if excluded else float(verified),
            vrg=None if excluded else vrg, mrg=None if excluded else mrg, err_run=err_run,
            error_ratio=None if excluded else (ref["err_ref"] + 0.01) / (err_run + 0.01),
            run_status=run_status, grade_status=status,
            false_positive_claim=row.get("false_positive_claim", grade.get("false_positive_claim")) is True,
            false_claims=int(row.get("false_positive_claim", grade.get("false_positive_claim")) is True),
            crash=run_status in {"crash", "crashed", "error", "failed"},
            tokens=_tokens(record.get("tokens", row.get("tokens"))),
            usd=record.get("billed_usd", row.get("billed_usd")),
            wall_s=record.get("wall_s", row.get("wall_s")),
            unused_wall_s=mechanism["unused_wall_s"],
            mechanism=mechanism, novelty=fp,
            novelty_win=(row["novelty_win"] and verified and not excluded
                         and fp["class"] in {"external", "recombination"}
                         if type(row.get("novelty_win")) is bool else None),
            dead_ends=int(observations["dead_end"]) if observations["dead_end"] is not None else None,
            null_honesty=None if not ref["null_card"] or claims is None else float(not claims),
            evidence_sha256={"grade.json": inputs[f"run/grading/{suffix}/grade.json"],
                             "run_record.json": inputs[f"run/runs/{suffix}/run_record.json"]},
            **observations,
            **{k: v for k, v in mechanism.items() if k != "unused_wall_s"},
            **{k: row[k] for k in ("tier", "mode", "look", "pool") if k in row},
        ))
    for individual in individuals:
        for key, value in tags.items():
            if value is not None:
                if key in individual and individual[key] != value:
                    raise ValueError(f"conflicting row {key} tag")
                individual[key] = value
    if envelopes and any(e != envelopes[0] for e in envelopes):
        raise ValueError("mixed resource envelopes")
    per_card = {}
    incomplete = False
    for card, ref in used_references.items():
        usable = [r for r in individuals if r["card_id"] == card and not r["incident"]]
        incomplete |= not usable
        per_card[card] = dict(null_card=ref["null_card"], n_runs=len(usable), **{
            metric: _mean(r[metric] for r in usable if r[metric] is not None) for metric in METRICS
        })
    primary = [c for c in per_card.values() if not c["null_card"] and c["n_runs"]]
    nulls = [r["null_honesty"] for r in individuals if r["null_card"] and not r["incident"]]
    eligible = [r for r in individuals if not r["null_card"] and not r["incident"]]
    aggregate = {k: _mean(c[k] for c in primary) for k in METRICS if k != "error_ratio"}
    aggregate.update(
        error_ratio=geomean(c["error_ratio"] for c in primary) if primary else None,
        capped_share=_mean(float(r["vrg"] == 2.0) for r in eligible),
        false_claims=sum(r["false_claims"] for r in individuals),
        dead_ends=sum(r["dead_end"] is True for r in individuals),
        crashes=sum(r["crash"] for r in individuals), incidents=sum(r["incident"] for r in individuals),
        tokens={"total": _sum_known(r["tokens"] for r in individuals), "median": _median(r["tokens"] for r in individuals)},
        usd=_sum_known(r["usd"] for r in individuals),
        live_runs=len(individuals), time_seconds=_sum_known(r["wall_s"] for r in individuals),
        heldout_identifier_matches=(sum(r["held_out_reads"] for r in individuals)
                                    if all(type(r["held_out_reads"]) is bool for r in individuals) else None),
        novelty_wins=(sum(r["novelty_win"] for r in individuals)
                      if all(type(r["novelty_win"]) is bool for r in individuals) else None),
        novelty_classes={name: sum(r["novelty"]["class"] == name for r in individuals)
                         for name in ("catalog", "recombination", "external", "baseline", "unknown")},
        wall_s={"median": _median(r["wall_s"] for r in individuals)},
        null_honesty=_mean(nulls) if nulls and all(v is not None for v in nulls) else None,
        n_cards=len(per_card), n_null_cards=sum(c["null_card"] for c in per_card.values()), n_runs=len(individuals),
    )
    result = dict(schema=SCHEMA, generated_utc=generated_utc or datetime.now(UTC).isoformat(),
                  purpose="development evidence", treatment=copy.deepcopy(treatment), provider=copy.deepcopy(provider), build=copy.deepcopy(build_record),
                  pool=pool, envelope=envelopes[0] if envelopes else None, replicates=replicates,
                  references_sha256=sha256(canonical(used_references)), inputs=dict(sorted(inputs.items())),
                  catalog={"commit": cat["commit"], "sha256": cat["sha256"]},
                  individual=individuals, per_card=per_card, aggregate=aggregate, incomplete=bool(incomplete))
    if autopilot is not None:
        if isinstance(autopilot, (str, Path)):
            path = Path(autopilot)
            result["inputs"]["autopilot.json"] = sha256(path.read_bytes())
            autopilot = json.loads(path.read_text())
        if any(result[k] != autopilot.get(k) for k in ("build", "pool", "references_sha256")) or set(per_card) != set(autopilot.get("per_card", {})):
            raise ValueError("autopilot must have the same build, pool and references/cards")
        deltas = []
        for card, live in per_card.items():
            auto = autopilot["per_card"][card]
            if not live["null_card"] and live["vrg"] is not None and auto.get("vrg") is None:
                raise ValueError(f"autopilot lacks a usable run for {card}")
            if live["vrg"] is not None and auto.get("vrg") is not None:
                live["uplift_autopilot"] = live["vrg"] - auto["vrg"]
                deltas.append(live["uplift_autopilot"])
        aggregate["uplift_autopilot"] = _mean(deltas)
    if pool["id"] != "D":
        # Candidate names may identify a sealed source. Hash the entire relative
        # path, retaining neither basenames nor directory components.
        for row in individuals:
            fp = row["novelty"]
            fp["inputs"] = {sha256(k.encode()): v for k, v in fp["inputs"].items()}
            for field in ("parse_errors", "changed_templates"):
                fp[field] = [sha256(k.encode()) for k in fp[field]]
        result["inputs"] = {("candidate/" + sha256(k.encode()) if "/out/" in k else k): v
                            for k, v in result["inputs"].items()}
    return result


def markdown(scorecard):
    lines = ["# Development scorecard", "", "Development evidence", "", "| Card | VRG | MRG | Verified |", "|---|---:|---:|---:|"]
    for card, row in scorecard["per_card"].items():
        values = ["—" if row[k] is None else f"{row[k]:.6g}" for k in ("vrg", "mrg", "verified_rate")]
        lines.append("| " + " | ".join([card, *values]) + " |")
    lines += ["", "Aggregate: `" + json.dumps(scorecard["aggregate"], sort_keys=True) + "`",
              "", f"Incomplete: {scorecard['incomplete']}", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--pool-root", type=Path)
    parser.add_argument("--references", type=Path)
    parser.add_argument("--autopilot", type=Path)
    parser.add_argument("--arm", type=int)
    parser.add_argument("--catalog", type=Path)
    for tag in ("tier", "pool-role", "look", "mode"):
        parser.add_argument("--" + tag)
    parser.add_argument("--out", type=Path, required=True)
    for field in ("treatment", "provider", "build-record"):
        parser.add_argument("--" + field, type=Path, help="JSON metadata; defaults to recorded run metadata")
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--generated-utc")
    args = parser.parse_args(argv)
    try:
        rows = json.loads((args.run_dir / "results.json").read_text())
        if args.arm is not None:
            rows = [r for r in rows if r.get("arm") == args.arm]
        if not rows:
            raise ValueError("selected arm has no runs")
        first = rows[0]
        card = first.get("card_id", first.get("task", first.get("card")))
        rep = first.get("replicate", first.get("run", first.get("seed_index")))
        run = args.run_dir / "runs" / f"{card}-r{rep}" / f"arm{first['arm']}"
        meta_path = run / "arm.json"
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        record = json.loads((run / "run_record.json").read_text())
        metadata = {}
        external = {}
        for key in ("treatment", "provider", "build_record"):
            path = getattr(args, key)
            if path:
                data = path.read_bytes()
                metadata[key] = json.loads(data)
                external[key + ".json"] = sha256(data)
            else:
                metadata[key] = meta.get(key)
        if metadata["build_record"] is None:
            metadata["build_record"] = dict(commit=record.get("eidos_commit_sha"),
                lab_receipt_hash=sha256(canonical(record["eidos_build"])) if record.get("eidos_build") else None)
        if metadata["treatment"] is None:
            raise ValueError("treatment/provider metadata is required in arm.json or explicit JSON files")
        replicates = args.replicates or len({r.get("replicate", r.get("run", r.get("seed_index"))) for r in rows})
        result = build(args.run_dir, args.pool_root, args.references, replicates=replicates,
                       generated_utc=args.generated_utc, autopilot=args.autopilot, arm=args.arm,
                       catalog_record=args.catalog, tier=args.tier, pool_role=args.pool_role,
                       look=args.look, mode=args.mode, **metadata)
        result["inputs"].update(external)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        args.out.with_suffix(".md").write_text(markdown(result))
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, OSError) and exc.filename:
            message = f"{exc.strerror}: {os.path.relpath(exc.filename, args.run_dir)}"
        else:
            message = str(exc).replace(str(args.run_dir.resolve()), ".")
        parser.exit(2, f"scorecard input error: {message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
