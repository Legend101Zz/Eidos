# Mac-side qualification status (local Mac session)

Machine: Apple M4, 24 GiB, macOS 26.3.1 (arm64). All environments, caches, labs and run outputs on the external SSD
(`/Volumes/Mrigesh SSD/eidos-mac`, via `env.sh`). Branch `mac/local-qualification` (from `origin/build/eidos-local-v1`
at 80c1e2d; merged 48d4338 at start). Internal disk free at start: 24.9 GiB.

## Item 1: macOS researcher-machine qualification (A1)

**Evidence class: macOS-researcher-machine runtime-verified** (same OS user; no `separate-identity`; no Lima VM).

What ran:

| Run | Command | SHA | Result |
|---|---|---|---|
| kit-1 | `PG_BIN=/opt/homebrew/opt/postgresql@16/bin EIDOS_KIT_RECEIPT=... bash scripts/qualify/macos_owner_kit.sh --keep` | 48d4338 + TMPDIR fix | passed (13/13 steps); job under `macos:seatbelt-sealed` |
| job-path home probe | kit-1 lab restarted with a fake `ANTHROPIC_API_KEY`; `lab run macq eidos.project.command@1.0.6` with a probe script | 48d4338 wheels | **FAILED containment**: key hidden, network denied, but the planted `~/.eidos-planted-homeprobe` and `~/.claude.json` were readable (`receipts/a1-job-path-home-read-before-fix.json`) |
| seatbelt probes | `OUT=... .venv/bin/python scripts/ci/seatbelt_probes.py` | 48d4338, then b7d9ed1 | passed both times (7 sealed controls passed; `separate-identity` failed as expected; negative control detected every access; permitted computation ok) |
| assessor probe | `scripts/ci/assessor_sandbox_probe.py` | 48d4338 | `protocol-passed`, all 7 controls passed under `macos:seatbelt-sealed` |
| kit-2 | kit at 1cf2728+edits | — | `init` failed: GitHub release asset HTTP 504 after 5 attempts (and 6 more `curl` tries); not a code failure |
| kit-3 | kit with `EIDOS_TEMPORAL_ARCHIVE_CACHE` (archive fetched via `gh release download`, sha256 = pinned `6c34c8ac…b2f0`) | 1cf2728 + kit edits (dirty) | passed 15/15 |
| **kit-4** | same, clean tree | **36aaffa** | **passed 15/15** (`receipts/a1-macos-owner-kit.json`): fresh wheels (`--no-cache`), init 13 s, start, doctor (only warnings: `separate-identity`, `lima-vm`), project, charter, fixture investigation (turn 1 → `input-requested`), real job `lab.pairs.by_group` under `macos:seatbelt-sealed`, `sealed_probe` (key hidden, `$HOME` file and listing refused, external network and lab API refused), stop, uninstall (no process/port left, venv+lab removed) |
| contained commands | `EIDOS_RESEARCH_PYTHON=$S/venvs/research/bin/python pytest -q tests/integration/test_contained_commands.py` | 48d4338 | 4 passed (incl. the D02 baseline contained) |

Fixes:

- **b7d9ed1 (core, security)**: sealed lab jobs and the assessor could read the user's home directory on macOS.
  The runner's sealed spec denied only the lab root + a fixed credential-dir list (`run_contained` already denied
  `$HOME`). Now `launcher.sealed_deny_read()` (lab roots + `$HOME` + credential dirs) is used by the runner, the
  assessor sandbox, `run_contained` and the CI probes; `python_prefixes()` also re-allows the interpreter's
  `sys.path`; the `no-credential-read` probe also fails if `$HOME` can be listed. Regression test
  `tests/integration/test_sealed_home_read.py` (4 tests, fail without the fix; project roots inside `$HOME` stay readable).
  Linux `bwrap` was not affected (home is not mounted).
