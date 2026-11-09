# Where Eidos writes, and how to keep it on an external SSD

Checked against `src/eidos/settings.py`, `supervisor/`, `runtime/`, `orchestration/` and the CLI at base 57dd5bd. Nothing here was run on a Mac.

## One variable moves almost everything: `EIDOS_HOME`

`EIDOS_HOME` (or `lab init --home DIR`; default `~/.eidos`) is the lab root. `lab init` creates these under it, and nothing else is needed on the internal disk:

| Path under `$EIDOS_HOME` | Contents |
|---|---|
| `config/` (0700), `config/secrets/` | `lab.json`, tokens, DB passwords, `label-canary` |
| `services/pg/data` | the private PostgreSQL cluster (ledger); its unix socket is in `services/pg/` |
| `services/temporal/` (`bin`, config, `--root`) | the pinned Temporal server binaries and config; Temporal's data is in the PostgreSQL cluster above |
| `artifacts/` | the content-addressed artifact store (`artifacts/staging` for in-flight writes) |
| `jobs/` | per-attempt working directories (source snapshot, staging, scratch, resume data, a private `tmp` for the macOS sandbox) |
| `logs/` | one log per service |
| `run/`, `backups/`, `envs/`, `reports/`, `cache/` | pid and status files, `lab backup` bundles, (reserved) env dir, (reserved) reports, (reserved) cache |

Use `export EIDOS_HOME=/Volumes/<SSD>/eidos-lab` for every shell that runs `lab`, and pass `--home` to `lab init` once. `lab.json` records the absolute paths, so do not move a lab directory after init without `lab backup` / `lab restore`.
`min_free_bytes` (512 MiB default, `lab.json`) is measured on the volume holding `$EIDOS_HOME`; it is the volume that matters for disk pressure.

## Outside `EIDOS_HOME` (you set these)

| Location | Variable / flag | Notes |
|---|---|---|
| uv wheel and build cache | `UV_CACHE_DIR=/Volumes/<SSD>/uv-cache` | default `~/.cache/uv` (macOS: `~/Library/Caches/uv`). The CI scripts install with `--no-cache` so a stale cached wheel of the same version is never used |
| uv-managed Pythons | `UV_PYTHON_INSTALL_DIR=/Volumes/<SSD>/uv-python` | default `~/.local/share/uv/python` |
| The lab venv and the research/project venv | create with `uv venv /Volumes/<SSD>/venvs/<name>` | **the project environment must not be inside `$EIDOS_HOME`**: the lab refuses an environment root or interpreter inside the lab directory (`runtime/runner.py:181`, `handlers/projects.py:91`), because it would expose lab data to jobs. Put it as a sibling directory on the SSD |
| Task bundles, switch/image example projects, wheels (`dist`) | wherever you put them | `lab task import` records the bundle path as the project root; keep it outside `$EIDOS_HOME` |
| `lab report --out-dir` | default `./reports` relative to the current directory | pass an explicit directory on the SSD |
| `lab backup --dest` | default `$EIDOS_HOME/backups/...` | pass a path on another volume for a real backup |
| Temporary files | `TMPDIR=/Volumes/<SSD>/tmp` (create it first) | see the list below; Python's `tempfile` honours `TMPDIR` |

## Temporary files that are NOT under `EIDOS_HOME`

These use the system temp directory (`TMPDIR`, else `/tmp` or `/var/folders/...` on macOS), so set `TMPDIR` to an SSD directory in the shell that runs `lab start` (the supervisor passes its environment on to its services) and when running the CLI:

| Writer | What | Size |
|---|---|---|
| `orchestration/activities.py:152` (assessor) | `mkdtemp(prefix="eidos-assess-")`: bundle, inputs and outputs of an assessor run; removed afterwards | small |
| `orchestration/activities.py:117,225`, `investigator/activities.py:152,155`, `investigator/replay.py:45` | `NamedTemporaryFile` while downloading an artifact to read it | the artifact size |
| `tasks/bundle.py:241` (`lab task answer`) | `TemporaryDirectory` for the reload check (validation inputs, recorded and fresh predictions) | the task's arrays |
| `api/app.py:136` | uses `artifacts/staging` (under `EIDOS_HOME`), not the system temp | n/a |
| `eidos_pytorch/fingerprint.py:169` | `eidos-behavior-*` directory for the fresh-process behaviour check | one checkpoint |
| `eidos_worker/validate.py:85`, `testing.py:37` | `eidos-fixture-*`, `eidos-test-*` while validating or testing components | small |
| macOS sandbox (`runtime/launcher.py:255-258`) | the job's private `tmp` is `jobs/<job>/<attempt>/tmp` (under `EIDOS_HOME`) and `TMPDIR` is pointed at it | counted in `jobs/` |
| Linux bubblewrap profile | a private tmpfs `/tmp` (memory) | not applicable on the Mac |

## Things that read or write the home directory regardless

* `~/.eidos` is only the default for `EIDOS_HOME`; with the variable set it is never created.
* Sandbox credential denial (`runtime/launcher.py:175`) lists `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`, `~/.pgpass`, `~/.git-credentials`, `~/.docker`, `~/.kube`, `~/.config/{gcloud,gh,git}`, `Library/Keychains` to *deny* reads. It does not write there.
* Client tools (Claude Code, Codex CLI, Claude Desktop) keep their own state under `~/.claude*`, `~/.codex*`, `~/Library/...`. Eidos never edits those; the qualification scripts pass MCP configuration per invocation. Those tools' own history and auth stay on the internal disk unless you relocate them yourself (`CODEX_HOME`, `CLAUDE_CONFIG_DIR` are the documented variables as far as I know; unverified here).
* PyTorch, Hugging Face and similar caches (for example `TORCH_HOME`, `HF_HOME`, `~/.cache`) are written by *project* code you run, not by Eidos. The research env's datasets are generated, not downloaded (`research/public/development/data.py`), but set `HF_HOME` and `TORCH_HOME` to the SSD if you run anything else.
* The pinned Temporal download (when `--temporal-source` is not given) is fetched once into `services/temporal/bin` under `EIDOS_HOME`.

## Suggested shell preamble

```bash
S=/Volumes/<SSD>
mkdir -p "$S/eidos-lab" "$S/uv-cache" "$S/uv-python" "$S/tmp" "$S/venvs" "$S/reports"
export EIDOS_HOME="$S/eidos-lab" UV_CACHE_DIR="$S/uv-cache" UV_PYTHON_INSTALL_DIR="$S/uv-python" TMPDIR="$S/tmp"
```
Do not put `EIDOS_HOME` on a filesystem that cannot hold PostgreSQL (needs POSIX permissions and `fsync`): format the SSD APFS or HFS+, not exFAT/FAT. Disk-image or network volumes were not considered.
