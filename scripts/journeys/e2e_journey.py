#!/usr/bin/env python3
"""End-to-end user journey against a lab installed from wheels, through the `lab` CLI and the MCP SDK client.

Run by scripts/journeys/e2e_journey.sh with the INSTALLED venv's python (never the checkout's src/). It does
init/start/doctor itself, so the record starts at a clean install.

Env: LAB_BIN, EIDOS_HOME, REPO, OUT, RESEARCH_PY (project interpreter: torch + numpy + the research lock),
     LAB_API_PORT, LAB_PG_PORT, LAB_TEMPORAL_PORT, PG_BIN, TEMPORAL_SOURCE (optional).

Every investigator decision comes from FIXTURE policies (deterministic scripts, no model, no provider calls).
Output: $OUT/journey-record.json = [{step, interface, status, ids, detail, seconds}], plus per-step artifacts.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

LAB = os.environ["LAB_BIN"]
HOME = os.environ["EIDOS_HOME"]
REPO = Path(os.environ["REPO"])
OUT = Path(os.environ["OUT"])
RESEARCH_PY = os.environ["RESEARCH_PY"]
DEV = REPO / "research" / "public" / "development"
PROJECT = "d01e2e"
RECORD: list[dict] = []


class StepFailed(Exception):
    pass


def lab(*args: str, check: bool = True, timeout: int = 900):
    r = subprocess.run([LAB, *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise StepFailed(f"lab {' '.join(args)} -> {r.returncode}\n{r.stdout[-1200:]}\n{r.stderr[-1200:]}")
    s = r.stdout.lstrip()
    try:
        return json.JSONDecoder().raw_decode(s)[0] if s[:1] in "{[" else s
    except json.JSONDecodeError:
        return s


def query(name: str, params: dict | None = None, project: str = PROJECT):
    return lab("query", name, "-p", project, "--params", json.dumps(params or {}))


def step(name: str, interface: str):
    """Decorator: run, time and record one journey step; a failure is recorded and stops the journey."""

    def deco(fn):
        def run():
            t0, ids, detail = time.monotonic(), {}, ""
            print(f"\n=== {name} [{interface}]", flush=True)
            try:
                ret = fn() or {}
                ids, detail, status = ret.get("ids", {}), ret.get("detail", ""), "pass"
            except Exception as exc:  # noqa: BLE001 - recorded, then the journey stops
                status, detail = "FAIL", "".join(traceback.format_exception_only(type(exc), exc))[-1500:]
                traceback.print_exc()
            RECORD.append(
                {
                    "step": name,
                    "interface": interface,
                    "status": status,
                    "ids": ids,
                    "detail": detail,
                    "seconds": round(time.monotonic() - t0, 1),
                }
            )
            print(f"[{status}] {name} {json.dumps(ids)[:300]} {detail[:300]}", flush=True)
            (OUT / "journey-record.json").write_text(json.dumps(RECORD, indent=2, sort_keys=True))
            if status != "pass":
                raise SystemExit(f"journey stopped at step: {name}")

        return run

    return deco


def wait_for(fn, what: str, timeout: float = 600):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(2)
    raise StepFailed(f"timed out waiting for {what}")


S: dict = {}  # ids shared between steps


@step("1 lab init / start / doctor on a clean wheel install", "lab CLI")
def s1():
    args = [
        "init",
        "--home",
        HOME,
        "--api-port",
        os.environ["LAB_API_PORT"],
        "--pg-port",
        os.environ["LAB_PG_PORT"],
        "--temporal-port",
        os.environ["LAB_TEMPORAL_PORT"],
    ]
    if os.environ.get("PG_BIN"):
        args += ["--pg-bin", os.environ["PG_BIN"]]
    if os.environ.get("TEMPORAL_SOURCE"):
        args += ["--temporal-source", os.environ["TEMPORAL_SOURCE"]]
    lab(*args)
    lab("start")
    doc = lab("doctor", "--json")
    bad = [c for c in doc if c["status"] == "fail"]
    if bad:
        raise StepFailed(f"doctor failures: {bad}")
    (OUT / "doctor.json").write_text(json.dumps(doc, indent=2))
    return {"ids": {"home": HOME}, "detail": f"{len(doc)} doctor checks, 0 fail"}


@step("2a package D01 bundle (research owner script, research/public untouched)", "research package_task.py")
def s2a():
    bundle = OUT / "D01-bundle"
    r = subprocess.run(
        [RESEARCH_PY, "package_task.py", "--task", "D01", "--out", str(bundle)],
        cwd=DEV,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise StepFailed(r.stderr[-1500:])
    S["bundle"] = str(bundle)
    # Held-out test INPUTS only (x, ids; labels never leave the research owner side) for candidate inference at answer time.
    S["test_inputs"] = str(OUT / "D01-test-inputs.npz")
    t = subprocess.run(
        [
            RESEARCH_PY,
            "-c",
            "import sys,numpy as np\nfrom data import dataset\n"
            "x,y,i=dataset('iris')[0]['test']\nnp.savez(sys.argv[1],x=x,ids=i)",
            S["test_inputs"],
        ],
        cwd=DEV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if t.returncode != 0:
        raise StepFailed(t.stderr[-800:])
    return {"ids": {"bundle": str(bundle)}, "detail": r.stdout.strip()[-200:]}


@step("2b project create, task import, install generic components", "lab CLI")
def s2b():
    lab("project", "create", PROJECT, "--budget", "compute_seconds=20000", "--budget", "trials=200")
    imp = lab("task", "import", PROJECT, S["bundle"], "--python", RESEARCH_PY)
    S["import"] = imp
    dirs = lab("component", "builtins")
    refs = {}
    for cid in ("eidos.project.command", "eidos.task.score-classification"):
        d = next(p for p in dirs if p.endswith(cid))
        r = lab("component", "install", PROJECT, d, "--env", "default")
        if r.get("validation", "passed") != "passed":
            raise StepFailed(f"{cid} validation {r}")
        refs[cid] = r.get("component_ref")
    return {"ids": {"charter": imp["charter_id"], **imp["artifacts"], **refs}}


@step("2c FIXTURE investigation bundle-baseline-replicate to conclusion", "lab CLI")
def s2c():
    a = S["import"]["artifacts"]
    r = lab(
        "investigate",
        "start",
        PROJECT,
        "--question",
        "Reproduce the D01 baseline over three seeds (fixture policy)",
        "--charter",
        S["import"]["charter_id"],
        "--policy",
        "bundle-baseline-replicate",
        "--input",
        f"train={a['train']}",
        "--input",
        f"validation={a['validation']}",
        "--input",
        f"task={a['task']}",
        "--limit",
        "compute_seconds=6000",
        "--limit",
        "trials=24",
        "--limit",
        "model_tokens=200000",
        "--limit",
        "turns=30",
    )
    inv = r["result"]["investigation_id"]
    S["inv"] = inv

    def done():
        d = query("get_investigation", {"investigation_id": inv})
        return d if d["status"] in ("concluded", "stopped", "failed", "paused") else None

    d = wait_for(done, "D01 investigation to conclude", 900)
    if d["status"] != "concluded":
        raise StepFailed(f"investigation ended {d['status']}")
    jobs = d["jobs"]
    train = [j for j in jobs if (j["component"] or "").startswith("eidos.project.command")]
    score = [j for j in jobs if (j["component"] or "").startswith("eidos.task.score-classification")]
    if not (len(train) == 3 and len(score) == 3 and all(j["status"] == "succeeded" for j in train + score)):
        raise StepFailed(f"unexpected jobs: {[(j['component'], j['status']) for j in jobs]}")
    S["train_jobs"], S["score_jobs"] = [j["id"] for j in train], [j["id"] for j in score]
    acc = [query("get_job", {"job_id": j["id"]})["terminal"]["receipt"]["metrics"]["accuracy"] for j in score]
    return {
        "ids": {"investigation": inv, "train_jobs": S["train_jobs"], "score_jobs": S["score_jobs"]},
        "detail": f"{len(d['turns'])} turns; validation accuracy {acc}",
    }


@step("2d task answer export + official validate_answer.py", "lab CLI + research validate_answer.py")
def s2d():
    out = OUT / "D01-answer"
    r = lab(
        "task",
        "answer",
        PROJECT,
        "--task-id",
        "D01",
        "--run-id",
        "e2e-fixture-1",
        "--out-dir",
        str(out),
        "--python",
        RESEARCH_PY,
        "--test-inputs",
        S["test_inputs"],
        "--candidate-job",
        S["train_jobs"][0],
        "--status",
        "no_improvement_found",
        "--claim-type",
        "operational_reliability",
        "--claim",
        "baseline reproduces; fresh-process reload identical",
        "--scope",
        "D01 public development task, CPU, hosted Linux, three seeds",
        "--investigation",
        S["inv"],
        "--run-label",
        "fixture-provider run (deterministic script, no model)",
        "--validator",
        str(DEV / "validate_answer.py"),
    )
    v = subprocess.run(
        [RESEARCH_PY, str(DEV / "validate_answer.py"), str(out / "answer.json")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if v.returncode != 0:
        raise StepFailed("validate_answer.py: " + (v.stdout + v.stderr)[-800:])
    reload = json.loads((out / "reload_check.json").read_text())
    if not reload["behavior_equal"]:
        raise StepFailed(f"reload check not identical: {reload}")
    return {
        "ids": {"answer": str(out / "answer.json")},
        "detail": f"validator rc=0 ({v.stdout.strip()[-120:]}); reload n={reload['n']} mismatches={reload['mismatches']}; "
        f"export validator rc={r['validator']['returncode']}",
    }


SW = "sw1"
SW_ADAPTER = "eidos_adapter:ADAPTER"


@step(
    "3 switch-order project: train, checkpoint branch, fresh-process behaviour check, model export", "lab CLI"
)
def s3():
    sys.path.insert(0, str(REPO / "tests" / "journeys"))
    from staging import stage_component  # copies a component dir and drops binary-only fixtures

    lab("project", "create", SW, "--budget", "compute_seconds=20000", "--budget", "trials=500")
    lab(
        "project",
        "register-env",
        SW,
        "--python",
        RESEARCH_PY,
        "--root",
        str(REPO / "examples" / "switch-order-project"),
        "--adapter",
        SW_ADAPTER,
    )
    refs = {}
    for name in ("eidos.pytorch.train", "eidos.pytorch.behavior-check"):
        staged, _ = stage_component(REPO / "components" / name, OUT / "stage")
        r = lab("component", "install", SW, str(staged), "--env", "default")
        if r.get("validation", "passed") != "passed":
            raise StepFailed(f"{name}: {r}")
        refs[name] = r["component_ref"]
    train, behavior = refs["eidos.pytorch.train"], refs["eidos.pytorch.behavior-check"]
    base = {"adapter": SW_ADAPTER, "builder": "OrderedSwitchNet", "seed": 41}
    r = lab(
        "run",
        SW,
        train,
        "--config",
        json.dumps({**base, "steps": 60, "checkpoint_every": 30}),
        "--environment",
        "default",
        "--wait",
        "--wall-seconds",
        "600",
    )
    assert r["job"]["status"] == "succeeded", r["job"].get("terminal")
    parent = next(o["artifact_id"] for o in r["job"]["outputs"] if o["slot"] == "checkpoint")
    entries = r["job"]["terminal"]["receipt"]["checkpoints"]["checkpoint"]["state_entries"]
    # Branch from the committed final checkpoint (declared checkpoint slot), continuing training to step 90.
    b = lab(
        "checkpoint",
        "branch",
        SW,
        "--parent",
        parent,
        "--component",
        train,
        "--migration",
        json.dumps({k: "copy" for k in entries}),
        "--stream-policy",
        "paired",
        "--wall-seconds",
        "600",
        "--config",
        json.dumps({**base, "steps": 90}),
    )
    bjob = b["launch"]["result"]["job_id"]
    bj = lab("job", "wait", SW, bjob)
    assert bj["status"] == "succeeded", bj.get("terminal")
    assert bj["terminal"]["receipt"]["restore_class"] == "exact", bj["terminal"]["receipt"].get(
        "restore_class"
    )
    child = next(o["artifact_id"] for o in bj["outputs"] if o["slot"] == "checkpoint")
    cfg = json.dumps({"adapter": SW_ADAPTER, "builder": "OrderedSwitchNet"})
    good = lab(
        "run",
        SW,
        behavior,
        "--input",
        f"checkpoint={child}",
        "--config",
        cfg,
        "--environment",
        "default",
        "--wait",
        "--wall-seconds",
        "600",
    )
    assert good["job"]["terminal"]["receipt"]["behavior_match"] is True, good["job"]["terminal"]
    dest = OUT / "export-branch"
    ex = lab(
        "export", SW, "--checkpoint", child, "--behavior-job", good["result"]["job_id"], "--dest", str(dest)
    )
    assert (dest / "checkpoint.pt").exists() and (dest / "eidos-export.json").exists()
    bad = lab(
        "run",
        SW,
        behavior,
        "--input",
        f"checkpoint={child}",
        "--config",
        json.dumps({"adapter": SW_ADAPTER, "builder": "SwitchNet"}),
        "--environment",
        "default",
        "--wait",
        "--wall-seconds",
        "600",
    )
    assert bad["job"]["terminal"]["receipt"]["behavior_match"] is False
    refused = subprocess.run(
        [LAB, "export", SW, "--checkpoint", child, "--behavior-job", bad["result"]["job_id"]],
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0 and "behavioral check did not match" in refused.stdout + refused.stderr, (
        "export with a failing behaviour check must be refused",
        refused.stdout[-300:],
    )
    return {
        "ids": {
            "project": SW,
            "parent_checkpoint": parent,
            "branch_job": bjob,
            "branch_checkpoint": child,
            "behavior_job": good["result"]["job_id"],
            "export_id": ex["result"]["export_id"],
            "wrong_builder_job": bad["result"]["job_id"],
        },
        "detail": "branch restore_class=exact; behaviour check passes in a fresh process; export recorded and written; "
        "export with the wrong-builder check refused. NOT covered: qualified assessment (see docs)",
    }


@step(
    "4 MCP SDK client: tools, read D01, start investigation, disconnect, reconnect, read back, components",
    "MCP stdio (mcp.Client)",
)
def s4():
    from mcp import Client, StdioServerParameters

    env = dict(os.environ, EIDOS_HOME=HOME)
    env.pop("EIDOS_TOKEN", None)
    params = StdioServerParameters(command=LAB, args=["mcp"], env=env)
    res: dict = {}

    async def go():
        async with Client(params) as c1:
            tools = await c1.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert {"start_investigation", "get_investigation", "list_events", "list_components"} <= set(
                names
            ), names
            res["tools"] = len(names)
            got = await c1.call_tool("get_investigation", {"project": PROJECT, "investigation_id": S["inv"]})
            assert not got.is_error and got.structured_content["status"] == "concluded", (
                got.structured_content
            )
            ev = await c1.call_tool(
                "list_events", {"project": PROJECT, "investigation_id": S["inv"], "limit": 500}
            )
            assert not ev.is_error and len(ev.structured_content["events"]) > 10
            res["d01_events_via_mcp"] = len(ev.structured_content["events"])
            start = await c1.call_tool(
                "start_investigation",
                {
                    "project": PROJECT,
                    "command_key": "e2e-mcp-inv-1",
                    "question": "MCP-started fixture investigation",
                    "charter_id": S["import"]["charter_id"],
                    "environment": "default",
                    "limits": {"turns": 4},
                    "investigator": {"runtime": "fixture", "settings": {"policy": "conclude-immediately"}},
                },
            )
            assert not start.is_error, start.structured_content
            res["mcp_inv"] = start.structured_content["result"]["investigation_id"]
        # client closed: detached only. New process, new session.
        async with Client(params) as c2:

            async def state():
                g = await c2.call_tool(
                    "get_investigation", {"project": PROJECT, "investigation_id": res["mcp_inv"]}
                )
                assert not g.is_error, g.structured_content
                return g.structured_content

            end = time.monotonic() + 120
            while True:
                g = await state()
                if g["status"] == "concluded" or time.monotonic() > end:
                    break
                await asyncio.sleep(2)
            assert g["status"] == "concluded", g["status"]
            rr = await c2.read_resource(f"lab://projects/{PROJECT}/investigations/{res['mcp_inv']}")
            assert json.loads(rr.contents[0].text)["id"] == res["mcp_inv"]
            comps = await c2.call_tool("list_components", {"project": PROJECT})
            assert not comps.is_error and "eidos.project.command" in json.dumps(comps.structured_content)
            res["components_listed"] = True
            retry = await c2.call_tool(
                "start_investigation",
                {
                    "project": PROJECT,
                    "command_key": "e2e-mcp-inv-1",
                    "question": "MCP-started fixture investigation",
                    "charter_id": S["import"]["charter_id"],
                    "environment": "default",
                    "limits": {"turns": 4},
                    "investigator": {"runtime": "fixture", "settings": {"policy": "conclude-immediately"}},
                },
            )
            assert retry.structured_content["replayed"] is True, "lost-reply retry must replay the receipt"

    asyncio.run(go())
    # the CLI sees the investigation the MCP client started
    cli = query("get_investigation", {"investigation_id": res["mcp_inv"]})
    assert cli["status"] == "concluded"
    return {
        "ids": res,
        "detail": "second session read back the investigation started in the first; CLI agrees",
    }


@step("5 HTML report, status, stop", "lab CLI")
def s5():
    rep = lab("report", PROJECT, "--investigation", S["inv"], "--out-dir", str(OUT / "report"))["report"]
    html = Path(rep).read_text()
    for needle in (S["inv"], S["train_jobs"][0]):
        assert needle in html, f"report lacks {needle}"
    sw = lab("report", SW, "--out-dir", str(OUT / "report-sw1"))["report"]
    final = lab("doctor", "--json")
    bad = [c for c in final if c["status"] == "fail"]
    assert not bad, bad
    lab("stop")
    return {
        "ids": {"report": rep, "report_switch": sw},
        "detail": f"{len(html)} bytes; doctor clean before stop",
    }


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        for fn in (s1, s2a, s2b, s2c, s2d, s3, s4, s5):
            fn()
    finally:
        ok = bool(RECORD) and all(r["status"] == "pass" for r in RECORD) and len(RECORD) == 8
        (OUT / "journey-record.json").write_text(
            json.dumps({"ok": ok, "steps": RECORD}, indent=2, sort_keys=True)
        )
        print("\nJOURNEY", "PASS" if ok else "INCOMPLETE/FAIL")
