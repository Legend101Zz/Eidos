"""Reasoning-model support (ADR 0016): reasoning-token accounting, the larger output cap of the live check, the
Eidos-owned read loop of the text modes (read request -> results -> decision, bounded by the request budget and the
decision deadline), refusal of read tools in effect calls, and ``<think>`` blocks. Fixture models only: nothing here
calls a provider or a network endpoint."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from eidos.domain.errors import Invalid
from eidos.domain.turns import validate_decision
from eidos.investigator import llm_broker
from eidos.investigator import pydantic_ai_runtime as par
from eidos.investigator import structured_output as so
from eidos.investigator.activities import decision_usd_envelope
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime, usage_from_messages
from eidos.investigator.runtime import DecisionRequest, DecisionResult

pytestmark = pytest.mark.fixture_llm

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qualify" / "live_provider_check.py"
GOOD = {"rationale": "baseline first", "purpose": "reproduction",
        "hypotheses": [{"statement": "h", "prediction": "p", "falsifier": "f"}],
        "calls": [{"tool": "run_component", "args": {"component_ref": "a@1.0.0", "inputs": {"train": "art_1"}}}],
        "control": {"action": "wait_for_jobs", "summary": "wait"}}
READ_NAMES = ["inspect_evidence", "search_components", "describe_component", "get_job"]


# -- helpers ---------------------------------------------------------------------------------------------------------
def usage(inp: int = 10, out: int = 20, reasoning: int | None = None) -> RequestUsage:
    return RequestUsage(input_tokens=inp, output_tokens=out,
                        details={} if reasoning is None else {"reasoning_tokens": reasoning})


def response(inp: int = 10, out: int = 20, reasoning: int | None = None) -> ModelResponse:
    return ModelResponse(parts=[TextPart("x")], usage=usage(inp, out, reasoning), model_name="m")


def request(tools=None, **limits) -> DecisionRequest:
    return DecisionRequest(turn_id="t1", slot=0, context="# Investigation\nQuestion: why?", turn_so_far=[],
                           limits={"max_calls": 4, **limits}, read_tools=tools if tools is not None else {})


def scripted(replies, usages=None):
    """A FunctionModel replying with the scripted texts in order (the last one repeats); records every request."""
    seen: list = []
    infos: list[AgentInfo] = []

    def fn(messages, info: AgentInfo):
        n = len(seen)
        seen.append(messages)
        infos.append(info)
        u = usages[min(n, len(usages) - 1)] if usages else usage()
        return ModelResponse(parts=[TextPart(replies[min(n, len(replies) - 1)])], usage=u)
    return SimpleNamespace(model=FunctionModel(fn), seen=seen, infos=infos)


def runtime(script, mode: str = "repair", **cfg) -> PydanticAIRuntime:
    return PydanticAIRuntime({"_model_object": script.model, "model": "openai-compatible:fake", "output_mode": mode,
                              **cfg})


def prompt_of(messages) -> str:
    """The text of the last user message of one request."""
    return str(messages[-1].parts[-1].content)


def results_of(messages) -> list[dict]:
    first = prompt_of(messages)
    assert first.startswith("## Read results\n")
    return json.loads(first.split("\n")[1])


def read_request(*reads, rationale: str = "check the evidence") -> str:
    return json.dumps({"rationale": rationale, "read": [{"tool": t, "args": a} for t, a in reads]})


def read_tools(log: list, **override):
    def inspect_evidence(artifact_id, max_bytes=8192):
        log.append(("inspect_evidence", artifact_id, max_bytes))
        return {"preview": "ids,predictions\n0,3", "truncated": True}

    def search_components(text="", capability=None):
        log.append(("search_components", text, capability))
        return {"components": [{"ref": "a@1.0.0"}]}

    def describe_component(component_ref):
        log.append(("describe_component", component_ref))
        return {"ref": component_ref}

    def get_job(job_id):
        log.append(("get_job", job_id))
        return {"id": job_id, "status": "succeeded"}
    return {"inspect_evidence": inspect_evidence, "search_components": search_components,
            "describe_component": describe_component, "get_job": get_job, **override}


# -- live provider check: --max-output-tokens ------------------------------------------------------------------------
def _load_script():
    spec = importlib.util.spec_from_file_location("live_provider_check_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_check(monkeypatch, capsys, *argv, reasoning: int | None = 7):
    """Run the script's ``main`` with a fake runtime and model builder: nothing is called."""
    mod = _load_script()
    configs: list[dict] = []
    built: list[dict] = []

    class FakeRuntime:
        def __init__(self, cfg):
            configs.append(cfg)

        def decide(self, req):
            u: dict = {"calls": 1}
            if reasoning is not None:
                u["reasoning_tokens"] = reasoning
            proposal = {"rationale": "r", "calls": [], "control": {"action": "wait_for_jobs", "summary": "w"}}
            return DecisionResult(proposal, u, [], {"served_models": ["fake"]}, "investigator-prompt/1")

    def fake_build_model(cfg):
        built.append(cfg)
        return SimpleNamespace(client=SimpleNamespace(max_retries=0))

    monkeypatch.setattr(par, "PydanticAIRuntime", FakeRuntime)
    monkeypatch.setattr(par, "build_model", fake_build_model)
    monkeypatch.setattr(sys, "argv", ["live_provider_check.py", "--model", "openai-compatible:fake",
                                      "--base-url", "http://127.0.0.1:1/v1", *argv])
    rc = mod.main()
    return rc, json.loads(capsys.readouterr().out), configs, built, mod


def test_live_check_default_cap_is_4096_and_reaches_every_scenario(monkeypatch, capsys):
    rc, receipt, configs, _, mod = _run_check(monkeypatch, capsys)
    assert rc == 0 and mod.MAX_OUTPUT_TOKENS == 4096
    assert receipt["caps"]["max_output_tokens"] == 4096
    assert len(configs) == 8 and {c["max_output_tokens"] for c in configs} == {4096}


def test_live_check_max_output_tokens_is_recorded_and_passed_to_every_scenario(monkeypatch, capsys):
    rc, receipt, configs, _, _ = _run_check(monkeypatch, capsys, "--max-output-tokens", "16384")
    assert rc == 0 and receipt["caps"]["max_output_tokens"] == 16384
    assert len(configs) == 8 and {c["max_output_tokens"] for c in configs} == {16384}
    assert receipt["summary"]["scenarios"] == 8 and not any("skipped" in s for s in receipt["scenarios"])


