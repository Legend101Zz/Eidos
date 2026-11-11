# 0025: A compact typed research workflow for external agents over MCP

Status: accepted (2026-11-11, round-3 item G3; recommendation 4 of diagnosis D2). Extends ADR 0017 (MCP argument
normalization), ADR 0021 (assessment by default, evidence-derived claim state) and ADR 0022 (dependent batches). The
expected effects are hypotheses for the next pilot, not measured gains.

## Evidence (D2 report, "Why arm 3 cost 39.2M tokens")

- 37.88M of arm 3's 39.20M tokens (96.6%) were cache reads: every request replays the whole context. D03-r1 used
  91 model responses and 101 tool calls; its first request already had 13,405 input tokens against about 1,870 in arms
  1 and 2. That points at MCP/setup context, without isolating tool-schema tokens.
- The failure traces were integration loops, not search: D03-r1 repaired command keys, purposes, source-upload schemas and
  artifact ids; D06-r3 had 100% validation accuracy and then spent the rest of its budget on upload/schema repair, scope
  repair, `assess_candidate` and six draft attempts.
- Source authoring needed a separate HTTP upload with manual contract metadata. Dependent batches existed only for the
  internal investigator. The study's `mcp_http.py` initialized a new MCP session on every call.
- One more gap found while building this: `record_note` was not an MCP tool, so an MCP-only agent could not record the
  `candidate_lock` that `assess_candidate` requires for an investigation (`candidate-not-locked`).

## Decision

Four MCP tools give an external coding agent the whole loop in a few small calls. Each is composed in the adapter from
ordinary lab commands and queries under the caller's own token (`src/eidos/mcp/workflow.py`); none is a new domain
operation, and the lab validates every step exactly as it validates a single MCP call or an investigator call.

| Tool | Does | Keys |
|---|---|---|
| `upload_source` | `files` (relative path → text) become an immutable `source-bundle/1` artifact through the lab's upload route, with the same bytes, media type, schema and role as the investigator's `author_source`. Absolute paths, `..` and non-text contents are refused before upload. | the call's `command_key` |
| `run_batch` | Up to 12 calls in order, with ADR 0022 references (`{"$ref": "call[i].field"}` or `call[i].outputs.<slot>`). | `<key>:<i>` per call; `<key>:batch` for the record |
| `wait_status` | Waits at most 50 s (default 20) for named jobs to end and a plan to have no active job, then summarizes them; with `claim_id` the derived claim state; with `investigation_id` its status. Read-only. | none |
| `submit_candidate` | Checks that the candidate's job succeeded, records `candidate_lock` `{"job_id"}`, files the `improves` claim and, with `assessment` (the `assess_candidate` arguments), requests its assessment; returns the claim state the lab derives. | `<key>:lock`, `<key>:claim`, `<key>:assess` |

### Batches use the investigator's executor

The batch core moved out of `investigator/activities.py` into `src/eidos/batches.py` (`BatchRunner`: dependency gate,
the one-CPU allowance, plan allowance of addendum R11, reference resolution, terminal-job cache). The investigator's
`execute_decision` now uses it with the same rules and messages (`tests/unit/test_dependent_batches.py`, 167 tests,
unchanged). `eidos.domain.turns.validate_refs` takes the vocabulary as parameters, so the MCP batch is checked by the
same reference rules with MCP tool names (`upload_source` produces `artifact_id`; `validate_component` is a job tool).

- **Validated before any effect.** Each call's arguments are checked against that MCP tool's own schema (a reference
  stands in for a value of the declared type), `project`/`command_key`/`expected_revision` inside a call are refused,
  and references are refused if malformed, forward or self, out of range, naming a field the tool does not produce,
  `outputs.*` of a non-job tool, or placed in `component_ref`, `environment`, `investigation_id` or `purpose`
  (`INVALID_REF`, `INVALID_BATCH`). Nothing reaches the lab.
- **Recorded prospectively.** The first command is a `record_note` of kind `mcp-batch` keyed `<key>:batch`, holding the
  calls' tools, derived `depends_on` and a digest of the calls, before any effect, as an accepted decision is persisted
  before it runs. The same key with other calls is refused there (`COMMAND_KEY_CONFLICT`) before anything runs.
- **Bounded waiting.** A call waiting for a job past `wait_seconds` (max 50) returns `status: pending` and `next_call`.
  Calling again with the same key and calls replays every committed call by its key (the lab returns the original
  receipt, `replayed: true`) and continues. Jobs keep running whether or not anyone waits.
