# Troubleshooting

Entries come from failures actually seen while building Eidos. Each gives symptom, cause, fix or check, and the evidence commit (`git show <sha>`). Entries marked **no repo evidence** are general guidance, not something this build observed.

## Installing

### A "fresh" install runs old code
* **Symptom:** a lab or test built from just-built wheels behaves like an earlier version; journey runs test stale code.
* **Cause:** `uv pip install --find-links dist eidos-lab` installed a cached wheel of the same version instead of the one just built.
* **Fix:** install built wheels with `--no-cache` (`scripts/ci/build_and_install.sh` does); with an in-place upgrade also pass `--reinstall`. Verify with `python -c "import eidos; print(eidos.__file__, eidos.__version__)"`.
* **Evidence:** `12a6c01`, `57dd5bd` (macOS kit uses `--no-cache`).

### `lab init` fails downloading Temporal, or CI setup fails on release assets (HTTP 504)
* **Symptom:** `lab init` or the CI setup step dies while fetching the pinned Temporal archive (or `uv`) from a GitHub release; three of nine CI jobs failed this way at `57dd5bd`. Infrastructure, not code.
* **Cause:** transient 5xx from GitHub release assets.
* **Fix:** `lab init` now retries with backoff. Avoid the network: pass `--temporal-source DIR` (a directory holding `temporal-server` and `temporal-sql-tool`), or set `EIDOS_TEMPORAL_ARCHIVE_CACHE=DIR` to a directory containing `temporal-server-<pinned sha256>.tgz` (verified against the pinned digest before use). Re-run the job if it persists.
* **Evidence:** `3a96f64` (retry, archive cache, CI cache and PyPI `uv` fallback); status note in `IMPLEMENTATION_STATUS.md`.

### Temporal ports collide with another lab
* **Symptom:** `lab start` fails or Temporal does not become healthy when two labs share a host.
* **Cause:** Temporal uses a compact block of 9 consecutive ports starting at `--temporal-port` (gRPC, membership and HTTP for four services). Another lab's block overlaps.
* **Fix:** give each lab a disjoint 9-port range (for example 7633 and 7733), a distinct `--api-port` and `--pg-port`. The config file is written when the lab is initialised or restored; a lab created earlier keeps what was written then, so changing ports means re-initialising or editing `services/temporal/config/eidos.yaml` and `lab.json` together (the code does not rewrite the file at start; this reading is inferred from `supervisor/services.py`).
* **Evidence:** `2cd4f95`, `docs/decisions/0002-regular-temporal-server.md`, `docs/ops/SANDBOX_CAPABILITIES.md`.

## Storage

### I/O errors, PostgreSQL will not start, or artifacts fail verification on an external SSD
* **Symptom:** `pg_ctl` or `lab doctor` errors mentioning I/O, permissions or `fsync`; artifact writes fail; disk-pressure refusals (`DISK_PRESSURE`).
* **Cause, generally:** a filesystem that cannot hold PostgreSQL (exFAT/FAT, network or removable volumes mounted without POSIX permissions), the volume unmounted or sleeping, or free space below `min_free_bytes`. **No repo evidence from the Mac session yet**; if the owner's session shows a specific failure, add it here.
* **Fix/check:** keep `EIDOS_HOME` on APFS or HFS+ (`docs/ops/STORAGE_PATHS.md`); `lab stop` before unmounting; `df` on the volume; `lab doctor` (disk and secret-permissions checks); `lab logs postgres`. New bytes are refused below `min_free_bytes` while committed evidence stays readable.
* **Evidence:** none for the SSD case; disk-pressure behaviour is in `docs/ops/INSTALL_AND_OPERATE.md`.

### The sandbox or host rebooted, or PostgreSQL was killed
* **Symptom:** after a reboot `lab status` shows services stopped, `lab start` complains about PostgreSQL, or jobs are "lost".
* **Cause:** processes died; a stale `postmaster.pid` may remain after a hard kill. **No repo evidence of a specific incident**; behaviour below is by design.
* **Fix/check:** `lab start` brings the services back; the reconciler re-dispatches admitted work without a live workflow, and the runner reattaches to surviving research processes by recorded handle (pid, start time, boot id) or reconciles them as lost and starts a new fenced attempt. If PostgreSQL will not start, read `lab logs postgres`; PostgreSQL itself recovers its WAL on start. Never delete `services/pg/data`. If the cluster is unrecoverable, `lab restore` from the last `lab backup`.
* **Evidence:** `docs/ops/INSTALL_AND_OPERATE.md` recovery behaviour; worker-kill recovery `37ebd36`, `2709265`.

## Running jobs

### Sealed profile: jobs cannot run the project interpreter, or `separate-identity` reports failed
* **Symptom:** under `linux:bwrap-sealed` as root with the `eidos-cand` user, `lab doctor`/probes show `separate-identity` failed or the job cannot execute Python.
* **Cause:** the candidate OS user cannot read a root-owned interpreter (for example under `/root`). Namespace isolation still holds; the identity control reports truthfully that it failed.
* **Fix:** put the project interpreter where the candidate can read and execute it (for example under `/opt/eidos`, world-readable).
* **Evidence:** `35e87ad`, `9a54934`, `docs/decisions/0004-sealed-profile-identity.md`.