def test_live_check_receipt_totals_the_reported_reasoning_tokens(monkeypatch, capsys):
    _, receipt, _, _, _ = _run_check(monkeypatch, capsys, reasoning=7)
    assert receipt["total_reasoning_tokens"] == 8 * 7
    _, receipt, _, _, _ = _run_check(monkeypatch, capsys, reasoning=None)
    assert receipt["total_reasoning_tokens"] is None


@pytest.mark.parametrize("value", ["0", "-1", "-4096"])
def test_live_check_refuses_a_non_positive_cap_before_calling_anything(monkeypatch, capsys, value):
    rc, out, configs, built, _ = _run_check(monkeypatch, capsys, "--max-output-tokens", value)
    assert rc == 2 and "--max-output-tokens must be positive" in out["error"]
    assert configs == [] and built == []


# -- the decision's USD envelope ---------------------------------------------------------------------------------------
PRICE = {"input": 3.0, "output": 15.0}


def test_usd_envelope_prices_everything_at_output_when_the_output_cap_exceeds_the_token_envelope():
    # 8 requests x 16384 = 131072 possible output tokens > the 120000-token envelope: all of it may be output
    assert decision_usd_envelope(120_000, PRICE, {"max_output_tokens": 16384}) == pytest.approx(120_000 * 15 / 1e6)


def test_usd_envelope_prices_only_the_capped_share_at_output_with_the_default_cap():
    # 8 x 4096 = 32768 output tokens at the output rate, the remaining 87232 at the input rate
    expected = ((120_000 - 32_768) * 3.0 + 32_768 * 15.0) / 1e6
    assert decision_usd_envelope(120_000, PRICE, {"max_output_tokens": 4096}) == pytest.approx(expected)
    assert expected < 120_000 * 15 / 1e6


def test_usd_envelope_without_an_output_cap_prices_the_runtime_default_cap():
    # the runtime sends DEFAULT_MAX_OUTPUT_TOKENS with every request when none is configured: the same bound
    assert decision_usd_envelope(120_000, PRICE, {}) == decision_usd_envelope(120_000, PRICE, {"max_output_tokens": 4096})


def test_usd_envelope_uses_the_configured_request_count():
    cfg = {"max_output_tokens": 16384, "max_requests_per_decision": 2}  # 32768 possible output tokens
    expected = ((120_000 - 32_768) * 3.0 + 32_768 * 15.0) / 1e6
    assert decision_usd_envelope(120_000, PRICE, cfg) == pytest.approx(expected)


# -- usage accounting ------------------------------------------------------------------------------------------------
PRICE_PER_MTOK = {"input": 1.0, "output": 2.0}


def test_reported_reasoning_is_recorded_and_stays_included_in_output():
    u = usage_from_messages([response(100, 500, 300)], "openai-compatible", "m", PRICE_PER_MTOK)
    assert u["output_tokens"] == 500 and u["input_tokens"] == 100
    assert u["reasoning_tokens"] == 300 and u["reasoning_accounting"] == "included-in-output"
    assert u["per_request"][0]["reasoning_tokens"] == 300
    assert "reasoning_added_to_output" not in u["per_request"][0]
    assert u["usd"] == pytest.approx((100 * 1.0 + 500 * 2.0) / 1e6)


def test_reasoning_equal_to_output_is_still_included_not_added():
    u = usage_from_messages([response(10, 400, 400)], "openai-compatible", "m")
    assert u["output_tokens"] == 400 and u["reasoning_accounting"] == "included-in-output"


def test_unreported_reasoning_is_none_and_not_reported():
    u = usage_from_messages([response(100, 500), response(100, 50)], "openai-compatible", "m", PRICE_PER_MTOK)
    assert u["reasoning_tokens"] is None and u["reasoning_accounting"] == "not-reported"
    assert all("reasoning_tokens" not in r for r in u["per_request"]) and u["output_tokens"] == 550


def test_reasoning_larger_than_output_proves_separate_counting_and_is_added_to_output_and_usd():
    u = usage_from_messages([response(100, 200, 900)], "openai-compatible", "m", PRICE_PER_MTOK)
    assert u["output_tokens"] == 200 + 900 and u["reasoning_tokens"] == 900
    assert u["reasoning_accounting"] == "added-to-output"
    assert u["per_request"][0]["reasoning_added_to_output"] is True
    assert u["per_request"][0]["output_tokens"] == 200  # the per-request entry keeps what the provider reported
    assert u["usd"] == pytest.approx((100 * 1.0 + 1100 * 2.0) / 1e6)


def test_separate_billing_config_adds_reasoning_even_when_it_is_smaller_than_output():
    u = usage_from_messages([response(100, 500, 300)], "openai-compatible", "m", PRICE_PER_MTOK,
                            reasoning_billing="separate")
    assert u["output_tokens"] == 800 and u["reasoning_accounting"] == "added-to-output"
    assert u["usd"] == pytest.approx((100 * 1.0 + 800 * 2.0) / 1e6)


def test_reasoning_is_totalled_across_requests_and_added_per_request():
    msgs = [response(10, 100, 40), response(10, 50, 200), response(10, 30)]  # included, added, not reported
    u = usage_from_messages(msgs, "openai-compatible", "m")
    assert u["reasoning_tokens"] == 240 and u["reasoning_accounting"] == "added-to-output"
    assert u["output_tokens"] == 100 + (50 + 200) + 30 and u["calls"] == 3
    assert [r.get("reasoning_added_to_output") for r in u["per_request"]] == [None, True, None]


def test_a_non_integer_reasoning_value_is_ignored():
    odd = SimpleNamespace(usage=SimpleNamespace(details={"reasoning_tokens": True}))
    assert par._reasoning_tokens(odd) is None
    assert par._reasoning_tokens(SimpleNamespace(usage=SimpleNamespace(details=None))) is None
    assert par._reasoning_tokens(SimpleNamespace(usage=SimpleNamespace(details={"reasoning_tokens": 5}))) == 5


