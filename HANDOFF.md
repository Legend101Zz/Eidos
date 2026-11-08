# Eidos handoff (2026-11-08, eidos-v0.5.2)

## Identity

- Repository: `Legend101Zz/Eidos`. Branch: `build/eidos-local-v1`. The controller maintains the draft PR `build/eidos-local-v1 → main`.
- **Current freeze: annotated tag `eidos-v0.6.0` → `c7b6681`.** CI was green on all ten jobs. It is the first tag with CI independent of GitHub's release CDN, and it includes the review-3 fixes, the Linux suspend/soak evidence and the licence audit.
- Previous freeze: `eidos-v0.5.2` → `e4558e2`. CI was green on all nine jobs, including `switch-journey` (154/154). It is the first tag with both qualified assessment and sealed checkpoint-and-stop working, and with the restored acceptance journey in CI.
- **`8301799` (contains `64ad288`) is RED in CI:** the `switch-journey` step 7 investigation parked after turn 5.
  - Cause, reproduced locally: the idle-trap rule refused whole decisions, including the scripted policy's publish+wait. That dropped the publish, so the policy re-proposed it forever. Also, a turn ending on the invalid-proposal limit had no wake.
  - Fixed in `08f9e01` and `63f7cac` (ADR 0007):
    - only pure waits with nothing outstanding are refused;
    - a refused-proposal turn gets 2 bounded immediate retries, then surfaces as "needs operator input";
    - `investigation_view` job visibility no longer uses a 5,000-event window that starts at seq 0;
    - the investigator's own effects count as news.
- **Post-tag head:** the current branch head needs CI before any tag such as `eidos-v0.5.3`.
- **Round 12:**
  - CI and `lab init` no longer depend on GitHub's release CDN: verified cache, then pinned download, then source build with recorded provenance.
  - Adversarial review 3 fixes, including a key leak through symlinked outputs in the lab-free driver.
  - A15 suspend/resume passes on Linux, and a soak was run.
  - Licence inventory, NOTICES and a troubleshooting guide were added.
- **Round 11 (owner decisions):**
  - Real-model checks, real MCP clients and MPS are qualified on the owner's Mac by the controller, using `docs/ops/MAC_QUALIFICATION_RUNBOOK.md`; results are pending.
  - The security defect the controller found is fixed (`3a094d8`): model-chosen commands could inherit `ANTHROPIC_API_KEY`, home and network. Untrusted commands now run under the sealed profile with no secrets, and only the investigator worker holds provider keys.
  - Download robustness for CI was added (`3a96f64`).
- Earlier freeze: annotated tag `eidos-v0.5.0` → `e822744`. All eight CI jobs are green there: lint-and-unit, integration, fresh-install, upgrade-across-releases, e2e-journey, components, macOS arm64, worker-compat.
- Earlier tags, never moved: `eidos-v0.1.0` → `78e4597`, `v0.2.0` → `37bd0c5`, `v0.3.0` → `8a33793`, `v0.4.0` → `c57dba9`, `v0.4.1` → `e328608`, `v0.5.0` → `e822744`, `v0.5.1` → `12b4822`, `v0.5.2` → `e4558e2`, `v0.6.0` → `c7b6681`.
- **Since v0.4.1:**
  - the macOS assessor runs under Seatbelt;
  - adversarial review 2, with five findings fixed (`docs/dev-runs/adversarial-review-2-2026-11-08.md`);
  - the cross-release upgrade test (v0.1.0 wheels → head);
  - the clean-install end-to-end journey through the CLI and the MCP SDK client (`docs/dev-runs/e2e-journey.md`);
  - `lab upgrade` now switches to the new interpreter;
  - `lab component install` is now idempotent.
