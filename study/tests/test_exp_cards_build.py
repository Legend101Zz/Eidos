"""Driver lifecycle, real stage-two tables and public/private artifact boundaries."""

import gzip
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from study.exp.cards import build, intake
from study.exp.cards.common import read_json, sha, subprocess_env, write_json
from study.exp.cards.select import check_table, select, table_fingerprint


def table(seed=1, minority=None):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(1600, 4)).astype(np.float32)
    y = np.arange(1600) % 2
    if minority:
        y[:] = 0
        y[:minority] = 1
    return dict(x=x, y=y.astype(str))


def test_real_table_rejections_and_fingerprint():
    sample = table()
    checked = check_table(sample, "classification")
    assert checked["split_sizes"] == dict(train=825, validation=275, test=500)
    reordered = dict(x=sample["x"][:, ::-1], y=sample["y"])
    assert table_fingerprint(**sample) == table_fingerprint(**reordered)
    with pytest.raises(ValueError, match="duplicate table"):
        check_table(reordered, "classification", fingerprints={checked["fingerprint"]})
    assert check_table(table(minority=48), "classification")["imbalanced"]
    with pytest.raises(ValueError, match="minority"):
        check_table(table(minority=48), "classification", imbalanced_count=4)
    with pytest.raises(ValueError, match="minority"):
        check_table(table(minority=20), "classification")
    sample["x"][:, :2] = 1
    with pytest.raises(ValueError, match="non-constant"):
        check_table(sample, "classification")
    sample = table()
    sample["x"][4, 0] = np.nan
    with pytest.raises(ValueError, match="missing"):
        check_table(sample, "classification")
    sample = table()
    sample["y"][0] = "?"
    with pytest.raises(ValueError, match="missing"):
        check_table(sample, "classification")
    sample = dict(x=np.ones((1600, 4)), y=np.zeros(1600))
    with pytest.raises(ValueError, match="600 train"):
        check_table(sample, "regression")


def cache_table(root, key, sample):
    raw = root / "raw/pmlb" / key
    raw.mkdir(parents=True)
    lines = ["a\tb\tc\td\ttarget"]
    lines += ["\t".join([*map(str, row), str(y)]) for row, y in zip(sample["x"], sample["y"], strict=True)]
    data = gzip.compress(("\n".join(lines) + "\n").encode())
    (raw / "data.tsv.gz").write_bytes(data)
    write_json(raw / "SOURCE.json", dict(source="pmlb", key=key, target="target", sha256={"data.tsv.gz": sha(data)}))


def catalog(tmp_path, count=12):
    cache = tmp_path / "private"
    cards = []
    for i in range(count):
        key = f"task-{i:03d}"
        cache_table(cache, key, table(i + 1))
        cards.append(dict(source="pmlb", key=key, family=key, n_rows=1600, n_features=4, row_band=0,
                          task_type="classification" if i % 2 == 0 else "regression"))
    path = tmp_path / "catalog.json"
    write_json(path, dict(seed=71, cards=cards))
    public = tmp_path / "cache"
    public.mkdir(parents=True)
    return path, public, tmp_path / "private"


# Use the actual preflight subprocess and a cheap fabricated packaging/reference result.
# Pool verification still checks the complete sealed layout and commitment.
FAKE_WORKER = '''
import os, sys, time
from pathlib import Path
from study.exp.cards.build import _worker
from study.exp.cards.common import read_json, write_json
from study.tests.test_exp_cards_pools import fake_card
os.nice(10)
action, request_path, response_path = sys.argv[1:]
r = read_json(request_path)
c = r['candidate']
if action == 'preflight':
    time.sleep(c.get('delay', 0))
    _worker(action, request_path, response_path)
else:
    time.sleep(c.get('build_delay', 0.03))
    card = fake_card(Path(r['root']), r['card_id'], not c.get('fail', False))
    write_json(card.parent.parent / 'controller' / r['card_id'] / 'provenance.json', c)
    manifest = read_json(card / 'manifest.json')
    manifest['task_type'] = c['task_type']
    manifest['metric'] = 'accuracy' if c['task_type'] == 'classification' else 'rmse'
    write_json(card / 'manifest.json', manifest)
    null = c.get('null_card', False)
    write_json(card.parent.parent / 'controller' / r['card_id'] / 'reference.json', dict(null_card=null))
    write_json(response_path, dict(ok=True, qualified=not c.get('fail', False), null_card=null))
'''


