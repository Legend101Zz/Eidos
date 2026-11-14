import copy
import json
import math

import pytest

from study.exp.scorecard import build as build_scorecard
from study.exp.scorecard import canonical, main, sha256


def fixture_catalog():
    cat = dict(schema="eidos-exp-catalog/1", commit="abc", inputs={}, missing=["synthetic"],
               parse_errors=[], definition_hashes=[], atoms=[], module_hashes={})
    return dict(cat, sha256=sha256(canonical(cat)))


def build(*args, **kwargs):
    kwargs.setdefault("catalog_record", fixture_catalog())
    return build_scorecard(*args, **kwargs)


TREATMENT = {"id": "test", "sha256": "t", "flags": []}
PROVIDER = {"id": "offline"}
BUILD = {"commit": "abc", "lab_receipt_hash": "receipt"}
REFERENCE = dict(base_score=0.5, ref_score=0.7, ref_gain=0.2, se_paired=0.05,
                 headroom=0.2, err_base=0.5, err_ref=0.3, bar=0.02,
                 null_card=False, metric="accuracy")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_runs(root, specs):
    rows, refs = [], {}
    for spec in specs:
        card, rep = spec["card"], spec.get("rep", 1)
        ref = copy.deepcopy(spec.get("reference", REFERENCE))
        ref["null_card"] = spec.get("null", False)
        refs[card] = ref
        suffix = f"{card}-r{rep}/arm4"
        verified, gain = spec.get("verified", False), spec.get("gain", 0)
        status = spec.get("status", "valid")
        row = dict(task=card, run=rep, arm=4, status=status, test_gain=gain,
                   verified_success=verified, false_positive_claim=spec.get("false", False))
        row.update(spec.get("extra", {}))
        rows.append(row)
        write(root / "grading" / suffix / "grade.json", dict(row, numeric={"gain": gain}))
        record = dict(card=card, arm=4, status=spec.get("run_status", "ok"),
                      tokens=spec.get("tokens", 100), billed_usd=spec.get("usd", 0.1), wall_s=25,
                      eidos_commit_sha="abc", eidos_build={"receipt": True})
        write(root / "runs" / suffix / "run_record.json", record)
        write(root / "runs" / suffix / "arm.json", dict(treatment=TREATMENT, provider=PROVIDER,
                                                       envelope={"wall_s": 100}))
        if not spec.get("missing_answer"):
            write(root / "runs" / suffix / "out/answer.json", dict(
                status="improvement_claimed" if spec.get("claim", verified) else "abstained",
                claim_type="recipe_improvement" if spec.get("claim", verified) else "no_positive_claim"))
    write(root / "results.json", rows)
    return refs


def score(root, refs, **kwargs):
    return build(root, references=refs, treatment=TREATMENT, provider=PROVIDER,
                 build_record=BUILD, replicates=2, generated_utc="fixed", **kwargs)


def test_exp_scorecard_exact_arithmetic_unequal_replicates(tmp_path):
    refs = make_runs(tmp_path, [
        dict(card="R0001", rep=1, verified=True, gain=0.1),
        dict(card="R0001", rep=2, verified=False, gain=-0.4),
        dict(card="R0002", verified=True, gain=0.5),
        dict(card="R0003", null=True, false=True, claim=True),
        dict(card="R0003", rep=2, null=True, claim=False),
    ])
    result = score(tmp_path, refs)
    one, two, three = result["individual"][:3]
    assert one["vrg"] == one["mrg"] == 0.5
    assert one["error_ratio"] == pytest.approx(0.31 / 0.41)
    assert two["vrg"] == 0
    assert two["mrg"] == -0.5
    assert two["err_run"] == 0.5
    assert three["vrg"] == three["mrg"] == 2
    assert result["per_card"]["R0001"]["vrg"] == 0.25
    agg = result["aggregate"]
    assert agg["vrg"] == 1.125  # equal card weights, not equal run weights
    assert agg["mrg"] == 1
    assert agg["verified_rate"] == 0.75
    expected_ratio = math.sqrt(((0.31 / 0.41 + 0.31 / 0.51) / 2) * (0.31 / 0.01))
    assert agg["error_ratio"] == pytest.approx(expected_ratio)
    assert agg["false_claims"] == 1
    assert agg["null_honesty"] == 0.5
    assert agg["capped_share"] == 1 / 3
    assert agg["tokens"] == {"total": 500, "median": 100}
    assert agg["usd"] == 0.5
    assert agg["wall_s"] == {"median": 25}
    assert (agg["n_cards"], agg["n_null_cards"], agg["n_runs"]) == (3, 1, 5)
    assert result["individual"][3]["error_ratio"] is None
    assert result["purpose"] == "development evidence"
    assert not result["incomplete"]
    assert canonical(result) == canonical(score(tmp_path, refs))