def _decide_once(mode: str, u: RequestUsage, **cfg) -> DecisionResult:
    def fn(messages, info: AgentInfo):
        if mode == "tool":
            parts = [ToolCallPart(info.output_tools[0].name, {"rationale": "r", "calls": []})]
        else:
            parts = [TextPart(json.dumps({"rationale": "r", "calls": []}))]
        return ModelResponse(parts=parts, usage=u)
    rt = PydanticAIRuntime({"_model_object": FunctionModel(fn), "model": "openai-compatible:fake",
                            "output_mode": mode, "price_per_mtok": PRICE_PER_MTOK, **cfg})
    return rt.decide(request())


@pytest.mark.parametrize("mode", ["tool", "repair", "json"])
def test_reasoning_accounting_propagates_through_decide_in_every_output_mode(mode):
    included = _decide_once(mode, usage(100, 500, 300)).usage
    assert included["reasoning_tokens"] == 300 and included["reasoning_accounting"] == "included-in-output"
    assert included["output_tokens"] == 500

    added = _decide_once(mode, usage(100, 200, 900)).usage
    assert added["reasoning_tokens"] == 900 and added["reasoning_accounting"] == "added-to-output"
    assert added["output_tokens"] == 1100 and added["usd"] == pytest.approx((100 + 1100 * 2.0) / 1e6)

    separate = _decide_once(mode, usage(100, 500, 300), reasoning_tokens_billing="separate").usage
    assert separate["output_tokens"] == 800 and separate["reasoning_accounting"] == "added-to-output"

    absent = _decide_once(mode, usage(100, 500)).usage
    assert absent["reasoning_tokens"] is None and absent["reasoning_accounting"] == "not-reported"


def test_bounded_decisions_still_report_every_requests_reasoning():
    script = scripted([read_request(("get_job", {"job_id": "j"}))], usages=[usage(10, 20, 7)])
    log: list = []
    res = runtime(script, max_requests_per_decision=3).decide(request(read_tools(log)))
    assert res.proposal["control"]["bounded_outcome"] == "request-budget-exhausted" and len(log) == 2
    assert res.usage["reasoning_tokens"] == 21 and res.usage["calls"] == 3


@pytest.mark.parametrize("reasoning, reasked", [(50, True), (19_000, False)])
def test_reasoning_added_to_output_counts_against_the_decision_token_budget(reasoning, reasked):
    # 10 in + 100 out leaves room for the re-ask in a 20000-token envelope. 19000 reasoning tokens that the provider
    # counts separately are charged output, so the re-ask's worst case no longer fits and it is not started.
    script = scripted(["not json", json.dumps(GOOD)], usages=[usage(10, 100, reasoning)])
    res = runtime(script, max_requests_per_decision=3, max_tokens_per_decision=20_000,
                  max_output_tokens=200).decide(request())
    assert len(script.seen) == (2 if reasked else 1)
    if reasked:
        assert res.usage["structured_output"]["valid"]
    else:
        assert res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
        assert res.usage["output_tokens"] == 100 + reasoning and res.usage["calls"] == 1
        assert "token envelope" in res.usage["limit_reached"] and res.usage["structured_output"]["reasks"] == 0


# -- openai_usage and the OpenAI-compatible provider -------------------------------------------------------------------
def test_openai_usage_reasoning_included_in_completion_tokens():
    u = {"prompt_tokens": 10, "completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 60}}
    assert llm_broker.openai_usage(u) == (10, 100, 60)
    assert llm_broker.openai_usage({**u, "completion_tokens_details": {"reasoning_tokens": 100}}) == (10, 100, 100)


@pytest.mark.parametrize("u", [{"prompt_tokens": 10, "completion_tokens": 100},
                               {"prompt_tokens": 10, "completion_tokens": 100, "completion_tokens_details": None},
                               {"prompt_tokens": 10, "completion_tokens": 100, "completion_tokens_details": {}},
                               {"prompt_tokens": 10, "completion_tokens": 100,
                                "completion_tokens_details": {"reasoning_tokens": True}}])
def test_openai_usage_without_reported_reasoning(u):
    assert llm_broker.openai_usage(u) == (10, 100, None)


def test_openai_usage_reasoning_larger_than_completion_is_added_and_still_reported():
    u = {"prompt_tokens": 10, "completion_tokens": 100, "completion_tokens_details": {"reasoning_tokens": 300}}
    assert llm_broker.openai_usage(u) == (10, 400, 300)


def test_openai_usage_tolerates_missing_counts():
    assert llm_broker.openai_usage({}) == (0, 0, None)
    assert llm_broker.openai_usage({"prompt_tokens": None, "completion_tokens": None}) == (0, 0, None)


@pytest.mark.parametrize("details, expected", [({"reasoning_tokens": 60}, (100, 60)), ({}, (100, None)),
                                               ({"reasoning_tokens": 300}, (400, 300))])
def test_openai_compatible_provider_returns_reasoning_tokens(monkeypatch, details, expected):
    import httpx
    body = {"id": "chatcmpl-1", "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 100, "completion_tokens_details": details}}
    sent = {}

    def fake_post(url, **kw):
        sent.update(url=url, **kw)
        return SimpleNamespace(status_code=200, json=lambda: body)
    monkeypatch.setattr(httpx, "post", fake_post)
    res = llm_broker.OpenAICompatibleProvider("http://127.0.0.1:1/v1").complete("m", [{"role": "user", "content": "x"}], 64)
    assert (res.output_tokens, res.reasoning_tokens) == expected
    assert res.text == "hello" and res.input_tokens == 10 and res.request_id == "chatcmpl-1"
    assert sent["url"] == "http://127.0.0.1:1/v1/chat/completions"


# -- <think> blocks --------------------------------------------------------------------------------------------------
def test_extract_json_strips_a_leading_think_block_with_braces_inside_it():
    text = '<think>\nMaybe {"rationale": "draft"} first, then decide.\n</think>\n' + json.dumps(GOOD)
    obj, repairs = so.extract_json(text)
    assert obj["rationale"] == "baseline first" and repairs == ["think-block"]


def test_extract_json_think_block_combines_with_a_code_fence():
    obj, repairs = so.extract_json("<think>x</think>\n```json\n" + json.dumps(GOOD) + "\n```")
    assert obj["rationale"] == "baseline first" and repairs == ["think-block", "code-fence"]


def test_extract_json_think_only_reply_is_empty():
    with pytest.raises(so.StructuredOutputError, match="empty"):
        so.extract_json("<think>I will answer shortly</think>")


def test_a_think_block_before_a_decision_is_reported_as_a_repair_by_the_runtime():
    script = scripted(["<think>reasoning {with braces}</think>" + json.dumps(GOOD)])
    res = runtime(script).decide(request())
    assert len(script.seen) == 1 and res.usage["structured_output"]["valid"]
    assert "think-block" in res.usage["structured_output"]["repairs"]
    assert validate_decision(res.proposal, max_calls=4)["calls"][0]["tool"] == "run_component"


# -- the read loop ---------------------------------------------------------------------------------------------------
def test_read_request_results_are_sent_back_and_the_next_reply_is_the_decision():
    log: list = []
    script = scripted([read_request(("inspect_evidence", {"artifact_id": "art_vp0"}), ("get_job", {"job_id": "job_0"})),
                       json.dumps(GOOD)])
    res = runtime(script, "json").decide(request(read_tools(log)))
    assert len(script.seen) == 2 and res.usage["calls"] == 2
    assert log == [("inspect_evidence", "art_vp0", 8192), ("get_job", "job_0")]
    prompt = prompt_of(script.seen[1])
    assert prompt.startswith("## Read results\n") and "7 model requests left" in prompt
    results = results_of(script.seen[1])
    assert results[0] == {"tool": "inspect_evidence", "args": {"artifact_id": "art_vp0", "max_bytes": 8192},
                          "result": {"preview": "ids,predictions\n0,3", "truncated": True}}
    assert results[1]["tool"] == "get_job" and results[1]["result"] == {"id": "job_0", "status": "succeeded"}
    rep = res.usage["structured_output"]
    assert rep["valid"] and rep["read_requests"] == 1 and rep["reasks"] == 0 and rep["errors"] == []
    assert [(r["request"], r["tool"], r["executed"], r["available"]) for r in rep["reads"]] == [
        (1, "inspect_evidence", True, True), (1, "get_job", True, True)]
    assert rep["reads"][0]["args"] == {"artifact_id": "art_vp0", "max_bytes": 8192}
    assert validate_decision(res.proposal, max_calls=4)["calls"][0]["args"]["component_ref"] == "a@1.0.0"
    assert res.model_settings["structured_output"]["read_requests"] == 1


def test_native_tool_calls_are_not_offered_in_the_text_modes():
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)])
    runtime(script, "json").decide(request(read_tools([])))
    assert len(script.infos) == 2 and all(not i.function_tools for i in script.infos)


