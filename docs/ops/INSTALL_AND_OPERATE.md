# Installing and operating Eidos (local v1)

Eidos runs entirely on your own machine. It does not need a cloud account, an OCI registry, Kubernetes or Managed Agents. The model endpoint can be local or hosted. A hosted LLM still receives the authorized context it is sent.

## Supported profiles

The authoritative v1 scope is **`docs/release/SUPPORTED.md`**. In short:

| Profile | v1 status |
|---|---|
| Linux x86_64, CPU | Supported. Hosted CI (Ubuntu 24.04, Python 3.12, PostgreSQL 16, Temporal 1.32.2), 15 jobs (13 on Linux) since 1.1.0. |
| macOS arm64, CPU and MPS | Supported. Qualified on the owner's Apple M4 (macOS 26.3.1): owner kit, MPS parity and cross-device resume (`docs/mac/STATUS.md`). |
| `linux:bwrap-sealed` | Supported; **the only profile for sealed (hostile-code) evaluation**. When the lab runs as root with the `eidos-cand` user, candidate code runs as that distinct OS identity if it can execute the project interpreter, and the assessor keeps the lab identity (since 1.1.0; ADR 0011). Root setup: § "Separate candidate identity (root)" below; `separate-identity` then passes for both roles. |
| `macos:seatbelt-sealed` | Supported, **same OS user**. `sandbox-exec` with a generated profile: the lab home, credential directories and `$HOME` are unreadable (except the attempt's own files); writes go only to staging/scratch/resume and a private tmp; no network; cleared environment; no inspection of other processes. It does **not** contain a hostile same-user program (launchd/XPC by other routes, mach IPC, signals). Plans requiring sealed execution are **refused** on macOS, and a Seatbelt receipt never satisfies them. The macOS assessor also runs under Seatbelt as the same user; its receipt reports `same_os_user: true`. |
| `macos:lima-vm-sealed` | Not supported in v1 (not implemented). |
| CUDA, distributed training, Windows/WSL | Not supported in v1. |

## Separate candidate identity (root)

`linux:bwrap-sealed` always confines candidate code in its own namespaces. With these root steps, candidate code
also runs as a distinct OS user, `eidos-cand`, while the assessor and scorer keep the lab identity (ADR 0011).
Receipts then report `separate-identity: passed`, and plans may require it. The CI job `separate-identity` runs
exactly these steps (`scripts/ci/separate_identity_setup.sh`) on Ubuntu 24.04.

1. Run the lab as root (the launcher must be able to switch to another user).
2. Install bubblewrap: `apt-get install bubblewrap`.
3. Allow unprivileged user namespaces for the candidate user. On Ubuntu 24.04 AppArmor restricts them:
   `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (persist it in `/etc/sysctl.d/`), or install an
   AppArmor profile that allows `bwrap` to create user namespaces.
4. Create the user: `useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin eidos-cand`.
5. Put each sealed-evaluation interpreter where `eidos-cand` can execute it, never in a root-only or home
   directory: `UV_PYTHON_INSTALL_DIR=/opt/eidos/python uv python install 3.12`, then
   `UV_PYTHON_INSTALL_DIR=/opt/eidos/python uv venv --python 3.12 /opt/eidos/envs/<name>`, install the project's
   dependencies into it, and `chmod -R a+rX /opt/eidos`. Register it with `lab project register-env` as usual.
6. Make the worker SDK the runner binds into the sandbox readable by `eidos-cand`: give every parent directory of
   the lab's installation `o+x` and the installation itself `o+rX` (the setup script does this for a path you
   pass it).
7. Make the lab home traversable but not listable: `chmod 0711 "$EIDOS_HOME"`. Its config and secrets stay 0700/0600.
8. Check: `runuser -u eidos-cand -- /opt/eidos/envs/<name>/bin/python -I -c pass` and
   `runuser -u eidos-cand -- bwrap --ro-bind / / --unshare-all true` must both succeed; `lab doctor` then reports
   `sealed-profile:separate-identity` as available.

If `eidos-cand` cannot execute the interpreter, the candidate identity is not used: the job runs in the same
namespaces as the lab user and its receipt reports `separate-identity: failed` (never a silent pass). If it can
execute the interpreter but cannot read the worker SDK, the job fails at start with an import error in its stderr.

## Provider keys

Store provider keys in the operating system's secret store once, and `lab start` reads them (ADR 0012):

```bash
lab credentials set anthropic          # hidden prompt; or: printf %s "$KEY" | lab credentials set anthropic
lab credentials status anthropic       # {"stored": true, ...}; never prints the key
lab start                              # reads stored keys; they reach only the investigator worker
lab credentials delete anthropic
```

- macOS uses the Keychain (`/usr/bin/security`); Linux uses libsecret's `secret-tool` (package `libsecret-tools`)
  and needs a running Secret Service such as gnome-keyring. Without either, `lab credentials` fails with a clear
  error: Eidos never stores keys in plaintext. Use `lab start --credentials-stdin` (NAME=VALUE lines) instead.
- Items are named `eidos/<lab id>/<provider>`; the lab id comes from the lab's home path, so a lab restored into a
  new directory needs its keys set again.
- A key exported in the shell for `lab start` wins over the stored one for that start.

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

Run `lab mcp` (stdio). Registration snippets for clients are in `docs/ops/mcp-clients.md`. Supported clients in v1: **Claude Code** and **Codex CLI** (verified for read and write paths, `docs/mac/receipts/a14-*.json`) and the **MCP Python SDK** client. Claude Desktop is not supported in v1.

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
   * **Old writers are fenced.** From migration 0004 every write to a ledger table must come from a writer that declares the current writer generation (the command service does, per transaction). An older Eidos process that is still running after the upgrade (for example after a manual, partial upgrade) gets `eidos ledger writer fenced ... stop it and use lab upgrade` on its next write, instead of colliding with the new schema (for example the event-sequence counter of migration 0003). Stop any such process; nothing it attempted was committed. Test: `tests/integration/test_writer_fence.py`.
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

