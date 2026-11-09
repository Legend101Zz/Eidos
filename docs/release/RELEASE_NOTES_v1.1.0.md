# Eidos v1.1.0 release notes (2026-11-09)

v1.1.0 implements the harness features of the design that v1.0.x did not have: the five remaining recovery cases of
the failure table, separate-identity sealed evaluation on Linux, provider keys in the OS secret store, a credential
broker for component LLM calls, structured-output repair for local models, and the optional PyGlove/Composer adapters
with a CheckList-style behaviour suite. There is no ledger migration. As before, there is **no scientific claim**:
A16 is a separate later release.

## Items: done and stopped

All six work items of the 1.1.0 brief were delivered; none was stopped.

| Item | Status | Decision record | Evidence |
|---|---|---|---|
| 4. Five recovery cases (`not-started` rows of `ACCEPTANCE_MATRIX.md` § C) | **done**: all five rows `verified` | ADR 0010 | `tests/integration/test_recovery_cases.py` (7 tests, real PostgreSQL) |
| 3. Assessor with a separate candidate user (Linux) | **done** | ADR 0011 | CI job `separate-identity` (root): `tests/integration/test_separate_identity_e2e.py` plus the probe and assessor sandbox tests |
| 2. Provider keys in the OS secret store | **done** | ADR 0012 | `tests/unit/test_secret_store.py`; CI `secret-store-macos` (temporary keychain), `secret-store-linux` (gnome-keyring on a private D-Bus session) |
| 1. Credential broker for components | **done** | ADR 0013 | `tests/integration/test_llm_broker.py` (11 tests, fake provider), `packages/eidos-worker-sdk/tests/test_llm_client.py`; example `components/eidos.synthetic.llm-examples` |
| 10. Structured-output repair for local models | **done**, measured | ADR 0014 | `tests/unit/test_structured_output_repair.py` (21 recorded qwen replies); `docs/release/evidence/g4-110-*.json` |
| 9. PyGlove and Composer adapters, behaviour suite | **done**; neither adapter was blocked | ADR 0015 (supersedes 0009) | `packages/eidos-pyglove` (82 tests), `packages/eidos-composer` (26 tests), CI `adapter-pyglove`, `adapter-composer`; `components/eidos.behavior.suite` |

## What changed

