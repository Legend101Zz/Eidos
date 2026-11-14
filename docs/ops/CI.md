# CI (GitHub Actions)

Workflow: `.github/workflows/ci.yml`. Standard GitHub-hosted runners only (`ubuntu-24.04`, `macos-15` arm64); no secrets, no paid runners, no real LLM providers.

## Triggers
`push` to `main`, `build/eidos-local-v1`, `task/**` and `release/**`; `pull_request` only for PRs targeting `main`. A commit on a task branch is therefore built once, not twice. `concurrency` cancels superseded runs of the same ref. Workflow permissions: `contents: read`. Every job has `timeout-minutes`. Uploaded artifacts are kept 3 days.

## Jobs
| Job | Proves |
|---|---|
| `lint-and-unit` | ruff, import-linter contracts, mypy (`scripts/ci/mypy.sh`: the server tree and worker packages at Python 3.12 from the locked mypy and `pyproject.toml` `[tool.mypy]`; the three worker packages again with a Python 3.9 target through a pinned `uvx mypy==1.19.1`, because mypy 2.x no longer targets 3.9), shellcheck, unit tests, MCP and reporting tests |
| `integration` | integration tests against a PostgreSQL 16 service and the Temporal CLI dev server |
| `worker-compat` | worker packages install and pass without server deps (Python 3.9 + pydantic<2; Python 3.12 + CPU torch unchanged) |
| `components` | built-in component suite (`tests/components`, 59 tests): manifests/fixtures, each component through the worker SDK, end-to-end CPU switch-order replication. Lab venv (`uv sync`) runs pytest and is asserted torch-free; one project venv with CPU torch 2.15.0 (`fetch_cpu_torch.sh` wheelhouse) + numpy serves as both `EIDOS_TORCH_PYTHON` and `EIDOS_NUMPY_PYTHON`; fixture checkpoints are regenerated in it first; any skipped test fails the job. Log uploaded on failure |
| `fresh-install` | `uv build --all-packages`; wheels installed into a clean venv (asserted not imported from the checkout); `lab init` (non-root `initdb`, pinned Temporal 1.32.2 downloaded and sha256-verified), `lab start`, `lab doctor --json` (any `fail` fails the job), `lab project create ci1` (built-in components through validate/publish), `lab status --json`, `lab stop`. Lab logs and results uploaded on failure |
| `macos-arm64-smoke` | **macOS arm64 GitHub-runner evidence, not researcher-machine qualification.** Same wheel build/install; PostgreSQL 16 from Homebrew; the lab lifecycle with each step reported separately (informational: failures are shown in the job summary and uploaded logs and do not stop later steps); worker SDK tests in a plain venv with only `eidos-wire` + `eidos-worker-sdk`; research-release smoke (`research/public/development`: `requirements.lock`, `check_bundles.py`, `package_task.py --task D02`, D02 baseline `train --seed 11`, CPU, one thread). The SDK and research smoke steps are required (they fail the job) |
| `upgrade-across-releases` | Cross-release upgrade, same lab directory. Builds the `eidos-v0.1.0` wheels from the tag (`git archive`; the job fetches tags), installs them in a clean venv and, with that **old** `lab`, creates real data: project, environment, component, a finished job, a concluded and an open FIXTURE investigation (deterministic policies, no model), a report. Then installs the head wheels over the same venv and runs `lab upgrade` on the running lab (migrations 0001 to 0003, restart). Verifies: ledger at 0003; `event_counter` in step; pre-upgrade events/records/jobs/artifacts byte-identical (md5 fingerprints from PostgreSQL); events gap-free via the new CLI; job, artifact bytes, component and both investigations readable; a new job runs; the open investigation starts a new turn on `steer` (new workers) and can be stopped; report renders; `lab doctor` has no `fail`. No checkpoint branch (needs a trained torch checkpoint). About 40 s locally. `result.json` is uploaded |
| `e2e-journey` | Scripted user journey from a clean wheel install (`scripts/journeys/e2e_journey.sh`; record in `docs/dev-runs/e2e-journey.md`). Builds a CPU-torch research env from `requirements.lock`, installs the wheels, then: `lab init/start/doctor`; D01 bundle packaged with the release's own script and imported; FIXTURE investigation (`bundle-baseline-replicate`) to conclusion; `lab task answer` validated by `validate_answer.py`; switch-order project train, `lab checkpoint branch`, fresh-process behaviour check, `lab export` (wrong-builder export refused); MCP SDK client (`mcp.Client`, stdio): list tools, read D01 investigation and events, `start_investigation`, disconnect, reconnect and read back, list components; HTML reports; `lab stop`. Qualified assessment is not covered (the existing switch journey has drifted; see the dev-run doc). `journey-record.json` is uploaded |
| `switch-journey` | The full switch-order acceptance journey (`tests/journeys/switch_journey.py`, steps 1 to 7, 154 checks) from a clean wheel install (`bash tests/journeys/run_ci.sh`; CPU torch 2.15.0 venv from the wheelhouse): branching numbers, exact no-change continuation, wrong builder refused, promotion gate, inconclusive guardrail, withdrawal, scorer reuse on the image project, fixture investigation with worker kill and recovery. About 5 minutes of journey time locally. Record in `docs/dev-runs/switch-journey.md`; `journey-run.json` uploaded |
| `separate-identity` | (since 1.1.0; ADR 0011) As root: `scripts/ci/separate_identity_setup.sh` (bubblewrap, unprivileged user namespaces, the `eidos-cand` user, a candidate-readable interpreter under `/opt/eidos`), bwrap diagnostics, then a sealed job as `eidos-cand` through the real runner and command service plus its assessment under the lab identity (`tests/integration/test_separate_identity_e2e.py`, which fails instead of skipping here), the isolation probes and the assessor sandbox tests |
| `secret-store-macos` | (since 1.1.0; ADR 0012) `tests/unit/test_secret_store.py` with real round trips against a temporary keychain file the tests create and delete |
| `secret-store-linux` | (since 1.1.0) the same with `secret-tool` against an unlocked `gnome-keyring-daemon` on a private `dbus-run-session` (`EIDOS_REQUIRE_SECRET_TOOL=1`) |
| `adapter-pyglove` | (since 1.1.0; ADR 0015) `scripts/ci/adapter_pyglove.sh`: fresh venv with exact pins (pyglove 0.4.5, torch 2.7.0 CPU), the adapter tests, and the wheel licence check |
| `adapter-composer` | (since 1.1.0; ADR 0015) `scripts/ci/adapter_composer.sh`: fresh venv with exact pins (mosaicml 0.32.1, torch 2.7.0, torchvision 0.22.0 CPU), hook-phase fixtures (phase counts, state at each phase, restore), and the wheel licence check |