def fake_launch(action, request, directory):
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / f"{action}.request.json"
    response = directory / f"{action}.response.json"
    write_json(request_path, request)
    response.unlink(missing_ok=True)
    process = subprocess.Popen([sys.executable, "-c", FAKE_WORKER, action, str(request_path), str(response)],
                               env=subprocess_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               start_new_session=True)
    return process, response


def run(path, cache, private, **kwargs):
    return build.build(path, cache, kwargs.pop("want", {"D": 2, "E1": 2, "H": 2}),
                       private_root=private, workers=kwargs.pop("workers", 3),
                       allow_root=True, launch=kwargs.pop("launch", fake_launch), **kwargs)


def test_parallel_acceptance_rejections_resume_and_h_privacy(tmp_path, capsys, monkeypatch):
    path, cache, private = catalog(tmp_path)
    data = read_json(path)
    data["cards"][0]["delay"] = 0.3
    # A feature-table sibling with a DIFFERENT target is rejected before consuming an ID.
    sibling = table(1)
    sibling["y"] = (1 - sibling["y"].astype(int)).astype(str)
    cache_table(private, "duplicate", sibling)
    duplicate = dict(data["cards"][0], key="duplicate", family="duplicate", delay=0)
    data["cards"].insert(2, duplicate)
    data["cards"][3]["fail"] = True
    write_json(path, data)
    monkeypatch.setattr(build, "_launch", fake_launch)
    monkeypatch.setattr(sys, "argv", ["build", "--catalog", str(path), "--cache", str(cache),
                                    "--private-root", str(private), "--want", "D=2,E1=2,H=2", "--allow-root"])
    build.main()
    result = read_json(cache / "build-state.json")
    state = read_json(private / "build-state.json")
    assert len(state["outcomes"]["0"]["card_id"]) == 5
    assert state["outcomes"]["1"]["card_id"] != state["outcomes"]["0"]["card_id"]
    assert state["outcomes"]["2"]["reason"] == "same feature table"
    assert len(state["outcomes"]["3"]["card_id"]) == 5
    assert state["outcomes"]["3"]["status"] == "unqualified"
    assignments = read_json(private / "assignments.json")
    public = json.dumps(result) + capsys.readouterr().out + (cache / "build.log").read_text()
    assert result["qualified"] >= 2
    for card in assignments["pools"]["H"]:
        assert card["card_id"] not in public
        assert card["key"] not in public
        assert not (cache / "pools/H").exists()
    for card in assignments["reserve"]:
        assert card["card_id"] not in public
    # Every staged artifact, including logs with candidate names, is private.
    assert (private / "H/pool.json").exists()
    before = (private / "build-state.json").read_bytes()
    def unexpected(*args):
        pytest.fail("completed resume launched work")
    assert run(path, cache, private, resume=True, launch=unexpected) == result
    assert (private / "build-state.json").read_bytes() == before
    # Parallel execution is real: later preflights finished before the first one committed.
    jobs = private / "jobs"
    assert (jobs / "1/preflight.response.json").stat().st_mtime < (jobs / "0/preflight.response.json").stat().st_mtime


def test_shortfall_priority_and_null_caps(tmp_path):
    path, cache, private = catalog(tmp_path, 7)
    result = run(path, cache, private, want={"D": 2, "E1": 2, "E2": 2, "E3": 2, "H": 2})
    assert result["shortfall"] == dict(D=0, E1=0, H=0, E2=1, E3=2)
    assert len(read_json(private / "H/pool.json")["card_ids"]) == 2
    other = tmp_path / "other"
    other.mkdir()
    path, cache, private = catalog(other, 12)
    data = read_json(path)
    for c in data["cards"]:
        c["null_card"] = True
    write_json(path, data)
    result = run(path, cache, private, want={"D": 8, "H": 2})
    assert result["pools"]["D"] == 1
    assert len(read_json(private / "H/pool.json")["card_ids"]) == 2
    assert len(read_json(private / "assignments.json")["reserve"]) == 9