1. **Recovery cases** (ADR 0010). Staged bytes are verified again (size and full rehash, short bounded re-check for
   lag) right before publication; a changed or vanished blob refuses the whole publication with a per-slot reason, and
   the runner re-stages once, then fails the attempt explicitly. `revise_question` changes an investigation's question
   at the next turn, never mid-turn, with a policy for admitted work and an exposure record; a withdrawal wakes the
   affected investigations and a withdrawn release admits no new work. Duplicate assessment evidence returns the same
   receipt (order-insensitive digest; the assessor's command key no longer conflicts on restart). A method withdrawn
   during confirmation blocks the plan and its assessment fails closed; `reassess_evidence_plan` continues under a
   corrected, approved assessor. A branch request retried with the same key returns the same branch.
2. **Separate candidate identity** (ADR 0011). Candidate code runs as `eidos-cand`; the assessor (which computes the
   assessment's scores) runs under the lab identity inside the same bwrap confinement. Two defects found on the way:
   the assessor used to drop to the candidate user and could not read its evidence, and a uv venv's interpreter was
   missing inside bwrap because an intermediate directory symlink was not bound. Root setup is scripted and documented.
3. **OS secret store** (ADR 0012). `lab credentials set|delete|status`; macOS Keychain and Linux libsecret; no plaintext
   fallback; `lab start` reads stored keys. The tests' separate review found two issues before release, both fixed:
   keys longer than `security -i`'s line could fail and echo fragments in the error (keys are now capped at 1,024
   characters, and `security`'s stderr is never passed on).
4. **Credential broker** (ADR 0013). Scoped per-attempt grants; a Unix-socket broker in the investigator worker (the
   only process with provider keys); the investigation's ledger is reserved before every provider call; call receipts;
   uncertain usage after timeouts and after a broker crash; sealed runs reach only the broker socket, and only when
   allowed. Worker SDK client `eidos_worker.llm`; `Invocation.llm_grant` (eidos-wire 1.1).
5. **Structured-output repair** (ADR 0014): `output_mode` `tool` (default), `repair`, `json`.
6. **Adapters and behaviour suite** (ADR 0015): `eidos-pyglove`, `eidos-composer`, `behavior-suite/1`.

7. **Independent review before release.** A read-only review of the full diff found two blockers (upgraded labs got no
   LLM broker principal; a release withdrawn mid-pipeline could leave a finishing job non-terminal) and several
   should-fixes in the broker, the repair layer and the sandbox binds. All were fixed with tests (`CHANGELOG.md`).

## Packages

| Package | 1.0.1 | 1.1.0 |
|---|---|---|
| `eidos-lab` | 1.0.1 | **1.1.0** |
| `eidos-wire` | 1.0.1 | **1.1.0**: optional `Invocation.llm_grant`; the schema major is still 1, and older workers ignore the field |
| `eidos-worker-sdk` | 1.1.0 | **1.2.0**: `eidos_worker.llm` client and `llm` injection; requires `eidos-wire>=1.1` |
| `eidos-pytorch` | 1.1.0 | 1.1.0 (unchanged) |
| `eidos-pyglove` | — | **0.1.0** (new, optional, outside the lab environment) |
| `eidos-composer` | — | **0.1.0** (new, optional, outside the lab environment) |

## Metrics: v1.1.0 compared with v1.0.1

| Measure | v1.0.1 | v1.1.0 |
|---|---|---|
| `ACCEPTANCE_MATRIX.md` § C rows `not-started` | 6 | **1** (node preemption / lost rank, distributed: unqualified) |
| § C rows verified by integration tests since the previous release | — | **5** |
| Test functions (all of `tests/`, packages and components) | 260 | **414** |
| Local unit + integration + MCP + reporting suite | 199 passed, 4 skipped | **371 passed, 7 skipped** (the 3 new skips are Linux-root-only tests that run in the `separate-identity` CI job); worker SDK 26 passed, also on Python 3.9 + pydantic 1.10 |
| CI jobs per push | 10 | **15** (`separate-identity`, `secret-store-macos`, `secret-store-linux`, `adapter-pyglove`, `adapter-composer`) |
| ADRs | 9 | **15** (0010–0015; 0015 supersedes 0009) |
| Wire golden fixtures | 15 | 17 (a valid invocation with an LLM grant, an invalid one without its token) |
| Built-in components | 12 | 14 (`eidos.synthetic.llm-examples`, `eidos.behavior.suite`) |
| G4 qwen2.5:3b-instruct: scenarios passed (of 8) / decisions obtained / executable (of 5) | 4 / 1 / 1 (tool mode, re-measured) | **5 / 5 / 2** (`json` mode) |
| G4 qwen2.5:7b-instruct: scenarios passed (of 8) / decisions obtained / executable (of 5) | 3 / 0 / 0 (tool mode, re-measured) | 3 / **5** / **1** (`json` mode) |

The G4 numbers come from one run per configuration on the owner's Mac (own Ollama, 29.5 minutes of a 60-minute box;
the 14B model was not run). The repair layer removed every malformed-output failure; the failures left are semantic
(unknown tool names, missing arguments), which the lab refuses. **Small local models still do not pass full
decision-schema conformance**; `docs/decisions/0014-structured-output-repair.md` has the table and caveats.

## Upgrading

A package update with no ledger migration (`lab upgrade` from the new wheels). After the upgrade, `lab` enrols the new
`svc-llm-broker` principal; the broker starts with the investigator worker. Components get LLM grants only after a
project sets `policy.llm_broker`. Workers on `eidos-wire` 1.0.x keep working: they ignore `llm_grant`.

For separate-identity sealed evaluation, follow `docs/ops/INSTALL_AND_OPERATE.md` § "Separate candidate identity
(root)"; note that the lab home must be traversable (0711) by `eidos-cand`.

## Limitations

- Unchanged from v1.0.0 (`RELEASE_NOTES_v1.0.0.md`): machine sleep/restart recovery not verified on hardware;
  macOS isolation is same-OS-user; CUDA and distributed training unqualified.
- Separate identity is verified in CI only (it needs root on Linux); it was not run on the owner's Mac (macOS has no
  candidate user).
- The LLM broker is tested with a fake provider; live providers through the broker are not separately qualified.
- `eidos-composer` targets the final, archived Composer release (0.32.1, torch < 2.7.1), whose checkpoint loader uses
  pickle: load only checkpoints the same run wrote.
- `eidos-pytorch`'s `__version__` still reads 1.0.0 while its metadata says 1.1.0 (pre-existing; the package did not
  change in this release, so it was not rebuilt).