- **Known defect in `eidos-v0.2.0`…`eidos-v0.5.0`, fixed after v0.5.0 in `8771bb3`:** `submit_assessment` crashed for every evidence plan, because pipeline arms store lists of jobs. As a result, no qualified assessment could be recorded on those tags. The clean-install journey's new qualified-assessment step found it, and it now guards against it in CI.
- **`eidos-v0.5.1` → `12b4822`** (CI green) is the first tag since v0.1.0 with working qualified assessment. `eidos-v0.5.0` stays as recorded, with this bug.
- **Also affected:** checkpoint-and-stop under the default sealed profile was broken from round 5 (`eidos-v0.4.0`) up to and including `eidos-v0.5.1`; it is fixed in `1a1c621`.
- **After `eidos-v0.5.1`:**
  - drift-sweep fixes;
  - `tests/mcp` and `tests/reporting` in CI;
  - the restored 154-check switch-order journey, now CI job `switch-journey`;
  - the fixture-policy stall fix.
  The final head needs a CI relay before tagging `eidos-v0.5.2`. Details: `IMPLEMENTATION_STATUS.md` § Round 8. Only CI-green SHAs get tagged.
- No `eidos-v1.0.0` tag exists, because the v1 acceptance criteria are not all met (see below).

## What v0.5.0 is

A development freeze of the complete local-v1 software stack described in `docs/design/eidos-claude-handoff/` (Revision 4). It contains:

- **Ledger and commands:** a PostgreSQL ledger with role grants and append-only triggers. Every planner, assessor, investigator and worker write goes through one authoritative command service with idempotency, revisions, fences and budgets.
- **Orchestration:** on the regular Temporal Server.
  - A coordinator workflow with Continue-As-New, bounded investigator turns and persisted decisions and ordered calls.
  - Stable command keys.
  - Jobs whose lifetime is independent of turns.
  - Patched, guarded upgrades.
