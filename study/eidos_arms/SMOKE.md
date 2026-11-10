# Lane B smoke receipts

All tasks were `SYNTHETIC_LINEAR_SMOKE`: a generated 3-feature, 2-class task with
160 training and 60 visible-validation examples. **D01, D02 and D12 were not run.**
All model traffic used MiniMax. The observed served model was
`MiniMax-M3.1-Flash`; no Anthropic endpoint or subscription was used.

Installed lab: `/Volumes/Mrigesh SSD/eidos-v1/venvs/pilot-1b-110`, from the
`dist-1.1.0` wheels, `eidos-lab==1.1.0`, Python 3.12.8. Research environment:
`/Volumes/Mrigesh SSD/eidos-mac/venvs/research/bin/python`, NumPy 2.5.4, Torch
2.15.0. Initial smoke generators incorrectly declared older NumPy/Torch pins;
the final generator records the installed versions. Initial receipts are retained.
The operator imported lane A's actual runner helpers through
`EIDOS_STUDY_LANE_A_ROOT`, before the controller combines the branches.

## Final qualifying runs

Raw run roots are under `/Volumes/Mrigesh SSD/eidos-v1/pilot-1/runs-workers/`.
Small committed receipts are in [evidence](evidence/smoke-summary.json).

| Run directory | Result | Wall s | Input† | Cache reads | Output | Total |
|---|---|---:|---:|---:|---:|---:|
| `lane-b-arm3-smoke-08` | `ok` | 142.443 | 51,080 | 908,278 | 16,140 | 975,498 |
| `lane-b-arm4-smoke-03` | `ok` | 510.646 | 107,116 | 95,220 | 74,901 | 277,237 |

Cache writes were zero in these receipts. Billed USD is unknown, so remains null.
These are observed receipt counts, not provider billing reconciliation.
† Arm 4 input is ledger input minus recovered cache counts. Rejected responses
without persisted raw cache detail remain in that input bucket; totals are unaffected.

Arm 3 ran `claude --bare -p ... --model MiniMax-M3.1-Flash` inside Seatbelt,
with the MiniMax Anthropic-compatible URL and an environment-only API key. Claude
reported the loopback HTTP MCP server connected; native MCP discovery and the
standard-library MCP transport were callable. Training and file access went through
the lab. The agent produced `out/answer.json`, model, source, dependency lock,
visible-validation predictions and evidence. Schema, every referenced artifact,
and all declared SHA-256 values passed. The answer makes no positive improvement
claim. Its original final message is in the committed run receipt.

Arm 4 used JSON mode, a 16,384 output cap, 300 s request timeout and a 600 s wall
ceiling. It produced accepted schema-valid decisions, successful training/scoring
and inference jobs, an explicit candidate lock and an answer. The selected job is
`job_cad2fc9f38924dec`. The baseline's visible-validation accuracy was 58/60
(0.9666667); this is an operational reproduction, not independent confirmation.
There was no assessor assessment: the imported charter is exploration-only and
does not define prospective success rules. The original investigator conclusion,
including its own export claims and limitations, is retained verbatim; actual
assessor bodies are recorded separately within `agent_claim` (empty here).

After the run, the final export code was exercised again against this immutable
job, without another model call, under `lane-b-arm4-smoke-03/export-verification`.
It retained the actual job argv/seed/input bindings and actual registered package
versions. The reproduction ran under Seatbelt with no provider credentials:

```text
Artifact-aware validation: True
Selected recipe reproduction exit: 0
{"n": 60, "reload_ids_equal": true, "reload_predictions_equal": true,
 "reproduced_ids_equal": true, "reproduced_predictions_equal": true}
```

## Deadline and isolation verification

The live `lane-b-arm4-deadline-10s` test used a deliberately shortened ceiling:

```text
status: timeout
wall_s: 10.15
provider_outer_kill: true
usage_uncertain: true
provider process returncode: 124; deadline_reached: true
```

No completed response receipt was available before cancellation. Its recorded
token counters are zero **observed**, with unknown actual provider usage; the
answer's unknown cost fields remain null. This is not a claim of a free request.
The process test also held an incomplete HTTP request open and verified that the
independent 1.5 s deadline closed its socket and exited with code 124. Closing a
client connection cannot establish the provider's final bill.

The sandbox allowed writing in its own run and denied reads of the worktree's
README, `pilot-1/tickets/CONTRACT.md`, and `/Users/comreton/.zshrc`. A scoped MCP
reader could list its project but a request for another existing project returned
`isError: true`, code `NOT_FOUND` (the lab hides unauthorized project existence).
macOS memory enforcement is best effort; Seatbelt uses the same OS identity.

The key scan examined 3,677 files, including the lab's PostgreSQL data, artifacts,
jobs, logs and smoke outputs: `key_found_in_files: []`. A final scan is performed
after shutdown as well: **3,956 files**, no full key or key-suffix matches.
Credentials were not persisted or changed in Keychain. The lab phase is `stopped`,
all recorded service PIDs have exited, and API/MCP/PostgreSQL/Temporal ports are
closed. Internal-disk free space at shutdown was 24,228,540 KiB, above 12 GiB.

