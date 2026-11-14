"""Second independent review: live writes, public attacker and failure recovery."""

import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from study.exp.cards import build, intake, pools
from study.exp.cards.common import read_json, subprocess_env, write_json
from study.exp.cards.package import prepare_arrays
from study.exp.cards.qualify import split_check
from study.exp.cards.select import check_table, same_feature_family
from study.tests.test_exp_cards_build import cache_table, catalog, run, table
from study.tests.test_exp_cards_pools import fake_card


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("fabricated build attempted network intake")
    monkeypatch.setattr(intake, "intake", refuse)


def counts_only(value):
    assert set(value) == {"processed", "accepted", "qualified", "rejected_by_reason", "pools", "shortfall",
                          "h_candidate_margin"}
    for item in value.values():
        assert type(item) is int or (type(item) is dict and
                                    all(type(k) is str and type(v) is int for k, v in item.items()))


def world_launch(action, request, directory):
    """Probe H's real stage 2 and fake packaging; no subprocess or download fallback."""
    directory.mkdir(parents=True, exist_ok=True)
    response = directory / f"{action}.response.json"
    c = request["candidate"]
    if action == "preflight":
        source = Path(request["cache"]) / "raw" / c["source"] / c["key"]
        # Pin the raw boundary before opening anything, including under mutation P7.
        assert source.parent.parent.parent.name == "private"
        try:
            result = check_table(intake.load_table(source), c["task_type"], seed=request["seed"],
                                 max_classes=request["max_classes"])
            result.update(ok=True, source_dir=str(source))
        except ValueError as error:
            result = dict(ok=False, reason=build._safe_reason(error))
    else:
        card = fake_card(Path(request["root"]), request["card_id"], not c.get("fail", False))
        controller = card.parent.parent / "controller" / request["card_id"]
        write_json(controller / "provenance.json", dict(c, seed=request["seed"],
                   source_info=dict(retrieved_utc="2026-11-14T12:34:56Z")))
        manifest = read_json(card / "manifest.json")
        manifest.update(task_type=c["task_type"], metric="accuracy" if c["task_type"] == "classification" else "rmse")
        write_json(card / "manifest.json", manifest)
        result = dict(ok=True, qualified=not c.get("fail", False), null_card=False)
    write_json(response, result)
    return SimpleNamespace(poll=lambda: 0, pid=0), response


