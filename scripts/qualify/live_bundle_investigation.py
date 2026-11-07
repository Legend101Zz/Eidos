#!/usr/bin/env python3
"""Lab-free, capped real-LLM investigation driver for one task bundle (eidos-task-bundle/0.1).

What is real: the investigator runtime port + Pydantic AI adapter, ``context-policy/1`` packing,
decision validation (required args), and the actual project commands run on the bundle.
What is NOT the full lab: no PostgreSQL ledger, Temporal, fencing, or isolation — the driver keeps
an in-memory record and executes a restricted tool set locally:

* ``run_component`` with ``eidos.project.command*`` (the bundle's own CLI, optional candidate
  ``inputs.source`` from ``author_component``) or ``eidos.task.score-classification*`` (validation);
* ``author_component`` (stores candidate project files; the driver does not build components);
* ``record_note``; control ``wait_for_jobs`` (jobs run synchronously) / ``conclude`` / ``stop``.

The investigator sees ONLY the bundle (brief, task.json, train/validation arrays via declared
inputs, project source) and the two generic components. Test inputs/labels are never used.

Caps: --max-requests (<= 60), --max-turns, 4096 output tokens per request, temperature unset,
per-command timeout. Key only from ANTHROPIC_API_KEY; never printed. Writes a JSON run record.

Usage (macOS arm64, the release's research env + the live-check requirements):
    PY=<research dev env python with numpy/torch>
    $PY -m pip install -r scripts/qualify/requirements-live.txt
    ANTHROPIC_API_KEY=... PYTHONPATH=src:packages/eidos-wire/src $PY scripts/qualify/live_bundle_investigation.py \
        --bundle /tmp/eidos-D02 --out /tmp/eidos-live-D02.json --model anthropic:claude-sonnet-5-5 \
        --max-requests 40 --max-turns 8
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _contract(component_dir: str) -> dict:
    """Config contract + summary from the shipped manifest (the same one the lab enforces)."""
    m = json.loads((_ROOT / "components" / component_dir / "eidos-component.json").read_text())
    return {"ref": f"{m['id']}@{m['release']}", "summary": m.get("summary", ""), "config_schema": m.get("config_schema")}


def contract_problems(calls: list, catalog: list) -> list:
    from eidos.domain.contracts import validate_config
    by_id = {c["ref"].split("@")[0]: c for c in catalog}
    out = []
    for i, c in enumerate(calls):
        if c["tool"] in ("run_component", "plan_trial"):
            entry = by_id.get(str(c["args"].get("component_ref", "")).split("@")[0])
            if entry is None:
                out.append(f"call {i}: unknown component {c['args'].get('component_ref')!r}")
            elif entry.get("config_schema"):
                out += [f"call {i}: {p}" for p in validate_config(c["args"].get("config") or {}, entry["config_schema"])]
    return out

HARD_MAX_REQUESTS = 60
SCORE = r"""
import json, sys, numpy as np
p = np.load(sys.argv[1], allow_pickle=False); r = np.load(sys.argv[2], allow_pickle=False)
pid, pred, rid, y = p["ids"], p["predictions"], r["ids"], r["y"]
assert set(pid.tolist()) == set(rid.tolist()) and len(set(pid.tolist())) == len(pid), "id mismatch"
o = {int(i): int(v) for i, v in zip(pid.tolist(), pred.tolist())}
c = [int(o[int(i)] == int(t)) for i, t in zip(rid.tolist(), y.tolist())]
print(json.dumps({"accuracy": sum(c) / len(c), "n": len(c), "correct": sum(c)}))
"""


class LocalLab:
    def __init__(self, bundle: Path, python: str, work: Path, timeout: int):
        self.bundle, self.python, self.work, self.timeout = bundle, python, work, timeout
        self.jobs: list[dict] = []
        self.artifacts: dict[str, Path] = {"art_train": bundle / "train.npz", "art_validation": bundle / "validation.npz",
                                           "art_task": bundle / "task.json"}
        self.sources: dict[str, dict] = {}
        self.notes: list[dict] = []

    def _job(self, component, status, inputs, config, receipt, outputs, error=None):
        jid = f"job_{len(self.jobs):03d}"
        j = {"id": jid, "component": component, "purpose": "open-search", "status": status, "inputs": inputs,
             "config": config, "receipt": receipt, "error": error, "outputs": outputs}
        self.jobs.append(j)
        return j

    def run_component(self, a: dict) -> dict:
        ref, inputs, config = a["component_ref"], dict(a.get("inputs") or {}), dict(a.get("config") or {})
        if ref.startswith("eidos.project.command"):
            return self._command(ref, inputs, config, a.get("seed"))
        if ref.startswith("eidos.task.score-classification"):
            return self._score(ref, inputs, config)
        return {"ok": False, "error": f"component {ref} is not available in the lab-free driver"}

    def _command(self, ref, inputs, config, seed):
        jdir = self.work / f"cmd{len(self.jobs):03d}"
        wd, out = jdir / "work", jdir / "out"
        wd.mkdir(parents=True)
        out.mkdir()
        for p in self.bundle.iterdir():
            if p.is_file() and not p.name.endswith(".npz"):
                shutil.copyfile(p, wd / p.name)
        src = inputs.get("source")
        if src:
            if src not in self.sources:
                return self._fail(ref, inputs, config, f"unknown source bundle {src}")
            for rel, text in self.sources[src].items():
                t = (wd / rel).resolve()
                if wd.resolve() not in t.parents:
                    return self._fail(ref, inputs, config, f"source path escapes work dir: {rel}")
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(text)
        for port, name in (config.get("files") or {"train": "train.npz", "validation": "validation.npz",
                                                    "task": "task.json"}).items():
            aid = inputs.get(port)
            if aid in self.artifacts and "/" not in name:
                shutil.copyfile(self.artifacts[aid], wd / name)
        s = str(config.get("seed", seed if seed is not None else 11))
        argv = [self.python if (i == 0 and x == "python") else x.replace("{work}", str(wd)).replace("{out}", str(out))
                .replace("{seed}", s) for i, x in enumerate(config.get("argv") or ["python", "project.py", "train",
                                                                                  "--inputs", "{work}", "--out", "{out}",
                                                                                  "--seed", "{seed}"])]
        t0 = time.time()
        try:
            r = subprocess.run(argv, cwd=wd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return self._fail(ref, inputs, config, f"timeout after {self.timeout}s")
        if r.returncode != 0:
            return self._fail(ref, inputs, config, f"exit {r.returncode}: {r.stderr[-1500:]}")
        outputs = []
        for p in sorted(out.iterdir()):
            aid = f"art_{len(self.artifacts):04d}"
            self.artifacts[aid] = p
            outputs.append({"slot": p.stem, "artifact_id": aid, "role": "model-export" if p.suffix == ".pt" else "output",
                            "schema": "prediction-array/1" if "prediction" in p.name else None})
        metrics = {}
        if (out / "receipt.json").exists():
            metrics = {k: v for k, v in json.loads((out / "receipt.json").read_text()).items()
                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
        j = self._job(ref, "succeeded", inputs, config, {"metrics": metrics, "seconds": round(time.time() - t0, 2),
                                                          "source": src or "bundle"}, outputs)
        return {"ok": True, "job_id": j["id"], "outputs": outputs}

    def _score(self, ref, inputs, config):
        pred, refa = self.artifacts.get(inputs.get("predictions")), self.artifacts.get(inputs.get("reference"))
        if pred is None or refa is None or refa.name != "validation.npz":
            return self._fail(ref, inputs, config, "scoring needs predictions and the validation reference only")
        r = subprocess.run([self.python, "-c", SCORE, str(pred), str(refa)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return self._fail(ref, inputs, config, r.stderr[-800:])
        m = json.loads(r.stdout)
        j = self._job(ref, "succeeded", inputs, config, {"metrics": m}, [])
        return {"ok": True, "job_id": j["id"], "metrics": m}

    def _fail(self, ref, inputs, config, msg):
        j = self._job(ref, "failed", inputs, config, None, [], {"category": "user-code", "message": msg[:1500]})
        return {"ok": False, "job_id": j["id"], "error": msg[:500]}

    def author_component(self, a: dict) -> dict:
        files = a.get("files") or {}
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            return {"ok": False, "error": "files must map relative paths to text"}
        sid = f"src_{len(self.sources):03d}"
        self.sources[sid] = files
        return {"ok": True, "bundle_artifact_id": sid, "note": "use as run_component inputs.source"}

    def view(self, question: str, horizon: int) -> dict:
        return {"investigation": {"id": "inv-live-bundle", "status": "active", "body": {
            "question": question, "inputs": {"train": "art_train", "validation": "art_validation", "task": "art_task"},
            "notes": "Lab-free driver: run_component (eidos.project.command*, eidos.task.score-classification*), "
                     "author_component (candidate project files usable as run_component inputs.source), record_note, "
                     "conclude. Jobs finish before your next turn."}},
            "horizon": horizon, "charter": {"study_id": "live-bundle", "exploration_only": True},
            "jobs": self.jobs[-25:], "claims": [], "events": [], "budgets": {}}


_PC = _contract("eidos.project.command")
CATALOG = [
    {"ref": _PC["ref"], "implements": "project-command/1", "kind": "artifact-op", "config_schema": _PC["config_schema"],
     "effects": ["change-weights"], "input": {}, "output": {},
     "summary": _PC["summary"] + " inputs: train/validation/task artifact ids, optional source (authored files)."},
    {"ref": "eidos.task.score-classification@1.0.0", "implements": "classification-score/1", "kind": "artifact-op",
     "effects": ["observe"], "input": {"predictions": "prediction-array/1", "reference": "task-array/1"}, "output": {},
     "summary": "validation accuracy: inputs predictions=<validation_predictions artifact>, reference=art_validation."},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model", default="anthropic:claude-sonnet-5-5")
    ap.add_argument("--python", default=sys.executable, help="interpreter with the bundle's requirements")
    ap.add_argument("--max-requests", type=int, default=40)
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--command-timeout", type=int, default=300)
    args = ap.parse_args()
    if args.max_requests > HARD_MAX_REQUESTS:
        print(json.dumps({"error": f"--max-requests above hard cap {HARD_MAX_REQUESTS}"}))
        return 2
    replay = None
    if args.model.startswith("replay:"):  # offline dry run: scripted decisions via FunctionModel (no key, no network)
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel
        script = iter(json.loads(Path(args.model[7:]).read_text()))

        def fn(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, next(script))])
        replay = FunctionModel(fn)
    elif not os.environ.get("ANTHROPIC_API_KEY") and args.model != "test":
        print(json.dumps({"error": "ANTHROPIC_API_KEY is not set; nothing was called"}))
        return 2
    from eidos.domain.turns import validate_decision
    from eidos.investigator.context import pack
    from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
    from eidos.investigator.runtime import DecisionRequest, ModelCallFailed

    bundle = args.bundle.resolve()
    task = json.loads((bundle / "task.json").read_text())
    question = (bundle / "brief.md").read_text().strip()
    work = Path(tempfile.mkdtemp(prefix="eidos-live-bundle-"))
    lab = LocalLab(bundle, args.python, work, args.command_timeout)
    record = {"driver": "eidos-live-bundle/1", "label": "lab-free real-LLM development run (not a full Eidos lab run)",
              "task_id": task.get("task_id"), "bundle_hashes": task.get("hashes"), "model_requested": args.model,
              "caps": {"max_requests": args.max_requests, "max_turns": args.max_turns, "max_output_tokens": 4096,
                       "temperature": "unset"}, "turns": [], "started_at": time.time()}
    spent = 0
    for turn in range(1, args.max_turns + 1):
        remaining = args.max_requests - spent
        if remaining <= 0:
            record["stop"] = "request cap reached"
            break
        per = min(8, remaining)
        packed = pack(lab.view(question, turn), CATALOG)
        rt = PydanticAIRuntime({"model": args.model, "max_output_tokens": 4096, "max_requests_per_decision": per,
                                "output_retries": 1, **({"_model_object": replay} if replay else {})})
        req = DecisionRequest(turn_id=f"t{turn}", slot=0, context=packed["text"], turn_so_far=[], limits={"max_calls": 6},
                              read_tools={})
        entry = {"turn": turn, "context_chars": packed["chars"]}
        try:
            res = rt.decide(req)
        except ModelCallFailed as exc:
            spent += max(1, int((exc.possible_usage or {}).get("calls") or 0))
            entry.update({"error": str(exc)[:500], "retryable": exc.retryable, "usage": exc.possible_usage})
            record["turns"].append(entry)
            if not exc.retryable:
                record["stop"] = "non-retryable model failure"
                break
            continue
        spent += int(res.usage.get("calls") or 0)
        entry.update({"usage": res.usage, "served_models": res.model_settings.get("served_models"),
                      "proposal": res.proposal})
        try:
            d = validate_decision(res.proposal, max_calls=6)
            problems = contract_problems(d["calls"], CATALOG)
            if problems:
                raise ValueError("; ".join(problems[:5]))
        except Exception as exc:  # unaccepted decision: recorded, no effects
            entry["accepted"] = False
            entry["validation_error"] = str(exc)[:500]
            record["turns"].append(entry)
            continue
        entry["accepted"] = True
        receipts = []
        for c in d["calls"]:
            if c["tool"] == "run_component":
                receipts.append(lab.run_component(c["args"]))
            elif c["tool"] == "author_component":
                receipts.append(lab.author_component(c["args"]))
            elif c["tool"] == "record_note":
                lab.notes.append(c["args"])
                receipts.append({"ok": True})
            else:
                receipts.append({"ok": False, "error": f"{c['tool']} is not available in the lab-free driver"})
        entry["receipts"] = receipts
        record["turns"].append(entry)
        if (d["control"] or {}).get("action") in ("conclude", "stop"):
            record["stop"] = "investigator concluded"
            record["conclusion"] = d["control"]
            break
    else:
        record["stop"] = "turn cap reached"
    record.update({"total_requests": spent, "jobs": lab.jobs, "notes": lab.notes,
                   "authored_sources": {k: sorted(v) for k, v in lab.sources.items()}, "finished_at": time.time(),
                   "usage_total": {"input_tokens": sum((t.get("usage") or {}).get("input_tokens") or 0 for t in record["turns"]),
                                   "output_tokens": sum((t.get("usage") or {}).get("output_tokens") or 0 for t in record["turns"])}})
    args.out.write_text(json.dumps(record, indent=2, default=str))
    print(json.dumps({k: record[k] for k in ("task_id", "stop", "total_requests", "usage_total")}, default=str))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
