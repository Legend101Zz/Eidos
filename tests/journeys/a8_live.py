#!/usr/bin/env python
"""A8 (G5): the full autonomous acceptance journey with a REAL model through the internal loop.

Project: the switch-order acceptance project (examples/switch-order-project). The operator sets up the
lab exactly as a researcher would before an investigation: the switch journey's step 1 (project,
environments, built-in components) and step 2 (charter committed before exploration), then qualifies
and promotes the paired-CI assessor and the evidence-plan template, and allows the investigator to
freeze plans that use approved templates. The investigator then gets a brief and the lab: no
reference solution, no test labels, and no scripted decisions.

During the run the investigator worker is SIGKILLed right after the first committed tool effect (its
first job admission). The supervisor restarts the worker, the activity is retried with the same
recorded command keys, and the next worker continues the investigation (hand-off).

    EIDOS_HOME=<lab> python tests/journeys/a8_live.py --model anthropic:claude-sonnet-5-5 --out receipt.json \\
        --torch-python <py with torch> [--base-url http://127.0.0.1:11535/v1] [caps]

The lab must already be running. For Anthropic, start it with ``lab start --credentials-stdin`` so the
key reaches only the investigator worker. Exit status 0 only if every A8 check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
import switch_journey as sj  # noqa: E402
from journey_lib import Blocked, Journey  # noqa: E402

BRIEF = """Investigation brief from the researcher (switch-order project).

The model: OrderedSwitchNet in the registered environment "default" (adapter "eidos_adapter:ADAPTER", builder
"OrderedSwitchNet"). It decides which of two switches fired first. It embeds 6 event types (12 dimensions),
pools them with position weights, and appends one extra input, the "recorder" flag (+1/-1): a noisy hint that
agrees with the label for about 98% of training examples. The readout is a Linear(13, 1) over [pooled
embedding (12 values), recorder (1 value)]. The standard recipe trains it with eidos.pytorch.train for 45 steps
(seed 41) and evaluates it with eidos.pytorch.evaluate on {"split": "test", "data_seed": 76102, "pairs": 4000}.
lab.pairs.by_group scores the evaluation's paired predictions by stratum.

Symptom: after 45 steps the model looks good on reference accuracy but poor on paired both-correct accuracy.

What I need from this investigation, in order. Use the tools; cite job and artifact ids.
1. Reproduce and diagnose the failure: train, evaluate and score the baseline, and locate where it fails.
2. Create a NEW research component with author_component (a small artifact-op of your own design that helps the
   diagnosis or the repair), then publish it after its validation job succeeds, and run it.
3. Test a repair both as a trial (plan_trial, then launch_trial) and as a checkpoint branch from the baseline
   checkpoint (plan_branch, then launch_branch).
4. Confirm the repair against the charter's success rule with a qualified evidence plan: file_claim,
   draft_evidence_plan with an approved template and assessor, freeze_evidence_plan, launch_evidence_plan. The
   assessment runs by itself; report its outcome honestly whatever it is.
5. Behaviour-check the repaired checkpoint in a fresh process (eidos.pytorch.behavior-check) and export it with
   export_model.