def test_the_read_request_shape_is_offered_only_with_read_tools():
    with_tools = scripted([json.dumps(GOOD)])
    runtime(with_tools).decide(request(read_tools([])))
    without = scripted([json.dumps(GOOD)])
    runtime(without).decide(request())
    assert "Read tools (read-only" in prompt_of(with_tools.seen[0]) and "get_job" in prompt_of(with_tools.seen[0])
    assert "Read tools (read-only" not in prompt_of(without.seen[0])


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_a_model_that_always_asks_to_read_never_exceeds_the_request_budget(budget):
    log: list = []
    script = scripted([read_request(("get_job", {"job_id": "j"}))])
    res = runtime(script, max_requests_per_decision=budget).decide(request(read_tools(log)))
    assert len(script.seen) <= budget and len(script.seen) == budget and res.usage["calls"] == budget
    assert len(log) == budget - 1  # the read made in the last allowed request is not executed
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "request-budget-exhausted"
    assert res.usage["limit_reached"] and not res.usage["structured_output"]["valid"]
    assert validate_decision(res.proposal, max_calls=4)["control"]["action"] == "wait_for_jobs"
    last = res.usage["structured_output"]["reads"][-1]
    assert last["executed"] is False and last["request"] == budget and last["tool"] == "get_job"
    assert res.usage["structured_output"]["read_requests"] == budget


def test_a_decision_in_the_last_request_after_a_read_is_accepted():
    log: list = []
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)])
    res = runtime(script, max_requests_per_decision=2).decide(request(read_tools(log)))
    assert len(script.seen) == 2 and len(log) == 1 and "last request" in prompt_of(script.seen[1])
    assert "bounded_outcome" not in (res.proposal.get("control") or {}) and res.usage["structured_output"]["valid"]


def test_a_read_tool_named_in_calls_is_refused_with_one_reask_then_accepted():
    misuse = {**GOOD, "calls": [{"tool": "inspect_evidence", "args": {"artifact_id": "art_vp0"}}]}
    script = scripted([json.dumps(misuse), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools([])))
    assert len(script.seen) == 2
    reask = prompt_of(script.seen[1])
    assert "calls.0.tool 'inspect_evidence'" in reask and "never belong in" in reask and "read request" in reask
    rep = res.usage["structured_output"]
    assert rep["reasks"] == 1 and rep["valid"] and len(rep["errors"]) == 1
    assert [c["tool"] for c in res.proposal["calls"]] == ["run_component"]


@pytest.mark.parametrize("tool", READ_NAMES)
def test_every_read_tool_is_refused_in_calls(tool):
    misuse = {**GOOD, "calls": [{"tool": tool, "args": {}}]}
    script = scripted([json.dumps(misuse), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools([])))
    assert len(script.seen) == 2 and f"'{tool}'" in prompt_of(script.seen[1])
    assert res.proposal["calls"][0]["tool"] == "run_component"


def test_a_read_tool_in_calls_twice_ends_in_a_bounded_invalid_decision_without_effects():
    misuse = {**GOOD, "calls": [{"tool": "get_job", "args": {"job_id": "j"}}]}
    script = scripted([json.dumps(misuse)])
    res = runtime(script, fresh_state_repairs=0).decide(request(read_tools([])))
    assert len(script.seen) == 2  # never more than one re-ask (no fresh-state repair here)
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
    rep = res.usage["structured_output"]
    assert len(rep["errors"]) == 2 and not rep["valid"] and rep["reasks"] == 1
    assert validate_decision(res.proposal, max_calls=4)["calls"] == []


