# Eidos v1 supported scope

This is the scope of the **v1.0.0 software release** (acceptance areas A1–A15 of `ACCEPTANCE_MATRIX.md`). Anything not listed under "Supported" is **not supported in v1**. That is a scope decision, not a failed check. Each row says why it is out of scope.

v1.0.0 makes no scientific claim. Whether Eidos helps an LLM do model research (A16) is a separate, later release, tagged `eval-*`.

## Supported

| Area | Supported in v1 | Evidence |
|---|---|---|
| Operating systems | **Linux x86_64** and **macOS arm64** (Apple Silicon) | Linux: hosted CI (9 Linux jobs of the 10-job workflow). macOS: the owner's M4 (macOS 26.3.1) and the macOS CI runner. See `docs/mac/STATUS.md`. |
| Devices | **CPU** on both platforms; **MPS** on macOS | CPU: switch journey and components. MPS: cpu/mps parity and cross-device resume (`docs/mac/receipts/a13-mps-smoke.json`). |
| Model providers | **Anthropic** (`anthropic:<model>`) and **OpenAI-compatible endpoints** (`openai-compatible:<model>` with a `base_url`) | Anthropic: live receipts and full-lab runs with `claude-sonnet-5-5`. OpenAI-compatible: adapter path verified against a local Ollama endpoint (errors, timeout, request limit, unknown model). For schema conformance with a capable local model, see gate G4 in `ACCEPTANCE_MATRIX.md` § E. |
| MCP clients | **Claude Code**, **Codex CLI**, and the **MCP Python SDK** client (stdio and loopback HTTP) | `docs/mac/receipts/a14-*.json`; `tests/mcp`; the CI e2e journey. |
| Candidate-code isolation, Linux | **`linux:bwrap-sealed`**: the only profile for **sealed (hostile-code) evaluation** | Hostile probes in `tests/integration/test_isolation_probes.py`; the sealed runner. |
| Candidate-code isolation, macOS | **`macos:seatbelt-sealed`**: Seatbelt (`sandbox-exec`), **same OS user** (limits below) | `scripts/ci/seatbelt_probes.py` (macOS CI and the owner's Mac). |
| Development execution | `dev:same-user` on both platforms, for exploration only, with a recorded waiver when stronger isolation is available | |
| Services | Local PostgreSQL 14+ (operator-installed) and the regular Temporal Server 1.32.2 (pinned, digest-verified, or built from source), managed by `lab` | `docs/ops/INSTALL_AND_OPERATE.md`. |
| Python | Lab: Python 3.11–3.13. Worker SDK: Python 3.9+, including Pydantic v1 projects | `tests/worker_compat/run_matrix.sh`. |

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

## Where this is referenced

`README.md`, `HANDOFF.md`, `ACCEPTANCE_MATRIX.md`, `docs/release/RELEASE_NOTES_v1.0.0.md`, `CHANGELOG.md`.