def test_smoke_balances_and_skips_finished_work(tmp_path):
    path, cache, private = catalog(tmp_path, 6)
    run(path, cache, private, want={"SMOKE": 2}, workers=2)
    assert Counter(c["task_type"] for c in read_json(cache / "manifests/SMOKE.json")["cards"]) == {"classification": 1, "regression": 1}
    smoke_state = read_json(private / "build-state.json")
    assert sum(r["status"] == "qualified" for r in smoke_state["outcomes"].values()) == 2
    # Mark one finished rejection in an interrupted state: it must be skipped.
    path2, cache2, private2 = catalog(tmp_path / "restart", 3)
    def timeout_launch(action, request, directory):
        request["candidate"]["delay"] = 5
        return fake_launch(action, request, directory)
    run(path2, cache2, private2, candidate_timeout=0.15, launch=timeout_launch, want={"D": 1})
    state = read_json(private2 / "build-state.json")
    assert all(r["reason"] == "candidate hard wall limit" for r in state["outcomes"].values())
    state["complete"] = False
    write_json(private2 / "build-state.json", state)
    def unexpected(*args):
        pytest.fail("resume retried a finished rejection")
    run(path2, cache2, private2, candidate_timeout=0.15, launch=unexpected, want={"D": 1}, resume=True)


def test_openml_only_restores_parity(tmp_path):
    path, cache, private = catalog(tmp_path, 4)
    data = read_json(path)
    # All PMLB candidates are regression; supplementary OpenML data must not run.
    for c in data["cards"]:
        c["task_type"] = "regression"
    data["cards"].append(dict(data["cards"][0], source="openml", key="987"))
    write_json(path, data)
    run(path, cache, private, want={"D": 6})
    assert read_json(private / "build-state.json")["outcomes"]["4"]["status"] == "skipped_balance"