## Earlier failures and accounting corrections

The full inventory is retained in [smoke-summary.json](evidence/smoke-summary.json).
Failed runs were not replaced or edited into successes. Original records are
retained beside post-run audit corrections; model-authored answers remain intact.

| Run | Final classification | Observed tokens | Finding |
|---|---|---:|---|
| arm3-01 | incident | 0 | Native Claude startup denied `/tmp/claude-501`; fixed `CLAUDE_CODE_TMPDIR`. |
| arm3-02/03 | crash | 0 | Native startup spun before model/MCP output; operator killed only own PIDs. Allowing the system timezone database fixed it. |
| arm3-04 | incident | unknown | MCP callable, but malformed nested argument types; timeout, no usable usage receipt. |
| arm3-05 | incident | unknown | MCP transport and model files worked; timeout before answer/final usage. |
| arm3-06 | invalid_output | 1,301,864 | Schema passed, but `out/project.py` resolved to missing `out/out/project.py`. Artifact audit rejected it. |
| arm3-07 | timeout | 955,426 | Captured partial usage, but did not finish its answer before 300 s. |
| arm4-01 | ok | 185,469 | Initial successful JSON investigation and answer; later recovered cache split from raw receipts. |
| arm4-02 | incident | 376,658 | Pre-bridge deadline failure: provider call continued after `stop_investigation`. |

Arm 3's precise argument failure was:

```json
{"config":{"argv":{"item":["python","project.py","train","--inputs","{work}","--out","{out}","--seed","{seed}"]},"seed":"11","timeout_seconds":"180"}}
```

The lab correctly returned `RESOLUTION_ERROR`, HTTP 422:

```text
config.argv: expected array, got dict
config.seed: expected integer, got str
config.timeout_seconds: expected number, got str
```

No validator or MCP contract was relaxed. `mcp_http.py` sends the same MCP call
using the exact JSON types in the agent's argument file.

Claude's native assistant events reported `{"input_tokens":0,"output_tokens":0}`
even after many calls. A direct MiniMax streaming probe proved the real usage was
in `message_delta` (9 input + 197 cache reads + 10 output = 216 tokens). The final
runner requests partial stream messages and merges those receipts by message ID,
without adding final cumulative usage twice. The zero counters in arm3-04/05 are
explicitly unknown spend, and those runs are classified as incidents.

For arm4-02, the worker originally returned at 01:47:12 IST with 357,889 observed
tokens. The ledger subsequently recorded another 18,769 tokens at
01:47:31.202895 IST, **19.470 s after the investigation's deadline**. Its final
total is 376,658; the original record and initial transcript are preserved. This
is the exact 1.1.0 cancellation defect that the per-run provider process addresses;
the shared investigator worker and `src/eidos` were not modified. The receipt is
[pre-bridge-late-usage.json](evidence/pre-bridge-late-usage.json).

The investigator also made ordinary task mistakes during earlier runs (for
example, passing a directory to `np.load` during inference, or trying the
checkpoint-only export tool on a raw project model). Those failures and original
claims remain in the ledger. The final tools paragraph explains the correct
project-artifact export path.

## Commands verified

All commands were run after sourcing `/Volumes/Mrigesh SSD/eidos-v1/env.sh`.
The installed interpreter is denoted by `$STUDY_PY` below.

```text
$STUDY_PY -m study.eidos_arms.smoke --arm 3 --run-dir .../lane-b-arm3-smoke-08 --wall-s 300
status=ok; total=975498; model_ids_seen=[MiniMax-M3.1-Flash]

$STUDY_PY -m study.eidos_arms.smoke --arm 4 --run-dir .../lane-b-arm4-smoke-03 --wall-s 600
status=ok; total=277237; model_ids_seen=[MiniMax-M3.1-Flash]

$STUDY_PY research/public/development/validate_answer.py .../out/answer.json
structural check passed; artifacts, security, costs and science not independently verified

uvx ruff check study/eidos_arms
All checks passed!

EIDOS_STUDY_LANE_A_ROOT=... $STUDY_PY -m unittest study.eidos_arms.test_contract -v
Ran 9 tests in 1.546s
OK

$STUDY_PY -m study.eidos_arms.lab stop
{"stopped": "/Volumes/Mrigesh SSD/eidos-v1/pilot-1/lab/shared"}
```

The local adapter additionally checks artifact containment, existence and digests;
the public validator's structural-only success was not treated as proof of those
properties. The focused unit suite covers accounting, streamed usage, real Eidos
assessment outcomes, candidate locking, artifact references and deadline process
termination. It runs without model calls. The combined lane A/B CLI and the 36
study runs await the controller's integration; no merge was performed here. The
final `control.scope.answer` scientific-field conversion has an offline receipt
fixture test; the live MiniMax smokes used the conservative fallback when their
conclusions did not provide that metadata.