- **Runner and launcher:** reattach-safe handles (pid + start time + boot id), lost-attempt reconciliation, resume after worker death, and checkpoint-and-stop.
- **Isolation:** `linux:bwrap-sealed` with hostile probes and recorded waivers, and a sandboxed assessor. On macOS, `macos:seatbelt-sealed` covers both candidates and the assessor. Its limits are below.
- **Thin worker packages:** `eidos-wire`, `eidos-worker-sdk` and `eidos-pytorch` (stdlib only, using the project's own torch).
- **Study rules:** charters and evidence plans, qualification gated on executed planted-effect and null-case receipts, checkpoint branching, versioned context policies and offline turn replay.
- **Exposure tracking.**
- **Local operations:** supervisor, CLI, MCP (stdio + loopback HTTP), HTML reports, doctor/status/logs, backup/restore, upgrade.
- **Components:** 13 built-in components.
- **Research handoff:** an adapter for the public research release (`eidos-task-bundle/0.1` → `eidos-answer/0.2`).

## What v0.5.0 is not

- **Not v1 acceptance.** macOS researcher machines, real-LLM lab investigations, external MCP hosts, CUDA/MPS and the other gaps listed below are open.
- **Not evidence that Eidos helps an LLM do model research.** That is the research question, and it is untested. A16 belongs to the independent evaluation workstream.
- **Not autonomous real-LLM behaviour.** Every investigator decision recorded in lab runs came from a deterministic fixture policy.
- **Not a frozen scientific study.** No final evaluation has been run, and no private evaluation material was sought or accessed.

## Install and run

```bash
uv build --all-packages -o dist && uv tool install --find-links dist eidos-lab   # or pip install from dist/
lab init                      # private PostgreSQL cluster + pinned Temporal Server under ~/.eidos (or --home)
lab start && lab doctor       # supervisor; doctor reports isolation profiles, disk, secrets, services
lab project create myproj
lab project register-env myproj --python /path/to/.venv/bin/python --root /path/to/project
lab investigate start myproj --question "..." --runtime pydantic-ai --model anthropic:<model>
lab investigate events myproj --investigation <id> --follow
lab report myproj --investigation <id>
lab mcp                       # stdio MCP server; client snippets in docs/ops/mcp-clients.md
lab backup / lab restore BUNDLE --home NEW_DIR / lab upgrade / lab stop --running-jobs detach
```

Full guide: `docs/ops/INSTALL_AND_OPERATE.md`. CI layout: `docs/ops/CI.md`. Exact test commands: `IMPLEMENTATION_STATUS.md` § How to run.

## Evidence classes

| Class | What |
|---|---|
| **Runtime-verified (hosted Linux, Ubuntu 24.04 x86_64, CPU)** | Everything in "What v0.5.0 is", exercised against real PostgreSQL 16, the regular Temporal Server 1.32.2 and real processes. Also: the switch-order acceptance journey (154 checks, CI job `switch-journey`), D01/D02/D12 bundle runs, a fresh wheel install, abrupt-stop/reattach/checkpoint-and-stop ops scripts and a performance smoke. **New:** real-time Temporal heartbeat-timeout, start-to-close, retry-policy and worker-SIGKILL tests pass on the dev server and against the lab's regular server (`tests/integration/test_temporal_realtime.py`, 4/4, 2026-11-08). **New in v0.5.0:**<br>• cross-release upgrade, 33 checks: records, artifacts and the event sequence are byte-identical, an open investigation continues, report and doctor pass (CI job `upgrade-across-releases`);<br>• the clean-install end-to-end journey, 8/8 steps (CI job `e2e-journey`): D01 import, fixture investigation and validated answer through the CLI; branch and export through the CLI; start, detach and read-back through the MCP SDK client. |
| **Fixture-only** | All investigator decisions inside lab runs: the D01/D02/D12 investigations and journey step 7. |
| **Development-qualified, lab-free** | The Anthropic path through the Pydantic AI adapter. Controller-run live receipt #3 passed 8/8. |
| **GitHub-runner evidence (macOS arm64)** | Wheel install, `lab init/start/doctor/project/status/stop`, worker SDK tests and research smoke. **New:** `macos:seatbelt-sealed` passed on a GitHub macOS 15 arm64 runner (`scripts/ci/seatbelt_probes.py`, CI run `42429763457`):<br>• all six sealed controls held under the sandbox;<br>• the same-user planted-effect control detected the accesses;<br>• the permitted computation read its input copy and wrote staging, scratch, resume and tmp, while project-root writes were refused.<br>**v0.5.0 (CI on `e822744`):** the hardened profile passed, including `no-process-inspection` against a same-user peer. The assessor ran under Seatbelt with a planted-effect assessment and all controls passed (`scripts/ci/assessor_sandbox_probe.py`). |
| **Unqualified** (Mac runs prepared, pending: macOS researcher machine, MPS, Claude Code/Codex, real-LLM full lab) | macOS researcher machine; Lima VM; CUDA/MPS; OpenAI-compatible providers; external MCP hosts (Claude Code/Desktop, Codex); sleep/suspend; multi-day runs; distributed training; performance on a declared reference machine. |
| **Scientifically unproven** | Any claim that Eidos improves verified outcomes under comparable budgets (A16). |

## Open items

| Item | Blocker | Next step |
|---|---|---|
| macOS researcher-machine qualification | Needs the owner's Mac. A hosted runner is not a researcher machine. | Owner runs `scripts/qualify/macos_owner_kit.sh` (`docs/ops/MACOS_OWNER_QUALIFICATION.md`). |
| macOS sealed isolation | `macos:seatbelt-sealed` covers candidates and the assessor, but it has passed its probes on a GitHub runner only; no researcher Mac and no full lab job under Seatbelt have been tested. It runs as the same OS user (no `separate-identity`), and **does not isolate against a hostile same-user program**: other launchd/XPC routes and signals stay open (review 2). Apple deprecates `sandbox-exec`. Lima VM is not implemented. | Run the owner kit on a Mac with a sealed job. Owner decides whether seatbelt is acceptable or Lima is required. |
| Real-LLM autonomous lab investigation | No provider key in the build sandbox, and the controller's budget is separate. | Owner/controller authorizes the lab-free D02/D12 runs (`scripts/qualify/live_bundle_investigation.py`, ≤ 40 requests per bundle), then a full-lab run. |
| OpenAI-compatible provider qualification | Needs an endpoint and a key. | Controller runs at most 10 calls (`IMPLEMENTATION_STATUS.md` § Live-provider runs). |
| External MCP hosts | No Claude Code/Desktop or Codex host in the sandbox. | Use the snippets in `docs/ops/mcp-clients.md`. The SDK client is already verified over stdio and HTTP. |
| CUDA/MPS restore classes, distributed training | Hardware. | Unauthorized here. |
| `macos:lima-vm-sealed` profile (the design's VM isolation on macOS) | **Owner/hardware item, not built.** It cannot be exercised in CI: GitHub's runner reference, under "Limitations for arm64 macOS runners", says nested virtualization is not supported because of a limitation of Apple's Virtualization Framework. The arm64 standard runner has 3 M1 CPUs, 7 GB RAM and a 14 GB SSD. Source: relayed by the controller. | Build and qualify it on an owner Mac with Lima, bwrap inside the VM and no broad host mounts. It is needed for macOS plans that require `separate-identity` or isolation from hostile same-user programs. |
| Investigator idles when it waits with nothing outstanding | **Fixed after v0.5.2 (`64ad288`, corrected in `63f7cac` after the red CI on `8301799`; ADR 0007).** Such a decision is refused at acceptance with the message "nothing to wait for: run something, request input, or conclude". The refusal is fed back within the turn and counts against the invalid-proposal limit. The runtime's own bounded no-effect outcome (request budget spent) is exempt. There is no workflow change. | Confirm CI on the head, then tag it. |
| Drift-sweep fixes after `eidos-v0.5.1` | Three silent failures, all fixed and green in CI on `2e61edf`: checkpoint-and-stop under bwrap, sealed-runner concurrency, and the fixture budget in the macOS owner kit. | See `IMPLEMENTATION_STATUS.md` § Drift sweep. These are not in any tag before the final one. |
| Review 2 open questions | Recorded, not fixed: idle-poll behaviour across rollover; the assessor with a distinct candidate user; old writers during upgrade. | `docs/dev-runs/adversarial-review-2-2026-11-08.md` |
| Interruption-table rows still `not-started` | Not built yet. | See ACCEPTANCE_MATRIX §C. These rows are: storage lag, question change/withdrawal, assessor dedupe, method withdrawn during confirmation, lost branch reply, node preemption. |
| Sleep/suspend, multi-day runs | Not exercised. | Owner machine. |
| Independent evaluation (A16) | External workstream. | Use `eval-*` tags for evaluation configurations, and freeze runs only from a clean tagged tree. |

## Owner decisions pending

0. **Project licence.**
   - The README says TBD.
   - The packages had claimed Apache-2.0 from the first skeleton commit; that claim was removed, so the built wheels now declare no licence.
   - The redistribution terms of `research/public/` are also undecided.
   - Details: `docs/ops/LICENSES.md`.


1. Run the macOS owner kit on a researcher Mac.
2. Authorize the real-LLM lab-free D02 and D12 runs, then a full-lab investigation.
3. Choose and fund an OpenAI-compatible endpoint for adapter qualification.
4. Exercise external MCP clients: Claude Code/Desktop and Codex.
5. Decide whether `macos:seatbelt-sealed` is acceptable as the macOS sealed profile for plans that do not require `separate-identity`, or whether a Lima VM profile must be built.

## Budget visibility

- At the controller's last check-in, session spend was USD 91.58 of the USD 150 cap, and ancillary spend was USD 0.56 of USD 10.
- This round added the work listed above. The controller holds the authoritative figure.
