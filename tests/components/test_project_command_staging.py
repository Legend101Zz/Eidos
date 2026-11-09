"""Regression for the first full-lab real-LLM run (docs/mac/receipts/a8-full-lab-D02.json): the investigator's
baseline job failed because declared inputs reached the work dir only through config.files, a rule nothing in
its context stated (the lab-free driver defaulted it). A fixture policy that knows ONLY the packed context must
be able to build a working call, and the component must stage declared inputs by the documented default."""

from __future__ import annotations

import json

from conftest import COMPONENTS, run_component
from eidos.application.handlers.queries import catalog_row
from eidos.investigator.context import pack

MANIFEST = json.loads((COMPONENTS / "eidos.project.command" / "eidos-component.json").read_text())
REF = f"{MANIFEST['id']}@{MANIFEST['release']}"
PROJECT = """
import json, os, sys
a = dict(zip(sys.argv[2::2], sys.argv[3::2]))
need = ["train.npz", "validation.npz", "task.json"]
missing = [n for n in need if not os.path.exists(os.path.join(a["--inputs"], n))]
if missing:
    sys.exit("missing inputs: %s" % missing)
os.makedirs(a["--out"], exist_ok=True)
json.dump({"ok": 1}, open(os.path.join(a["--out"], "receipt.json"), "w"))
"""


def _context_only_policy(context: str, available: dict) -> dict:
    """Builds a call from the context text alone: the catalog line, its optional inputs and its config example."""
    line = next(json.loads(x) for x in context.splitlines()
                if x.startswith("{") and '"project-command/1"' in x)
    ports = set(line.get("optional_input") or {}) | set(line.get("input") or {})
    example = line["config_schema"]["examples"][0]
    assert "staged" in line["config_schema"]["properties"]["files"]["description"]
    return {"tool": "run_component", "args": {"component_ref": line["ref"], "config": example,
                                              "inputs": {p: a for p, a in available.items() if p in ports}}}


def test_context_states_the_input_staging_rule_and_the_default_call_works(tmp_path):
    view = {"investigation": {"id": "inv", "status": "active", "body": {"question": "reproduce the baseline"}},
            "horizon": 1, "charter": None, "jobs": [], "claims": [], "events": [], "budgets": {}, "turns": []}
    ctx = pack(view, [catalog_row(REF, MANIFEST, "active")])["text"]
    call = _context_only_policy(ctx, {"train": "art_train", "validation": "art_val", "task": "art_task"})
    assert "files" not in call["args"]["config"]  # nothing beyond what the context showed
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.py").write_text(PROJECT)
    data = {"art_train": (b"npz", "application/x-npz"), "art_val": (b"npz", "application/x-npz"),
            "art_task": ({"task_id": "t"}, "application/json")}
    res = run_component(tmp_path, "eidos.project.command", MANIFEST["entry"], cwd=proj,
                        inputs={p: data[a] for p, a in call["args"]["inputs"].items()}, config=call["args"]["config"])
    assert res.manifest.status == "succeeded", res.manifest.error
    receipt = json.loads(res.text("receipt"))
    assert receipt["staged_inputs"] == {"train": "train.npz", "validation": "validation.npz", "task": "task.json"}


def test_failure_message_names_what_the_work_dir_held(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.py").write_text(PROJECT)
    res = run_component(tmp_path, "eidos.project.command", MANIFEST["entry"], cwd=proj,
                        inputs={"train": (b"npz", "application/x-npz")},
                        config={"argv": ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}"]})
    assert res.manifest.status == "failed"
    msg = res.manifest.error["message"]
    assert "missing inputs" in msg and "train.npz" in msg  # the job error the investigator sees says why