### Sealed profile: a job cannot import extension code from the checkout (`No module named ...`)
* **Symptom:** a run with `python_path` pointing into a repository directory fails with `ModuleNotFoundError` only since the sealed profile became the default.
* **Cause:** sealed jobs see only their own mounts; arbitrary host paths, including the lab home and your checkout, are invisible.
* **Fix:** use a published extension bound as `extension.source/1`, or for development request `dev:same-user` explicitly with an `isolation_waiver` reason (the waiver is recorded in the lock and the assessment). The same waiver is needed in evidence plans (`required_isolation.waiver`).
* **Evidence:** `fa6a3c5`, `docs/dev-runs/switch-journey.md`.

### Sealed profile: checkpoint-and-stop (SIGUSR1) does nothing
* **Symptom:** `lab stop --running-jobs ...` or a cancel-with-checkpoint under bwrap does not produce a checkpoint.
* **Cause:** SIGUSR1 cannot cross bwrap's session and PID namespace.
* **Fix:** fixed in current code: the runner also drops `scratch/.eidos-stop-requested` and the worker SDK polls it. Upgrade if you are on v0.4.0 to v0.5.1.
* **Evidence:** `1a1c621`, `db675bf`.

### Jobs queue behind one long sealed training job
* **Symptom:** with the sealed profile as default, every other job waits while a long job runs.
* **Cause:** the sealed runner used one slot.
* **Fix:** it now runs 2 concurrent attempts by default; set `extra.sealed_runner_slots` in `config/lab.json` (falls back to `extra.runner_slots`) and restart.
* **Evidence:** `cabbecd`.

### A worker was killed and the turn seems stuck for minutes
* **Symptom:** an investigator-worker was SIGKILLed; the turn stays open.
* **Cause:** previously the activity waited for the 600 s start-to-close timeout.
* **Fix:** turn activities heartbeat and the turn workflow uses a 30 s heartbeat timeout; recovery takes about 30 s and the supervisor restarts the worker.
* **Evidence:** `37ebd36`; journey step 7 in `docs/dev-runs/switch-journey.md`.

## Investigations

### An investigation looks stuck
Check in this order:
1. `lab investigate show PROJECT INV`: if the status text says **needs operator input (repeated refused proposals)**, the investigator's proposals were refused three times; the coordinator stops investigating on its own and wakes only on operator news (a `steer`, `pause`/`resume`, or a new job result). Read the latest turn's refusal reasons in `lab investigate events`, then `lab investigate steer` with a corrected instruction or `stop` it.
2. If the last turn ended `waiting` and nothing is running (`lab status` job counts), the lab is on a **quiet idle poll**: since idle polls spend no model turn, a turn starts only on a wake, on news since the last turn's horizon (job, evidence and assessment events), or after `max_idle_turn_seconds` (6 hours). A `steer_investigation` wakes it at once.
3. A decision that is only `wait_for_jobs` with nothing outstanding (no non-terminal job, no executing evidence plan, no launching call in the same decision) is refused and fed back within the turn, so it should not park; if you see this pattern in an older lab, upgrade.
4. `lab logs workflow-worker` and `lab logs investigator-worker`; confirm both services are running in `lab status`.
* **Evidence:** `04e232f` (idle poll needs news), `08f9e01` and `docs/decisions/0007-refused-proposals-never-park.md` (refused proposals, "needs operator input"), `63f7cac`, `64ad288`, `2fedb42` (waits with nothing outstanding; the fixture policy stall).

## Upgrading and operating

### After `lab upgrade` the services run old code
* **Symptom:** version in `lab status` or behaviour did not change.
* **Cause:** `lab upgrade` restarts the services on the installation that *ran* the upgrade. Running it from the old venv's `lab` upgrades nothing.
* **Fix:** install the new wheels, then run the **new** installation's `lab upgrade`; check the reported `lab_python` before/after. See the upgrade section of `docs/ops/INSTALL_AND_OPERATE.md`.
* **Evidence:** `556fe66`, `scripts/ci/upgrade_across_releases.sh`.

## Development and CI

### PostgreSQL tests: `EIDOS_TEST_PG_ADMIN` is set but the tests fail (or used to skip silently)
* **Symptom:** `tests/integration` errors at fixture setup with a connection failure.
* **Cause:** `EIDOS_TEST_PG_ADMIN` (a SQLAlchemy URL with a superuser, for example `postgresql+psycopg://postgres:devpass@127.0.0.1:5432/postgres`) points at a server that is down or has the wrong password. Previously such tests were skipped, hiding the problem; now an explicitly configured but unusable server is a failure. Unset the variable to get the default behaviour documented in `tests/conftest.py`.
* **Fix:** start the server or correct the URL; run the integration suite with the variable set as in `IMPLEMENTATION_STATUS.md`.
* **Evidence:** `181e50d`.
