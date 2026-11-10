# Study arms 3 and 4

Public integration hook: `study.eidos_arms.run_arm3(run_dir)` and `run_arm4(run_dir)`.
Lane A's `study/run_arm.py` already dispatches to these functions. They re-execute the
worker with the installed lab interpreter; the controller needs no Eidos Python imports.
The merged `study/prompts/task.md` is read for every invocation and substitutes
`{{TOOLS_PARAGRAPH}}` with the corresponding tools file. No study cards were run here.

Installed environment: `/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1b-110`, created from
`dist-1.1.0` (`eidos-lab==1.1.0`, `eidos-pytorch==1.1.0`). `lab-rc` was inspected but
is **1.0.0rc1**, so was not used. Python is 3.12.8. PostgreSQL is Homebrew 15.18;
Temporal regular-server 1.32.2 binaries were copied from `a8lab/services/temporal/bin`
and their operator-supplied provenance is recorded by `lab init`.

```sh
source "/Volumes/Mrigesh SSD/eidos-v1/env.sh"
STUDY_PY="/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1b-110/bin/python"
"$STUDY_PY" -m study.eidos_arms.lab start
# After the controller combines the lanes:
"$STUDY_PY" study/run_arm.py --arm 3 --run-dir /absolute/run/directory
"$STUDY_PY" study/run_arm.py --arm 4 --run-dir /absolute/run/directory
"$STUDY_PY" -m study.eidos_arms.lab stop
```

One lab serves all runs, with a fresh project per invocation. The default home is
`pilot-1/lab/shared`; ports are API 28765, PostgreSQL 25544, Temporal 27633–27641,
and loopback HTTP MCP 28766. Startup is serialized with a filesystem lock. Stop
only after all arms finish. Overrides: `EIDOS_STUDY_HOME`, `EIDOS_STUDY_PYTHON`,
`EIDOS_STUDY_MCP_PORT`, and `EIDOS_STUDY_TEMPORAL_SOURCE`. Model/runtime settings
come exclusively from `investigator.json` and are read anew per arm 4 run.

The existing `minimax_api_key` Keychain item is read into `lab start
--credentials-stdin` as `OPENAI_API_KEY`; no Keychain item is changed. Claude gets
the same key only through its child environment and the Anthropic-compatible URL.
MCP gets a generated, expiring project token, referenced through an environment
variable in `mcp.json`, and that principal is revoked when arm 3 returns. It has
component/research permissions but cannot start a second internal model agent.
Provider secrets never reach MCP, job processes, model prompts, argv or files.

Arm 3 uses lane A's actual `agent_env`, `sandbox_command`, `run_stream`, and token
tracker. The adapter adds `CLAUDE_CODE_TMPDIR`, UTC, and read access to the macOS
system timezone database under `/private/var/db/timezone` (native Bun/ICU otherwise
spins before startup). Other filesystem permissions are lane A's profile. The
plain standard-library `mcp_http.py` client is available when native MiniMax tool
arguments lose JSON types; it invokes the same loopback MCP tools. It contains no
scientific functions. Partial stream events supply usage omitted from native
assistant events, including on runs killed before a final Claude result.

Arm 4 reuses installed `import_bundle` and the built-in project-command and
classification-score components. Only text files from `run_dir/task` are embedded
beside the common prompt; input arrays are lab artifacts. This also works with
1.1.0 JSON mode's limited read-tool support. No references, grader data or test
inputs are supplied. Its model-token budget is 8,000,000; turns, trials and compute
are deliberately generous. The run directory can lower limits for non-study smoke.

Eidos 1.1.0 has a per-turn deadline, but no investigation-wide provider cancellation.
Each arm 4 run therefore has its own credential-free loopback forwarding process:
the configured HTTPS base URL remains the upstream, while Eidos uses the local
transport URL recorded in `provider_bridge.json`. Bodies and responses are forwarded
unchanged. An independent timer exits that process at the wall deadline, closing
provider connections and preventing retries from reaching the provider. A separate
watchdog stops the investigation and cancels its jobs. No shared worker is killed.
An interrupted, unreceipted provider response may still have unknown billable usage;
uncertain receipts are retained and flagged. Export/accounting time is included in
`wall_s`; it is not additional search time. macOS memory containment is best effort,
and Seatbelt runs under the same OS identity.

Arm 4 exports an explicit `candidate_lock`, then an assessor-confirmed candidate,
then an unedited reproduced baseline. It never selects the last search candidate
just because it ran last. If the investigator ends early without a baseline and
time remains, an explicitly recorded operator baseline fallback uses the supplied
training command within the remaining envelope. If no export is available, the
answer abstains. No new job is started after a wall timeout.

The submission contains immutable source, model, visible-validation predictions,
dependency lock, training/selection receipts and an `eidos-answer/0.2` answer.
Visible-validation predictions are labeled; the evaluator supplies inference inputs
separately. The investigator supplies scientific answer fields in its concluding
`control.scope.answer`; measurements can cite actual job IDs, which the adapter
exports as evidence receipts. Missing scientific metadata produces an inconclusive
answer with no positive claim. Original investigator conclusions and full assessor
bodies appear in `agent_claim`, and in the selection receipt. Exporting never
executes model-authored code on the host. The reproduction command replays the
selected job's exact source, argv and staged inputs; its lock records actual
registered package versions and preserves the original task lock separately.
Both arms validate schema, relative artifact paths, existence and SHA-256 values.

Arm 4's `transcript.jsonl` exports ledger events, full turns, persisted model
responses, jobs, notes, claims, assessments, plans and exports. Usage comes from
`model.usage` events once (including failures), with cached input separated using
raw model responses. Aggregate Eidos input includes cached tokens. Cache detail
for rejected/failed responses without a persisted raw response remains unavailable,
so their cached input stays in `input`; `total` still comes from ledger receipts.
Costs remain null unless recorded. Provider 429/5xx/quota failures, infrastructure
failures and multiple served model IDs are incidents; an expired wall is a timeout,
model budget exhaustion is `token_budget`, malformed investigator proposals are
`crash`, and an invalid submission is `invalid_output`.

For pre-merge smoke only, `EIDOS_STUDY_LANE_A_ROOT` lets the operator import lane
A's public runner helpers and method without copying/modifying those files. It is
not passed into the sandbox. `EIDOS_STUDY_SMOKE_PROMPT` supplies a non-study base
prompt; normal runs require the merged prompt. See [SMOKE.md](SMOKE.md) for receipts.
