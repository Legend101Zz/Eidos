"""Synthetic labeled text through the lab's LLM broker (example for ADR 0013).

The component never holds a provider key: it receives a scoped grant (models, request and token limits, expiry) and
calls ``llm.complete``. Every call is a receipt in the lab's ledger; the dataset records the call id per row, so a
row can be traced to the exact model call that produced it.
"""

from __future__ import annotations

from eidos_worker import ContractError

PROMPT_REVISION = "labeled-examples/1"
SYSTEM = ("You write one short, realistic example sentence for a text-classification dataset. Reply with the sentence "
          "only: no quotes, no numbering, no explanation.")


def _config(config):
    labels = config.get("labels")
    n = config.get("per_label", 2)
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x for x in labels):
        raise ContractError("config.labels must be a non-empty list of label names")
    if not isinstance(n, int) or not 1 <= n <= 50:
        raise ContractError("config.per_label must be an integer between 1 and 50")
    max_tokens = config.get("max_tokens_per_example", 60)
    if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 400:
        raise ContractError("config.max_tokens_per_example must be an integer between 1 and 400")
    return labels, n, max_tokens, str(config.get("topic", "customer messages"))


def clean(text):
    """One line, without surrounding quotes or list markers; None if nothing usable is left."""
    line = next((x.strip() for x in str(text).splitlines() if x.strip()), "")
    line = line.lstrip("-*0123456789.) ").strip().strip('"').strip("'").strip()
    return line if 3 <= len(line) <= 500 else None


def run(inputs, config, *, writer=None, llm=None):
    labels, n, max_tokens, topic = _config(config)
    if llm is None:
        raise ContractError("this component needs an LLM grant: the project policy must enable the broker "
                            "(policy.llm_broker), and a sealed run must allow it (allow_llm_broker)")
    rows, rejected, tokens, models = [], 0, 0, set()
    for label in labels:
        for i in range(n):
            out = llm.complete([{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": f"Topic: {topic}. Label: {label}. Example {i + 1}."}],
                               max_tokens=max_tokens, request_key=f"{label}:{i}")
            tokens += int((out.usage or {}).get("input_tokens") or 0) + int((out.usage or {}).get("output_tokens") or 0)
            models.add(out.model)
            text = clean(out.text)
            if text is None:
                rejected += 1
                continue
            rows.append({"text": text, "label": label, "call_id": out.call_id, "model": out.model,
                         "prompt_revision": PROMPT_REVISION})
    writer.stage_json("dataset", rows, schema="labeled-text/1")
    return {"prompt_revision": PROMPT_REVISION, "requested": len(labels) * n, "generated": len(rows),
            "rejected": rejected, "calls": len(labels) * n, "models": sorted(models), "tokens": tokens}