def test_public_counts_completion_and_probe_h(tmp_path, monkeypatch, capsys):
    path, public, private = catalog(tmp_path, 62)
    salt = b"\x05" * 32
    (private / "assignment.seed").write_bytes(salt)
    data = read_json(path)
    # Public stage-2 rejects and hidden qualification failures, as in reviewer probe H.
    cache_table(private, "minority", table(100, minority=20))
    data["cards"].insert(5, dict(data["cards"][0], key="minority", family="minority"))
    data["cards"][3]["fail"] = True
    data["cards"][25]["fail"] = True
    write_json(path, data)
    events = []

    def slow_launch(action, request, directory):
        # Observe between every pair of candidate events, including after private save().
        events.append(action)
        assert not (public / "build-state.json").exists(), "public progress exposed before completion"
        time.sleep(0.002)
        return world_launch(action, request, directory)

    monkeypatch.setattr(build, "_launch", slow_launch)
    # In-process probe workers have no OS process group, including on failed assertions.
    monkeypatch.setattr(build, "_stop", lambda process: None)
    monkeypatch.setattr(sys, "argv", ["build", "--catalog", str(path), "--cache", str(public),
                                    "--private-root", str(private), "--want", "D=6,E1=6,E2=6,H=8", "--allow-root"])
    verify = pools.verify
    def private_verify(root, *args, **kwargs):
        root = Path(root)
        if root.parent == public / "pools":
            assert root.name == "D", "E/H verification or assembly used the public cache"
        return verify(root, *args, **kwargs)
    monkeypatch.setattr(pools, "verify", private_verify)
    build.main()
    result = read_json(public / "build-state.json")
    assert build.build(path, public, dict(D=6, E1=6, E2=6, H=8), private_root=private,
                       allow_root=True, resume=True) == result
    counts_only(result)
    counts_only(json.loads(capsys.readouterr().out))
    counts_only(json.loads((public / "build.log").read_text()))
    assert len(events) > 100
    assert result["h_candidate_margin"] == result["accepted"] - result["pools"]["D"]
    records = read_json(private / "build-state.json")["outcomes"]
    ids = [int(r["card_id"][1:]) for r in (records[i] for i in sorted(records, key=int)) if "card_id" in r]
    assert len(ids) >= 60
    assert ids != sorted(ids), "card identifiers reveal acceptance order"
    assert sum(i < len(ids) for i in ids) < len(ids) // 10, "identifiers concentrated in accepted prefix"
    for prov in (public / "pools/D/controller").glob("*/provenance.json"):
        info = read_json(prov)
        expected = int.from_bytes(hmac.digest(salt, f"{info['source']}:{info['key']}".encode(), "sha256")[:8], "big")
        assert info["seed"] == expected
        assert info["seed"] not in {data["seed"], int(hashlib.sha256(info["key"].encode()).hexdigest()[:16], 16)}
        assert info["source_info"]["retrieved_utc"] == "2026-11-14"
    mtimes = {p.stat().st_mtime_ns for p in (public / "pools/D").rglob("*") if p.is_file()}
    assert len(mtimes) == 1, "D exposes staging/build timestamps"
    assert {p.name for p in (public / "pools").iterdir()} == {"D"}
    assert {p.name for p in (public / "manifests").iterdir()} == {"D.json", "E1.json", "E2.json"}
    # Attacker uses public tables (our fabricated originals), stage 2 and E manifests only.
    accepted, signatures = [], []
    for c in data["cards"]:
        try:
            r = check_table(intake.load_table(private / "raw/pmlb" / c["key"]), c["task_type"], seed=1)
        except ValueError:
            continue
        if any(same_feature_family(r["feature_signature"], s) for s in signatures):
            continue
        accepted.append(c["key"])
        signatures.append(r["feature_signature"])
    for e in ("E1", "E2"):
        manifest = read_json(public / f"manifests/{e}.json")
        assert set(manifest) == {"pool", "commitment", "cards"}
        assert all(set(c) == {"id", "task_type", "metric", "bar_rule"} for c in manifest["cards"])
    public_ids = [int(c["id"][1:]) for e in ("E1", "E2")
                  for c in read_json(public / f"manifests/{e}.json")["cards"]]
    guessed = {accepted[i] for i in public_ids if i < len(accepted)}
    assignment = read_json(private / "assignments.json")
    actual = {c["key"] for e in ("E1", "E2") for c in assignment["pools"][e]}
    assert len(guessed & actual) <= len(actual) ** 2 / len(accepted), "public replay recovers E above chance"


def test_regression_float32_duplicate_agreement():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(2400, 4)).astype(np.float32)
    y = rng.normal(size=2400) * 5 + 100
    indices = rng.choice(2400, 120, replace=False)
    x = np.vstack([x, x[indices]])
    y = np.concatenate([y, y[indices] * (1 + 1e-9)])
    arrays, provenance = prepare_arrays(x, y, "regression", 123)
    checked = split_check({s: (v["x"], v["y"], v["ids"]) for s, v in arrays.items()})
    assert checked["passed"], checked
    assert provenance["duplicate_rows_dropped"] > 0
    # Training statistics are still fitted on retained TRAIN rows only.
    rows = provenance["split_source_rows"]["train"]
    assert provenance["target_scaling"]["mean"] == float(y[rows].mean())
    assert provenance["target_scaling"]["std"] == float(y[rows].std())


def test_driver_max_classes(tmp_path):
    path, public, private = catalog(tmp_path, 1)
    sample = table()
    sample["y"] = (np.arange(1600) % 3).astype(str)
    import shutil
    shutil.rmtree(private / "raw/pmlb/task-000")
    cache_table(private, "task-000", sample)
    result = run(path, public, private, want={"D": 1}, max_classes=2)
    assert result["accepted"] == 0
    assert result["rejected_by_reason"] == {"classification requires": 1}
    record = read_json(private / "build-state.json")["outcomes"]["0"]
    assert record["status"] == "rejected" and "card_id" not in record


def test_worker_nice_failure_is_harmless(tmp_path, monkeypatch):
    path, _, private = catalog(tmp_path, 1)
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    write_json(request, dict(candidate=read_json(path)["cards"][0], cache=str(private), seed=1,
                             max_classes=2, allow_root=True))
    def denied(*args):
        raise PermissionError("already at maximum niceness")
    monkeypatch.setattr(os, "nice", denied)
    try:
        build._worker("preflight", request, response)
    except OSError:
        result = {}
    else:
        result = read_json(response)
    assert result.get("ok"), "worker aborted because niceness could not be increased"


