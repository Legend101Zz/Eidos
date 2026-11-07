#!/usr/bin/env python3
"""Local performance smoke against the design's proposed targets (declared workload below).

Workload: one CPU training job saturating a core (``eidos.pytorch.train``, long run) while the
client issues N cheap already-resolved commands (``record_exposure``) and N authorized queries;
then a short project command whose output visibility is timed from the job's publication to the
first successful client read. Targets (design §Local release targets): p95 command acceptance
<= 2 s; evidence visible <= 30 s after publication. Results are for THIS host only.

Usage: EIDOS_HOME=... python tests/ops/perf_smoke.py <train-project> <command-project> [N]
"""

import json
import statistics
import sys
import time

from eidos.client import LabClient
from eidos.settings import LabConfig


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def latest(c, project, cid):
    refs = [x["ref"] for x in c.query("search_components", {"text": cid, "limit": 50}, project=project)["components"]
            if x["ref"].split("@")[0] == cid]
    return max(refs, key=lambda r: tuple(int(v) for v in r.split("@")[1].split(".")))


def main():
    train_p, cmd_p = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    cfg = LabConfig.load()
    c = LabClient(cfg.api_url, cfg.token("admin"), client_name="perf-smoke")
    load = c.command("run_component", {"component_ref": latest(c, train_p, "eidos.pytorch.train"), "wall_seconds": 600,
                     "config": {"adapter": "eidos_adapter:ADAPTER", "builder": "OrderedSwitchNet", "steps": 400000, "seed": 7}},
                     project=train_p)["result"]["job_id"]
    while c.query("get_job", {"job_id": load}, project=train_p)["status"] != "running":
        time.sleep(0.5)
    time.sleep(3)
    cmd_lat, q_lat = [], []
    for i in range(n):
        t = time.perf_counter()
        c.command("record_exposure", {"subject": f"perf-{i}", "disclosed": {"i": i}}, project=cmd_p)
        cmd_lat.append(time.perf_counter() - t)
        t = time.perf_counter()
        c.query("list_jobs", {}, project=cmd_p)
        q_lat.append(time.perf_counter() - t)
    ref = latest(c, cmd_p, "eidos.project.command")
    j = c.command("run_component", {"component_ref": ref, "wall_seconds": 120, "config": {
        "argv": ["python", "-c", "import os; os.makedirs('{out}', exist_ok=True); open('{out}/x.txt','w').write('1')"]}},
        project=cmd_p)["result"]["job_id"]
    admitted = time.time()
    seen = None
    while seen is None and time.time() - admitted < 120:
        job = c.query("get_job", {"job_id": j}, project=cmd_p)
        if job["outputs"]:
            seen = time.time()
            published = min(time.mktime(time.strptime(o["created_at"][:19], "%Y-%m-%d %H:%M:%S")) for o in job["outputs"])
        time.sleep(0.2)
    c.command("cancel_run", {"job_id": load}, project=train_p)
    res = {
        "host_note": "hosted Linux build sandbox (4 vCPU); one training job running during measurement",
        "commands": {"n": n, "p50_s": statistics.median(cmd_lat), "p95_s": pct(cmd_lat, 0.95), "max_s": max(cmd_lat),
                     "target_p95_s": 2.0},
        "queries": {"n": n, "p50_s": statistics.median(q_lat), "p95_s": pct(q_lat, 0.95)},
        "evidence_visibility": {"admission_to_visible_s": round(seen - admitted, 2) if seen else None,
                                "publication_to_visible_s_upper_bound": round(seen - published + 1, 2) if seen else None,
                                "target_s": 30},
    }
    res["commands"]["meets_target"] = res["commands"]["p95_s"] <= 2.0
    res["evidence_visibility"]["meets_target"] = bool(seen) and res["evidence_visibility"]["publication_to_visible_s_upper_bound"] <= 30
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
