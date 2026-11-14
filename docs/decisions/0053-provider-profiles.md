# 0053: Prospective study provider profiles

Date: 2026-11-14. Status: accepted.

A study selects a credential-free `eidos-provider/1` profile by explicit resolver
argument, then `EIDOS_STUDY_PROVIDER`, then `minimax-m3.1-flash`. The provider patch
precedes treatment flags and final overrides. The default has an empty patch and
preserves the immutable investigator configuration, MiniMax credential names,
bridge command and exact served-model guard. Profile identity (id, wire and SHA-256
of the profile file bytes) accompanies arm, run, import and prompt receipts;
workers refuse prepared provider drift. `python -m study.exp.providers check`
validates every profile without credentials. Any profile containing `REPLACE_ME`
remains valid as an inventory entry but cannot resolve for execution.

The lab reads the selected Keychain item only when credentials are required,
passing one `<credential_env>=<key>\n` line to `lab start --credentials-stdin`.
The production security executable remains `/usr/bin/security`; tests inject a
fake. API keys are removed from the lab CLI environment. A locked provider marker
forces stop/start when a running supervisor has different provider credentials;
fixture runs start without Keychain access or a provider relay. Claude CLI arms
require the profile's explicit Anthropic-compatible CLI settings.
Stops must confirm supervisor exit (with one retry); starts must confirm running
and the requested stdin credential names before the provider marker is written.
An already-running or unapplied-credentials answer triggers a confirmed stop and
one fresh start attempt, then refuses if still unconfirmed. Fixture starts use
`--credentials-stdin` with empty input so the CLI cannot load stored keys.
Arm-independent agent environments do not expose the provider selection and
remain usable for profiles without a Claude CLI path.

Both OpenAI chat and Anthropic Messages use the per-run loopback deadline bridge.
Anthropic forwards the Messages query and its small header allowlist, preserving
identifier audit, response backoff metadata and independent exit 124 at the wall
deadline. The Anthropic SDK receives the bridge's root URL. Profile prices use
`price_per_mtok.input/output`; configured per-run `model_usd` limits use the
existing reservation and receipt settlement mechanism. Served ids match only the
profile's case-sensitive fnmatch patterns; fixture ids require a fixture runtime.
The pinned Anthropic 2.6.0 SDK sends `Authorization: Bearer <key>` and
`anthropic-version: 2026-10-21` (`anthropic/_client.py:782-801`,
`anthropic/_constants.py:7`). The relay accepts either a nonempty `x-api-key` or
Bearer credential, rejects conflicting values, and forwards only `x-api-key`,
the required version, optional beta and Content-Type. A real SDK and the
`build_model` path are tested through a bridge process to a loopback fake HTTP
upstream. Required eval audits refuse fixture runtimes before lab startup.
For `openai-chat`, malformed Content-Length now returns HTTP 400 instead of
crashing the handler: this is the one intended wire behavior difference from
main (in addition to the required receipt's `wire` field).

In pinned pydantic-ai 2.66.0, `tool_choice='auto'` alone still resolves to required
when text output is disallowed (`models/_tool_choice.py:105-106`). Anthropic's
model profile has `supports_forced_tool_choice`; disabling it degrades implicit
forcing to auto (`models/anthropic/__init__.py:2647-2649`). An explicit Eidos
`settings.tool_choice: "auto"` applies that capability independently of model
name. The regression uses the same forcing-capable `claude-3-5-haiku-latest`
model with and without the setting and observes `any` versus `auto` on outgoing
requests. For shipped `claude-haiku-5-5` and `claude-sonnet-5-5`, the installed
library already sends no forced choice: native JSON-schema output uses
`output_config`, with no tools/tool_choice. The switch is dormant for these
profiles. Sonnet's `output_mode` stays `repair` as specified and is untested live;
the tests do not establish that Sonnet needs repair. No thinking, effort or
sampling parameter is added.

The library retains raw `stop_reason` in `provider_details.finish_reason`
(`models/anthropic/__init__.py:2338-2340`). Eidos intercepts Anthropic `refusal`
before the agent can re-ask, preserves all response usage and raw refusal metadata,
and records `provider-refused` through the existing bounded-outcome path with no
proposal. It is outside `LOCAL_DECISION_BOUNDS`, so the coordinator cannot use its
slot/turn structured-output retries for a refusal. Normal bounded finalization
remains available. The prompt revision and every default remain unchanged. All provider
verification uses fixtures; callable model availability and live pricing remain
the orchestrator's responsibility.

`openai-default` selects `openai:gpt-5-nano` with JSON output, served pattern
`gpt-5-nano*`, USD 0.05/0.40 per million input/output tokens and a USD 0.60 run
limit, with no Claude CLI arm. On 14 Nov 2026 the orchestrator listed 96 models
(HTTP 200), including gpt-5-nano, the cheapest listed text model at standard-tier
prices; strict required function calling and JSON-schema response requests both
returned HTTP 200, with schema-valid JSON and served id `gpt-5-nano-2025-08-07`.
These are orchestrator measurements; no live checks were repeated here.