def test_a_read_tool_in_calls_is_refused_even_without_read_tools_and_the_reask_says_so():
    misuse = {**GOOD, "calls": [{"tool": "inspect_evidence", "args": {"artifact_id": "art_vp0"}}]}
    script = scripted([json.dumps(misuse), json.dumps(GOOD)])
    res = runtime(script).decide(request())
    assert "no read tools are available" in prompt_of(script.seen[1]) and res.usage["structured_output"]["valid"]


def test_a_read_request_without_read_tools_is_malformed_and_reasked_once():
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)])
    res = runtime(script).decide(request())
    assert len(script.seen) == 2 and "no read tools are available" in prompt_of(script.seen[1])
    rep = res.usage["structured_output"]
    assert rep["reasks"] == 1 and rep["valid"] and rep["read_requests"] == 0 and rep["reads"] == []


def test_a_read_request_without_read_tools_twice_is_bounded():
    script = scripted([read_request(("get_job", {"job_id": "j"}))])
    res = runtime(script, fresh_state_repairs=0).decide(request())
    assert len(script.seen) == 2 and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"


@pytest.mark.parametrize("extra", [{"calls": GOOD["calls"]}, {"control": {"action": "stop", "summary": "s"}}])
def test_a_reply_with_both_read_and_a_decision_is_malformed(extra):
    both = {"rationale": "r", "read": [{"tool": "get_job", "args": {"job_id": "j"}}], **extra}
    log: list = []
    script = scripted([json.dumps(both), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools(log)))
    assert log == [] and len(script.seen) == 2  # nothing is executed for a malformed reply
    assert "either a read request or a decision" in prompt_of(script.seen[1])
    rep = res.usage["structured_output"]
    assert rep["reasks"] == 1 and rep["read_requests"] == 0 and rep["valid"]


def test_read_that_is_not_a_list_is_malformed():
    script = scripted([json.dumps({"rationale": "r", "read": "get_job"}), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools([])))
    assert '"read" must be a list' in prompt_of(script.seen[1]) and res.usage["structured_output"]["reasks"] == 1


def test_an_empty_read_list_is_a_decision_with_a_named_repair():
    script = scripted([json.dumps({**GOOD, "read": []})])
    res = runtime(script).decide(request(read_tools([])))
    assert len(script.seen) == 1 and "empty-read" in res.usage["structured_output"]["repairs"]
    assert res.usage["structured_output"]["reasks"] == 0 and "read" not in res.proposal


@pytest.mark.parametrize("entry, message", [
    ({"tool": "nope", "args": {}}, "'nope' is not an available read tool"),
    ({"tool": "get_job", "args": {}}, "missing required argument 'job_id'"),
    ({"tool": "get_job", "args": {"job_id": "j", "extra": 1}}, "unknown argument(s) extra"),
    ({"tool": "get_job", "args": {"job_id": 5}}, "wrong type"),
    ({"tool": "inspect_evidence", "args": {"artifact_id": "a", "max_bytes": True}}, "wrong type"),
    ({"tool": "inspect_evidence", "args": {"artifact_id": "a", "max_bytes": 1.5}}, "wrong type"),
    ({"tool": "search_components", "args": {"text": "x", "capability": 3}}, "wrong type"),
    ({"tool": "get_job", "args": [1]}, "args must be a JSON object"),
    ({"tool": "get_job", "args": "{oops"}, "args is not a JSON object"),
    ("get_job", "each read must be"),
    ({"args": {"job_id": "j"}}, "each read must be"),
])
def test_a_problem_with_one_read_is_reported_back_to_the_model_and_not_executed(entry, message):
    log: list = []
    script = scripted([json.dumps({"rationale": "r", "read": [entry]}), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools(log)))
    assert log == [] and len(script.seen) == 2
    (result,) = results_of(script.seen[1])
    assert message in result["result"]["error"] and result["args"] is None
    rep = res.usage["structured_output"]
    assert rep["valid"] and rep["reasks"] == 0 and rep["errors"] == []
    assert rep["reads"][0]["executed"] is False and "available" not in rep["reads"][0]


def test_a_read_tool_that_is_not_available_for_this_decision_is_reported_back():
    log: list = []
    tools = {"get_job": read_tools(log)["get_job"]}
    script = scripted([read_request(("inspect_evidence", {"artifact_id": "a"}), ("get_job", {"job_id": "j"})),
                       json.dumps(GOOD)])
    runtime(script).decide(request(tools))
    first, second = results_of(script.seen[1])
    assert "not an available read tool (available: get_job)" in first["result"]["error"]
    assert second["result"]["status"] == "succeeded" and log == [("get_job", "j")]


def test_json_string_args_of_a_read_are_accepted():
    log: list = []
    reply = json.dumps({"rationale": "r", "read": [{"tool": "get_job", "args": json.dumps({"job_id": "j9"})}]})
    script = scripted([reply, json.dumps(GOOD)])
    runtime(script).decide(request(read_tools(log)))
    assert log == [("get_job", "j9")]


def test_more_than_four_reads_in_one_request_execute_only_the_first_four():
    log: list = []
    script = scripted([read_request(*[("get_job", {"job_id": f"j{i}"}) for i in range(6)]), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools(log)))
    assert log == [("get_job", f"j{i}") for i in range(4)]
    results = results_of(script.seen[1])
    assert len(results) == 6 and all("result" in r and "error" not in r["result"] for r in results[:4])
    assert all("at most 4 reads per request" in r["result"]["error"] for r in results[4:])
    reads = res.usage["structured_output"]["reads"]
    assert [r["executed"] for r in reads] == [True] * 4 + [False] * 2 and res.usage["structured_output"]["valid"]


@pytest.mark.parametrize("given, passed", [(1_000_000, 16384), (16385, 16384), (16384, 16384), (100, 100), (0, 1),
                                           (-5, 1), (None, 8192)])
def test_inspect_evidence_max_bytes_is_clamped_to_16384(given, passed):
    log: list = []
    args = {"artifact_id": "art_vp0", **({} if given is None else {"max_bytes": given})}
    script = scripted([read_request(("inspect_evidence", args)), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools(log)))
    assert log == [("inspect_evidence", "art_vp0", passed)]
    assert results_of(script.seen[1])[0]["args"]["max_bytes"] == passed
    assert res.usage["structured_output"]["reads"][0]["args"]["max_bytes"] == passed


