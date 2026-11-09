import json
import subprocess
import sys
from pathlib import Path

import pytest

from eidos_wire import OutputManifest, sha256_file
from eidos_worker import ArtifactReader, ArtifactWriter, ContractError, check_finite, classify
from eidos_worker.testing import make_invocation, read_output_json, run_in_process, run_subprocess
from eidos_worker.validate import validate_component

SRC = Path(__file__).parent / "sample_component"
CSV = b"g,pred_a,label_a,pred_b,label_b\nx,1,1,0,0\nx,1,0,0,0\ny,1,1,1,1\n"


def _inv(tmp_path, entry, **kw):
    kw.setdefault("source_dir", SRC)
    return make_invocation(tmp_path, entry, **kw)


def test_subprocess_protocol_success(tmp_path):
    inv = _inv(
        tmp_path, "pair_tools:run", inputs={"predictions": (CSV, "text/csv")}, config={"group_by": "g"}
    )
    proc = run_subprocess(inv)
    assert proc.returncode == 0, proc.stderr
    man = OutputManifest.read(inv.staging_dir)
    assert man.status == "succeeded" and man.receipt["input_rows"] == 3
    assert read_output_json(inv, man, "report") == {"x": [2, 1], "y": [1, 1]}
    entry = next(e for e in man.outputs if e.slot == "report")
    assert entry.schema == "grouped-pair-counts/1"
    assert sha256_file(Path(inv.staging_dir) / entry.filename) == (entry.digest, entry.size)
    log = next(e for e in man.outputs if e.role == "log")
    assert b"scored 3 rows" in (Path(inv.staging_dir) / log.filename).read_bytes()


def test_injection_only_accepted_kwargs(tmp_path):
    inv = _inv(tmp_path, "pair_tools:only_context", seed=7)
    man = run_in_process(inv)
    assert man.status == "succeeded" and man.receipt["seed"] == 7 and man.receipt["returned"] is True


@pytest.mark.parametrize(
    "entry,category",
    [
        ("pair_tools:boom", "user-code"),
        ("pair_tools:nan", "numerical"),
        ("pair_tools:oom", "resource"),
        ("pair_tools:mem", "resource"),
        ("pair_tools:sneaky", "contract"),
        ("pair_tools:big", "contract"),
        ("pair_tools:missing", "contract"),
        ("nomodule_xyz:f", "contract"),
    ],
)
def test_failure_categories_exit_zero(tmp_path, entry, category):
    inv = _inv(tmp_path, entry, limits={"max_output_bytes": 150})
    proc = run_subprocess(inv)
    assert proc.returncode == 0, proc.stderr
    man = OutputManifest.read(inv.staging_dir)
    assert man.status == "failed" and man.error["category"] == category
    assert man.error["traceback_tail"] and len(man.error["traceback_tail"]) <= 4000
    assert [e.slot for e in man.outputs if e.role != "log"] == []


def test_unreadable_invocation_exit_2(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("{}")
    proc = subprocess.run(
        [sys.executable, "-m", "eidos_worker.run", str(bad)], capture_output=True, text=True
    )
    assert proc.returncode == 2


def test_reader_refuses_undeclared_and_verifies_digest(tmp_path):
    inv = _inv(tmp_path, "pair_tools:run", inputs={"predictions": (CSV, "text/csv")})
    reader = ArtifactReader(inv.inputs)
    assert len(list(reader.rows("predictions"))) == 3
    assert reader.binding("predictions") is inv.inputs["predictions"]
    with pytest.raises(ContractError):
        reader.path("other")
    with pytest.raises(ContractError):
        reader.open("/etc/passwd")
    with pytest.raises(ContractError):
        reader.open("predictions", "wb")
    Path(inv.inputs["predictions"].path).write_bytes(CSV + b"tamper")
    with pytest.raises(ContractError, match="digest"):
        ArtifactReader(inv.inputs).path("predictions")


def test_reader_json_and_jsonl(tmp_path):
    inv = _inv(tmp_path, "x:y", inputs={"j": {"a": 1}, "l": (b'{"a":1}\n\n{"a":2}\n', "application/jsonl")})
    reader = ArtifactReader(inv.inputs)
    assert reader.json("j") == {"a": 1}
    assert [r["a"] for r in reader.rows("l")] == [1, 2]
    with pytest.raises(ContractError):
        list(reader.rows("j"))


def test_writer_rules(tmp_path):
    w = ArtifactWriter(tmp_path, {"max_output_bytes": 50})
    for bad in ("../x", "a/b", ".hidden", "", "a b", "x" * 200):
        with pytest.raises(ContractError):
            w.stage_json(bad, {})
    w.stage_json("ok", {"a": 1})
    with pytest.raises(ContractError):
        w.stage_json("ok", {})
    target = tmp_path / "real.txt"
    target.write_text("hi")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ContractError):
        w.stage_file("l", link)
    with pytest.raises(ContractError):
        w.stage_json("nan", {"a": float("nan")})
    with pytest.raises(ContractError, match="limit"):
        w.stage_file("big", b"x" * 100)
    assert [p.name for p in (tmp_path / "outputs").iterdir()] == ["ok.json"]  # no temp leftovers


def test_writer_rows_csv_jsonl_and_file(tmp_path):
    w = ArtifactWriter(tmp_path)
    e1 = w.stage_rows("c", iter([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]), format="csv", schema="s/1")
    e2 = w.stage_rows("j", [{"a": 1}], format="jsonl")
    src = tmp_path / "src.bin"
    src.write_bytes(b"abc")
    e3 = w.stage_file("f", src, media_type="application/octet-stream", role="checkpoint")
    assert (tmp_path / e1.filename).read_text() == "a,b\n1,x\n2,y\n"
    assert e2.media_type == "application/jsonl" and e3.size == 3 and e3.role == "checkpoint"
    assert len(w.entries) == 3


def test_model_client_only_with_port(tmp_path):
    from eidos_worker import RunContext

    assert RunContext(_inv(tmp_path, "x:y")).model is None
    inv = _inv(
        tmp_path / "b", "x:y", ports={"model.client/1": {"url": "http://127.0.0.1:1/x", "token": "SECRET"}}
    )
    ctx = RunContext(inv)
    assert ctx.model is not None and "SECRET" not in repr(ctx.model)
    ctx.close()


def test_model_client_posts_json():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from eidos_worker import ModelClient

        assert ModelClient(f"http://127.0.0.1:{srv.server_port}/", "tok").call({"p": 1}) == {"ok": True}
    finally:
        srv.shutdown()
    assert seen == {"auth": "Bearer tok", "body": {"p": 1}}


def test_check_finite_and_classify():
    assert check_finite({"a": [1.0, 2]}) == {"a": [1.0, 2]}
    with pytest.raises(Exception) as e:
        check_finite(float("inf"))
    assert classify(e.value) == "numerical"
    assert classify(RuntimeError()) == "user-code"


def test_validate_component_runs_fixtures():
    report = validate_component(SRC)
    status = {f.path: (f.passed, f.skipped) for f in report.fixtures}
    assert status["fixtures/ok.json"] == (True, False), report
    assert status["fixtures/bad-config.json"] == (True, False), report
    assert status["fixtures/missing.json"] == (False, True)
    assert report.ok


def test_validate_reports_manifest_problems(tmp_path):
    (tmp_path / "eidos-component.json").write_text(json.dumps({"id": "x"}))
    report = validate_component(tmp_path)
    assert not report.ok and report.manifest_problems