def test_openml_suite_listing_local_http_and_vocab(tmp_path, monkeypatch):
    responses = {
        "/study/353": {"study": {"name": "OpenML-CTR23 curated", "tasks": {"task_id": [5, 6]}}},
        "/task/5": {"task": {"input": [{"data_set": {"data_set_id": 17, "target_feature": "outcome"}}]}},
        "/task/6": {"task": {"input": [{"data_set": {"data_set_id": 18, "target_feature": "outcome"}}]}},
    }
    for key, name in ((17, "validreg"), (18, "fried")):
        responses[f"/data/{key}"] = {"data_set_description": {"name": name, "licence": "custom terms"}}
        responses[f"/data/qualities/{key}"] = {"data_qualities": {"quality": [
            {"name": "NumberOfInstances", "value": "2000"}, {"name": "NumberOfFeatures", "value": "6"},
            {"name": "UnrelatedArray", "value": []}]}}
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(time.monotonic())
            assert self.headers["User-Agent"] == "eidos-card-intake"
            data = responses.get(self.path)
            self.send_response(200 if data is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(data if data is not None else {"error": "fixture absent"}).encode())

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(intake, "OPENML_API", base)
    monkeypatch.setattr(intake, "_check_url", lambda url: None)
    try:
        suite = intake.openml_suite(tmp_path, allow_root=True, pause=0.02)
        assert suite["network_requests"] == 7
        assert min(np.diff(requests)) >= 0.018
        assert intake.openml_suite(tmp_path, allow_root=True, pause=0)["network_requests"] == 0
        result = select(b"dataset\ttask\tn_instances\tn_features\tn_classes\n", openml_cards=suite["cards"])
        assert [c["name"] for c in result["cards"]] == ["validreg"]
        assert result["cards"][0]["target"] == "outcome"
        assert result["cards"][0]["licence_string"] == "custom terms"
        with pytest.raises(RuntimeError, match="limit"):
            intake.SuiteListing(tmp_path, True, max_requests=0).get("task/999")
        responses["/study/353"] = {"study": {"name": "unrelated suite"}}
        responses["/study/list/main_entity_type/task"] = {"studies": {"study": [{"id": 999, "name": "CTR23"}]}}
        responses["/study/999"] = {"study": {"name": "CTR23 fallback", "tasks": {"task_id": []}}}
        fallback = intake.openml_suite(tmp_path / "fallback", allow_root=True, pause=0)
        assert fallback["returned_name"] == "CTR23 fallback"
        assert "not CTR23" in fallback["errors"][0]
        monkeypatch.setattr(intake, "OPENML_API", base + "/missing")
        missing = intake.openml_suite(tmp_path / "missing", allow_root=True, pause=0)
        assert not missing["cards"]
        assert len(missing["errors"]) == 2
        assert all('HTTP 404: {"error": "fixture absent"}' in error for error in missing["errors"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    arff = b"@attribute x {10,2,5}\n@attribute z numeric\n@data\n2,1\n10,2\n"
    assert intake.parse_arff(arff, "z")["x"].ravel().tolist() == [1, 0]


def test_resume_partial_state_and_reject_changed_options(tmp_path):
    path, cache, private = catalog(tmp_path, 3)
    private.mkdir(exist_ok=True)
    (private / "assignment.seed").write_bytes(b"s" * 32)
    signature = dict(catalog_sha256=sha(path.read_bytes()), want={"D": 1}, budget_seconds=480, candidate_timeout=1500, max_classes=10)
    write_json(private / "build-state.json", dict(signature=signature, next_id=1, complete=False, elapsed_seconds=0,
               outcomes={"0": dict(status="rejected", reason="missing", candidate=read_json(path)["cards"][0])}))
    write_json(cache / "build-state.json", {"complete": False})
    run(path, cache, private, want={"D": 1}, resume=True)
    outcomes = read_json(private / "build-state.json")["outcomes"]
    assert outcomes["0"]["reason"] == "missing"
    assert not (private / "jobs/0").exists()
    assert len(outcomes["1"]["card_id"]) == 5
    with pytest.raises(ValueError, match="options do not match"):
        run(path, cache, private, want={"D": 1}, resume=True, budget_seconds=100)


def test_reserve_stopping_and_shortfall_public_seed_independence(tmp_path):
    path, cache, private = catalog(tmp_path, 20)
    run(path, cache, private, want={"D": 2})
    state = read_json(private / "build-state.json")
    assert sum(r["status"] == "qualified" for r in state["outcomes"].values()) == 20
    assert len(state["outcomes"]) == 20
    from study.exp.cards.pools import assign
    cards = [dict(c, card_id=f"R{i+1:03d}") for i, c in enumerate(read_json(path)["cards"][:7])]
    sizes = dict(D=2, E1=2, E2=2, E3=2, H=2)
    first = assign(cards, sizes, 71, tmp_path / "private-a/seed", allow_shortfall=True)
    second = assign(cards, sizes, 71, tmp_path / "private-b/seed", allow_shortfall=True)
    assert first != second
    assignments = read_json(tmp_path / "private-a/assignments.json")
    assert {p: len(v) for p, v in assignments["pools"].items()} == dict(D=2, E1=2, H=2, E2=1, E3=0)


def test_cli_errors_do_not_disclose_private_card_paths(tmp_path, monkeypatch, capsys):
    path, cache, private = catalog(tmp_path, 1)
    private.mkdir(exist_ok=True)
    def failure(*args, **kwargs):
        raise ValueError("missing private/H/seal/R901/source-secret")
    monkeypatch.setattr(build, "build", failure)
    monkeypatch.setattr(sys, "argv", ["build", "--catalog", str(path), "--cache", str(cache),
                                    "--private-root", str(private), "--want", "D=1", "--allow-root"])
    with pytest.raises(SystemExit) as error:
        build.main()
    assert error.value.code == 1
    public = capsys.readouterr()
    assert "R901" not in public.out + public.err
    assert "source-secret" not in public.out + public.err
    assert "source-secret" in (private / "driver-error.log").read_text()