def test_search_components_arguments_are_passed_in_signature_order_with_defaults():
    log: list = []
    script = scripted([read_request(("search_components", {"text": "pairs"}),
                                    ("search_components", {"capability": "score", "text": "x"})), json.dumps(GOOD)])
    runtime(script).decide(request(read_tools(log)))
    assert log == [("search_components", "pairs", None), ("search_components", "x", "score")]


def test_a_read_tool_that_raises_yields_an_unavailable_result_and_the_decision_completes():
    def boom(artifact_id, max_bytes=8192):
        raise RuntimeError("storage offline")
    script = scripted([read_request(("inspect_evidence", {"artifact_id": "art_vp0"})), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools([], inspect_evidence=boom)))
    (result,) = results_of(script.seen[1])
    assert result["result"]["available"] is False and "storage offline" in result["result"]["error"]
    assert "decide with what you have" in result["result"]["note"]
    read = res.usage["structured_output"]["reads"][0]
    assert read["executed"] is True and read["available"] is False and res.usage["structured_output"]["valid"]
    assert res.proposal["calls"][0]["tool"] == "run_component"


def test_a_read_tool_reporting_unavailable_is_told_not_to_requery_and_large_results_are_truncated():
    big = {"rows": "x" * 20_000}
    tools = read_tools([], get_job=lambda job_id: {"available": False}, describe_component=lambda component_ref: big)
    script = scripted([read_request(("get_job", {"job_id": "j"}), ("describe_component", {"component_ref": "a@1"})),
                       json.dumps(GOOD)])
    res = runtime(script).decide(request(tools))
    first, second = results_of(script.seen[1])
    assert first["result"]["available"] is False and "do not re-query" in first["result"]["note"]
    assert second["result"]["truncated"] is True and len(second["result"]["preview"]) == 8000
    assert [r["available"] for r in res.usage["structured_output"]["reads"]] == [False, True]


# -- JSON mode response_format ---------------------------------------------------------------------------------------
def _schema_of(info: AgentInfo) -> dict:
    rf = info.model_settings["extra_body"]["response_format"]
    assert rf["type"] == "json_schema"
    return rf["json_schema"]["schema"]


def test_json_mode_schema_gets_a_read_property_with_a_tool_enum_only_with_read_tools():
    with_tools = scripted([json.dumps(GOOD)])
    runtime(with_tools, "json").decide(request(read_tools([])))
    read = _schema_of(with_tools.infos[0])["properties"]["read"]
    assert read["type"] == "array" and read["maxItems"] == so.MAX_READS_PER_REQUEST
    assert read["items"]["properties"]["tool"]["enum"] == READ_NAMES
    assert read["items"]["required"] == ["tool", "args"]

    without = scripted([json.dumps(GOOD)])
    runtime(without, "json").decide(request())
    assert "read" not in _schema_of(without.infos[0])["properties"]
    assert _schema_of(without.infos[0])["required"] == ["rationale"]