6. Conclude with a short summary of the diagnosis, the evidence and its limits.
Every job finishes in a later turn: use wait_for_jobs after launching work.
The model budget for this investigation is small (about USD 2): combine independent calls in one decision, avoid
re-reading what the context already shows, and move on to the evidence plan and the export once a repair works."""

JOB_TOOLS = ("run_component", "author_component", "launch_trial", "launch_branch")


def setup_protocols(j: Journey) -> None:
    """Qualify and promote the assessor and the template (the operator's job, before the investigation)."""
    with j.step("q", "operator: qualify and promote the assessor and the evidence-plan template; auto-freeze policy"):
        suite = json.loads((REPO / "components/eidos.assessors.paired-ci/controls/suite.json").read_text())
        sid = j.cmd_result("register_control_suite", {"suite": suite, "suite_id": "suite:paired-ci-author"})
        rq = sj.qualify(j, "assessor", sid["suite_id"], sj.ASSESSOR)
        ok = j.check("assessor qualification passed", rq is not None and rq["body"]["status"] == "passed",
                     status=rq and rq["body"]["status"])
        if not ok:
            raise Blocked("assessor not qualified")
        j.cmd_result("promote_protocol", {"release_ref": sj.ASSESSOR, "scope": ["improves"], "note": "author suite only"})
        base_req = {"claim": {"kind": "improves", "statement": "qualification control"}, "success_rule_id": "early-repair",
                    "required_isolation": {"profile": "dev:same-user", "controls": []},
                    "assessor": {"ref": sj.ASSESSOR}, "seeds": {"count": 3, "start": 41},
                    "evaluation": sj.EVAL_CFG["evaluation"],
                    "common_train": {"adapter": sj.ADAPTER, "builder": "OrderedSwitchNet", "steps": 45},
                    "common_evaluate": {"adapter": sj.ADAPTER}, "budget": {"steps": 45}}
        planted = {**base_req, "baseline": {"description": "unedited"},
                   "candidate": {"description": "silence recorder", "post": [{"component": "eidos.pytorch.weight-edit",
                                                                              "config": sj.EDIT_CFG}]}}
        null = {**base_req, "baseline": {"description": "unedited"}, "candidate": {"description": "identical"}}
        tsuite = {"target": {"kind": "template", "ref": sj.TEMPLATE}, "scope": ["improves"],
                  "origin": {"authored_by": "operator setup (author suite, not independent)", "committed_before_run": True},
                  "controls": [
                      {"id": "planted-distinct-candidate", "type": "planted", "deterministic": True, "repetitions": 1,
                       "expected_outcome": "distinct-arms-planned", "input": {"inputs": {"request": planted}}},
                      {"id": "null-identical-recipes", "type": "null", "deterministic": True, "repetitions": 1,
                       "expected_outcome": "identical-arms", "input": {"inputs": {"request": null}}},
                      {"id": "null-smuggled-threshold", "type": "null", "deterministic": True, "repetitions": 1,
                       "expected_outcome": "refused", "expect_refusal": True,
                       "input": {"inputs": {"request": {**planted, "min_effect": 0.0}}}}]}
        tsid = j.cmd_result("register_control_suite", {"suite": tsuite, "suite_id": "suite:template-author"})
        tq = sj.qualify(j, "template", tsid["suite_id"], sj.TEMPLATE, runner="journey.template-qualifier@1.0.0")
        ok = j.check("template qualification passed", tq is not None and tq["body"]["status"] == "passed",
                     status=tq and tq["body"]["status"])
        if not ok:
            raise Blocked("template not qualified")
        j.cmd_result("promote_protocol", {"release_ref": sj.TEMPLATE, "scope": ["improves"], "note": "author suite only"})
        j.cmd_result("update_profile_policy", {"policy": {"auto_freeze_approved_templates": True}})
        j.check("investigator may freeze plans that use approved templates", True)


def _turn_rows(j: Journey, shown: dict) -> list[dict]:
    rows = []
    for t in shown["turns"]:
        tt = j.query("get_turn", {"turn_id": t["id"]})
        rows.append({"seq": t["seq"], "status": t["status"], "stop_reason": t["stop_reason"], "decisions": [
            {"slot": d["slot"], "accepted": d["accepted"], "usage": (d["body"] or {}).get("usage"),
             "served_models": ((d["body"] or {}).get("model") or {}).get("served_models"),
             "calls": [c["tool"] for c in (d["body"] or {}).get("calls", [])],
             "rationale": str((d["body"] or {}).get("rationale", ""))[:600],
             "control": (d["body"] or {}).get("control"), "error": (d["body"] or {}).get("error")}
            for d in tt["decisions"]]})
    return rows


def _export_loads(j: Journey, a: argparse.Namespace, export: dict) -> dict:
    """Download the exported checkpoint and load it into a freshly built model with a strict load."""
    import tempfile
    ck = (export.get("body") or {}).get("checkpoint", {}).get("artifact_id")
    if not ck:
        return {"ok": False, "why": "export has no checkpoint"}
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "checkpoint.pt"
        j.client.download(j.project, ck, str(path))
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(REPO / 'examples/switch-order-project')!r})\n"
            "import torch\n"
            "from eidos_pytorch.checkpoint import load_checkpoint\n"
            "import eidos_adapter\n"
            "st = load_checkpoint(sys.argv[1])\n"
            "builder = (st['manifest'].get('builder') or {}).get('name') or 'OrderedSwitchNet'\n"
            "builder = builder.split(':')[-1] if builder.split(':')[-1] in ('SwitchNet', 'OrderedSwitchNet') else 'OrderedSwitchNet'\n"
            "m = eidos_adapter.ADAPTER.build({'builder': builder})\n"
            "m.load_state_dict(st['entries']['model'], strict=True)  # the exported weights, strict\n"
            "m.eval()\n"
            "with torch.no_grad():\n"
            "    out = m(torch.randint(0, 6, (4, 8)), torch.tensor([1.0, 0.0, 1.0, 0.0]))\n"
            "assert out.shape == (4,) and torch.isfinite(out).all()\n"
            "w = m.readout.weight.detach()\n"
            "print(json.dumps({'ok': True, 'builder': builder, 'params': sum(p.numel() for p in m.parameters()),"
            " 'recorder_weight': float(w[0, -1])}))\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(REPO / p) for p in (
            "packages/eidos-pytorch/src", "packages/eidos-worker-sdk/src", "packages/eidos-wire/src"))}
        r = subprocess.run([a.torch_python, "-c", code, str(path)], capture_output=True, text=True, env=env, timeout=300)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {"ok": False, "stderr": r.stderr[-800:]}


