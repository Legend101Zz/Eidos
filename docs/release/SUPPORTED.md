# Eidos v1 supported scope

This is the scope of the **v1 software releases (1.0.0, 1.0.1)** (acceptance areas A1–A15 of `ACCEPTANCE_MATRIX.md`). Anything not listed under "Supported" is **not supported in v1**. That is a scope decision, not a failed check. Each row says why it is out of scope.

v1.0.0 makes no scientific claim. Whether Eidos helps an LLM do model research (A16) is a separate, later release, tagged `eval-*`.

## Supported

| Area | Supported in v1 | Evidence |
|---|---|---|
| Operating systems | **Linux x86_64** and **macOS arm64** (Apple Silicon) | Linux: hosted CI (13 Linux jobs of the 15-job workflow, since 1.1.0). macOS: the owner's M4 (macOS 26.3.1) and the macOS CI runner. See `docs/mac/STATUS.md`. |
| Devices | **CPU** on both platforms; **MPS** on macOS | CPU: switch journey and components. MPS: cpu/mps parity and cross-device resume (`docs/mac/receipts/a13-mps-smoke.json`). |
| Model providers | **Anthropic** (`anthropic:<model>`) and **OpenAI-compatible endpoints** (`openai-compatible:<model>` with a `base_url`) | Anthropic: live receipts and full-lab runs with `claude-sonnet-5-5`. OpenAI-compatible: the adapter is verified against a local Ollama endpoint (errors, timeout, request limit, unknown model, schema-valid decisions). **No local model of 14B or less passed full decision-schema conformance on the owner's Mac**: `qwen2.5:14b` passed 4/8 and was too slow for an investigation. Use a capable model; the result is recorded in G4 of `ACCEPTANCE_MATRIX.md` § E. Since 1.1.0, `investigator.settings.output_mode: "json"` adds bounded structured-output repair and JSON mode (ADR 0014): it removes malformed-output failures (qwen2.5 3B 1→5 and 7B 0→5 decisions obtained of 5), but not semantic ones (3B 1→2, 7B 0→1 executable of 5); `docs/release/evidence/g4-110-summary.json`. Since 1.1.1, **reasoning models** (ADR 0016): **MiniMax-M3.1-Flash** through its OpenAI-compatible endpoint (`https://api.minimax.io/v1`) passed **8/8 in `json` mode** and 6/8 in `tool` mode with the settings below (`docs/release/evidence/g4-111-minimax-m31flash-*.json`). |
| MCP clients | **Claude Code**, **Codex CLI**, and the **MCP Python SDK** client (stdio and loopback HTTP) | `docs/mac/receipts/a14-*.json`; `tests/mcp`; the CI e2e journey. |
| Candidate-code isolation, Linux | **`linux:bwrap-sealed`**: the only profile for **sealed (hostile-code) evaluation**. Since 1.1.0, with root setup, candidate code runs as the distinct `eidos-cand` user and the assessor keeps the lab identity (`separate-identity` passed for both; ADR 0011) | Hostile probes in `tests/integration/test_isolation_probes.py`; the sealed runner; CI job `separate-identity` (`tests/integration/test_separate_identity_e2e.py`, as root). |
| Candidate-code isolation, macOS | **`macos:seatbelt-sealed`**: Seatbelt (`sandbox-exec`), **same OS user** (limits below) | `scripts/ci/seatbelt_probes.py` (macOS CI and the owner's Mac). |
| Provider keys | Since 1.1.0: the **OS secret store** (`lab credentials set|delete|status`; macOS Keychain, Linux libsecret), environment variables, or `lab start --credentials-stdin`. No plaintext store. Keys reach only the investigator worker | `tests/unit/test_secret_store.py`; CI jobs `secret-store-macos` (a temporary keychain) and `secret-store-linux` (gnome-keyring on a private D-Bus session); ADR 0012. |
| Component LLM calls | Since 1.1.0: **scoped, budgeted grants through the lab's credential broker** for components that declare `llm` use and projects that enable `policy.llm_broker`; worker SDK client `eidos_worker.llm` (Python 3.9+, standard library). Sealed runs get a grant only when the run or plan allows it, and reach only the broker socket | `tests/integration/test_llm_broker.py` (fake provider: budget refusal, expiry, wrong invocation, key never in the component's view, receipts, uncertain usage, crash recovery, a Seatbelt-sealed job); example `components/eidos.synthetic.llm-examples`; ADR 0013. Live providers through the broker are not separately qualified. |
| Development execution | `dev:same-user` on both platforms, for exploration only, with a recorded waiver when stronger isolation is available | |
| Services | Local PostgreSQL 14+ (operator-installed) and the regular Temporal Server 1.32.2 (pinned, digest-verified, or built from source), managed by `lab` | `docs/ops/INSTALL_AND_OPERATE.md`. |
| Python | Lab: Python 3.11–3.13. Worker SDK: Python 3.9+, including Pydantic v1 projects | `tests/worker_compat/run_matrix.sh`. |
| Worker wire contract | **Schema major 1** (`eidos-wire/1`), JSON Schema 2020-12 files in `schemas/eidos-wire/v1/` generated from the server boundary models. Worker packages `eidos-wire`, `eidos-worker-sdk`, `eidos-pytorch`: **Python 3.9 and later** (standard library only; Pydantic v1 projects work). The server validates every received manifest against the schema; unknown fields from a newer minor are accepted | `tests/unit/test_wire_schemas.py`, `packages/eidos-worker-sdk/tests/test_wire_fixtures.py` (worker-compat matrix, Python 3.9 + pydantic<2), ADR `docs/decisions/0008-versioned-wire-json-schemas.md`. |

