# Performance smoke, 2026-11-07 (hosted Linux build sandbox, 4 vCPU)

Command: `EIDOS_HOME=... python tests/ops/perf_smoke.py switchdev d02dev 200`

**Workload:** one `eidos.pytorch.train` job saturating a core. During it, the client issued 200 `record_exposure` commands and 200 `list_jobs` queries. A short project-command job was then timed until its output was visible.

| Measure | Result | Design target |
|---|---|---|
| Command acceptance p50 / p95 / max | 5.1 ms / 6.4 ms / 82 ms | p95 ≤ 2 s |
| Query p50 / p95 | 5.9 ms / 7.3 ms | n/a |
| Evidence visibility after publication | ≤ 1.3 s (upper bound; 0.42 s from admission) | ≤ 30 s |

These numbers apply to this host and workload only. This is not a reference-machine measurement, and multi-day load has not been exercised.
