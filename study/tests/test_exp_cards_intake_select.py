"""No external network: all downloads are served by a local fabricated fixture."""

import csv
import gzip
import io
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from study.exp.cards import intake
from study.exp.cards.select import EXCLUDED, family, select


@pytest.fixture
def source_server(monkeypatch):
    tsv = gzip.compress(b"first\tsecond\ttarget\n1\t2\tcat\n3\t4\tdog\n")
    arff = b"@relation demo\n@attribute a numeric\n@attribute b {red,blue}\n@attribute outcome {yes,no}\n@data\n1,red,yes\n2,blue,no\n"
    responses = {"/datasets/demo/demo.tsv.gz": tsv,
                 "/datasets/demo/metadata.yaml": b"license: CC0\n",
                 "/download/1.arff": arff,
                 "/summary.tsv": b"dataset\ttask\tn_instances\tn_features\tn_classes\tminority_class_size\n"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["User-Agent"] == "eidos-card-intake"
            assert "Authorization" not in self.headers
            data = responses[self.path]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    responses["/api/1"] = json.dumps({"data_set_description": {
        "default_target_attribute": "outcome", "url": base + "/download/1.arff", "licence": "CC0"}}).encode()
    monkeypatch.setattr(intake, "PMLB_BASE", base + "/datasets")
    monkeypatch.setattr(intake, "OPENML_BASE", base + "/api")
    monkeypatch.setattr(intake, "SUMMARY_URL", base + "/summary.tsv")
    # Explicit test-only injection. Production URL restrictions remain HTTPS and host allowlisted.
    monkeypatch.setattr(intake, "_check_url", lambda url: None if url.startswith(base + "/") else (_ for _ in ()).throw(ValueError()))
    yield responses
    server.shutdown()
    server.server_close()
    thread.join()


def test_intake_both_sources_and_cache_refusals(tmp_path, source_server):
    for source, key in (("pmlb", "demo"), ("openml", "1")):
        directory = intake.intake(source, key, tmp_path, allow_root=True)
        table = intake.load_table(directory)
        assert table["x"].shape == (2, 2)
        metadata = json.loads((directory / "SOURCE.json").read_text())
        assert metadata["n_rows"] == 2 and metadata["licence_string"] == "CC0"
        assert metadata["sha256"] and metadata["retrieved_utc"]
        with pytest.raises(ValueError, match="empty"):
            intake.intake(source, key, tmp_path, allow_root=True)
    first = intake.summary(tmp_path, allow_root=True)
    assert first == intake.summary(tmp_path, allow_root=True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").write_text("gitdir: ignored")
    with pytest.raises(ValueError, match="git work tree"):
        intake.intake("pmlb", "demo", root / "cache", allow_root=True)
    with pytest.raises(ValueError, match="cache"):
        intake.intake("pmlb", "demo", tmp_path)


def test_reject_unsupported_arff_and_network_hosts():
    for dtype in ("string", "date", "relational"):
        with pytest.raises(ValueError, match="unsupported"):
            intake.parse_arff(f"@relation x\n@attribute x {dtype}\n@data\na".encode(), "x")
    with pytest.raises(ValueError, match="sparse"):
        intake.parse_arff(b"@attribute target numeric\n@data\n{0 1}", "target")
    for url in ("http://github.com/x", "https://evil.example/x", "https://token@www.openml.org/x",
                "https://www.openml.org.evil.example/x"):
        with pytest.raises(ValueError, match="allowed"):
            intake._check_url(url)
    table = intake.parse_arff(b"@attribute 'a b' numeric\n@attribute target {'yes, ok',no}\n@data\n1,'yes, ok'\n2,no", "target")
    np.testing.assert_array_equal(table["y"], ["yes, ok", "no"])


def fabricated_summary():
    rows = []
    for kind in ("classification", "regression"):
        for band, n in enumerate((2000, 8000, 30000)):
            for i in range(16):
                # Alphabetic names prevent accidental digit-stripping into one family.
                name = f"task{chr(97+i)}{chr(97+band)}{kind[0]}"
                rows.append([name, kind, n, 5, 2 if kind == "classification" else 0, n // 4])
    for excluded in EXCLUDED:
        rows.append([f"{excluded}_example", "classification", 2000, 5, 2, 800])
    for i in range(8):
        rows.append([f"GAMETES_{i}", "classification", 2000, 5, 2, 800])
        rows.append([f"feynman_{i}", "regression", 2000, 5, 0, 0])
        rows.append([f"strogatz_{i}", "regression", 2000, 5, 0, 0])
        rows.append([f"imbalance{chr(97+i)}", "classification", 2000, 5, 2, 60])
    rows.extend([["tiny", "regression", 100, 5, 0, 0], ["too_many_classes", "classification", 2000, 5, 11, 400]])
    output = io.StringIO()
    writer = csv.writer(output, delimiter="\t")
    writer.writerow(["dataset", "task", "n_instances", "n_features", "n_classes", "minority_class_size"])
    writer.writerows(rows)
    return output.getvalue().encode()


def test_selection_exclusions_family_caps_balance_and_determinism():
    data = fabricated_summary()
    catalog = select(data, 77, 60)
    assert catalog == select(data, 77, 60)
    assert catalog != select(data, 78, 60)
    cards = catalog["cards"]
    assert len(cards) == 60
    assert Counter(c["task_type"] for c in cards) == {"classification": 30, "regression": 30}
    assert set(c["row_band"] for c in cards) == {0, 1, 2}
    assert max(Counter(c["family"] for c in cards).values()) <= 2
    assert sum(c["imbalanced"] for c in cards) <= 4
    assert sum(c["family"] in {"feynman", "strogatz"} for c in cards) <= 4
    assert not any(s in c["key"].lower() for c in cards for s in EXCLUDED)
    assert len(catalog["rules"]["excluded_name_substrings"]) == len(EXCLUDED)
    assert family("GAMETES_20_1_0") == "gametes"
    assert family("mfeat_fourier") == family("mfeat_factors")


def test_missing_minority_statistics_and_infeasible_roster_defer_to_stage_two():
    summary = b"dataset\ttask\tn_instances\tn_features\tn_classes\timbalance\npublictask\tclassification\t2000\t4\t2\t0.1\n"
    result = select(summary, 77, 60)
    assert result["eligible_per_task"] == {"classification": 1}
    assert len(result["cards"]) == 1
    assert result["cards"][0]["minority_fraction"] is None
    assert "card_id" not in result["cards"][0]
    assert result["cards"] == select(summary, 77)["cards"]
