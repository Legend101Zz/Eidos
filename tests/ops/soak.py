"""Soak (Linux): many small jobs and fixture investigations for N minutes against a running lab, sampling
resource use over time. Records per-minute: open fds and RSS of every lab service, PostgreSQL data size,
event horizon, max open-workflow history length (temporal CLI, if given) and job submit->visible-terminal
latency. Pass/fail: no service restarts, every job terminal, fd and RSS growth bounded (last vs first
quartile median, reported; flagged if >2x), latency p95 reported.
Usage: EIDOS_HOME=... python tests/ops/soak.py <project-with-eidos.project.command> <minutes> <out.json>
       [--temporal-cli PATH --temporal-address HOST:PORT]"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from eidos.client import LabClient
from eidos.settings import LabConfig


def proc_stats(pid: int) -> dict:
    try:
        fds = len(os.listdir(f"/proc/{pid}/fd"))
        rss = next(int(x.split()[1]) for x in Path(f"/proc/{pid}/status").read_text().splitlines() if x.startswith("VmRSS"))
        return {"fds": fds, "rss_kb": rss}
    except (OSError, StopIteration):
        return {}


def du(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> int:
    project, minutes, out = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3])
    tcli = sys.argv[sys.argv.index("--temporal-cli") + 1] if "--temporal-cli" in sys.argv else None
    taddr = sys.argv[sys.argv.index("--temporal-address") + 1] if "--temporal-address" in sys.argv else None
    cfg = LabConfig.load()
    c = LabClient(cfg.api_url, cfg.token("admin"), client_name="soak")
    refs = [x["ref"] for x in c.query("search_components", {"text": "eidos.project.command", "limit": 50},
                                      project=project)["components"] if x["ref"].startswith("eidos.project.command@")]
    ref = max(refs, key=lambda r: tuple(int(v) for v in r.split("@")[1].split(".")))
    status0 = json.loads((cfg.root / "run" / "status.json").read_text())
    restarts0 = {k: v.get("restarts", 0) for k, v in status0["services"].items()}
    latencies: list[float] = []
    jobs: list[str] = []
    invs: list[str] = []
    stop = threading.Event()

    def submit_loop():
        i = 0
        while not stop.is_set():
            i += 1
            t0 = time.time()
            j = c.command("run_component", {"component_ref": ref, "wall_seconds": 120, "config": {
                "argv": ["python", "-c", f"import os; os.makedirs('{{out}}', exist_ok=True); open('{{out}}/x.txt','w').write('{i}')"]}},
                project=project)["result"]["job_id"]
            jobs.append(j)
            while not stop.is_set() and time.time() - t0 < 120:
                if c.query("get_job", {"job_id": j, "include_lock": False}, project=project)["status"] in (
                        "succeeded", "failed", "cancelled"):
                    latencies.append(time.time() - t0)
                    break
                time.sleep(0.25)
            if i % 10 == 0:  # a fixture investigation every ten jobs (one bounded turn, no model)
                inv = c.command("start_investigation", {"question": "soak", "charter_id": None, "limits": {"turns": 2},
                                "investigator": {"runtime": "fixture", "settings": {"policy": "conclude-immediately"}}},
                                project=project)["result"]["investigation_id"]
                invs.append(inv)
            time.sleep(1)

    th = threading.Thread(target=submit_loop, daemon=True)
    th.start()
    samples = []
    end = time.time() + minutes * 60
    while time.time() < end:
        st = json.loads((cfg.root / "run" / "status.json").read_text())
        svc = {k: proc_stats(v["pid"]) for k, v in st["services"].items() if v.get("pid")}
        hist = None
        if tcli and taddr:
            r = subprocess.run([tcli, "workflow", "list", "--address", taddr, "--query", "ExecutionStatus='Running'",
                                "-o", "json", "--limit", "200"], capture_output=True, text=True)
            try:
                hist = max((int(w.get("historyLength", 0)) for w in json.loads(r.stdout or "[]")), default=0)
            except ValueError:
                hist = None
        samples.append({"t": round(time.time(), 1), "services": svc, "pg_bytes": du(cfg.root / "services" / "pg"),
                        "horizon": c.query("project_summary", {}, project=project)["horizon"],
                        "max_open_history": hist, "jobs_submitted": len(jobs), "latency_n": len(latencies)})
        time.sleep(60)
    stop.set()
    th.join(timeout=150)
    final = {j: c.query("get_job", {"job_id": j, "include_lock": False}, project=project)["status"] for j in jobs}
    st = json.loads((cfg.root / "run" / "status.json").read_text())
    restarts = {k: v.get("restarts", 0) - restarts0.get(k, 0) for k, v in st["services"].items()}

    def growth(key):
        out = {}
        q = max(1, len(samples) // 4)
        for name in samples[0]["services"]:
            a = [s["services"].get(name, {}).get(key) for s in samples[:q]]
            b = [s["services"].get(name, {}).get(key) for s in samples[-q:]]
            a, b = [x for x in a if x], [x for x in b if x]
            if a and b:
                out[name] = round(statistics.median(b) / statistics.median(a), 2)
        return out

    lat = sorted(latencies)
    res = {"minutes": minutes, "jobs": len(jobs), "investigations": len(invs),
           "job_status_counts": {s: list(final.values()).count(s) for s in set(final.values())},
           "service_restarts": restarts, "fd_growth_ratio": growth("fds"), "rss_growth_ratio": growth("rss_kb"),
           "pg_bytes_first_last": [samples[0]["pg_bytes"], samples[-1]["pg_bytes"]],
           "max_open_history_last": samples[-1]["max_open_history"],
           "submit_to_terminal_s": {"p50": lat[len(lat) // 2] if lat else None,
                                    "p95": lat[int(len(lat) * 0.95)] if lat else None, "n": len(lat)},
           "samples": samples}
    bad = [k for k, v in res["fd_growth_ratio"].items() if v > 2] + [k for k, v in res["rss_growth_ratio"].items() if v > 2]
    res["ok"] = (not any(restarts.values())) and set(final.values()) <= {"succeeded"} and not bad
    res["flags"] = bad
    out.write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if k != "samples"}, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
