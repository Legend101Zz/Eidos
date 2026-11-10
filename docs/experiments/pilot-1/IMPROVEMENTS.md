# Eidos improvements found while preparing and running development pilot 1

Each entry records:
- what the pilot preparation exposed;
- the change;
- how it was verified;
- the measured effect.

Changes made before the charter commit are part of the Eidos version the pilot runs. Changes made after it are not used by the pilot's runs. They are listed separately and reported as post-pilot work.

## Before the charter (used by the pilot)

### I1. Reasoning-model support (ADR 0016; `task/reasoning-models` e8fb8e4 + c3fed47)

**Found by:** the pilot's model gate. The shared model, MiniMax-M3.1-Flash, a reasoning model, was run through Eidos's live provider check (`scripts/qualify/live_provider_check.py`, 8 scenarios, at most 24 requests per run) on 11 November 2026.

| Configuration | Scenarios passed | Main failure |
|---|---|---|
| Eidos 1.1.0, json mode, 4096-token output cap | 5/8 | Reasoning tokens used up the output cap before any answer ("Model token limit (4096) exceeded before any response was generated"). |
| Eidos 1.1.0, tool mode, 4096 cap | 4/8 | the same |
| Eidos 1.1.0, json mode, cap raised to 16384 by hand | 6/8 | A 120 s request timeout on the large catalog. The model put a read tool (`inspect_evidence`) into its effect calls, because JSON mode had no way to read evidence before deciding. |
| **With I1, json mode, 16384 / 300 s** | **8/8** | — |
| With I1, tool mode, 16384 / 300 s | 6/8 | The tool loop still exhausted 16384 tokens once, and one large-catalog decision was invalid. |

Receipts: `pilot-1/gate/` (before), and `docs/release/evidence/g4-111-minimax-m31flash-{json,tool}.json` (after). Each configuration was run once. The model is stochastic, so the scores are single observations, not rates.

**Changes:**
- The output cap and the request timeout are explicit settings, and recorded in receipts.
- Reasoning tokens are recorded per request and counted against the output envelope.
- JSON and repair modes gained a bounded read-request loop. The model may ask for allowed read tools before deciding; every round counts against the request and token budgets. A read tool can never pass as an effect call: the runtime and the lab validator both reject it.

**Review:** an independent review (Codex) found two major issues, and both were fixed in c3fed47:
- the final request in a decision could overrun the remaining reservation; requests are now admitted only if their worst case fits;
- the credential broker inferred whether a provider bills reasoning tokens separately; it is now an explicit setting.

**Tests:** 531 passed, 7 skipped (unit, integration, MCP, reporting). There are about 145 new tests. ruff, lint-imports, mypy (3.12 and 3.9 targets) and the wire-schema check are clean. CI: e8fb8e4 green; c3fed47 see the branch's CI run.

## After the charter (not used by the pilot's runs)
Found during the pilot preparation; fixed or being fixed on `task/post-pilot-fixes`. These are not used by the pilot's runs.
- **I2. MCP tool arguments.** With MiniMax driving Claude Code, MCP tool calls arrived with arrays wrapped as `{"item": [...]}` and numbers as strings. Eidos correctly rejected them (HTTP 422), but agents wasted turns, and the study had to ship a side transport (`study/eidos_arms/mcp_http.py`).
- **I3. Stop and deadline.** `stop_investigation` and the investigation deadline did not cancel an in-flight provider request (smoke run arm4-02). The study worked around it with an outer provider bridge.
- **I4. Assessor evidence path.** The built-in paired-CI assessor needs paired-report rows, so ordinary multiclass accuracy needs an authored adapter before an assessor verdict is possible (`study/eidos_arms/CHARTER_RULES.md`).