- 1cf2728, 36aaffa (kit): work dir under `$TMPDIR` (was hard-coded `/tmp`), `EIDOS_KIT_RECEIPT`; new `sealed_probe`
  and `uninstall` steps; the receipt recorded the receipt-writer's Python (3.14.5) instead of the lab's (3.12.8); job
  isolation profile in the receipt; logs kept next to a failed receipt; doc's expected doctor warnings were wrong for
  macOS (it said "sealed profile unavailable (no bubblewrap)"; doctor reports seatbelt ok).

What was wrong in the cloud-written kit/runbook (for the cloud session):

- `mktemp -d /tmp/...` ignores `TMPDIR` (writes to the internal disk).
- No uninstall step and no containment probe of the job path, although the brief/A1 needs both.
- `doctor` step can never fail (`...; true`); only the receipt's `passed` reflects doctor `fail` checks (left as is).
- `lab init`'s "next" hint prints `EIDOS_HOME=/Volumes/Mrigesh SSD/... lab start` unquoted (breaks on paths with spaces).

Open: GitHub release-asset downloads (browser URL) returned 504 for the whole session; the API asset route worked. See REQUESTS.md.

## Item 2: real MCP clients (A14)

**Evidence class: macOS-researcher-machine runtime-verified** for Claude Code and Codex CLI as external MCP hosts over
stdio. Claude Desktop: **unqualified** (not installed); its snippet was verified without Desktop (owner item).

Lab: `EIDOS_HOME=$S/lab`, venv `$S/venvs/lab` from fresh wheels at 41ccf01 (`uv pip install --no-cache --reinstall`),
project `macq`, fixture investigation `inv-mcpq` (`--policy note-then-wait`). Nothing global was edited: configs are
per-invocation files from `gen_configs.py`.

| Run | Command | Result |
|---|---|---|
| Claude Code read | `run_clients.sh --home $S/lab --lab $S/venvs/lab/bin/lab --project macq --investigation inv-mcpq --out $S/runs/mcp/run1 --write --only claude --run` | ok: called `get_investigation`, `list_events`, `list_components`, read `lab://projects/macq/investigations/inv-mcpq`; final JSON status `active` and 4 components = lab (`a14-claude.json`) |
| Claude Code write | same run, `--write` phase | ok: one `steer_investigation`; lab record holds exactly one steering entry with the marker (by `admin`); revision 1→2 (`a14-claude-write.json`) |
| Codex read | same with `--only codex` | ok: `get_investigation`, `list_events`, `list_components`, and it read the resource too (`read_mcp_resource`); final JSON = lab (`a14-codex.json`) |
| Codex write | same | ok: one `steer_investigation`, exactly one entry, revision 2→3; the steering wake produced turn 2 in the lab (`a14-codex-write.json`) |
| Desktop snippet | `$S/venvs/lab/bin/python scripts/qualify/mcp_clients/check_desktop_snippet.py <snippet> --project macq --investigation inv-mcpq` | ok: static checks; live stdio launch of exactly that command/env: `initialize` (`eidos-lab`, protocol 2025-11-25), 34 tools, 7 resource templates, `get_investigation` (`a14-claude-desktop-snippet-check.json`) |

Usage: Claude Code runs used the Max subscription (4 and 6 turns, model `claude-opus-5-5`); Codex used its own account
(input 161k/83k tokens, mostly cached; output 328/231). No Console credits.

Fixes to the cloud-written scripts (not run on a Mac before): the verifier searched the whole event stream for the
status (tool results contain it), so it now judges the final answer and its JSON line against the lab; there was no
write-path check (added `--write`, `--only`); `EIDOS_HOME` was not passed to the verifier's `lab query`; added
`--no-session-persistence` / `--ephemeral`; client stdin from `/dev/null` (Codex waits on stdin otherwise: "Reading
additional input from stdin..."). Runbook assumptions corrected (Codex needs no approval override and does read
resources; `--max-turns` works although not in `--help`).

