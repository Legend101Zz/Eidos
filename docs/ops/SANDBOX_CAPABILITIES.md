# Sandbox capabilities (build environment, verified 2026-11-07)

This records what the Anthropic Managed Agents build sandbox actually provided when the
Eidos local-v1 build started. It describes the *build* environment only. It is not evidence
about researcher machines, macOS, VM isolation or GPU behaviour.

## Host

| Item | Observed |
|---|---|
| OS | Ubuntu 24.04.5 LTS, kernel 6.18.44 (Firecracker VM), x86_64 |
| CPU / RAM | 4 vCPU, 15 GiB RAM, no swap (documentation said up to 8 GB; observed is larger) |
| Disk | `/` shared by `/workspace` and `/tmp`: 30 GiB available of 252 GiB device |
| User | `root` (uid 0). A `postgres` system user exists. No unprivileged build user by default. |
| Python | 3.11.17 (system default), 3.10, 3.12.3, 3.13 present; `uv python install 3.12` works (python-build-standalone via GitHub) |
| uv / node | uv 0.8.17, node v22.22.0 |
| Toolchain | gcc, make available |
| Git identity | `Claude <noreply@anthropic.com>`; remote `https://github.com/Legend101Zz/Eidos.git` via platform git proxy |

## Network egress

| Endpoint | Result |
|---|---|
| `https://pypi.org/simple/` | 200 |
| `https://download.pytorch.org/whl/cpu/` | 200 (index). **Wheel links point at `download-r2.pytorch.org`, which does not resolve (DNS failure).** The same wheel paths fetched from `download.pytorch.org` work (HTTP 206/200). |
| `https://temporal.download/` (root) | 522; `https://temporal.download/cli/archive/latest?platform=linux&arch=amd64` → 200 (CLI 1.10.0, server 1.32.2, UI 2.56.1) |
| `github.com` releases | 200 (`temporalio/temporal` v1.32.2 server tarball downloaded) |
| `api.github.com` | reachable but unauthenticated rate limit was already exhausted at first call (403 "rate limit exceeded"). Poll sparingly. |

### CPU PyTorch workaround

`uv pip install torch --index-url https://download.pytorch.org/whl/cpu` fails (r2 host).
Working procedure: download the wheel from `download.pytorch.org` into a local wheelhouse,
rename `%2B` to `+`, then `uv pip install --find-links /workspace/.wheelhouse "torch==2.15.0+cpu"`
(other dependencies resolve from PyPI). torch 2.15.0+cpu, cp311: 210 MB wheel, ~890 MB venv.
Script: `scripts/dev/fetch_cpu_torch.sh`.

## Services

| Service | Result |
|---|---|
| PostgreSQL 16.15 (`/usr/lib/postgresql/16/bin`) | Not running initially. `initdb` + `pg_ctl start` as user `postgres` on a throwaway cluster (port 5499, unix socket dir in /tmp) **works**. |
| Temporal regular server 1.32.2 (`temporal-server` from GitHub release) | **Works** against PostgreSQL 16 using plugin `postgres12`: `temporal-sql-tool create` / `setup-schema -v 0.0` / `update-schema --schema-name postgresql/v12/{temporal,visibility}` (schemas embedded in the tool), then `temporal-server --root R --config config --env development start`. Cluster health `SERVING`; namespace registration works; a Python SDK (temporalio 1.35.0) workflow+activity round trip succeeded. |
| Temporal dev server (`temporal server start-dev`) | CLI present; used only for development/tests per design. |
| Docker | CLI 29.9.2 present; **daemon not running** (`/var/run/docker.sock` absent). Container-based isolation cannot be exercised here. |

### Port-layout gotcha

The stock `development-postgres12.yaml` binds frontend 7233, history 7234, matching 7235,
worker 7239, plus membership 6933–6939 and HTTP 7243. The controller's helper slots
allocate Temporal ports 7234 and 7235, which collide with a stock lead server on 7233.
Eidos therefore generates Temporal configs from a **base port** with all internal service
ports derived from it (frontend = base, other services in a disjoint range). Helper slots
must use base ports far apart from each other (see `eidos.ops.temporal_config`).

## Implications for verification

- Linux x86_64 only. macOS installation, Apple Silicon, VM isolation and container isolation
  cannot be proven from this sandbox; they remain qualification gaps.
- No GPU. All tensor work is CPU-only and small.
- Running as root: OS-user isolation profiles (separate UIDs for investigator / trainer /
  assessor / sealed evaluator) can be *exercised* here with `useradd`, but this is a
  build sandbox, not a researcher machine.
- No model-provider API keys. All investigator tests here use deterministic fixture
  providers and are labelled fixture-only.
- Handoff manifest: all 11 files in `docs/design/eidos-claude-handoff/manifest.json` verified
  by SHA-256 (11 ok, 0 mismatched).