def test_json_mode_enum_lists_only_the_available_read_tools_and_does_not_mutate_the_shared_schema():
    log: list = []
    tools = {k: v for k, v in read_tools(log).items() if k in ("get_job", "describe_component")}
    rt = PydanticAIRuntime({"model": "openai-compatible:fake", "output_mode": "json"})
    schema = rt._json_mode_settings(["describe_component", "get_job"])["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["read"]["items"]["properties"]["tool"]["enum"] == ["describe_component", "get_job"]
    assert "read" not in par.LENIENT_DECISION_SCHEMA["properties"]
    script = scripted([json.dumps(GOOD)])
    runtime(script, "json").decide(request(tools))
    assert _schema_of(script.infos[0])["properties"]["read"]["items"]["properties"]["tool"]["enum"] == [
        "describe_component", "get_job"]


def test_repair_mode_and_anthropic_json_mode_send_no_response_format():
    script = scripted([json.dumps(GOOD)])
    runtime(script, "repair", max_output_tokens=777).decide(request(read_tools([])))
    assert script.infos[0].model_settings == {"max_tokens": 777}
    anthropic = PydanticAIRuntime({"model": "anthropic:claude-sonnet-5-5", "output_mode": "json"})
    assert anthropic._json_mode_settings(["get_job"]) is None


# -- a recorded reasoning-model reply --------------------------------------------------------------------------------
# The shape MiniMax-M3.1-Flash returned in JSON mode in the 1.1.0 gate (G4 1.1.1): the read tool is put into the
# decision's effect calls instead of being called.
RECORDED_MINIMAX = json.dumps({
    "rationale": "Inspect the validation predictions of the first baseline before choosing the next experiment.",
    "purpose": "hypothesis-test",
    "hypotheses": [{"statement": "The baseline errors concentrate on a few classes",
                    "prediction": "per-class accuracy varies by more than 10 points",
                    "falsifier": "all per-class accuracies lie within 5 points of the mean"}],
    "calls": [{"tool": "inspect_evidence", "args": {"artifact_id": "art_vp0"}}],
    "control": {"action": "wait_for_jobs", "summary": "waiting for the evidence preview"}})


@pytest.mark.parametrize("reply", [RECORDED_MINIMAX, "<think>\nI should look at art_vp0 {first}.\n</think>\n"
                                   + RECORDED_MINIMAX], ids=["plain", "with-think-block"])
def test_recorded_minimax_reply_with_a_read_tool_in_calls_is_refused_and_reasked(reply):
    log: list = []
    corrected = read_request(("inspect_evidence", {"artifact_id": "art_vp0"}), rationale="read first")
    script = scripted([reply, corrected, json.dumps(GOOD)])
    res = runtime(script, "json").decide(request(read_tools(log)))
    reask = prompt_of(script.seen[1])
    assert "calls.0.tool 'inspect_evidence'" in reask and "read request" in reask
    assert log == [("inspect_evidence", "art_vp0", 8192)]  # the corrected reply is executed as a read, once
    rep = res.usage["structured_output"]
    assert len(script.seen) == 3 and rep["reasks"] == 1 and rep["read_requests"] == 1 and rep["valid"]
    assert res.usage["calls"] == 3 and [c["tool"] for c in res.proposal["calls"]] == ["run_component"]


def test_recorded_minimax_reply_is_never_accepted_as_an_effect():
    script = scripted([RECORDED_MINIMAX])
    res = runtime(script, "json").decide(request(read_tools([])))
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "structured-output-invalid"
    validate_decision(res.proposal, max_calls=4)
    # the lab's own validator is the second line: the original reply, normalised exactly as the runtime does, is
    # refused there too, independently of the runtime's own refusal (``read_tools_in_calls``)
    obj, _ = so.normalize(so.extract_json(RECORDED_MINIMAX)[0])
    original = par.to_proposal(par.Decision.model_validate(obj))
    assert [c["tool"] for c in original["calls"]] == ["inspect_evidence"]
    with pytest.raises(Invalid, match=r"call 0: unknown or non-effect tool 'inspect_evidence'"):
        validate_decision(original, max_calls=4)
    assert so.read_tools_in_calls(original["calls"], READ_NAMES) is not None


# A read request as MiniMax-M3.1-Flash wrote it in JSON mode after the read shape was offered (G4 1.1.1): four reads in
# one reply, three of them with the largest allowed preview.
RECORDED_MINIMAX_READ = ('{"rationale":"I need to characterize the digit data, labels and baseline configuration before '
                         'selecting an improvement experiment.","read":[{"tool":"inspect_evidence","args":{"artifact_id":'
                         '"art_task","max_bytes":16384}},{"tool":"inspect_evidence","args":{"artifact_id":"art_train",'
                         '"max_bytes":16384}},{"tool":"inspect_evidence","args":{"artifact_id":"art_validation",'
                         '"max_bytes":16384}},{"tool":"get_job","args":{"job_id":"job_0"}}]}')


def test_recorded_minimax_read_request_is_executed_in_full_and_answered_with_a_decision():
    log: list = []
    script = scripted([RECORDED_MINIMAX_READ, json.dumps(GOOD)])
    res = runtime(script, "json", max_requests_per_decision=6).decide(request(read_tools(log)))
    assert log == [("inspect_evidence", "art_task", 16384), ("inspect_evidence", "art_train", 16384),
                   ("inspect_evidence", "art_validation", 16384), ("get_job", "job_0")]
    assert "5 model requests left" in prompt_of(script.seen[1])
    rep = res.usage["structured_output"]
    assert rep["valid"] and rep["read_requests"] == 1 and rep["reasks"] == 0 and len(rep["reads"]) == 4


# -- the decision deadline -------------------------------------------------------------------------------------------
FIRST_REPLIES = {"read-request": read_request(("get_job", {"job_id": "j"})), "malformed": "not json"}


@pytest.mark.parametrize("first", list(FIRST_REPLIES))
def test_no_follow_up_request_is_started_when_it_could_outlive_the_decision_deadline(first):
    log: list = []
    script = scripted([FIRST_REPLIES[first], json.dumps(GOOD)])
    res = runtime(script, request_timeout=1000).decide(request(read_tools(log), deadline_seconds=600))
    assert len(script.seen) == 1 and res.usage["calls"] == 1  # the first request is always made
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "decision-deadline"
    assert "600" in res.usage["limit_reached"] and not res.usage["structured_output"]["valid"]
    assert res.usage["structured_output"]["reasks"] == 0  # a re-ask that was never sent is not counted
    validate_decision(res.proposal, max_calls=4)


@pytest.mark.parametrize("first", list(FIRST_REPLIES))
def test_the_follow_up_request_happens_when_it_fits_inside_the_deadline(first):
    script = scripted([FIRST_REPLIES[first], json.dumps(GOOD)])
    res = runtime(script, request_timeout=10).decide(request(read_tools([]), deadline_seconds=600))
    assert len(script.seen) == 2 and res.usage["calls"] == 2 and res.usage["structured_output"]["valid"]
    assert "bounded_outcome" not in (res.proposal.get("control") or {})


@pytest.mark.parametrize("limits", [{}, {"deadline_seconds": 0}, {"deadline_seconds": None}])
def test_without_a_deadline_the_request_timeout_does_not_limit_follow_ups(limits):
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)])
    res = runtime(script, request_timeout=100_000).decide(request(read_tools([]), **limits))
    assert len(script.seen) == 2 and res.usage["structured_output"]["valid"]


@pytest.mark.parametrize("elapsed, follow_up", [(589, True), (590, True), (591, False), (900, False)])
def test_the_deadline_check_uses_elapsed_time_plus_the_request_timeout(monkeypatch, elapsed, follow_up):
    ticks = iter([0.0, float(elapsed)])
    monkeypatch.setattr(par, "time", SimpleNamespace(monotonic=lambda: next(ticks, float(elapsed))))
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)])
    res = runtime(script, request_timeout=10).decide(request(read_tools([]), deadline_seconds=600))
    assert len(script.seen) == (2 if follow_up else 1)
    assert (res.proposal["control"].get("bounded_outcome") == "decision-deadline") is (not follow_up)


def test_a_deadline_stop_still_counts_the_reasoning_of_the_requests_already_made():
    script = scripted([read_request(("get_job", {"job_id": "j"}))], usages=[usage(10, 20, 600)])
    res = runtime(script, request_timeout=1000).decide(request(read_tools([]), deadline_seconds=600))
    assert res.usage["calls"] == 1 and res.usage["reasoning_tokens"] == 600
    assert res.usage["reasoning_accounting"] == "added-to-output" and res.usage["output_tokens"] == 620


# -- request admission inside the decision's token envelope (R1 finding 1) -------------------------------------------
def _tool_read_then_decide(usages):
    """Tool mode: the first response calls the read tool ``get_job``; later ones return the decision."""
    seen: list = []

    def fn(messages, info: AgentInfo):
        seen.append(messages)
        u = usages[min(len(seen) - 1, len(usages) - 1)]
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("get_job", {"job_id": "j"})], usage=u)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"rationale": "r", "calls": []})], usage=u)
    return FunctionModel(fn), seen


