# Installing and operating Eidos (local v1)

Eidos runs entirely on your own machine. It does not need a cloud account, an OCI registry, Kubernetes or Managed Agents. The model endpoint can be local or hosted. A hosted LLM still receives the authorized context it is sent.

## Supported profiles

| Profile | Status |
|---|---|
| Linux x86_64, CPU | Runtime-verified in the hosted build sandbox (Ubuntu 24.04, Python 3.12, PostgreSQL 16, Temporal 1.32.2). That is a build environment, not a researcher machine. |
| `linux:bwrap-sealed` isolation | Runtime-verified with hostile probes. When the lab runs as root with the `eidos-cand` user, candidate code runs as that distinct OS identity if it can execute the project interpreter. Provision sealed-evaluation environments under a world-readable prefix (e.g. `UV_PYTHON_INSTALL_DIR=/opt/eidos/python uv python install 3.12`, venv under `/opt/eidos/envs/`, `chmod -R a+rX`); `separate-identity` then passes (verified). |
| macOS (arm64/x86_64) host-native `dev:same-user` | **Unqualified on a researcher machine.** GitHub-hosted macOS arm64 runner evidence only: wheel install, `lab init/start/doctor/project/status/stop`, worker SDK tests, research smoke (`.github/workflows/ci.yml`, job `macos-arm64-smoke`). Owner kit: `docs/ops/MACOS_OWNER_QUALIFICATION.md`. |
| macOS `macos:seatbelt-sealed` (candidate code) | **Implemented; GitHub-runner evidence only.** `sandbox-exec` with a generated Seatbelt profile: the lab home and common credential directories are unreadable (except the attempt's own files), writes go only to staging/scratch/resume and a private tmp, no network, cleared environment. Same OS user, so `separate-identity` fails; mach IPC (e.g. the Keychain via securityd) is not filtered. Apple deprecates `sandbox-exec`; every run's receipt records the probe results, so a broken control shows up as `failed`, not assumed. CI runs the hostile probes plus a same-user planted-effect control (`scripts/ci/seatbelt_probes.py`). The macOS assessor process still runs `dev:same-user`. |
| macOS `macos:lima-vm-sealed` | **Not implemented.** Needed only where a plan requires `separate-identity` or VM-level isolation on macOS. |
| CUDA / MPS devices | Discovered and reported. **Unqualified.** Exact restore across devices is never claimed. |
| Windows / WSL | Not supported in v1. |

## Prerequisites

- Python 3.11–3.13 for the lab. The research projects keep their own interpreters.
- PostgreSQL ≥ 14 server binaries, found on PATH, under `/usr/lib/postgresql/*/bin`, or via Homebrew `postgresql@*`. Eidos creates and owns a private cluster under the lab directory. It does not use your existing databases.
- Network access once, to download the pinned Temporal Server release (sha256-verified). Alternatively, pass `--temporal-source DIR` pointing at existing `temporal-server`/`temporal-sql-tool` binaries.
- Optional: `bubblewrap` (Linux) for the sealed evaluation profile. On macOS the sealed candidate profile uses the system `/usr/bin/sandbox-exec`.

## Install

```bash
uv tool install eidos-lab          # or: pip install eidos-lab   (from built wheels: --find-links dist/)
lab init                           # default lab root ~/.eidos (or --home / $EIDOS_HOME)
lab start                          # supervisor: PostgreSQL, Temporal (regular server), API, workers
lab doctor                         # versions, health, isolation profiles, disk, secrets permissions
```

`lab init` performs these steps:
1. Creates `config/` (mode 0700, secrets 0600), `services/`, `artifacts/`, `jobs/`, `logs/`, `run/` and `backups/`.
2. Initializes PostgreSQL with separate ledger roles (migrator / writer / reader) and a separate Temporal role and databases.
3. Applies migrations and grants.
4. Issues tokens for you (`admin.token`) and for each service principal. Only token hashes are stored in the ledger.

Ports default to API 8765, PostgreSQL 5544 and Temporal 7633–7641. Use `--api-port/--pg-port/--temporal-port` to choose others; Temporal needs 9 consecutive free ports.

## Bring a project

```bash
lab project create myproj --budget compute_seconds=36000 --budget trials=500
lab project register-env myproj --python /path/to/project/.venv/bin/python --root /path/to/project --adapter my_adapter:ADAPTER
lab component install myproj "$(lab component builtins | python -c 'import json,sys;print([p for p in json.load(sys.stdin) if p.endswith("eidos.pytorch.train")][0])')"
lab charter commit myproj charter.json            # success rules BEFORE exploration
lab investigate start myproj --question "..." --charter charter:<study> --runtime pydantic-ai --model anthropic:<model>
lab investigate events myproj --investigation <id> --follow   # detach/reattach any time with the cursor
lab report myproj --investigation <id>
```

`register-env` records the interpreter and its installed packages. It never installs or upgrades anything in your project environment. Worker SDK modules are put on the job's `PYTHONPATH`. If a component's pins conflict with your packages, the conflict is reported instead of being "fixed".

For task bundles that follow `eidos-task-bundle/0.1`, see `lab task import` / `lab task answer` and `docs/dev-runs/research-public-v0.1.0.md`.

## MCP

Run `lab mcp` (stdio). Registration snippets for clients are in `docs/ops/mcp-clients.md`. External clients have not been qualified yet.

## Operations

| Task | Command | Notes |
|---|---|---|
| Status | `lab status [--json]` | Services, projects, job counts, disk pressure |
| Logs | `lab logs [service] --lines N` | One file per service under `logs/` |
| Stop | `lab stop --running-jobs detach\|drain\|cancel` | `detach` leaves research processes running; on restart the runner reattaches by recorded handle (pid + start time + boot id) |
| Backup | `lab backup [--dest DIR]` | Needs the lab running (PostgreSQL up). Sets a maintenance flag (new commands, publications and outbox dispatch get 503 `MAINTENANCE`), takes a watermark, dumps the ledger and both Temporal databases, then copies and **verifies** every referenced blob. The result is a directory with `manifest.json` (format `eidos-backup/1`, ledger revision, event watermark, checksums). Credentials are excluded. Commands committed after the watermark are not in the bundle; that is the loss window. Running research processes keep running. See "Upgrade, backup and restore" below. |
| Restore | `lab restore BUNDLE --home NEW_DIR [ports]` | Verifies checksums, restores into a **fresh** directory (refused if it already holds a lab), issues **new** role passwords and re-enrols every principal with new tokens, restores the ledger and both Temporal databases and all blobs, and re-detects the sealed profile. Nothing is scheduled until `lab start`. See below. |
| Upgrade | `lab upgrade` | Run the NEW installation's `lab`: it stops the services, applies forward-only migrations, points `lab_python` at the interpreter that ran it, re-detects the sealed profile, updates the recorded version and restarts. See below. |
| Disk pressure | automatic | New bytes are refused below `min_free_bytes` (`DISK_PRESSURE`). Committed evidence stays readable. |

Recovery behaviour:
- Interrupted turns resume from their persisted decision and command keys. A retry returns the original receipt.
- Lost runner processes are reattached, or reconciled as lost, followed by a new fenced attempt.
- The reconciler re-dispatches admitted work that has no live workflow.
- Budgets live in the ledger and never reset with workflow history.

## Upgrade, backup and restore

Behaviour as implemented in `src/eidos/supervisor/maintenance.py`.

**Upgrade**
1. Take a `lab backup` first (migrations are forward-only; there is no downgrade).
2. Install the new wheels over the same environment (`uv pip install --no-cache --reinstall --find-links dist eidos-lab`) or into a new one. Do not edit `lab.json` by hand.
3. Run `lab upgrade` **with the new installation's `lab`**. If the lab is running it stops all services first (old writers must not run during a migration), starts PostgreSQL, applies the ledger migrations (`ledger_revision` before/after is reported), then:
   * sets `lab_python` to the interpreter that ran the upgrade, so services restart on the new code (the old interpreter is reported as `lab_python.before`);
   * re-detects the sealed profile (`linux:bwrap-sealed` or `macos:seatbelt-sealed`) and updates `sealed_available`;
   * records the new version and, if the lab had been running, starts it again (a stopped lab stays stopped).
4. Verify with `lab doctor` and `lab status`. In-flight workflows replay on the new workers; recorded histories must replay on new workflow code (`tests/integration/test_workflows.py`). Old scientific records are never rewritten.
A cross-release test (v0.1.0 wheels to head, 33 checks) runs in CI as `upgrade-across-releases`.

**Backup**
* Run while the lab is running. Writers are quiesced only while the ledger watermark and database dumps are taken; blobs are immutable and are copied afterwards and verified by digest. A missing or corrupt referenced blob aborts the backup and removes the partial bundle.
* The default destination is `$EIDOS_HOME/backups/eidos-backup-<UTC>`. For a real backup use `--dest` on another volume. The manifest lists jobs that were running at the watermark.
* Never copy `services/pg/data` while PostgreSQL runs as a substitute for this command.

**Restore**
* `lab restore BUNDLE --home NEW_DIR [--api-port N --pg-port N --temporal-port N]` needs a directory without an existing lab and uses the ports you give (a distinct 9-port Temporal block). Use the new installation's `lab`; `lab_python` is that interpreter.
* Credentials are not in the bundle: new PostgreSQL role passwords are generated and every principal is re-enrolled with a new token. Re-issue tokens to anything that held the old ones (MCP clients using `EIDOS_TOKEN`; the admin token file is rewritten under `NEW_DIR/config/secrets`).
* Start with `EIDOS_HOME=NEW_DIR lab start`. The reconciler re-dispatches unfinished work before scheduling; the runner treats process handles from the old boot as lost and reconciles them. Work published after the backup watermark is not in the bundle and has to be redone or reconciled.
* The restore records `extra.restored_from` (bundle, watermark, creation time) in `lab.json`.

