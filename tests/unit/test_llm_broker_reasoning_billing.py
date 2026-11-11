"""The LLM broker's reasoning-token accounting is a configured contract, not an inference from relative counts
(ADR 0016): ``included`` (the OpenAI-compatible default: ``completion_tokens`` counts reasoning) or ``separate``
(the provider reports reasoning on top, so it is charged on top). Fixture providers and a faked ``httpx.post``:
nothing here calls a provider or a network endpoint."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidos.investigator import llm_broker
from eidos.investigator.llm_broker import (
    REASONING_BILLING,
    AnthropicProvider,
    LlmBroker,
    OpenAICompatibleProvider,
    openai_usage,
    openai_usage_accounted,
    providers_from_env,
)

pytestmark = pytest.mark.fixture_llm

MSGS = [{"role": "user", "content": "x"}]


def usage_obj(completion: int = 100, reasoning: int | None = None, prompt: int = 10) -> dict:
    u: dict = {"prompt_tokens": prompt, "completion_tokens": completion}
    if reasoning is not None:
        u["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return u


def fake_httpx(monkeypatch, usage: dict, sent: dict | None = None) -> None:
    import httpx
    body = {"id": "chatcmpl-1", "choices": [{"message": {"content": "hello"}}], "usage": usage}

    def fake_post(url, **kw):
        if sent is not None:
            sent.update(url=url, **kw)
        return SimpleNamespace(status_code=200, json=lambda: body)
    monkeypatch.setattr(httpx, "post", fake_post)


# -- openai_usage ----------------------------------------------------------------------------------------------------
def test_separate_billing_charges_reasoning_on_top_of_completion_tokens():
    # the reviewed case: a provider billing reasoning separately reports 100 completion + 60 reasoning tokens
    u = usage_obj(100, 60)
    assert openai_usage(u, "separate") == (10, 160, 60)
    assert openai_usage_accounted(u, "separate") == (10, 160, 60, "added-to-output")


def test_included_billing_does_not_add_reasoning_to_completion_tokens():
    u = usage_obj(100, 60)
    assert openai_usage(u) == (10, 100, 60)  # "included" is the default
    assert openai_usage(u, "included") == (10, 100, 60)
    assert openai_usage_accounted(u, "included") == (10, 100, 60, "included-in-output")
    assert openai_usage_accounted(usage_obj(100, 100)) == (10, 100, 100, "included-in-output")


def test_included_billing_charges_reasoning_that_contradicts_the_contract():
    # more reasoning than completion tokens cannot be "included": charged, never undercharged
    u = usage_obj(100, 300)
    assert openai_usage(u, "included") == (10, 400, 300)
    assert openai_usage_accounted(u, "included") == (10, 400, 300, "added-to-output")
    assert openai_usage(u, "separate") == (10, 400, 300)


@pytest.mark.parametrize("billing", REASONING_BILLING)
@pytest.mark.parametrize("u", [usage_obj(100), {**usage_obj(100), "completion_tokens_details": None},
                               {**usage_obj(100), "completion_tokens_details": {}},
                               usage_obj(100, True)])
def test_reasoning_not_reported_is_never_invented(billing, u):
    assert openai_usage(u, billing) == (10, 100, None)
    assert openai_usage_accounted(u, billing) == (10, 100, None, "not-reported")


@pytest.mark.parametrize("billing", REASONING_BILLING)
def test_missing_counts_are_zero(billing):
    assert openai_usage({}, billing) == (0, 0, None)
    assert openai_usage_accounted({"prompt_tokens": None, "completion_tokens": None}, billing) == (0, 0, None, "not-reported")


@pytest.mark.parametrize("bad", ["", "Included", "inferred", "auto", None, True, 1])
def test_openai_usage_rejects_an_unknown_billing_contract(bad):
    with pytest.raises(ValueError, match="reasoning_billing"):
        openai_usage(usage_obj(100, 60), bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reasoning_billing"):
        openai_usage_accounted({}, bad)  # type: ignore[arg-type]


# -- OpenAICompatibleProvider ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("billing, usage, expected", [
    ("separate", usage_obj(100, 60), (160, 60, "added-to-output")),
    ("included", usage_obj(100, 60), (100, 60, "included-in-output")),
    ("included", usage_obj(100, 300), (400, 300, "added-to-output")),
    ("separate", usage_obj(100), (100, None, "not-reported")),
    ("included", usage_obj(100), (100, None, "not-reported"))])
def test_provider_result_carries_charged_output_reasoning_and_accounting(monkeypatch, billing, usage, expected):
    sent: dict = {}
    fake_httpx(monkeypatch, usage, sent)
    p = OpenAICompatibleProvider("http://127.0.0.1:1/v1", reasoning_billing=billing)
    res = p.complete("m", MSGS, 64)
    assert (res.output_tokens, res.reasoning_tokens, res.reasoning_accounting) == expected
    assert res.text == "hello" and res.input_tokens == 10 and res.request_id == "chatcmpl-1"
    assert sent["url"] == "http://127.0.0.1:1/v1/chat/completions"


def test_provider_defaults_to_included_and_never_shows_its_key():
    p = OpenAICompatibleProvider("http://127.0.0.1:1/v1/", "sk-secret")
    assert p.reasoning_billing == "included" and p.base_url == "http://127.0.0.1:1/v1"
    assert "sk-secret" not in repr(p) and "included" in repr(p)


@pytest.mark.parametrize("bad", ["", "Separate", "both", None])
def test_provider_rejects_an_unknown_billing_contract_at_construction(bad):
    with pytest.raises(ValueError, match="reasoning_billing"):
        OpenAICompatibleProvider("http://127.0.0.1:1/v1", "k", reasoning_billing=bad)  # type: ignore[arg-type]


# -- providers_from_env ----------------------------------------------------------------------------------------------
ENV = {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oai"}
EPS = {"openai-compatible": "http://127.0.0.1:1/v1"}


def test_providers_from_env_defaults_every_openai_compatible_provider_to_included():
    ps = providers_from_env(ENV, EPS)
    assert isinstance(ps["anthropic"], AnthropicProvider)
    assert ps["openai-compatible"].reasoning_billing == "included" and ps["openai"].reasoning_billing == "included"
    ps = providers_from_env(ENV, EPS, {})
    assert ps["openai-compatible"].reasoning_billing == "included" and ps["openai"].reasoning_billing == "included"


def test_providers_from_env_wires_the_setting_to_the_named_provider():
    ps = providers_from_env(ENV, EPS, {"openai-compatible": "separate"})
    assert ps["openai-compatible"].reasoning_billing == "separate" and ps["openai"].reasoning_billing == "included"
    ps = providers_from_env(ENV, EPS, {"openai": "separate", "openai-compatible": "included"})
    assert ps["openai-compatible"].reasoning_billing == "included" and ps["openai"].reasoning_billing == "separate"


def test_providers_from_env_setting_for_an_unconfigured_provider_is_valid_and_unused():
    ps = providers_from_env({"OPENAI_API_KEY": "sk-oai"}, None, {"openai-compatible": "separate"})
    assert sorted(ps) == ["openai"] and ps["openai"].reasoning_billing == "included"


@pytest.mark.parametrize("bad", [{"openai-compatible": "inferred"}, {"openai": ""}, {"openai": None},
                                 {"openai-compatible": "separate", "openai": "sometimes"}])
def test_providers_from_env_rejects_an_invalid_value_naming_the_key(bad):
    with pytest.raises(ValueError, match="reasoning_billing\\['openai(-compatible)?'\\]"):
        providers_from_env(ENV, EPS, bad)


@pytest.mark.parametrize("key", ["openai_compatible", "OpenAI", "fake", "", "ollama"])
def test_providers_from_env_rejects_an_unknown_provider_key_naming_it(key):
    with pytest.raises(ValueError, match="unknown provider") as exc:
        providers_from_env(ENV, EPS, {key: "separate"})
    assert repr(key) in str(exc.value)


def test_providers_from_env_rejects_anthropic_with_the_reason():
    with pytest.raises(ValueError, match="anthropic.*not accepted.*no reasoning count"):
        providers_from_env(ENV, EPS, {"anthropic": "separate"})


def test_providers_from_env_rejects_a_setting_that_is_not_a_mapping():
    with pytest.raises(ValueError, match="map a provider name"):
        providers_from_env(ENV, EPS, "separate")  # type: ignore[arg-type]


# -- through LlmBroker.handle ----------------------------------------------------------------------------------------
TOKEN = "eidos_llmg_proj1.abcdef"


class FakeClient:
    """Records every command; ``broker_reserve_call`` returns a reservation, ``broker_settle_call`` a charge."""

    def __init__(self, model: str = "openai-compatible:m"):
        self.model, self.commands = model, []

    def command(self, name, payload, *, project, key):
        self.commands.append((name, payload))
        if name == "broker_reserve_call":
            return {"result": {"call_id": "call_1", "model": self.model, "replayed": False}}
        return {"result": {"charged_usd": 0.5}}

    def settled(self) -> dict:
        (payload,) = [p for n, p in self.commands if n == "broker_settle_call"]
        return payload


def ask(broker: LlmBroker) -> tuple[int, dict]:
    return broker.handle(TOKEN, {"messages": MSGS, "max_tokens": 64, "model": "openai-compatible:m"})


@pytest.mark.parametrize("billing, usage, expected", [
    ("separate", usage_obj(100, 60), {"input_tokens": 10, "output_tokens": 160, "reasoning_tokens": 60,
                                      "reasoning_accounting": "added-to-output"}),
    ("included", usage_obj(100, 60), {"input_tokens": 10, "output_tokens": 100, "reasoning_tokens": 60,
                                      "reasoning_accounting": "included-in-output"}),
    ("separate", usage_obj(100), {"input_tokens": 10, "output_tokens": 100, "reasoning_accounting": "not-reported"})])
def test_broker_settles_and_returns_the_charged_usage(monkeypatch, billing, usage, expected):
    fake_httpx(monkeypatch, usage)
    client = FakeClient()
    provider = OpenAICompatibleProvider("http://127.0.0.1:1/v1", reasoning_billing=billing)
    status, body = ask(LlmBroker(client, {"openai-compatible": provider}))
    assert status == 200 and body["text"] == "hello" and body["call_id"] == "call_1" and body["usd"] == 0.5
    settle = client.settled()
    assert settle["status"] == "ok" and settle["usage"] == expected and settle["provider_request_id"] == "chatcmpl-1"
    assert body["usage"] == expected


def test_broker_usage_stays_as_before_for_a_provider_without_an_accounting_label():
    class Plain:
        def complete(self, model, messages, max_tokens):
            return llm_broker.ProviderResult("echo", 7, 5, "req-1")
    client = FakeClient("fake:echo")
    status, body = LlmBroker(client, {"fake": Plain()}).handle(
        TOKEN, {"messages": MSGS, "max_tokens": 64, "model": "fake:echo"})
    assert status == 200 and body["usage"] == {"input_tokens": 7, "output_tokens": 5}
    assert client.settled()["usage"] == {"input_tokens": 7, "output_tokens": 5}
