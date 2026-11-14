# 0053: Prospective study provider profiles

Date: 2026-11-14. Status: accepted.

A study selects a credential-free `eidos-provider/1` profile by explicit resolver
argument, then `EIDOS_STUDY_PROVIDER`, then `minimax-m3.1-flash`. The provider patch
precedes treatment flags and final overrides. The default has an empty patch and
preserves the immutable investigator configuration, MiniMax credential names,
bridge command and exact served-model guard. Profile identity (id, wire and SHA-256
of the profile file bytes) accompanies arm, run, import and prompt receipts;
workers refuse prepared provider drift. `python -m study.exp.providers check`
validates every profile without credentials. OpenAI's `REPLACE_ME` is valid as an
inventory entry but cannot resolve for execution.

The lab reads the selected Keychain item only when credentials are required,
passing one `<credential_env>=<key>\n` line to `lab start --credentials-stdin`.
The production security executable remains `/usr/bin/security`; tests inject a
fake. API keys are removed from the lab CLI environment. A locked provider marker
forces stop/start when a running supervisor has different provider credentials;
fixture runs start without Keychain access or a provider relay. Claude CLI arms
require the profile's explicit Anthropic-compatible CLI settings.

Both OpenAI chat and Anthropic Messages use the per-run loopback deadline bridge.
Anthropic forwards the Messages query and its small header allowlist, preserving
identifier audit, response backoff metadata and independent exit 124 at the wall
deadline. The Anthropic SDK receives the bridge's root URL. Profile prices use
`price_per_mtok.input/output`; configured per-run `model_usd` limits use the
existing reservation and receipt settlement mechanism. Served ids match only the
profile's case-sensitive fnmatch patterns; fixture ids require a fixture runtime.

In pinned pydantic-ai 2.66.0, `tool_choice='auto'` alone still resolves to required
when text output is disallowed (`models/_tool_choice.py:105-106`). Anthropic's
model profile has `supports_forced_tool_choice`; disabling it degrades implicit
forcing to auto (`models/anthropic/__init__.py:2647-2649`). An explicit Eidos
`settings.tool_choice: "auto"` applies that capability independently of model
name. Sonnet stays in `repair` mode as specified; its library profile already
refuses forcing. No thinking, effort or sampling parameter is added.

The library retains raw `stop_reason` in `provider_details.finish_reason`
(`models/anthropic/__init__.py:2338-2340`). Eidos intercepts Anthropic `refusal`
before the agent can re-ask, preserves all response usage and raw refusal metadata,
and records `provider-refused` through the existing bounded-outcome path with no
proposal. It is outside `LOCAL_DECISION_BOUNDS`, so the coordinator cannot use its
slot/turn structured-output retries for a refusal. Normal bounded finalization
remains available. The prompt revision and every default remain unchanged. All provider
verification uses fixtures; callable model availability and live pricing remain
the orchestrator's responsibility.