## Item 3: MPS (A13)

**Evidence class: macOS-researcher-machine runtime-verified** for the eidos-pytorch training path on `mps` vs `cpu`,
`devices()` reporting and checkpoint save/restore across cpu/mps. **CUDA: unqualified (no hardware).**

What ran (research venv from `research/public/development/requirements.lock`, torch 2.15.0, Python 3.12.8):

| Run | Command | SHA | Result |
|---|---|---|---|
| smoke, before fix | `$S/venvs/research/bin/python scripts/qualify/mps_smoke.py --require-mps --out ...` | c1efd8e | **fail**: MPS `train_loop(deterministic=True)` raised `embedding_dense_backward_mps does not have a deterministic implementation`; the script's retry with `deterministic=False` failed the same way because determinism stayed on process-wide (`receipts/a13-mps-smoke-pre-fix.json`) |
| **smoke** | same (script extended) | **5813dc2** | **ok** (`receipts/a13-mps-smoke.json`): cpu and mps final loss 0.2757698 (identical), eval accuracy 0.984 both, parameter max diff ≤ 4e-7; params on `mps:0`; MPS determinism `warn-only` with the embedding backward listed; no CPU fallbacks; `devices()` reports mps available, cuda not, `exact_restore_across_devices: false`. Checkpoint at step 20, resume to 45: cpu→cpu bit-exact; mps→mps, mps→cpu, cpu→mps within tolerance (Δloss ≤ 3e-8, Δacc 0) |
| eidos-pytorch tests | `$S/venvs/torchtest/bin/python -m pytest -q packages/eidos-pytorch/tests` (torch 2.15.0 arm64) | 5813dc2 | 19 passed (incl. 3 MPS-only tests that ran on MPS) |
| components | `EIDOS_TORCH_PYTHON=$S/venvs/torchtest/bin/python ... pytest -q -rs tests/components` after `build_fixtures.py` | 5813dc2 | 59 passed, 0 skipped |

Fixes:
- **5813dc2 (core, eidos-pytorch)**: `train_loop` scopes determinism (restores the previous global setting, also on
  exceptions); on `mps` a deterministic request runs warn-only and the receipt records `device` and
  `determinism {requested, mode, nondeterministic_ops}`; `rng.mps` captured/restored in checkpoints. Regression tests
  in `packages/eidos-pytorch/tests/test_pytorch.py` (5 new; all fail without the fix).
- 5813dc2^ (script): `mps_smoke.py` now also checks checkpoint save/restore across devices, records determinism and
  `devices()`; same-device CPU resume must be exact.

What was wrong in the cloud-written smoke: its retry path could never work (global determinism leaked from the first
call), and it did not test checkpoint save/restore across devices or device reporting, which A13 needs.

## Item 4: OpenAI-compatible provider, local endpoint (part of A9)

**Evidence class: real-LLM development (local model, no cost)** for the `openai-compatible` adapter path.
**Schema conformance of `qwen2.5:3b-instruct`: failed** (a model finding, not tuned for).

