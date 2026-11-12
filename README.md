# Eidos

[![CI](https://github.com/Legend101Zz/Eidos/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Legend101Zz/Eidos/actions/workflows/ci.yml) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) ![Platforms: Linux | macOS](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS-lightgrey.svg) ![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)

Eidos is a local research harness for investigating machine-learning models. You bring a project, a question and a budget. An investigator can inspect evidence, propose experiments, run jobs and report what happened; you can also drive the lab yourself through the CLI or an MCP client.

The lab keeps a ledger of commands, artifacts, budgets and observed results. For a scientific claim, it can bind a prospectively defined charter to a frozen evidence plan and a separately qualified assessor. Those controls help make the work reviewable; they do not make a model's reasoning correct or guarantee an improvement. A useful result can be a negative or inconclusive one.

A typical journey is:

1. Install the lab and check its local services with `lab doctor`.
2. Register your project and its Python environment, then define the question and success rules.
3. Start an investigation or connect an MCP client. Inspect the jobs, evidence and costs as work proceeds.
4. Review the report, limitations and any independently assessed result before using an exported model.

Eidos is an alpha research tool, not a hosted service. Local operation requires PostgreSQL and Temporal; using a hosted model also requires a provider account and credentials. Its sandbox guarantees differ by platform. See [Isolation](#isolation-model) and [Status and evidence](#status-and-evidence) before using it for sensitive work.

The repository's package version is `1.1.0`; see the [release notes](docs/release/RELEASE_NOTES_v1.1.0.md) and [changelog](CHANGELOG.md) for what is in a particular revision. Research comparing Eidos with coding-agent alternatives is ongoing; development experiments and tests do not establish scientific superiority.

[How it works](#how-it-works) · [Install and quickstart](#what-you-can-do-with-it-today) · [Supported scope](#supported-scope-v1) · [Documentation](#documentation-map) · [Contributing](#contributing)

## How Eidos keeps the investigator honest

Eidos provides controls for several research practices. Their guarantees depend on the configured policy, the recorded evidence and the isolation profile:

| Habit of a careful researcher | What Eidos enforces |
|---|---|
| Decide what counts as success before looking | A **charter** with the success rules is committed before exploration. Plans cannot override its thresholds; a later change is a new revision that keeps the old rule and records what had been seen |
| Use a sound method | **Evidence plans** come from **qualified templates**: templates that have shown they detect a planted effect and stay silent on a null case |
| Let someone else judge | A separate **assessor**, qualified the same way, compares the frozen plan with what actually ran |
| Keep a lab notebook | Admitted work, available cost and isolation receipts, and recorded **exposures** (who saw which result, and when) are kept in the ledger; uncertain or unavailable usage remains unknown |
| Claim only what the evidence supports | Claims carry an assessed outcome and a scope; failures, nulls and inconclusive results are kept |

**A worked example** (the switch-order project in [`examples/switch-order-project`](examples/switch-order-project/)). A small PyTorch model must decide which of two switches fired first. One input, the "recorder", agrees with the label 98% of the time, which is a tempting shortcut. The useful metric is *both-correct*: the model gets an example **and** its order-swapped twin right. In the v1.0.0 real-model run, `claude-sonnet-5-5` worked as the investigator on its own ([`a8-sonnet-3-recheck.json`](docs/release/evidence/a8-sonnet-3-recheck.json)):

1. **Diagnosis.** The baseline had 0.984 accuracy but only 0.2625 both-correct, and 0 of 701 adjacent pairs were both correct. The investigator concluded that the model probably relies on the recorder shortcut, and wrote that it had not tested this directly.
2. **Fix.** Group-balanced training from scratch reached 0.467 both-correct but dropped accuracy to 0.7125, breaking the charter's guardrail, so it was not claimed. The same training continued on a **checkpoint branch** of the baseline reached 0.785 both-correct with accuracy 0.9955.
3. **Assessed result.** A frozen evidence plan ran baseline and candidate again; the numbers reproduced exactly. The independent assessor returned **inconclusive**. The investigator's report listed the weak points itself: one training seed, one evaluation draw, and 45 extra training steps that confound the comparison. It did not claim more than that.

The run cost USD 1.81 in model calls.

## The research behind it (paper in preparation)

The study asks one question: **does the same LLM make more independently verified, useful model improvements through Eidos than as a well-equipped coding agent?** To make that a fair test, it compares four arms that share one LLM, one task set and one resource envelope, so the only difference is what the LLM works through:

| Arm | What the LLM gets |
|---|---|
| 1 | A coding agent with a research brief and ordinary tools |
| 2 | Arm 1 plus the component library and written research method, without Eidos |
| 3 | The same coding agent operating Eidos through MCP |
| 4 | The Eidos internal investigator |

```mermaid
flowchart LR
  S["Same LLM, task and budget"] --> A1["Arm 1: coding agent"]
  S --> A2["Arm 2: agent plus library and method"]
  S --> A3["Arm 3: agent driving Eidos over MCP"]
  S --> A4["Arm 4: Eidos investigator"]
  A1 --> G["Independent grader"]
  A2 --> G
  A3 --> G
  A4 --> G
  G --> D["Decision: arm 4 minus arm 2"]
```

**Decision rule.** The primary contrast is arm 4 minus arm 2. A practically useful advantage needs at least +10 percentage points in verified success rate, with the lower end of the 95% confidence interval above +10 points. The owner fixed this rule before any comparative result.

The study has development-pilot evidence; it does not establish that Eidos improves research performance. The public research materials are [`research/public/`](research/public/): the study protocol, 14 development task cards, the answer format and draft analysis code. The plan for running the study, and for testing the harness, is [`docs/experiments/README.md`](docs/experiments/README.md).

## How it works

**The research loop.** An investigation moves through the same stages a careful researcher would, and each stage leaves a record:

```mermaid
flowchart LR
  C["Charter: success rules first"] --> P["Evidence plan from a qualified template"]
  P --> T["Trials, branches and new components"]
  T --> A["Independent assessment"]
  A --> R["Claim, report and exported model"]
```

The investigator works in **bounded turns**. Each turn reads the evidence from the ledger, makes one model decision, and runs the accepted actions with recorded command keys. Exploration is free between turns; claims go through the plan and the assessor.

**The architecture.**

```mermaid
flowchart LR
  subgraph Clients
    CLI[lab CLI]
    MCPC[Claude Code / Codex / MCP SDK]
  end
  MCPS[MCP server<br/>lab mcp]
  API[Command API<br/>authenticated, loopback]
  LEDGER[(PostgreSQL ledger<br/>records, events, budgets,<br/>receipts, exposures)]
  STORE[(Artifact store<br/>content-addressed)]
  TEMPORAL[Temporal Server<br/>investigation, turn,<br/>job, assessment workflows]
  subgraph Workers
    INV[Investigator worker]
    RUN[Training / sealed runner]
    ASS[Assessor worker]
  end
  SANDBOX[Sandboxed jobs<br/>bwrap on Linux,<br/>Seatbelt on macOS]
  PROV[Model providers<br/>Anthropic, OpenAI-compatible]
  SUP[Supervisor<br/>start, restart, reconcile]

  CLI --> API
  MCPC --> MCPS --> API
  API --> LEDGER
  API --> STORE
  API -- outbox relay --> TEMPORAL
  TEMPORAL --> INV & RUN & ASS
  INV -- commands --> API
  INV --> PROV
  RUN --> SANDBOX
  RUN -- receipts --> API
  ASS -- assessments --> API
  SUP -.-> API & TEMPORAL & INV & RUN & ASS & LEDGER
```

- **Ledger.** Only the command service holds write credentials. Workers, the investigator and jobs act only through authenticated commands. Since migration 0004, writers from an older version are fenced after an upgrade.
- **Investigations.** An investigation is a coordinator workflow that starts one bounded turn at a time. A turn is: context from the ledger, then a model decision, then the accepted calls run with recorded command keys. Jobs outlive turns.

**The roles.**

| Role | What it does |
|---|---|
| Investigator | The LLM. Reads evidence, proposes and runs experiments, writes components; acts only through commands |
| Command service | The single write path: authenticates every caller, checks revisions and budgets, commits to the ledger |
| Ledger | PostgreSQL records of charters, runs, claims, plans, budgets, receipts and exposures |
| Workers | Separate processes that train, evaluate and score in the project's own Python environment |
| Assessor | A separate identity that checks a frozen plan against what actually ran and issues the verdict |
| Sandboxes | Isolate candidate code: bubblewrap on Linux, Seatbelt on macOS; each attempt gets an isolation receipt |
| Supervisor | Starts, health-checks, restarts and reconciles PostgreSQL, Temporal and the workers |

![Topology of the complete local v1 harness](docs/design/eidos-claude-handoff/architecture/model-research-harness-architecture.svg)

*The local v1 topology from the design (Revision 4, [`docs/design/`](docs/design/eidos-claude-handoff/architecture/model-research-harness-design.md)). It shows the designed system; what v1 supports is in [Supported scope](#supported-scope-v1).*

### Key features

- **Bounded, durable investigations.**
  - Turns run as Temporal workflows with persisted decisions and stable command keys.
  - A crashed worker resumes from the ledger.
  - Budgets (model tokens, USD, compute, trials) are reserved before work and settled after it.
- **Your project, unmodified.**
  - A thin adapter connects an existing PyTorch project.
  - The worker SDK is standard library only and installs nothing into your environment.
- **Research components.**
  - Training, evaluation, weight edits, behaviour checks and scorers are versioned, published releases.
  - The investigator can author new ones; they are validated before they are published.
- **Checkpoint branching.** Fork from immutable parents with explicit state migration, restore classes and cost accounting.
- **Evidence rules.**
  - Success rules are committed in a charter before exploration.
  - Plans come from qualified templates, and an assessor qualified with planted and null controls judges them.
  - Thresholds cannot be overridden.
- **Operator interfaces.** The `lab` CLI, an MCP server (stdio and loopback HTTP) and static HTML reports.
- **Local operations.** A supervisor manages PostgreSQL and Temporal, with `doctor`, backup and restore, upgrade, disk-pressure refusal and crash reconciliation.

### Isolation model

Every job attempt gets an isolation receipt from probes that actually ran, and the receipt records which controls held.
- **Linux, `linux:bwrap-sealed`.** Namespaces, read-only inputs, no network, no host writes, no credential or label reads, and a separate scorer process. This is the only profile for **sealed (hostile-code) evaluation**.
- **macOS, `macos:seatbelt-sealed`.**
  - `sandbox-exec` keeps the lab home, credentials and `$HOME` unreadable, allows no network, writes only to staging and scratch, and denies inspection of other processes.
  - It runs as the **same OS user**: launchd/XPC stay reachable by other routes, mach IPC is not filtered and signals are allowed. It does not contain a hostile same-user program.
  - So macOS labs **refuse plans that require sealed execution**, and a Seatbelt receipt never satisfies them.
- **`dev:same-user`.** Exploration only. Asking for it when a stronger profile is available needs a recorded waiver, which stays visible in locks, plans and assessments.

Details and limits: [`docs/release/SUPPORTED.md`](docs/release/SUPPORTED.md), [ADR 0006](docs/decisions/0006-macos-seatbelt-profile.md).

### Budget and ledger model

- Budgets are per investigation and per dimension: `model_tokens`, `model_usd` (at configured prices), `compute_seconds`, `trials`, `turns`.
- Each model decision reserves its worst-case envelope before any request and settles to the actual usage afterwards. A request that may have been charged before a timeout is recorded as uncertain usage.
- A retried call reuses its recorded command key, so a crash after a committed effect never admits work twice.
- Exposures (reads of outcomes) are recorded, so later claims can be checked against what was seen.

## What you can do with it today

The lab runs locally on a Linux or macOS machine and manages PostgreSQL and Temporal. A hosted model requires its own provider account and credentials; a compatible local model can be configured separately.

### Install

Requirements:
- Python 3.12 (recommended; the server supports 3.11–3.13) and [`uv`](https://docs.astral.sh/uv/);
- PostgreSQL 14 or later server binaries (for example Homebrew `postgresql@16`, or `postgresql-16` on Ubuntu);
- network access once, for the pinned, digest-verified Temporal Server download.

From source, build the wheels and install them into a fresh environment. This is what CI's `fresh-install` job and the macOS owner kit do:

```bash
git clone https://github.com/Legend101Zz/Eidos && cd Eidos
uv build --no-cache --all-packages -o dist
uv venv --python 3.12 ~/eidos-venv
uv pip install --no-cache --python ~/eidos-venv/bin/python --find-links dist eidos-lab
export PATH="$HOME/eidos-venv/bin:$PATH"
```

If you already have a compatible set of release wheels, place them in `dist/`, create the virtual environment, install from that directory, and add its `bin` directory to your `PATH`.

### Quickstart

**1. Create and start a lab.** The lab owns a private PostgreSQL cluster and a Temporal Server under its home directory.

```bash
lab init --home ~/eidos-lab --pg-bin /opt/homebrew/opt/postgresql@16/bin   # Linux: /usr/lib/postgresql/16/bin
export EIDOS_HOME=~/eidos-lab
lab start
lab doctor
```

**2. Register a project.** The switch-order example is used here; this is what the acceptance journey does.

```bash
lab project create demo --budget compute_seconds=20000 --budget trials=100
lab project register-env demo --python "$(command -v python3)" --root examples/switch-order-project --adapter eidos_adapter:ADAPTER
lab component install demo components/eidos.project.command
```

The project's own environment needs `torch`. Eidos records what is installed and never changes it.

**3. Connect Claude Code or Codex over MCP.** `lab mcp` is a stdio MCP server; clients launch it themselves. Generate client configurations with absolute paths:

```bash
python3 scripts/qualify/mcp_clients/gen_configs.py --home "$EIDOS_HOME" --lab "$(command -v lab)" --out ./mcp-configs
```

Then register the generated snippet in your client: `docs/ops/mcp-clients.md`. Claude Code and Codex CLI were verified on read and write paths (`docs/mac/receipts/a14-*.json`).

**4. Run an investigation.**
- Set `EIDOS_MODEL` and `EIDOS_SETTINGS_JSON` for your chosen provider. If you use a `model_usd` limit, include the provider's current `price_per_mtok` in the settings; the lab refuses a dollar limit without it. A model provider key reaches only the investigator worker. Pass it on stdin: `printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" | lab start --credentials-stdin` (stop the lab first).
- A charter with prospective success rules must be committed before exploration: `lab charter commit` (see `lab charter commit --help`). Put the resulting ID in `CHARTER_ID`.
- Set `INVESTIGATION_ID` to the ID returned by the start command before running the show and report commands:

```bash
lab investigate start demo --question "..." --charter "$CHARTER_ID" --runtime pydantic-ai \
  --model "$EIDOS_MODEL" --settings "$EIDOS_SETTINGS_JSON" \
  --limit model_tokens=600000 --limit turns=12
lab investigate show demo "$INVESTIGATION_ID"
lab report demo --investigation "$INVESTIGATION_ID" --out-dir report/
lab stop
```

The complete, scripted version of this journey (setup, qualified protocols and a real-model investigation) is `tests/journeys/a8_live.py`. Operations guide: [`docs/ops/INSTALL_AND_OPERATE.md`](docs/ops/INSTALL_AND_OPERATE.md); problems: [`docs/ops/TROUBLESHOOTING.md`](docs/ops/TROUBLESHOOTING.md).

## Supported scope (v1)

| | Supported |
|---|---|
| Platforms | Linux x86_64, macOS arm64; CPU, plus MPS on macOS |
| Model providers | Anthropic; OpenAI-compatible endpoints (protocol; see G4 below) |
| MCP clients | Claude Code, Codex CLI, MCP Python SDK |
| Sealed (hostile-code) evaluation | Linux with bubblewrap only |

Out of scope, with reasons: Windows, CUDA, distributed training, Lima VM, Claude Desktop, hosted multitenancy. Details: [`docs/release/SUPPORTED.md`](docs/release/SUPPORTED.md).

v1 makes **no scientific claim**: whether Eidos helps an LLM do model research (A16) is a separate later release; see [The research behind it](#the-research-behind-it-paper-in-preparation).

## Status and evidence

| Gate | Result | Evidence |
|---|---|---|
| G1 licence | pass | `LICENSE`, `NOTICE`; wheel metadata checked in CI |
| G2 scope | pass | [`SUPPORTED.md`](docs/release/SUPPORTED.md) |
| G3 isolation honesty | pass | `tests/unit/test_seatbelt_policy.py`, `tests/integration/test_sealed_plan_refusal.py` |
| G4 OpenAI-compatible | adapter verified; no local model of 14B or less passed full conformance (`qwen2.5:14b`: 4/8). Since v1.1.0 the `json` output mode removes malformed-output failures (qwen2.5 3B: 1→5, 7B: 0→5 decisions of 5) but not semantic ones (executable 3B 1→2, 7B 0→1 of 5) | [`g4-*.json`](docs/release/evidence/), [`g4-110-summary.json`](docs/release/evidence/g4-110-summary.json) |
| G5 real-model investigation (A8) | pass: `claude-sonnet-5-5` completed the full journey autonomously. The run receipt failed one check because of a bug in the checking script's export loader; the checks were re-evaluated from the ledger, with no model call, in `a8-sonnet-3-recheck.json` | [`a8-sonnet-3*.json`](docs/release/evidence/) |
| G6 local operations | disk exhaustion, service kills and abrupt stop pass on macOS; **machine sleep/restart not verified on hardware** (released without it by owner decision) | [`g6-*.json`](docs/release/evidence/) |
| G7 review items | pass | `CHANGELOG.md` |
| G8 docs | pass | this page, `docs/` |
| G9 final verification (v1.0.0) | pass: suite 167 passed / 4 skipped, switch journey 154/154, macOS owner kit 15/15, CI 10/10, independent review | [`g9-*`](docs/release/evidence/), `ACCEPTANCE_MATRIX.md` § E |
| v1.0.1 follow-up | mypy 0 errors in CI (also at a Python 3.9 target for worker packages); wire JSON Schemas and 15 golden fixtures; suite 199 passed / 4 skipped, worker packages 22 passed / 1 skipped; CI 10/10 on `main` | [`RELEASE_NOTES_v1.0.1.md`](docs/release/RELEASE_NOTES_v1.0.1.md) |
| v1.1.0 features | all six items done (ADRs 0010–0015); 5 of the 6 `not-started` failure-table rows now verified; suite 371 passed / 7 skipped, worker SDK 26 passed; CI 15/15 on the release commit | [`RELEASE_NOTES_v1.1.0.md`](docs/release/RELEASE_NOTES_v1.1.0.md) |

CI runs 15 jobs on every push: lint and unit, integration (PostgreSQL and Temporal), worker compatibility, components, fresh install, macOS arm64, upgrade across releases, end-to-end journey, switch journey, a Temporal source-build fallback, and since v1.1.0 separate identity (as root), the OS secret store on macOS and Linux, and the PyGlove and Composer adapters. Full criteria: [`ACCEPTANCE_MATRIX.md`](ACCEPTANCE_MATRIX.md). Release notes: [`docs/release/RELEASE_NOTES_v1.1.0.md`](docs/release/RELEASE_NOTES_v1.1.0.md) (and [v1.0.1](docs/release/RELEASE_NOTES_v1.0.1.md), [v1.0.0](docs/release/RELEASE_NOTES_v1.0.0.md)). History: [`CHANGELOG.md`](CHANGELOG.md). Documents dated before 2026-11-09 cite earlier commit IDs: [`COMMIT_ID_MAP.md`](docs/release/COMMIT_ID_MAP.md).

## Documentation map

| Document | What |
|---|---|
| `docs/ops/INSTALL_AND_OPERATE.md` | install, projects, MCP, backup/restore, upgrade |
| `docs/ops/TROUBLESHOOTING.md` | symptoms, causes, fixes |
| `docs/ops/mcp-clients.md` | MCP server and client registration |
| `docs/author-guide/` | writing components and using the worker SDK |
| `docs/design/` | the design (immutable) |
| `docs/decisions/` | architecture decision records |
| `docs/mac/STATUS.md` | macOS qualification record |
| `docs/release/` | supported scope, release notes, evidence |
| [`docs/experiments/README.md`](docs/experiments/README.md) | experiments and testing plan (R0–R3), GitHub workflow, grading |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | branches, pull requests, CI, commit identity |
| `research/public/` | public research release: study protocol, task cards, answer format (immutable) |
| `ACCEPTANCE_MATRIX.md` | release acceptance criteria and gate results |

## Contributing

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md): branches, pull requests, CI and commit identity. The research and testing work is planned in [`docs/experiments/README.md`](docs/experiments/README.md).

### Development

```bash
uv sync
uv run ruff check src tests && uv run lint-imports
bash scripts/ci/mypy.sh              # mypy: server at 3.12, worker packages also at a 3.9 target
uv run pytest tests/unit tests/mcp tests/reporting -q
EIDOS_TEST_PG_ADMIN=postgresql+psycopg://postgres:<password>@127.0.0.1:5432/postgres uv run pytest tests/integration -q
bash tests/journeys/run_ci.sh        # the 154-check switch-order acceptance journey from fresh wheels
```

Integration tests need a PostgreSQL superuser URL and, for workflow tests, a Temporal CLI (`EIDOS_TEMPORAL_CLI`). The workflow is in `.github/workflows/ci.yml`; see `docs/ops/CI.md`.

### Repository

| Path | What |
|---|---|
| `src/eidos` | lab: ledger, command service, API, workflows, investigator, supervisor, CLI, MCP, reports |
| `packages/` | `eidos-wire`, `eidos-worker-sdk`, `eidos-pytorch`: standard-library worker packages for research environments |
| `components/` | built-in research components |
| `examples/` | switch-order and image example projects |
| `research/public/` | imported public research release (immutable; its own notices; not covered by the licence below) |

## License

Apache License 2.0: [`LICENSE`](LICENSE), [`NOTICE`](NOTICE). The licence covers this repository except `research/public/`, which keeps its own notices (`research/public/development/NOTICES.md`). Third-party licences: [`docs/ops/LICENSES.md`](docs/ops/LICENSES.md).