## Reasoning models (since 1.1.1; ADR 0016)

A reasoning model spends part of each request's output-token cap on reasoning, and its requests take longer. With the
1.1.0 defaults (4096 output tokens, 120 s) MiniMax-M3.1-Flash failed 3 of 5 decision scenarios with `Model token
limit (4096) exceeded before any response was generated`. Recommended `investigator.settings`:

| Setting | Recommended | Why |
|---|---|---|
| `output_mode` | `"json"` (OpenAI-compatible endpoints) | 8/8 against 6/8 for `tool` in the G4 1.1.1 measurement; read tools work through the read-request loop |
| `max_output_tokens` | `16384` | reasoning tokens count against it; one `tool`-mode request still ran past 16384 |
| `request_timeout` | `300` (seconds) | the longest one-request decision took about 62 s in `json`, the four-request read decision 145 s, a two-request `tool` decision about 121 s; at 16384 tokens / 120 s one request timed out in 1.1.0 |
| `turn_deadline_seconds` | `1200` (at least 2 × `request_timeout`) | the `decide` activity's timeout; the text-mode loop does not start a request that could outlive it (`bounded_outcome: decision-deadline`) |
| `max_requests_per_decision` | `6` to `8` (default 8) | read rounds and the re-ask count against it; the measured read decision used 4 |
| `max_tokens_per_decision` | `120000` (default) | the reservation prices `max_requests_per_decision × max_output_tokens` of it at the output rate, which at 8 × 16384 is all of it |

Usage receipts record reasoning tokens (`reasoning_tokens`, `reasoning_accounting`). They are counted inside the
output tokens, as the OpenAI-compatible API reports them; set `reasoning_tokens_billing: "separate"` for a provider
that counts them separately.

Measured (one run per configuration, 2026-11-10, five decision scenarios of eight):

| Model | Mode | Cap / timeout | Passed (of 8) | Requests | Output tokens (reasoning) |
|---|---|---|---|---|---|
| MiniMax-M3.1-Flash, 1.1.0 code | json | 4096 / 120 s | 5 | | |
| MiniMax-M3.1-Flash, 1.1.0 code | tool | 4096 / 120 s | 4 | | |
| MiniMax-M3.1-Flash, 1.1.0 code (patched cap) | json | 16384 / 120 s | 6 | | |
| MiniMax-M3.1-Flash, 1.1.0 code (patched cap) | tool | 16384 / 120 s | 4 | | |
| MiniMax-M3.1-Flash, **1.1.1** | **json** | 16384 / 300 s | **8** | 11 | 34127 (25690) |
| MiniMax-M3.1-Flash, **1.1.1** | tool | 16384 / 300 s | 6 | 15 | 82150 (51141) |

`tool` mode's two failures: one decision ran past 16384 output tokens (22770 of its 24326 output tokens were
reasoning), and one named a tool that does not exist (`catalog_query`), which the lab refused. One run per
configuration: a one-scenario difference is within run-to-run variation.

## Isolation: what Seatbelt does and does not do

Profiles are sets of probed controls, recorded in an isolation receipt for each attempt. Only **hostile-code profiles** satisfy a plan that requires sealed execution (`required_isolation.sealed: true`, or a named hostile-code profile). In v1 that is **`linux:bwrap-sealed`** only.

`macos:seatbelt-sealed` runs candidate code **as the same OS user as the lab**. It passes these probed controls:
- the lab home, credential directories and the user's home directory are unreadable;
- writes only to staging, scratch and resume directories;
- no network;
- a cleared environment;
- no inspection of other processes;
- no exec of `launchctl`, `osascript` or `open`, and no securityd mach services.

It does **not** contain a hostile same-user program:
- **launchd and XPC stay reachable by other routes**, for example a copied `launchctl` binary or direct mach messages (adversarial review 2). Mach IPC is not filtered.
- Signals to other same-user processes are not denied, because training code must be able to signal its own children.
- Hard links into exempted directories may be possible (low confidence, review 2).
- `sandbox-exec` is deprecated by Apple. If a macOS release breaks it, the lab falls back to `dev:same-user`.
- The macOS assessor also runs under Seatbelt as the same OS user. Its isolation receipt says so: `same_os_user: true`, `hostile_code_isolation: false`.

