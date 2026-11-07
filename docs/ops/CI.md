# CI (GitHub Actions)

Workflow: `.github/workflows/ci.yml`. Standard GitHub-hosted runners only (`ubuntu-24.04`, `macos-15` arm64); no secrets, no paid runners, no real LLM providers.

## Triggers
`push` to `build/eidos-local-v1` and `task/**`; `pull_request` only for PRs targeting `main`. A commit on a task branch is therefore built once, not twice. `concurrency` cancels superseded runs of the same ref. Workflow permissions: `contents: read`. Every job has `timeout-minutes`. Uploaded artifacts are kept 3 days.

## Jobs
| Job | Proves |
|---|---|
| `lint-and-unit` | ruff, import-linter contracts, unit tests |
| `integration` | integration tests against a PostgreSQL 16 service and the Temporal CLI dev server |
| `worker-compat` | worker packages install and pass without server deps (Python 3.9 + pydantic<2; Python 3.12 + CPU torch unchanged) |
| `fresh-install` | `uv build --all-packages`; wheels installed into a clean venv (asserted not imported from the checkout); `lab init` (non-root `initdb`, pinned Temporal 1.32.2 downloaded and sha256-verified), `lab start`, `lab doctor --json` (any `fail` fails the job), `lab project create ci1` (built-in components through validate/publish), `lab status --json`, `lab stop`. Lab logs and results uploaded on failure |
| `macos-arm64-smoke` | **macOS arm64 GitHub-runner evidence, not researcher-machine qualification.** Same wheel build/install; PostgreSQL 16 from Homebrew; the lab lifecycle with each step reported separately (informational: failures are shown in the job summary and uploaded logs and do not stop later steps); worker SDK tests in a plain venv with only `eidos-wire` + `eidos-worker-sdk`; research-release smoke (`research/public/development`: `requirements.lock`, `check_bundles.py`, `package_task.py --task D02`, D02 baseline `train --seed 11`, CPU, one thread). The SDK and research smoke steps are required (they fail the job) |

## Pinned actions
`actions/checkout` v4.2.2 (11bd719…), `astral-sh/setup-uv` v6.8.0 (d0cc045…), `actions/upload-artifact` v4.6.2 (ea165f8…); full SHAs are in the workflow with the tag in a trailing comment. Update by resolving `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>` (use the peeled `^{}` commit for annotated tags).

## Running the scripts locally (`scripts/ci/`)
- `fresh_install.sh`: build + clean venv + full lab lifecycle. Env: `CI_WORK`, `LAB_HOME`, `LAB_API_PORT`, `LAB_PG_PORT`, `LAB_TEMPORAL_PORT`, `PG_BIN`, `TEMPORAL_SOURCE` (directory containing `temporal-server`/`temporal-sql-tool`; omit to download the pinned release).
- `build_and_install.sh`, `lab_cycle.sh {init|start|doctor|project|status|stop|all}`, `sdk_tests.sh`, `research_smoke.sh`.
- Requires `uv`, PostgreSQL >= 14 binaries. When run as root the lab uses `runuser -u postgres`; as a normal user (CI) it runs PostgreSQL as that user.