def live(j: Journey, a: argparse.Namespace) -> None:
    with j.step("A8", f"REAL-MODEL autonomous investigation ({a.model}): diagnose, author, trial, branch, assess, "
                      "export; investigator-worker crash after a committed effect"):
        c = j.S.get("charter")
        if not c:
            raise Blocked("needs the charter")
        home = os.environ["EIDOS_HOME"]
        settings = {"max_requests_per_decision": a.max_req_per_decision, "max_output_tokens": a.max_output_tokens,
                    "max_tokens_per_decision": a.max_tokens_per_decision, "request_timeout": a.request_timeout}
        if a.price:
            settings["price_per_mtok"] = json.loads(a.price)
        if a.base_url:
            settings["base_url"] = a.base_url
        if a.recheck:
            # Re-evaluate the A8 checks of a recorded run (no new investigation, no model call); the kill record
            # comes from that run's receipt.
            prev = json.loads(Path(a.previous_receipt).read_text())
            inv, kills = a.recheck, prev.get("kills") or []
            j.rec.update(investigation=inv, rechecked_from=a.previous_receipt)
            j.note(f"recheck of recorded investigation {inv}: checks re-evaluated from the ledger; no model call")
            _checks(j, a, inv, kills, home)
            return
        inv_id = f"inv-a8-{int(time.time()) % 1000000}"
        limits = ["--limit", f"model_tokens={a.model_tokens}", "--limit", f"turns={a.max_turns}",
                  "--limit", "compute_seconds=6000", "--limit", "trials=60"]
        if a.model_usd:
            limits += ["--limit", f"model_usd={a.model_usd}"]
        rc, data, err = j.cli("investigate", "start", j.project, "--question", BRIEF, "--charter", c["charter_id"],
                              "--environment", "default", "--runtime", "pydantic-ai", "--model", a.model,
                              "--max-turns", str(a.max_turns), "--max-model-calls-per-turn", str(a.max_calls_per_turn),
                              "--settings", j.at_file(settings), "--investigation-id", inv_id, *limits)
        if not j.check("lab investigate start (pydantic-ai runtime, real model)", rc == 0 and bool(data), err=err[-400:]):
            raise Blocked("could not start")
        inv = data["result"]["investigation_id"]
        j.rec["investigation"] = inv
        kills: list[dict] = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set() and not kills and not a.no_kill:
                try:
                    now = j.client.query("get_investigation", {"investigation_id": inv}, project=j.project)
                except Exception:  # noqa: BLE001
                    time.sleep(0.05)
                    continue
                if now["jobs"]:
                    pid = sj.investigator_pid(home)
                    if pid:
                        os.kill(pid, signal.SIGKILL)
                        after = j.client.query("get_investigation", {"investigation_id": inv}, project=j.project)
                        kills.append({"pid": pid, "t": time.time(), "jobs_at_kill": [x["id"] for x in now["jobs"]],
                                      "turns_after_kill": [(t["id"], t["status"]) for t in after["turns"]]})
                time.sleep(0.01)

        th = threading.Thread(target=watcher, daemon=True)
        th.start()
        end, status, shown = time.monotonic() + a.wait_minutes * 60, None, {}
        while time.monotonic() < end:
            shown = j.query("get_investigation", {"investigation_id": inv})
            status = shown["status"]
            if status != "active" or shown.get("needs_input"):
                break
            if len([t for t in shown["turns"] if t["status"] != "open"]) >= a.max_turns:
                time.sleep(30)
                shown = j.query("get_investigation", {"investigation_id": inv})
                status = shown["status"]
                break
            time.sleep(10)
        stop.set()
        th.join(timeout=5)
        if status == "active":  # turn cap, needs input or wall cap: stop it so nothing keeps spending
            r = j.cmd("stop_investigation", {"investigation_id": inv, "running_job_policy": "drain"})
            j.rec["stopped_by_driver"] = r
            j.note(f"investigation stopped by the driver after the wait (status was active, needs_input="
                   f"{shown.get('needs_input')})")
        time.sleep(5)
        _checks(j, a, inv, kills, home, status_hint=status)


