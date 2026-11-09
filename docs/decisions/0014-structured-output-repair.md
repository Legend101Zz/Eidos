# 0014: Bounded structured-output repair for local models

Status: accepted (2026-11-09, release 1.1.0).

**Context.** G4 (v1.0.0) qualified the OpenAI-compatible adapter against a local Ollama endpoint, but no local model
of 14B or less passed decision-schema conformance. Most failures were malformed output: the provider tool-call
output mode (Pydantic AI's output tool) received arguments the model had not formed as the schema requires, and the
single output retry was exhausted (`UnexpectedModelBehavior: Exceeded maximum output retries (1)`).

**Decision.** The investigator runtime adapter gets an `output_mode` setting (`investigator.settings.output_mode`):

- `tool` (default; unchanged): the provider's tool-call output, as qualified with Anthropic.
- `repair`: the model replies with one JSON object in a lenient shape (call arguments as a JSON object rather than
  a JSON string). The reply goes through a bounded repair layer (`eidos.investigator.structured_output`):
  1. **strict extraction**: one balanced JSON object, with named repairs for a code fence, leading or trailing
     prose, trailing commas, and single quotes / Python literals (parsed with `ast.literal_eval`, never `eval`);
     the lenient shapes are mapped onto the typed `Decision` model (also named repairs), which validates it;
  2. **one schema-guided re-ask** when the reply still fails: the validation error (field paths and messages) is
     sent back with the history, without read tools, under a request reserved for it from the decision's budget;
  3. if that also fails: an explicit no-effect decision with `bounded_outcome: structured-output-invalid`.
- `json`: `repair` plus constrained decoding where the endpoint supports it: OpenAI-compatible `response_format`
  with a JSON schema of the lenient shape (Ollama maps it onto its `format`). Anthropic has no JSON mode, so there
  the repair path alone applies.

**Never silent.** Every decision in `repair`/`json` mode carries `usage.structured_output` = `{mode, json_mode,
repairs, reasks, errors, valid}`, copied into the decision's model settings (and so into the turn's decision record
and its receipt). The re-ask is an ordinary model request: it is counted in `usage.calls`, its tokens are in the
usage and charged against the decision's reservation like any other request, and it can never exceed
`max_requests_per_decision` (one request is held back for it; with a budget of one request there is no re-ask).

**Measurement.** `scripts/qualify/live_provider_check.py --output-mode tool|repair|json` re-runs the G4 scenarios.
Before/after receipts for `qwen2.5:3b-instruct` and `qwen2.5:7b-instruct` on the owner's Mac are in
`docs/release/evidence/g4-110-*.json`, with the recorded malformed replies turned into unit fixtures
(`tests/fixtures/structured-output/`, 21 recorded replies, `tests/unit/test_structured_output_repair.py`).

**Results (owner's Mac, Apple M4, own Ollama on 127.0.0.1:11535; 2026-11-09 15:09–15:38 UTC, 29.5 of the 60 minutes
allowed; the 14B model was not run).** Five scenarios expect a valid decision (`docs/release/evidence/g4-110-summary.json`):

| Model | Output mode | Scenarios passed (of 8) | Decision obtained (of 5) | Domain-valid (of 5) | Executable (G4 pass, of 5) | Output-retry failures |
|---|---|---|---|---|---|---|
| qwen2.5:3b-instruct | tool (before) | 4 | 1 | 1 | 1 | 4 |
| qwen2.5:3b-instruct | **json** (after) | **5** | **5** | **3** | **2** | 0 |
| qwen2.5:3b-instruct | repair (no JSON mode) | 2 | 5 | 1 | 0 | 0 |
| qwen2.5:7b-instruct | tool (before) | 3 | 0 | 0 | 0 | 5 |
| qwen2.5:7b-instruct | **json** (after) | 3 | **5** | **2** | **1** | 0 |

What this shows, and what it does not:
- The repair layer removed the failure G4 found: no decision was lost to malformed output in `json` or `repair` mode
  (every reply became a typed decision; one `repair` reply needed the re-ask, which fixed it).
- The remaining failures are **semantic**: the models name tools that do not exist (`search_components` as an effect
  call, a capability id as a tool) or omit required arguments. The lab's validator refuses those, as it should.
  Neither model passes full conformance after the change; small local models remain unsuitable as investigators.
- Plain `repair` without JSON mode did worse than `tool` mode for the 3B model in this sample; `json` is the mode to
  use with Ollama. Each configuration ran once (n=5 decision scenarios), so differences of one scenario are noise.
- In `json` mode the models no longer used the read tools (they wrote read-tool names as effect calls).

**Alternatives.** Raising `output_retries`: unbounded cost for the same failure, and the errors stay inside the
library. Pydantic AI's own prompted/native output modes: they hide which repair happened and how many requests it
cost. A grammar-constrained local server only: does not help hosted endpoints or Anthropic.

**Reversal.** If a provider's native structured output proves reliable for small models, make it the default for
that provider; the repair report stays.
