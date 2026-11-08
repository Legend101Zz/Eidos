#!/usr/bin/env python3
"""Seed / snapshot / verify helper for scripts/ci/upgrade_across_releases.sh (standard library only).

  check.py seed     create real data with the installed (old) `lab`; write seed.json
  check.py snapshot fingerprint the ledger straight from PostgreSQL; write snapshot.json
  check.py verify   with the upgraded `lab`: compare against seed.json + snapshot.json, write result.json

Env: LAB_BIN, EIDOS_HOME, PG_BIN, WORK (scratch dir), ECHO_DIR (component dir), OLD_TAG, OUT (result dir).
Seeding uses only commands that exist in eidos-v0.1.0 (`lab project create/register-env`, `lab component install`,
`lab run`, `lab investigate start`, `lab report`). The investigations use FIXTURE policies (`conclude-immediately`,
`note-then-wait`): deterministic scripts, no model is involved. A checkpoint branch needs a trained checkpoint
(torch), which is out of scope for a few-minute job; it is recorded as not created.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The newest migration in this checkout (the head the upgrade must reach).
HEAD_REVISION = max(f.name.split("_")[0] for f in (Path(__file__).resolve().parents[3] / "src/eidos/persistence/migrations/versions")
                    .glob("[0-9]*_*.py"))

LAB = os.environ["LAB_BIN"]
HOME = Path(os.environ["EIDOS_HOME"])
OUT = Path(os.environ["OUT"])
WORK = Path(os.environ.get("WORK", OUT))
PROJECT = "upg1"
OUT.mkdir(parents=True, exist_ok=True)


def lab(*args: str, check: bool = True) -> dict | list | str:
    r = subprocess.run([LAB, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(
            f"lab {' '.join(args)} failed ({r.returncode}):\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
        )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        if check and r.stdout.strip() and not r.stdout.lstrip().startswith(("{", "[")):
            return r.stdout
        # trailing non-JSON chatter: take the first JSON document
        dec = json.JSONDecoder()
        s = r.stdout.lstrip()
        try:
            return dec.raw_decode(s)[0]
        except json.JSONDecodeError:
            return r.stdout


def query(name: str, params: dict | None = None):
    return lab("query", name, "-p", PROJECT, "--params", json.dumps(params or {}))


def psql(sql: str) -> list[list[str]]:
    secret = (HOME / "config" / "secrets" / "pg-superuser").read_text().strip()
    cfg = json.loads((HOME / "config" / "lab.json").read_text())
    r = subprocess.run(
        [
            f"{os.environ['PG_BIN']}/psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(cfg["pg_port"]),
            "-U",
            "postgres",
            "-d",
            cfg["db_name"],
            "-At",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        env={**os.environ, "PGPASSWORD": secret},
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"psql failed: {r.stderr[-800:]}")
    return [line.split("\t") for line in r.stdout.splitlines() if line]


def wait_for(fn, what: str, timeout: float = 90):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {what} (last={last!r})")


def inv_state(inv: str) -> dict:
    d = query("get_investigation", {"investigation_id": inv})
    return {
        "status": d["status"],
        "turns": [[t["id"], t["status"], t.get("stop_reason")] for t in d["turns"]],
    }


# ---------------------------------------------------------------------------------------------------------------------
def seed() -> None:
    echo = os.environ["ECHO_DIR"]
    py = os.environ["LAB_PYTHON"]
    lab("project", "create", PROJECT)
    lab("project", "register-env", PROJECT, "--python", py, "--root", echo)
    comp = lab("component", "install", PROJECT, echo, "--env", "lab")
    run = lab("run", PROJECT, "test.echo@1.0.0", "--config", '{"value": 7}', "--environment", "lab", "--wait")
    job = run["result"]["job_id"]
    assert run["job"]["status"] == "succeeded", run["job"]
    out_art = next(a["artifact_id"] for a in run["job"]["outputs"] if a["slot"] == "report")
    concluded = lab(
        "investigate",
        "start",
        PROJECT,
        "--question",
        "fixture: conclude immediately",
        "--policy",
        "conclude-immediately",
        "--environment",
        "lab",
    )["result"]["investigation_id"]
    open_inv = lab(
        "investigate",
        "start",
        PROJECT,
        "--question",
        "fixture: note then wait (left open across the upgrade)",
        "--policy",
        "note-then-wait",
        "--environment",
        "lab",
    )["result"]["investigation_id"]
    wait_for(lambda: inv_state(concluded)["status"] == "concluded", "concluded investigation")
    wait_for(
        lambda: any(t[1] == "completed" for t in inv_state(open_inv)["turns"]),
        "open investigation first turn",
    )
    time.sleep(2)  # let outbox/relay settle so the snapshot is quiescent
    report = lab("report", PROJECT, "--out-dir", str(OUT / "report-old"))["report"]
    dl = WORK / "echo-output.json"
    lab("artifact", "download", PROJECT, out_art, str(dl))
    seed_doc = {
        "old_tag": os.environ.get("OLD_TAG"),
        "project": PROJECT,
        "component": comp.get("component_ref"),
        "job_id": job,
        "job_output_artifact": out_art,
        "job_output_content": json.loads(dl.read_text()),
        "job_receipt": run["job"]["terminal"]["receipt"],
        "investigation_concluded": concluded,
        "investigation_open": open_inv,
        "open_state": inv_state(open_inv),
        "report": report,
        "checkpoint_branch": "not created: needs a trained torch checkpoint; out of scope for the CI job",
    }
    (OUT / "seed.json").write_text(json.dumps(seed_doc, indent=2, sort_keys=True))
    print(json.dumps(seed_doc, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------------------------------------------------
TABLE_COUNT = (
    "events",
    "records",
    "jobs",
    "artifacts",
    "commands",
    "projects",
    "turns",
    "decisions",
    "budget_entries",
)
FPR = {
    "events": "SELECT md5(string_agg(concat_ws('|',seq,project_id,investigation_id,type,subject,body::text,command_id,created_at),"
    "'~' ORDER BY seq)) FROM ledger.events WHERE seq <= {w}",
    "records": "SELECT md5(string_agg(concat_ws('|',kind,id,revision,project_id,digest,created_by,created_at),'~' "
    "ORDER BY kind,id,revision)) FROM ledger.records WHERE project_id='" + PROJECT + "'",
    "jobs": "SELECT md5(string_agg(concat_ws('|',id,kind,status,epoch,lock_digest,terminal::text,created_at),'~' ORDER BY id)) "
    "FROM ledger.jobs WHERE project_id='" + PROJECT + "'",
    "artifacts": "SELECT md5(string_agg(concat_ws('|',id,digest,size,schema,role,producer_job),'~' ORDER BY id)) "
    "FROM ledger.artifacts WHERE project_id='" + PROJECT + "'",
}


def snapshot(path: Path) -> dict:
    snap: dict = {"counts": {t: int(psql(f"SELECT count(*) FROM ledger.{t}")[0][0]) for t in TABLE_COUNT}}
    n, lo, hi = psql("SELECT count(*), COALESCE(min(seq),0), COALESCE(max(seq),0) FROM ledger.events")[0]
    snap["events"] = {"count": int(n), "min": int(lo), "max": int(hi)}
    snap["revision"] = psql("SELECT version_num FROM ledger.alembic_version")[0][0]
    snap["fingerprints"] = {k: psql(q.format(w=int(hi)))[0][0] for k, q in FPR.items()}
    snap["watermark"] = int(hi)
    path.write_text(json.dumps(snap, indent=2, sort_keys=True))
    return snap


# ---------------------------------------------------------------------------------------------------------------------
class Checks:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"check": name, "status": "pass" if ok else "FAIL", "detail": detail})
        print(f"[{'pass' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)
        return ok


def all_events() -> list[dict]:
    events, cursor = [], 0
    while True:
        page = query("list_events", {"cursor": cursor, "limit": 500})
        events += page["events"]
        if not page.get("more"):
            return events
        cursor = page["next_cursor"]


def verify() -> None:
    seed_doc = json.loads((OUT / "seed.json").read_text())
    old = json.loads((OUT / "snapshot.json").read_text())
    ck = Checks()
    w = old["watermark"]
    new = snapshot(OUT / "snapshot-after.json")

    ck("ledger migrated to head", new["revision"] == HEAD_REVISION, f"{old['revision']} -> {new['revision']}")
    ck(
        "event_counter in step with events",
        psql("SELECT last_seq FROM ledger.event_counter")[0][0] == str(new["events"]["max"]),
        f"last_seq={psql('SELECT last_seq FROM ledger.event_counter')[0][0]} max(seq)={new['events']['max']}",
    )
    for t, n in old["counts"].items():
        ck(f"row count not reduced: ledger.{t}", new["counts"][t] >= n, f"{n} -> {new['counts'][t]}")
    for k in FPR:  # taken before any new activity, so even records/jobs/artifacts must be unchanged
        ck(
            f"pre-upgrade {k} byte-identical",
            new["fingerprints"][k] == old["fingerprints"][k],
            old["fingerprints"][k],
        )
    events = all_events()
    seqs = [e["seq"] for e in events]
    ck(
        "events readable via CLI, strictly increasing, gap-free",
        seqs == list(range(seqs[0], seqs[0] + len(seqs))) and len(seqs) == new["events"]["count"],
        f"{len(seqs)} events, seq {seqs[0]}..{seqs[-1]}",
    )

    # records created before the upgrade, read through the new CLI
    job = query("get_job", {"job_id": seed_doc["job_id"]})
    ck(
        "finished job still visible",
        job["status"] == "succeeded" and job["terminal"]["receipt"]["echoed"] is True,
        f"{job['id']} {job['status']}",
    )
    ck(
        "job receipt unchanged",
        {k: v for k, v in job["terminal"]["receipt"].items() if k != "wall_seconds"}
        == {k: v for k, v in seed_doc["job_receipt"].items() if k != "wall_seconds"},
    )
    dl = WORK / "echo-output-after.json"
    lab("artifact", "download", PROJECT, seed_doc["job_output_artifact"], str(dl))
    ck("job output artifact bytes identical", json.loads(dl.read_text()) == seed_doc["job_output_content"])
    ck("old job listed", any(j["id"] == seed_doc["job_id"] for j in query("list_jobs")))
    comps = lab("component", "list", PROJECT)
    ck("installed component listed", "test.echo@1.0.0" in json.dumps(comps))
    done = query("get_investigation", {"investigation_id": seed_doc["investigation_concluded"]})
    ck("concluded investigation readable and concluded", done["status"] == "concluded", done["status"])
    ck(
        "concluded investigation events present",
        any(e["investigation_id"] == done["id"] and e["type"] == "turn.completed" for e in events),
    )
    open_inv = seed_doc["investigation_open"]
    ck(
        "open investigation still listed with its turn history",
        inv_state(open_inv)["turns"] == seed_doc["open_state"]["turns"]
        and inv_state(open_inv)["status"] == "active",
        json.dumps(inv_state(open_inv)),
    )

    # the upgraded lab keeps working: a new job, and the open investigation's workflow reacts on new workers
    run = lab("run", PROJECT, "test.echo@1.0.0", "--config", '{"value": 9}', "--environment", "lab", "--wait")
    ck(
        "new job runs to success on upgraded lab",
        run["job"]["status"] == "succeeded",
        run["result"]["job_id"],
    )
    lab("investigate", "steer", PROJECT, open_inv, "continue after the upgrade")
    wait_for(
        lambda: len(inv_state(open_inv)["turns"]) >= 2, "second turn of the open investigation", timeout=120
    )
    turns = inv_state(open_inv)["turns"]
    ck(
        "open investigation's workflow continues on new workers (steer -> new turn)",
        len(turns) >= 2,
        json.dumps(turns),
    )
    lab("investigate", "stop", PROJECT, open_inv)
    ck(
        "open investigation can be stopped",
        wait_for(lambda: inv_state(open_inv)["status"] == "stopped", "stopped", 30),
    )

    after = snapshot(OUT / "snapshot-after.json")
    ck(
        "pre-upgrade events still byte-identical after new activity",
        psql(FPR["events"].format(w=w))[0][0] == old["fingerprints"]["events"],
    )
    ck(
        "new events continue the sequence",
        after["events"]["max"] > w
        and after["events"]["count"] == after["events"]["max"] - after["events"]["min"] + 1,
        f"watermark {w} -> {after['events']['max']}",
    )
    rep = lab("report", PROJECT, "--out-dir", str(OUT / "report-new"))["report"]
    html, html_old = Path(rep).read_text(), Path(seed_doc["report"]).read_text()
    ck(
        "report generates on upgraded lab and names old data",
        all(
            s in html
            for s in (
                PROJECT,
                seed_doc["job_id"],
                seed_doc["investigation_concluded"],
                seed_doc["investigation_open"],
            )
        ),
        f"{len(html)} bytes (old report {len(html_old)} bytes)",
    )
    ck(
        "old report referenced the same records",
        all(s in html_old for s in (seed_doc["job_id"], seed_doc["investigation_concluded"])),
    )
    r = subprocess.run([LAB, "doctor", "--json"], capture_output=True, text=True)
    doctor = json.loads(r.stdout)
    (OUT / "doctor-after.json").write_text(json.dumps(doctor, indent=2))
    bad = [c for c in doctor if c["status"] == "fail"]
    ck(
        "lab doctor passes (no fail)",
        r.returncode == 0 and not bad,
        "; ".join(f"{c['check']}: {c['detail']}" for c in bad),
    )
    ck(
        "doctor reports migration at head",
        any(c["check"] == "ledger-migration" and HEAD_REVISION in c["detail"] for c in doctor),
    )

    result = {
        "old_tag": seed_doc["old_tag"],
        "ok": all(c["status"] == "pass" for c in ck.rows),
        "checks": ck.rows,
        "before": old,
        "after": after,
        "seed": seed_doc,
    }
    (OUT / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(
            "upgrade verification FAILED: " + ", ".join(c["check"] for c in ck.rows if c["status"] != "pass")
        )
    print(f"ALL {len(ck.rows)} CHECKS PASSED")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "seed":
        seed()
    elif cmd == "snapshot":
        print(json.dumps(snapshot(OUT / "snapshot.json"), indent=2, sort_keys=True))
    elif cmd == "verify":
        verify()
    else:
        raise SystemExit(__doc__)