def _checks(j: Journey, a: argparse.Namespace, inv: str, kills: list, home: str, status_hint: str | None = None) -> None:
    st = j.rec["steps"]["A8"]
    if True:
        shown = j.query("get_investigation", {"investigation_id": inv})
        status = shown["status"]
        view = j.query("investigation_view", {"investigation_id": inv,
                                               "horizon": j.query("project_summary", {})["horizon"]})
        turns = _turn_rows(j, shown)
        results = view.get("call_results") or []
        ok_calls = {}
        for r in results:
            if r["ok"]:
                ok_calls.setdefault(r["tool"], []).append(r.get("result"))
        jobs = shown["jobs"]
        by_comp = {}
        for jb in jobs:
            by_comp.setdefault(str(jb.get("component") or ""), []).append(jb)
        j.rec.update(investigation_final={"status": status, "needs_input": shown.get("needs_input"),
                                          "conclusion": (shown.get("body") or {}).get("conclusion")},
                     turns=turns, call_results=results, evidence_plans=view.get("evidence_plans"),
                     jobs=[{k: jb.get(k) for k in ("id", "status", "kind", "component", "purpose")} for jb in jobs],
                     kills=kills)
        st["numbers"].update(turns=len(turns), jobs=len(jobs))
        # ---- A8 elements -------------------------------------------------------------------------------
        succeeded = lambda pref: [x for x in jobs if str(x.get("component") or "").startswith(pref)  # noqa: E731
                                  and x["status"] == "succeeded"]
        j.check("diagnosis evidence: the baseline was trained, evaluated and scored by stratum",
                bool(succeeded("eidos.pytorch.train")) and bool(succeeded("eidos.pytorch.evaluate"))
                and bool(succeeded("lab.pairs.by_group")),
                train=len(succeeded("eidos.pytorch.train")), evaluate=len(succeeded("eidos.pytorch.evaluate")),
                score=len(succeeded("lab.pairs.by_group")))
        published = [r.get("component_ref") or r.get("ref") for r in ok_calls.get("publish_component", []) if isinstance(r, dict)]
        j.check("new component: author_component -> validation -> publish_component",
                bool(ok_calls.get("author_component")) and bool(published), published=published)
        ran_new = [x for x in jobs if any(p and str(x.get("component") or "").startswith(str(p).split("@")[0])
                                          for p in published) and x["status"] == "succeeded"]
        j.check("the new component ran successfully", bool(ran_new), jobs=[x["id"] for x in ran_new])
        status_of = {x["id"]: x["status"] for x in jobs}
        launched = lambda tool: [(r or {}).get("job_id") for r in ok_calls.get(tool, []) if isinstance(r, dict)]  # noqa: E731
        trial_ok = [jid for jid in launched("launch_trial") if status_of.get(jid) == "succeeded"]
        branch_ok = [jid for jid in launched("launch_branch") if status_of.get(jid) == "succeeded"]
        j.check("trial executed (plan_trial + launch_trial, job succeeded)", bool(trial_ok),
                launched=launched("launch_trial"), statuses=[status_of.get(x) for x in launched("launch_trial")])
        j.check("checkpoint branch executed (plan_branch + launch_branch, job succeeded)", bool(branch_ok),
                launched=launched("launch_branch"), statuses=[status_of.get(x) for x in launched("launch_branch")])
        plans = view.get("evidence_plans") or []
        assessed = [p for p in plans if p.get("assessment")]
        j.check("qualified assessment: the investigator's frozen evidence plan was launched and assessed",
                bool(assessed), plans=[(p["plan_id"], p["status"], p.get("assessment")) for p in plans])
        exports = [x for x in j.query("list_records", {"kind": "export", "limit": 50, "summary": False})
                   if (x.get("body") or {}).get("investigation_id") == inv or x.get("created_by", "").startswith("svc-inv")]
        if not exports and ok_calls.get("export_model"):
            ids = [r.get("export_id") for r in ok_calls["export_model"] if isinstance(r, dict)]
            exports = [j.query("get_record", {"kind": "export", "id": i}) for i in ids if i]
        j.check("export recorded through export_model", bool(exports), exports=len(exports))
        loaded = _export_loads(j, a, exports[-1]) if exports else {"ok": False}
        j.rec["export_load"] = loaded
        j.check("the exported model loads into a freshly built OrderedSwitchNet (strict restore)", loaded.get("ok") is True,
                loaded=loaded)
        j.check("investigation concluded autonomously", status == "concluded", status=status)
        # ---- crash, same key, hand-off -------------------------------------------------------------------
        if not a.no_kill:
            j.check("the investigator-worker was SIGKILLed right after a committed effect", len(kills) == 1, kills=kills)
            if kills:
                k = kills[0]
                j.check("the supervisor restarted the investigator-worker (hand-off to a new process)",
                        sj.investigator_pid(home) not in (None, k["pid"]), old=k["pid"], new=sj.investigator_pid(home))
        accepted_calls, per_slot = [], {}
        for t in shown["turns"]:
            for d in j.query("get_turn", {"turn_id": t["id"]})["decisions"]:
                if d["accepted"]:
                    per_slot[(t["id"], d["slot"])] = per_slot.get((t["id"], d["slot"]), 0) + 1
                    accepted_calls += [cl for cl in d["body"]["calls"]]
        j.check("exactly one accepted decision per (turn, slot)", all(v == 1 for v in per_slot.values()))
        keys = [cl["command_key"] for cl in accepted_calls]
        j.check("persisted command keys are unique (a retry re-used the recorded key)", len(keys) == len(set(keys)))
        events = j.client.query("list_events", {"investigation_id": inv, "cursor": 0, "limit": 5000}, project=j.project)["events"]
        inv_job_ids = {x["id"] for x in jobs}
        admitted = [e["subject"] for e in events if e["type"] == "job.admitted" and e["subject"] in inv_job_ids]
        j.check("one job.admitted event per job (no duplicate admission after the kill)",
                len(admitted) == len(set(admitted)), admitted=len(admitted), distinct=len(set(admitted)))
        bud = j.query("budgets", {"investigation_id": inv})["budgets"]
        j.rec["budgets"] = bud
        j.check("model charges are visible in the budget ledger", (bud.get("model_tokens") or {}).get("used", 0) > 0,
                model_tokens=bud.get("model_tokens"), model_usd=bud.get("model_usd"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=f"a8{int(time.time()) % 100000}")
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url")
    ap.add_argument("--price", help='USD per Mtok, e.g. {"input": 2.0, "output": 10.0}')
    ap.add_argument("--torch-python", default=os.environ.get("EIDOS_TORCH_PYTHON", sys.executable))
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", default=os.environ.get("TMPDIR", "/tmp") + "/a8-work")
    ap.add_argument("--max-turns", type=int, default=16)
    ap.add_argument("--max-calls-per-turn", type=int, default=2)
    ap.add_argument("--max-req-per-decision", type=int, default=4)
    ap.add_argument("--max-output-tokens", type=int, default=4096)
    ap.add_argument("--max-tokens-per-decision", type=int, default=120000)
    ap.add_argument("--model-tokens", type=int, default=1500000)
    ap.add_argument("--model-usd", type=float, default=0.0)
    ap.add_argument("--request-timeout", type=float, default=300)
    ap.add_argument("--wait-minutes", type=int, default=150)
    ap.add_argument("--no-kill", action="store_true")
    ap.add_argument("--skip-setup", action="store_true", help="reuse --project (state file in --workdir)")
    ap.add_argument("--setup-only", action="store_true", help="operator setup only; no investigation")
    ap.add_argument("--recheck", help="re-evaluate the A8 checks of this recorded investigation (no model call)")
    ap.add_argument("--previous-receipt", help="with --recheck: that run's receipt (for its kill record)")
    a = ap.parse_args()
    Path(a.workdir).mkdir(parents=True, exist_ok=True)
    os.chdir(REPO)
    j = Journey(a.project, Path(a.out))
    j.rec.update(a8_live=True, model=a.model, base_url=a.base_url,
                 caps={k: getattr(a, k) for k in ("max_turns", "max_calls_per_turn", "max_req_per_decision",
                                                   "max_output_tokens", "model_tokens", "model_usd")})
    state = Path(a.workdir) / f"{a.project}.state.json"
    if a.skip_setup and state.exists():
        j.S = json.loads(state.read_text())
    else:
        sj.step1(j, a)
        sj.step2(j, a)
        setup_protocols(j)
        state.write_text(json.dumps(j.S, default=str))
    if not a.setup_only:
        live(j, a)
    j.save()
    bad = [k for k, v in j.rec["steps"].items() if v["status"] != "pass"]
    print("\nSUMMARY", {k: v["status"] for k, v in j.rec["steps"].items()}, "->", a.out)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