@pytest.mark.parametrize("kill", [False, True])
def test_real_worker_contains_temporary_files(tmp_path, monkeypatch, kill):
    path, _, private = catalog(tmp_path, 1)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    monkeypatch.setenv("TMPDIR", str(sentinel))
    request = dict(candidate=read_json(path)["cards"][0], cache=str(private), root=str(private / "staging"),
                   source_dir=str(private / "raw/pmlb/task-000"), seed=1, card_id="R9001", budget_seconds=20,
                   max_classes=2, allow_root=True)
    process, response = build._launch("build", request, private / "jobs/0")
    assert "--request" not in process.args and "--response" not in process.args
    assert all(str(private / "jobs") not in arg for arg in process.args)
    observed_private_temp = False
    observed_external = False
    deadline = time.monotonic() + 90
    try:
        while process.poll() is None and time.monotonic() < deadline:
            observed_external |= bool(list(sentinel.iterdir()))
            observed_private_temp |= any(p.name.startswith("cards-baseline-") for p in (private / "tmp").rglob("*"))
            if kill and (observed_private_temp or observed_external):
                break
            time.sleep(0.01)
        if kill:
            build._stop(process)
        else:
            process.wait(timeout=5)
            assert read_json(response)["ok"], read_json(response)
        assert not observed_external and not list(sentinel.iterdir()), "worker wrote outside private root"
        assert observed_private_temp, "did not observe real baseline temp staging"
        assert not list((private / "tmp").iterdir()), "worker temp remains after stop/completion"
    finally:
        if process.poll() is None:
            build._stop(process)


def test_real_driver_contains_temp_and_wipes_on_resume(tmp_path, monkeypatch):
    path, public, private = catalog(tmp_path, 1)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    env = dict(os.environ, TMPDIR=str(sentinel))
    command = [sys.executable, "-m", "study.exp.cards.build", "--catalog", str(path), "--cache", str(public),
               "--private-root", str(private), "--want", "D=1", "--budget-seconds", "1", "--allow-root"]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    leaked = False
    try:
        while process.poll() is None:
            leaked |= bool(list(sentinel.iterdir()))
            time.sleep(0.01)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        counts_only(json.loads(stdout))
        assert not leaked and not list(sentinel.iterdir())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    state = read_json(private / "build-state.json")
    assert state["worker_pids"] and all(pid > 0 for pid in state["worker_pids"])
    stale = private / "tmp/stale"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "sensitive").write_text("previous interrupted run")
    run(path, public, private, want={"D": 1}, budget_seconds=1, resume=True)
    assert not list((private / "tmp").iterdir())
    assert (private / "tmp").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("journal", [False, True])
def test_resume_refuses_live_previous_worker_before_wiping(tmp_path, journal):
    path, public, private = catalog(tmp_path, 1)
    run(path, public, private, want={"D": 1}, launch=world_launch)
    previous = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True, env=subprocess_env())
    try:
        state = read_json(private / "build-state.json")
        state["worker_pids"] = [] if journal else [previous.pid]
        if journal:
            marker = private / "tmp/previous/worker.pid"
            marker.parent.mkdir(parents=True)
            marker.write_text(str(previous.pid))
        write_json(private / "build-state.json", state)
        stale = private / "tmp/still-in-use"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("live worker")
        with pytest.raises(ValueError, match="previous worker.*alive"):
            run(path, public, private, want={"D": 1}, launch=world_launch, resume=True)
        assert stale.exists()
    finally:
        os.killpg(previous.pid, signal.SIGTERM)
        previous.wait()
    run(path, public, private, want={"D": 1}, launch=world_launch, resume=True)
    assert not stale.exists()


def test_h_draw_covers_all_candidates_across_private_seeds(tmp_path):
    """H-first must leave every qualified slot eligible, even slots D/E would pick."""
    cards = [dict(card_id=f"R{i:04d}", key=f"source-{i}", family=f"family-{i}",
                  task_type="classification", n_rows=2000) for i in range(20)]
    seen = set()
    for seed in range(80):
        private = tmp_path / str(seed)
        private.mkdir()
        (private / "seed").write_bytes(seed.to_bytes(32, "big"))
        pools.assign(cards, dict(D=4, E1=4, H=4), 71, private / "seed")
        assignment = read_json(private / "assignments.json")
        seen.update(c["card_id"] for c in assignment["pools"]["H"])
    assert seen == {c["card_id"] for c in cards}, "public D/E draw permanently excludes qualified H candidates"