- Jobs run one at a time; evidence plans get the batch's allowance (1), whatever the call's arguments say. Failures give
  dependents `DEPENDENCY_FAILED`, a missing output slot `REF_UNRESOLVED`; independent calls still run. Each receipt
  shows its resolved references (path, value). Lineage is the lab's own: each job's lock names the uploaded source or the
  earlier job's output artifact.

### Submission produces the derived state, never asserts it

`submit_candidate` writes nothing the lab would not accept from separate calls, and reads the state back through
`get_claim`: a filed claim with no confirming assessment is `candidate-submitted` (ADR 0021). A refused assessment is
returned as `status: refused` with its reason codes (it is recorded by the lab, as before), and the claim remains
`candidate-submitted`, which the tool description calls a complete result. Confirmation still needs a conformant,
healthy assessor pass on a plan verified at freeze; selection data stays exploratory. A job that has not succeeded is
refused (`CANDIDATE_NOT_READY`) before anything is recorded.

### Compact responses and schemas

- New tools return ids, statuses and artifact handles (`outputs: {slot: artifact_id}`); a component receipt is inlined
  only up to 800 characters of JSON, otherwise its keys and `expand: get_run`. `get_artifact_access` and `get_run`
  expand any handle.
- Tool results' text content is compact JSON (it was indented). `structured_content` is unchanged.
- Shared boilerplate is stated once in the server instructions: the `command_key` semantics (retry with the same key and
  arguments; `COMMAND_KEY_CONFLICT`), `expected_revision` and errors. Each schema keeps a one-line reminder. Descriptions
  no longer repeat "(Mutating command `x`.)": read-only versus mutating is in the tool annotations; the grant stays.
  The per-tool `payload` description is one line, and composite tools have no `payload`.

**Measured** (`scratch/measure_tools.py`; compact JSON of `tools/list`; token counts use `cl100k_base` from tiktoken as a
proxy, since no Anthropic tokenizer is available offline):

| | tools | `tools/list` bytes | tokens (cl100k) | description bytes | instructions bytes |
|---|---|---|---|---|---|
| before (7e6641d) | 35 | 46,387 | 9,597 | 6,792 | 499 |
| after | 39 | 39,726 | 8,572 | 6,922 | 754 |

The tool list is 14% smaller (−6,661 bytes, about −1,025 tokens) while adding four tools; the 35 original tools alone
shrank by 24% (46,387 to 35,266 bytes); the four new tools take 4,471. Response sizes on a real lab (two jobs, fit then score;
`tests/integration/test_mcp_workflow_lab.py` setup): two `get_run` reads were 9,799 bytes (5,513 without the lock),
and `wait_status` for both was 413 bytes. The finished three-call `run_batch` was 1,202 bytes. A `run_component` receipt
went from 354 to 294 bytes with compact JSON. `tests/mcp` bounds the tool list at 41,000 bytes.

### Session reuse in the study transport

`study/eidos_arms/mcp_http.py` has a `Session` that initializes once and reuses the server's `Mcp-Session-Id` for
every call; across invocations of the CLI it keeps the id in `.mcp_session.json` (mode 0600, bound to a digest of the
URL and token, never the token). A session the server no longer knows (HTTP 404, as the MCP SDK answers) is
re-initialized once and the request re-sent; the server did not process it. The CLI prints the result once (without the
`content` text that repeats `structuredContent`). Native MCP clients are unaffected. `--new-session` forces a fresh
session.

## What does not change

The same validated domain operations, idempotency keys, revisions, provenance and claim rules apply: every effect is a
lab command under the caller's token and grants; the adapter keeps no state between calls; no validator or assessor is
relaxed. The existing 35 tools keep their names, arguments and `structured_content`.

## Not done, and limits

- The batch's per-call receipts are returned and each effect is recorded by the lab, but there is no
  `decision_receipts` record for an MCP batch (that record belongs to an investigator decision). The `mcp-batch` note
  records the plan, not the outcomes.
- Concurrency inside an MCP batch is fixed at one job; parallel arms still need a project resource policy and direct
  `launch_evidence_plan`.
- A batch blocks the MCP request while it waits (≤ 50 s). Clients with shorter request timeouts should pass a smaller
  `wait_seconds`.
- Token counts are a proxy (cl100k); the arm-3 prompt cost of the next pilot is the real measurement. Whether fewer, smaller
  calls free budget for better experiments is the hypothesis D2 states; it is not shown here.
- `wait_status` polls (1 s); there is no server push.