Setup: private `ollama serve` (Ollama 0.9.6) started by this session with `HOME=$S/ollama-home`,
`OLLAMA_MODELS=$S/ollama-models`, `OLLAMA_HOST=127.0.0.1:11535` (the owner's `~/.ollama` and server untouched); one
model pulled, `qwen2.5:3b-instruct` (1.9 GB, on the SSD); server stopped by its PID afterwards.

Command (SHA c1efd8e+ with the script change committed below):
`PYTHONPATH=src:packages/eidos-wire/src .venv/bin/python scripts/qualify/live_provider_check.py --model openai-compatible:qwen2.5:3b-instruct --base-url http://127.0.0.1:11535/v1`

Result (`receipts/a9-openai-compatible-ollama-qwen2.5-3b.json`), 13 requests, 3/8 scenarios pass:

| Scenario | Outcome |
|---|---|
| conformance-fresh / after-baselines / large-catalog / read-tools-available | model-call-failed: `UnexpectedModelBehavior: Exceeded maximum output retries (1)` (the model did not produce a valid structured decision) |
| conformance-failed-job | a schema-valid but empty decision (no calls, no control): not a pass |
| provider-timeout | pass: `ModelTimeoutError`, retryable |
| request-limit-with-tool-loop | pass: bounded `request-budget-exhausted` outcome |
| unknown-model-id | pass: `ModelNotFoundError` 404, non-retryable |

What this shows: the adapter's OpenAI-compatible transport, timeout/404 classification, disabled transport retries
and request bounding work against a real OpenAI-compatible server. What it does not show: that a ≤3B local model can
act as an investigator (it cannot here); larger or hosted OpenAI-compatible models remain unqualified.

Fix to the cloud-written script: `live_provider_check.py` could only run Anthropic (it required `ANTHROPIC_API_KEY`
and hard-coded `anthropic:` for the unknown-model scenario); it now takes `--base-url`/`--api-key-env` and runs the
same scenarios on any provider.

## Item 5: real-model investigations with Anthropic (A8)

**Evidence class: real-LLM DEVELOPMENT runs on macOS arm64** (no builder intervention during any run; investigator
saw only the bundle: brief, task.json, project.py, train/validation; no test data, labels or reference solution).

### Security precondition (before any paid call)

| Check | How | Result |
|---|---|---|
| contained commands | `EIDOS_RESEARCH_PYTHON=$S/venvs/research/bin/python pytest -q tests/integration/test_contained_commands.py tests/integration/test_sealed_home_read.py` at the merged head | 5 + 5 passed |
| driver path, end to end | `live_bundle_investigation.py --model replay:<script>` (offline, no key) with a fake `ANTHROPIC_API_KEY` in the driver's environment; scripted decisions: a hostile probe, the D02 baseline, scoring | probe: key hidden, planted `$HOME` file and `$HOME` listing refused, network refused (env of the command: 9 variables); baseline trained; validation accuracy 0.838; all under `macos:seatbelt-sealed` |
| lab job path | kit-4 `sealed_probe` (item 1) | held after b7d9ed1 |
| **lab services** | lab started with a fake key marker; `ps eww` over the user's processes | **FAILED before e36bfb1**: the key was in the supervisor, the PostgreSQL postmaster and the investigator worker. Fixed in **e36bfb1** (supervisor gets credentials over stdin, scrubbed env for every service, `lab start --credentials-stdin`); after it only `eidos.apps.role investigator-worker` held it (both env and stdin paths), and during the real full-lab run as well |

### Runs (all `anthropic:claude-sonnet-5-5`, served `claude-sonnet-5-5`; list price $2/$10 per Mtok)

| Run | Command | SHA | Requests | Tokens in/out | Cost | Outcome |
|---|---|---|---|---|---|---|
| 5.1 lab-free D02 | `with_anthropic_key.py -- .venv/bin/python scripts/qualify/live_bundle_investigation.py --bundle $S/bundles/D02 --python $S/venvs/research/bin/python --max-requests 40 --max-turns 8` | e36bfb1 | 8 | 29,434 / 8,073 | $0.1396 (settled 14c) | turn cap (8 turns, 1 request each), all 8 decisions accepted, 0 refused; baseline reproduced (val acc 0.838), a seed variant 0.830, then a model-authored modification failed (`exit 2/3`); no conclusion (`a8-live-D02.json`) |
| 5.1b lab-free D12 (null) | same with D12 | e36bfb1 | 8 | 28,783 / 9,860 | $0.1562 (settled 16c) | turn cap; 8 accepted, 0 refused; val acc 0.5 (chance) twice; authored one candidate project file, its runs failed; no improvement claimed (`a8-live-D12.json`) |
| 5.2 full lab D02 | `with_anthropic_key.py --stdin-credential ... -- /bin/bash scripts/qualify/full_lab_live.sh --bundle $S/bundles/D02 ...` (`CREDENTIALS_STDIN=1`, `PRICE={"input":2,"output":10}`) | 4d494ce scripts; lab wheels from e36bfb1 sources | 20 | 172,477 / 12,130 | $0.4663 (lab `model_usd` used 0.466254; settled 47c) | 5 turns (cap), 5 decisions accepted, 0 refused, every decision `wait_for_jobs` after one `run_component`; the turn-1 baseline job **failed** because `eidos.project.command` places inputs in the work dir only via `config.files` (the lab-free driver defaults it, so the identical call works there) and its failure (`job.terminal.error`, with the stderr tail) was in the investigator's context [correction: an earlier version of this line said `error: null`; that came from my receipt query reading `attempts[-1].error`, see the re-run section]; turns 2-5 probed the staging layout; no conclusion. `key_found_in_lab_files: []` (`a8-full-lab-D02.json`, worker log `a8-full-lab-D02-investigator-worker.log`) |

Ledger (`eidosctl ancillary`): reserved 365/365/310c, settled **14 + 16 + 47 = 77c**; ancillary available now 867c of
1000c (was 944c). Claude Code / Codex runs (item 2) used the Max subscription / Codex account, not Console credits.

Fixes:
- **e36bfb1 (core, security)**: provider key only in the investigator worker (see above); regression tests
  `tests/unit/test_supervisor_credentials.py` (4).
- 4d494ce (script) `full_lab_live.sh`: key over stdin; `PRICE="${PRICE:-{...}}"` appended a stray `}` whenever PRICE
  was set (broke `--settings`); unquoted `$LAB`; builtins helper used `lab` from PATH; the wait loop never ended for a
  needs-input investigation (90 min); doctor output to `/tmp`.

Observations: under Seatbelt, torch's libomp prints `OMP: Warning #179: Function Can't set size of /tmp file failed`
(it ignores TMPDIR; harmless). The lab-free driver's results over-state what the same model does in the full lab
because of the `files` default difference above.

## Other evidence from this session

Full macOS test run by a Sonnet subagent (own PostgreSQL on 5499, Temporal CLI dev server, private lab ports), at
b7d9ed1 and again at e36bfb1: unit 43, integration 67 (+3 legitimate bwrap/`/opt/eidos` skips), mcp 18 (+1 skip;
19/19 against a live lab), reporting 7, components 59 (no skips), sdk 20, eidos-pytorch 19 (torch 2.15.0 Mac wheel),
`fresh_install.sh` ok, `e2e_journey.sh` JOURNEY PASS, switch journey 154/154, `upgrade_across_releases.sh` 33/33,
`research_smoke.sh` ok, seatbelt and assessor probes ok, lint + import contracts clean. Logs: `$S/suite/logs{,2}/`.

Storage: internal free 24.9 GiB at start; see the final report for the end figure. One internal-disk write not under
my control: `~/Library/Caches/ollama/updates/.../Ollama-darwin.zip` (218 MB, 16:26, while my Ollama server ran with
`HOME` on the SSD; probably Ollama's updater). Left in place (owner's Ollama setup); see REQUESTS.md.

## Final (2026-11-08)

Merged `origin/build/eidos-local-v1` (no new commits after fa8c19e's successors already merged). On the final head:
ruff (src tests scripts) clean, import contracts 5/5, unit 43, contained/home/seatbelt-policy 15, eidos-pytorch 19
(torch 2.15.0 arm64, MPS tests ran on MPS), components 59 (no skips). The full lab-level suites (integration, journeys,
fresh install, upgrade) ran green at e36bfb1 (subagent); later commits changed only scripts, docs and one test.
All servers started by this session (lab, kit labs, Ollama on 11535, test PostgreSQL on 5499) are stopped.
Internal disk free: 24.9 GiB at start, 24.5 GiB at end (≈0.33 GiB, of which 218 MB is the Ollama update cache noted
in REQUESTS.md; the rest is system/client-session noise; no Eidos data on the internal disk).

## Re-run: full-lab D02 after the cloud's project.command fix (2026-11-08)

Merged `origin/build/eidos-local-v1` at 2df52bb (fast-forward; includes merge 99be3ba and the a39ab1a fix:
`eidos.project.command` 1.0.7 stages declared inputs by default). Fresh wheels with `uv build --no-cache` into
`$S/dist-2df52bb`, lab venv reinstalled `--no-cache --reinstall`.

Key re-check before the paid run (fake marker via `lab start --credentials-stdin`, then a probe job through
`eidos.project.command@1.0.7`): only `eidos.apps.role investigator-worker` held the marker; the job's environment had 10
variables and no key; `$HOME`, `~/.claude.json`, external network and the lab API were refused; no lab file contained it.

Run: `full_lab_live.sh` with `MAX_TURNS=12 MAX_CALLS_PER_TURN=1 MAX_REQ_PER_DECISION=5` (≤ 60 requests; refused-proposal
retries count as turns) `MODEL_USD=2 MODEL_TOKENS=1000000`, `anthropic:claude-sonnet-5-5`, key via stdin
(`receipts/a8-full-lab-D02-rerun.json`). The script now takes these caps from the environment (defaults unchanged)
and records job errors/metrics, call arguments and rationales.

| | First run (5.2) | Re-run |
|---|---|---|
| Wheels / component | e36bfb1 sources, project.command 1.0.6 | 2df52bb, project.command 1.0.7 |
| Caps | 5 turns × 2 × 4 = 40 req, $3 | 12 turns × 1 × 5 = 60 req, $2 |
| Baseline job | **failed** (inputs not staged without `config.files`) | **succeeded first time**, val acc 0.8384 (scored in turn 2) |
| Turns / decisions | 5 / 5 accepted, 0 refused | 3 / 3 accepted, 0 refused |
| Jobs | 5 (1 failed baseline, 4 staging probes) | 2 (baseline, scoring), both succeeded |
| What it did | probed the work-dir layout | baseline → score → `author_component` for a standardized-input MLP trainer |
| End | max turns, no conclusion | **stalled** after turn 3; operator stop (labelled builder intervention); no conclusion |
| Claim | none | none (it stated "probably under-trained" as a hypothesis; nothing filed, nothing to support) |
| Requests / tokens | 20 / 172,477 in, 12,130 out | 15 / 122,044 in, 6,706 out |
| Cost (settled) | $0.4663 (47c) | $0.3111 (32c; reserved 250c) |
| Key in lab files | [] | [] |

Why the re-run stalled (new defect, platform-independent): the turn-3 `author_component` call was **INVALID** (the
model passed `environment: {"base": "project-root"}`; `validate_component` resolves `environment` as a registered
environment name, and the context line `author_component {files:{path: text}, environment}` does not say so). No
validation job was created, but the decision's control was `wait_for_jobs` and the turn ended `waiting`. The
idle-trap rule only refuses pure waits at acceptance time, and a failed call is not in `NEWS_TYPES`, so nothing
woke the investigation (max idle 6 h). Also, in the full lab `author_component` builds a *component* (validate →
publish), while the lab-free driver treats it as "candidate project files for `run_component inputs.source`": the
investigator used the lab-free meaning.

Why the first run's receipt showed `error: null` (traced): it did not. The ledger stored the failure in
`job.terminal.error` (category `user-code`, message with the stderr tail, worker traceback), and the
investigator's view source and packed contexts contained it. The `null` came from my own post-run query, which read
`attempts[-1].error`; attempt records have no `error` field. REQUESTS.md item 5 is retracted; STATUS and the first
receipt are annotated.

Ledger: ancillary spent 165c of 1000c (this re-run 32c); available 835c.

## Re-run 2: full-lab D02 at 4bf5974 (after the fixes for REQUESTS 8-10)

Merged `origin/build/eidos-local-v1` at 4bf5974 (fast-forward); fresh wheels `uv build --no-cache` into `$S/dist-4bf5974`,
lab venv reinstalled `--no-cache --reinstall`. Fake-marker check first: only `eidos.apps.role investigator-worker`
held it; the probe job's environment had 10 variables and no key; `$HOME`, `~/.claude.json`, network and lab API
refused; no lab file contained it. Same caps and command as re-run 1 (12 × 1 × 5 ≤ 60 requests, `model_usd=2`), key via
stdin (`receipts/a8-full-lab-D02-rerun2.json`).

| | First run (5.2) | Re-run 1 (2df52bb) | Re-run 2 (4bf5974) |
|---|---|---|---|
| Baseline job | failed | succeeded first time | succeeded first time (val acc 0.8384); also seeds 12, 13 |
| Turns / decisions | 5 / 5 accepted | 3 / 3 accepted | 6 / 6 accepted, 0 refused decisions |
| Refused / invalid | 0 | 0 (but an INVALID `author_component` call) | turn 3 `invalid-proposals`: it waited for jobs already finished (nothing outstanding) → bounded immediate retry |
| Jobs | 5 (1 failed) | 2 | 4 (baseline ×3 seeds, 1 scoring), all succeeded |
| `author_source` / `author_component` | — / — | — / 1 (INVALID) | **4** / 0; all 4 sources registered, **none ever run** |
| End | turn cap, no conclusion | stalled, operator stop | **parked** after turn 6 (bounded `request-budget-exhausted` wait, nothing outstanding) → operator stop after >10 min; no conclusion |
| Claim | none | none | none filed. Its rationale asserted the baseline is "badly undertrained" and expected "large gains": an untested hypothesis, no candidate was evaluated, so nothing supports it |
| Requests / tokens | 20 / 172k in, 12k out | 15 / 122k in, 6.7k out | 31 / 329k in, 28.8k out |
| Cost (settled) | $0.4663 (47c) | $0.3111 (32c) | **$0.9465 (95c)**, reserved 250c |
| Key in lab files | [] | [] | [] |

Intervention (labelled builder intervention): turn 6 completed 2026-11-08 19:43:54 IST with nothing outstanding;
`stop_investigation` at 19:55 IST after the >10-minute park. No steering.

Why it stalled (diagnosed, platform-independent; REQUESTS 11-13):
1. **Authored sources are invisible in the packed context.** `author_source` registers a source bundle (no job), and
   its id reaches the investigation view only as the `subject` of an `artifact.registered` event; the context packer
   (`investigator/context.py`) has no events or authored-sources section. None of the 4 bundle ids
   (`art_4ea5…`, `art_1ce2…`, `art_c38a…`, `art_8ffc…`) appears in any packed context, and the turn-4/5/6 contexts are
   identical in size (11,347 chars). The model said each time it would run the source "next turn" because "the
   bundle id is only known after" authoring, then re-authored.
2. With `max_model_calls_per_turn=1` it cannot author and run in one turn, so (1) blocks the whole-trainer path.
3. **The bounded no-effect outcome still parks.** Turn 6 exhausted the decision's request bound and produced the
   runtime's exempt `wait_for_jobs` with nothing outstanding; nothing wakes it (max idle 6 h). The ADR 0007 exemption
   should not apply when nothing is outstanding.
4. Minor: turn 6 made **6** requests against `max_requests_per_decision=5` (pydantic-ai reported a request_limit of 4
   plus output retries; the last two responses were cut at 4,096 output tokens). The run total (31) stayed under 60.

Ledger: ancillary spent 260c of 1000c (this run 95c); available 740c.