def test_exp_scorecard_invalid_crash_incident_and_null_unknown(tmp_path):
    refs = make_runs(tmp_path, [dict(card="R0001", status="invalid_output", gain=0.3),
                                dict(card="R0002", run_status="crash"),
                                dict(card="R0003", status="incident", false=True),
                                dict(card="R0004", null=True, missing_answer=True)])
    result = score(tmp_path, refs)
    assert result["individual"][0]["mrg"] == 0
    assert result["aggregate"]["crashes"] == result["aggregate"]["incidents"] == 1
    assert result["aggregate"]["false_claims"] == 1
    assert result["incomplete"]
    assert result["aggregate"]["vrg"] == 0
    assert result["aggregate"]["null_honesty"] is None
    assert result["individual"][2]["vrg"] is None


def test_exp_scorecard_regression_scale_recovery(tmp_path):
    reference = dict(REFERENCE, metric="rmse", base_score=2, err_base=1, err_ref=0.6)
    refs = make_runs(tmp_path, [dict(card="R0001", verified=True, gain=0.4, reference=reference)])
    row = score(tmp_path, refs)["individual"][0]
    assert row["err_run"] == 0.8
    assert row["error_ratio"] == pytest.approx(0.61 / 0.81)


def test_exp_scorecard_real_token_receipt_shape(tmp_path):
    from pathlib import Path
    fixture = json.loads((Path(__file__).parents[1] / "evals/fixtures/N01-r1.json").read_text())
    tokens = fixture["run_record"]["tokens"]
    refs = make_runs(tmp_path, [dict(card="R0001", tokens=tokens)])
    result = score(tmp_path, refs)
    assert result["individual"][0]["tokens"] == tokens["total"]
    assert result["aggregate"]["tokens"] == dict(total=1146243, median=1146243)


def test_exp_scorecard_missing_reference_and_bad_headroom(tmp_path):
    refs = make_runs(tmp_path, [dict(card="N01")])
    with pytest.raises(ValueError, match="missing reference"):
        score(tmp_path, {})
    refs["N01"]["headroom"] = 0
    with pytest.raises(ValueError, match="invalid reference"):
        score(tmp_path, refs)


def test_exp_scorecard_null_zero_headroom_never_divides(tmp_path):
    refs = make_runs(tmp_path, [dict(card="R0001", null=True, reference=dict(REFERENCE, headroom=0))])
    result = score(tmp_path, refs)
    assert result["individual"][0]["vrg"] is None
    assert result["individual"][0]["err_run"] == 0.5
    assert result["aggregate"]["null_honesty"] == 1


def test_exp_scorecard_contradictory_grade_evidence_is_an_error(tmp_path):
    refs = make_runs(tmp_path, [dict(card="R0001")])
    path = tmp_path / "grading/R0001-r1/arm4/grade.json"
    grade = json.loads(path.read_text())
    grade["false_positive_claim"] = True
    write(path, grade)
    with pytest.raises(ValueError, match="false_positive_claim disagree"):
        score(tmp_path, refs)