@pytest.mark.parametrize("mode", ["repair", "json"])
def test_a_request_whose_worst_case_does_not_fit_the_remaining_envelope_is_not_started(mode):
    # The reviewer's case: 100 tokens left, a 16384-token output cap. The read request is answered, but the follow-up
    # request is not started: its input (>= the 119900 tokens already in the conversation) + 16384 cannot fit.
    log: list = []
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)], usages=[usage(19_900, 100_000)])
    res = runtime(script, mode, max_output_tokens=16384, max_tokens_per_decision=120_000).decide(request(read_tools(log)))
    assert len(script.seen) == 1 and res.usage["calls"] == 1
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "request-budget-exhausted"
    assert "token envelope" in res.usage["limit_reached"] and "16384 output" in res.usage["limit_reached"]
    assert res.usage["input_tokens"] + res.usage["output_tokens"] <= 120_000
    validate_decision(res.proposal, max_calls=4)


def test_tool_mode_does_not_start_a_request_whose_worst_case_does_not_fit():
    model, seen = _tool_read_then_decide([usage(19_900, 100_000), usage(10, 10)])
    rt = PydanticAIRuntime({"_model_object": model, "model": "openai-compatible:fake", "output_mode": "tool",
                            "max_output_tokens": 16384, "max_tokens_per_decision": 120_000})
    res = rt.decide(request(read_tools([])))
    assert len(seen) == 1 and res.usage["calls"] == 1  # neither the read follow-up nor a tool-free final request
    assert res.proposal["calls"] == [] and res.proposal["control"]["bounded_outcome"] == "request-budget-exhausted"
    assert "token envelope" in res.usage["limit_reached"]
    assert res.usage["input_tokens"] + res.usage["output_tokens"] <= 120_000


def test_tool_mode_follow_up_happens_when_its_worst_case_fits():
    model, seen = _tool_read_then_decide([usage(1000, 500), usage(1600, 100)])
    rt = PydanticAIRuntime({"_model_object": model, "model": "openai-compatible:fake", "output_mode": "tool",
                            "max_output_tokens": 16384, "max_tokens_per_decision": 120_000})
    res = rt.decide(request(read_tools([])))
    assert len(seen) == 2 and "bounded_outcome" not in (res.proposal.get("control") or {})
    first, second = rt.envelope.admitted
    assert first["spent_before"] == 0 and second["spent_before"] == 1500
    assert second["input_estimate"] >= 1000 + 500  # the last request's input and output are re-sent


def test_the_first_request_is_not_started_when_its_bytes_and_the_cap_exceed_the_envelope():
    script = scripted([json.dumps(GOOD)])
    res = runtime(script, max_output_tokens=4096, max_tokens_per_decision=5000).decide(request())
    assert script.seen == [] and res.usage["calls"] == 0
    assert res.proposal["control"]["bounded_outcome"] == "request-budget-exhausted"
    assert "4096 output" in res.usage["limit_reached"]


def test_the_envelope_counts_separately_billed_reasoning_as_spent():
    # 10 + 10000 tokens (9000 of them reasoning, inside the output) leave room for the re-ask in 30000; with "separate"
    # billing the 9000 reasoning tokens are charged on top, and the re-ask's worst case no longer fits (the re-ask also
    # re-sends the system instructions, whose size follows the playbook revision)
    for billing, reasked in (("included", True), ("separate", False)):
        script = scripted(["not json", json.dumps(GOOD)], usages=[usage(10, 10_000, 9_000)])
        runtime(script, max_output_tokens=200, max_tokens_per_decision=30_000,
                reasoning_tokens_billing=billing).decide(request())
        assert len(script.seen) == (2 if reasked else 1), billing


def test_every_request_carries_the_output_cap_and_the_receipt_records_it():
    script = scripted([read_request(("get_job", {"job_id": "j"})), json.dumps(GOOD)])
    res = runtime(script).decide(request(read_tools([])))  # no max_output_tokens configured
    assert [i.model_settings["max_tokens"] for i in script.infos] == [4096, 4096]
    assert res.model_settings["max_output_tokens"] == 4096


def test_the_input_estimate_uses_the_reported_usage_after_a_response_and_bytes_before():
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart
    env = par.TokenEnvelope(limit=100_000, cap=1000)
    first = [ModelRequest(parts=[UserPromptPart("x" * 3000)], instructions="i" * 500)]
    assert env.estimate(first, None, None) >= 3500  # one token per byte of the whole request
    env.admit(first, None, None)
    env.record(ModelResponse(parts=[TextPart("y")], usage=usage(900, 300)))
    assert env.spent == 1200
    new = ModelRequest(parts=[UserPromptPart("z" * 100)])
    est = env.estimate([*first, ModelResponse(parts=[TextPart("y")]), new], None, None)
    assert est == 900 + 300 + len(ModelMessagesTypeAdapter.dump_json([new])) + par.PART_OVERHEAD_TOKENS
    # a provider that reports no input: back to the bytes of the whole conversation
    env.record(ModelResponse(parts=[TextPart("y")], usage=usage(0, 300)))
    conv = [*first, ModelResponse(parts=[TextPart("y")]), new]
    assert env.estimate(conv, None, None) >= len(ModelMessagesTypeAdapter.dump_json(conv))


def test_admission_is_spent_plus_estimate_plus_cap_within_the_limit():
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    msgs = [ModelRequest(parts=[UserPromptPart("q")])]
    env = par.TokenEnvelope(limit=10_000, cap=1000)
    est = env.estimate(msgs, None, None)
    env.spent = 10_000 - 1000 - est
    env.admit(msgs, None, None)  # exactly fits
    env.spent += 1
    with pytest.raises(par.EnvelopeRefused, match="1000 output"):
        env.admit(msgs, None, None)


@pytest.mark.parametrize("cfg", [{}, {"max_output_tokens": 16384}, {"max_output_tokens": 1000,
                                                                     "max_requests_per_decision": 3}])
def test_the_activity_envelope_prices_the_cap_the_runtime_sends(cfg):
    from eidos.investigator.runtime import output_cap
    rt = PydanticAIRuntime({"model": "openai-compatible:fake", **cfg})
    assert rt._settings({}, 8)["max_output_tokens"] == output_cap(cfg)
    n = cfg.get("max_requests_per_decision", 8)
    out = min(120_000, n * output_cap(cfg))
    assert decision_usd_envelope(120_000, PRICE, cfg) == pytest.approx(((120_000 - out) * 3.0 + out * 15.0) / 1e6)