So, on macOS:
- **Plans requiring sealed execution are refused** at `freeze_evidence_plan` and again at `launch_evidence_plan`, with a message naming the lab's strongest profile and `linux:bwrap-sealed`.
- A Seatbelt receipt **never** satisfies a sealed requirement at assessment: the isolation finding blocks protocol-passed. Code: `src/eidos/domain/isolation.py` (`HOSTILE_CODE_PROFILES`, `requires_sealed`). Tests: `tests/unit/test_seatbelt_policy.py`, `tests/integration/test_sealed_plan_refusal.py`.
- Seatbelt still satisfies **control-level** requirements (a plan listing individual controls), and it is the default for candidate code and model-chosen commands on macOS. It raises the bar against accidental or unsophisticated access.

Hostile-code evaluation on a Mac needs a Linux machine (or, later, a VM profile).

On Linux, `linux:bwrap-sealed` reports `separate-identity` as failed unless a distinct candidate user exists and the interpreter is readable by it (for example under `/opt`). A plan that lists `separate-identity` explicitly is satisfied only when the control passed.

## Not supported in v1

| Item | Status | Why |
|---|---|---|
| Windows | not supported in v1 | Owner decision: no Windows service manager, sandbox or CI in scope. |
| CUDA / GPU other than MPS | not supported in v1 | No CUDA hardware was available to qualify restore classes and determinism. The code paths for device profiles exist, but are unqualified. |
| Distributed or multi-node training | not supported in v1 | The design keeps the adapter boundary (gang scheduling, rank loss). Single-node only. |
| Lima VM (`macos:lima-vm-sealed`) | not supported in v1 | Owner decision: Seatbelt with documented limits. The profile name is reserved and is not implemented. |
| Sealed (hostile-code) evaluation on macOS | not supported in v1 | Seatbelt runs as the same OS user (see above). Use `linux:bwrap-sealed`. |
| Claude Desktop as an MCP client | not supported in v1 | Owner decision. A config snippet exists (`scripts/qualify/mcp_clients/`), and was checked statically and over stdio, but Claude Desktop itself is not qualified. |
| Hosted OpenAI-compatible services (OpenAI, vLLM servers, etc.) | protocol-compatible, not separately qualified | The adapter speaks the OpenAI-compatible protocol and was qualified against a local Ollama endpoint. Hosted services were not run. |
| Remote (non-loopback) API exposure | not supported in v1 | The HTTP API binds loopback. Remote exposure needs its own authenticated transport profile (design §Operator/API integrity). |
| Full TUI or web app, hosted multitenancy, cluster scheduling | not supported in v1 | Outside the local v1 release contract (design §Complete local v1). |
| Multi-day investigations | not exercised | A 20-minute soak, Continue-As-New rollover, suspend/resume and abrupt-stop recovery are verified. A multi-day run is not. |
| Scientific claims (A16) | separate later release (`eval-*` tags) | It needs blinded, independent evaluation across tasks and hardware. |

## Optional integrations (since 1.1.0; ADR 0015, which supersedes ADR 0009)

These integrations are optional in the design ("How the four scientific libraries enter this design") and are not part of acceptance areas A1–A16. The adapters are separate worker-side packages installed into the research project's environment; the lab server and `eidos-worker-sdk` gain no dependency.

| Integration | Status | Pinned and tested with | Evidence |
|---|---|---|---|
| PyGlove transform/search-space adapter | `eidos-pyglove` 0.1.0: enumerate/materialise candidates by DNA, `pg.patch` transforms, export of the materialised PyTorch builder with its transform revision; loading checks the revision and allowed modules | `pyglove==0.4.5`, CPython 3.12, `torch==2.7.0` CPU | `packages/eidos-pyglove/tests` (82 tests); CI job `adapter-pyglove` |
| MosaicML Composer trainer adapter | `eidos-composer` 0.1.0: a Composer callback that fires `eidos-pytorch` training-extension phases for the verified events only (`after-model-construction`, `after-backward`, `after-optimizer-step`, `on-evaluation`, `on-checkpoint-save`, `on-checkpoint-load`); gradient accumulation refused. Composer upstream is archived and 0.32.1 loads checkpoints with pickle: load only checkpoints the same run wrote | `mosaicml==0.32.1`, CPython 3.12, `torch==2.7.0`, `torchvision==0.22.0` CPU | `packages/eidos-composer/tests` (26 tests: phase counts, gradient/parameter state per phase, restore on resume); CI job `adapter-composer` |
| CheckList behavioural contracts | adopted: `behavior-suite/1` (`components/eidos.behavior.suite` 0.1.0) with invariance and direction-of-change templates, author-supplied transformations and groups, scored from recorded raw predictions; plus the earlier paired expectations and restore-invariance contract | standard library | `tests/components/test_behavior_suite.py`, component fixtures |

## Where this is referenced

`README.md`, `HANDOFF.md`, `ACCEPTANCE_MATRIX.md`, `docs/release/RELEASE_NOTES_v1.0.0.md`, `CHANGELOG.md`.