## Pinned actions
`actions/checkout` v4.2.2 (11bd719…), `astral-sh/setup-uv` v6.8.0 (d0cc045…), `actions/upload-artifact` v4.6.2 (ea165f8…); full SHAs are in the workflow with the tag in a trailing comment. Update by resolving `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>` (use the peeled `^{}` commit for annotated tags).

## Running the scripts locally (`scripts/ci/`)
- `fresh_install.sh`: build + clean venv + full lab lifecycle. Env: `CI_WORK`, `LAB_HOME`, `LAB_API_PORT`, `LAB_PG_PORT`, `LAB_TEMPORAL_PORT`, `PG_BIN`, `TEMPORAL_SOURCE` (directory containing `temporal-server`/`temporal-sql-tool`; omit to download the pinned release).
- `components.sh`: component suite as in the `components` job. Env: `CI_WORK`, `EIDOS_WHEELHOUSE` (reuse an existing CPU torch wheelhouse).
- `mypy.sh`: both mypy invocations of the `lint-and-unit` job. No environment variables; needs `uv` (and network for the pinned `uvx` mypy on first use).
- `build_and_install.sh`, `lab_cycle.sh {init|start|doctor|project|status|stop|all}`, `sdk_tests.sh`, `research_smoke.sh`.
- Requires `uv`, PostgreSQL >= 14 binaries. When run as root the lab uses `runuser -u postgres`; as a normal user (CI) it runs PostgreSQL as that user.
- `upgrade_across_releases.sh`: the `upgrade-across-releases` job. Env: `OLD_TAG` (default `eidos-v0.1.0`), `CI_WORK`, `LAB_API_PORT`, `LAB_PG_PORT`, `LAB_TEMPORAL_PORT`, `PG_BIN`, `TEMPORAL_SOURCE`, `KEEP_WORK=1`. Helper: `scripts/ci/upgrade/check.py` (seed / snapshot / verify). Results: `$CI_WORK/out/result.json`. The upgrade is in place (new wheels over the same venv, which `lab.json` `lab_python` names): `lab upgrade` does not rewrite `lab_python`.

## Study evaluations

`.github/workflows/study-evals.yml` runs on pushes to `main` and `task/**`, and
on pull requests touching `study/**` or that workflow. Its research environment
installs numpy, scikit-learn, torch, `study/requirements-analysis.txt` and
`pytest==9.2.0` (the version pinned in `uv.lock`). Run the complete study suite with
`"$EIDOS_RESEARCH_PYTHON" -m pytest study/tests -q -rs`; pytest collects both
function tests with fixtures and unittest classes. `EIDOS_RESEARCH_PYTHON` selects
the research interpreter and `EIDOS_STUDY_PYTHON` selects the separately installed
lab interpreter, both exported by the setup step. The job explicitly selects the
default `pilot4-v1` treatment and caps numerical library threads at one. The later
installed lab arm contract/parity tests retain their unittest runner.