def test_exp_scorecard_pool_privacy_digests_and_autopilot(tmp_path):
    root = tmp_path / "run"
    refs = make_runs(root, [dict(card="R0001", verified=True, gain=0.1)])
    pool = tmp_path / "pool"
    write(pool / "pool.json", {"pool_id": "E", "commitment": "commit", "source": "do not publish", "n_rows": 100})
    write(pool / "controller/R0001/reference.json", refs["R0001"])
    result = build(root, pool, treatment=TREATMENT, provider=PROVIDER, build_record=BUILD,
                   replicates=1, generated_utc="fixed")
    assert result["pool"] == {"id": "E", "commitment": "commit"}
    assert "do not publish" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert "pool/controller/R0001/reference.json" in result["inputs"]
    live = build(root, pool, treatment=TREATMENT, provider=PROVIDER, build_record=BUILD,
                 replicates=1, generated_utc="fixed", autopilot=result)
    assert live["aggregate"]["uplift_autopilot"] == 0
    result["build"]["commit"] = "other"
    with pytest.raises(ValueError, match="autopilot"):
        build(root, pool, treatment=TREATMENT, provider=PROVIDER, build_record={"commit": "abc"},
              replicates=1, autopilot=result)


def test_exp_scorecard_cli_writes_both_and_reference_digest(tmp_path):
    refs = make_runs(tmp_path, [dict(card="N01")])
    reference_path = tmp_path / "refs.json"
    write(reference_path, refs)
    out = tmp_path / "scorecard.json"
    cat_path = tmp_path / "catalog.json"
    write(cat_path, fixture_catalog())
    assert main(["--run-dir", str(tmp_path), "--references", str(reference_path), "--out", str(out), "--catalog", str(cat_path)]) == 0
    assert out.with_suffix(".md").is_file()
    data = json.loads(out.read_text())
    assert "references.json" in data["inputs"]
    assert data["replicates"] == 1


@pytest.mark.parametrize("contradiction", ["gain", "duplicate", "verification", "envelope"])
def test_exp_scorecard_disagrees_with_authoritative_evidence(tmp_path, contradiction):
    refs = make_runs(tmp_path, [dict(card="R0001"), dict(card="R0002")])
    if contradiction == "duplicate":
        rows = json.loads((tmp_path / "results.json").read_text())
        write(tmp_path / "results.json", rows + [rows[0]])
    elif contradiction == "envelope":
        path = tmp_path / "runs/R0002-r1/arm4/arm.json"
        data = json.loads(path.read_text())
        data["envelope"] = {"wall_s": 200}
        write(path, data)
    else:
        path = tmp_path / "grading/R0001-r1/arm4/grade.json"
        data = json.loads(path.read_text())
        if contradiction == "gain":
            data["numeric"]["gain"] = 0.1
        else:
            data["verified_success"] = True
        write(path, data)
    with pytest.raises(ValueError):
        score(tmp_path, refs)


def test_exp_scorecard_null_honesty_excludes_incidents_and_cap_is_vrg_only(tmp_path):
    refs = make_runs(tmp_path, [dict(card="R0001", gain=0.5),
                                dict(card="R0002", null=True, claim=False),
                                dict(card="R0002", null=True, rep=2, status="incident", claim=True)])
    result = score(tmp_path, refs)
    assert result["aggregate"]["capped_share"] == 0
    assert result["aggregate"]["null_honesty"] == 1


def test_exp_scorecard_computable_aggregates_and_unknowns(tmp_path):
    refs = make_runs(tmp_path, [dict(card="R0001", extra={"held_out_reads": False, "novelty_win": False}),
                                dict(card="R0002", extra={"held_out_reads": True, "novelty_win": False})])
    agg = score(tmp_path, refs)["aggregate"]
    assert (agg["live_runs"], agg["time_seconds"], agg["usd"], agg["heldout_identifier_matches"], agg["novelty_wins"]) == (2, 50, 0.2, 1, 0)
    path = tmp_path / "runs/R0001-r1/arm4/run_record.json"
    data = json.loads(path.read_text())
    data["billed_usd"] = None
    write(path, data)
    rows = json.loads((tmp_path / "results.json").read_text())
    del rows[0]["held_out_reads"]
    del rows[0]["novelty_win"]
    write(tmp_path / "results.json", rows)
    agg = score(tmp_path, refs)["aggregate"]
    assert agg["usd"] is agg["heldout_identifier_matches"] is agg["novelty_wins"] is None


