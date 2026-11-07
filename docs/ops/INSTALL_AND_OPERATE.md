# Installing and operating Eidos (local v1)

Eidos runs entirely on your own machine. It does not need a cloud account, an OCI registry, Kubernetes or Managed Agents. The model endpoint can be local or hosted. A hosted LLM still receives the authorized context it is sent.

## Supported profiles

| Profile | Status |
|---|---|
| Linux x86_64, CPU | Runtime-verified in the hosted build sandbox (Ubuntu 24.04, Python 3.12, PostgreSQL 16, Temporal 1.32.2). That is a build environment, not a researcher machine. |
| `linux:bwrap-sealed` isolation | Runtime-verified with hostile probes. When the lab runs as root with the `eidos-cand` user, candidate code runs as that distinct OS identity if it can execute the project interpreter. Provision sealed-evaluation environments under a world-readable prefix (e.g. `UV_PYTHON_INSTALL_DIR=/opt/eidos/python uv python install 3.12`, venv under `/opt/eidos/envs/`, `chmod -R a+rX`); `separate-identity` then passes (verified). |
| macOS (arm64/x86_64) host-native `dev:same-user` | **Unqualified.** The code paths exist; no macOS run has been performed. |
| macOS `macos:lima-vm-sealed` | **Not implemented/qualified.** Plans that require sealed execution cannot be admitted on macOS. |
| CUDA / MPS devices | Discovered and reported. **Unqualified.** Exact restore across devices is never claimed. |
| Windows / WSL | Not supported in v1. |

## Prerequisites

- Python 3.11–3.13 for the lab. The research projects keep their own interpreters.
- PostgreSQL ≥ 14 server binaries, found on PATH, under `/usr/lib/postgresql/*/bin`, or via Homebrew `postgresql@*`. Eidos creates and owns a private cluster under the lab directory. It does not use your existing databases.
- Network access once, to download the pinned Temporal Server release (sha256-verified). Alternatively, pass `--temporal-source DIR` pointing at existing `temporal-server`/`temporal-sql-tool` binaries.
- Optional: `bubblewrap` (Linux) for the sealed evaluation profile.

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
| Backup | `lab backup [--dest DIR]` | Quiesces commands and outbox dispatch, then dumps the ledger and Temporal persistence, then copies and **verifies** every referenced blob. Credentials are excluded. Commands committed after the watermark are not in the bundle; that is the loss window. |
| Restore | `lab restore BUNDLE --home NEW_DIR [ports]` | Verifies checksums, restores into a fresh directory, issues new role passwords and tokens (re-enrollment), and reconciles jobs at the next `lab start`. |
| Upgrade | `lab upgrade` | Forward-only migrations plus a restart. Recorded workflow histories must replay on new workflow code (`tests/integration/test_workflows.py`). |
| Disk pressure | automatic | New bytes are refused below `min_free_bytes` (`DISK_PRESSURE`). Committed evidence stays readable. |

Recovery behaviour:
- Interrupted turns resume from their persisted decision and command keys. A retry returns the original receipt.
- Lost runner processes are reattached, or reconciled as lost, followed by a new fenced attempt.
- The reconciler re-dispatches admitted work that has no live workflow.
- Budgets live in the ledger and never reset with workflow history.
