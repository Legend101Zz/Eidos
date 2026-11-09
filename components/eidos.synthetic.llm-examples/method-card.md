# eidos.synthetic.llm-examples 0.1.0

**What it does.** Generates a small labeled text dataset (`labeled-text/1`: `text`, `label`, `call_id`, `model`,
`prompt_revision`) by asking an LLM for one example sentence per call, through the lab's credential broker
(ADR 0013). It is the example of a component with its own bounded LLM calls.

**Grant.** Declares `llm` use (`role: synthetic-data-generator`, `prompt_revision: labeled-examples/1`,
`max_calls: 20`, `max_tokens: 8000`). The lab narrows this with the project's `policy.llm_broker` (model allowlist,
limits, prices) and issues a short-lived grant per attempt. The component never sees a provider key. Under a sealed
profile it gets no grant unless the run or plan sets `allow_llm_broker`.

**Config.** `labels` (list of names), `per_label` (1-50), `max_tokens_per_example` (1-400), `topic`.

**Receipt.** `requested`, `generated`, `rejected` (responses that were not a usable single sentence), `calls`,
`models`, `tokens`. Each row's `call_id` names the `llm_call` receipt in the ledger (model, provider request id,
usage, response digest).

**Limits.** The output reflects the model and prompt revision; it is synthetic data, not evidence about any real
population. Responses are cleaned to one line; nothing checks that a sentence fits its label. Re-running regenerates
(model outputs can differ); the published dataset, not the regeneration, is the reproducible artifact.

**Fixtures.** Author tests only: without a grant the run fails with a contract error; an empty label list is refused.
The broker path is tested in `tests/integration/test_llm_broker.py`.