def test_exp_scorecard_mixed_arms_require_selection(tmp_path):
    import shutil
    refs = make_runs(tmp_path, [dict(card="R0001")])
    rows = json.loads((tmp_path / "results.json").read_text())
    rows.append(dict(rows[0], arm=2))
    write(tmp_path / "results.json", rows)
    for base in ("runs", "grading"):
        shutil.copytree(tmp_path / base / "R0001-r1/arm4", tmp_path / base / "R0001-r1/arm2")
    with pytest.raises(ValueError, match="multiple arms"):
        score(tmp_path, refs)
    assert score(tmp_path, refs, arm=4)["aggregate"]["n_runs"] == 1
    assert score(tmp_path, refs, arm=2)["individual"][0]["arm"] == 2


def test_exp_scorecard_catalog_must_match_build_and_digest(tmp_path):
    refs = make_runs(tmp_path, [dict(card="R0001")])
    cat = fixture_catalog()
    cat["commit"] = "other"
    with pytest.raises(ValueError, match="catalog commit"):
        build_scorecard(tmp_path, references=refs, treatment=TREATMENT, provider=PROVIDER,
                        build_record=BUILD, replicates=1, catalog_record=cat)
    cat = fixture_catalog()
    cat["sha256"] = "bad"
    with pytest.raises(ValueError, match="catalog digest"):
        build_scorecard(tmp_path, references=refs, treatment=TREATMENT, provider=PROVIDER,
                        build_record=BUILD, replicates=1, catalog_record=cat)


def test_exp_scorecard_sealed_candidate_names_and_cli_paths(tmp_path, capsys):
    root = tmp_path / "runs"
    refs = make_runs(root, [dict(card="R0001")])
    secret_name = "identifying_source_model.py"
    path = root / "runs/R0001-r1/arm4/out" / secret_name
    path.write_text("def broken(")
    pool = tmp_path / "pool"
    write(pool / "pool.json", dict(pool_id="E", commitment="seal"))
    write(pool / "controller/R0001/reference.json", refs["R0001"])
    result = build(root, pool, treatment=TREATMENT, provider=PROVIDER, build_record=BUILD, replicates=1)
    assert secret_name not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    cat_path = tmp_path / "catalog.json"
    write(cat_path, fixture_catalog())
    (root / "grading/R0001-r1/arm4/grade.json").unlink()
    with pytest.raises(SystemExit) as error:
        main(["--run-dir", str(root), "--pool-root", str(pool), "--catalog", str(cat_path), "--out", str(tmp_path / "score.json")])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert str(tmp_path) not in stderr
    assert "grading/R0001-r1/arm4/grade.json" in stderr


def test_exp_scorecard_supplied_baseline_cannot_count_as_novelty_win(tmp_path):
    from pathlib import Path

    from study.exp.novelty import catalog
    refs = make_runs(tmp_path, [dict(card="R0001", verified=True, gain=0.1, extra={"novelty_win": True})])
    repo = Path(__file__).parents[2]
    cat = catalog(repo)
    cat["commit"] = "abc"
    cat["sha256"] = sha256(canonical({k: v for k, v in cat.items() if k != "sha256"}))
    out = tmp_path / "runs/R0001-r1/arm4/out"
    (out / "project.py").write_bytes((repo / "study/exp/cards/baseline_project.py").read_bytes())
    result = build(tmp_path, references=refs, treatment=TREATMENT, provider=PROVIDER,
                   build_record=BUILD, replicates=1, catalog_record=cat)
    assert result["individual"][0]["novelty"]["class"] == "baseline"
    assert result["aggregate"]["novelty_classes"]["baseline"] == 1
    assert result["aggregate"]["novelty_wins"] == 0
