"""Fixture-runtime receipts must yield their served id, so a model-free run is not classified as an incident."""

from study.eidos_arms.common import usage


def event(u):
    return {"type": "model.usage", "body": {"usage": u}}


def test_fixture_receipt_reports_its_served_id():
    receipt = {"usd": 0.0, "calls": 1, "model": "fixture:autopilot-v2", "fixture": True, "provider": "fixture",
               "input_tokens": 10, "output_tokens": 2}
    _, models, _, _ = usage([event(receipt)])
    assert models == ["fixture:autopilot-v2"]


def test_live_receipt_model_field_is_not_trusted_as_served():
    # Live receipts keep the existing rule: only served_models / per_request ids count.
    receipt = {"usd": 0.001, "model": "MiniMax-M3.1-Flash", "provider": "openai-compatible",
               "input_tokens": 10, "output_tokens": 2}
    _, models, _, _ = usage([event(receipt)])
    assert models == []
    receipt = dict(receipt, served_models=["MiniMax-M3.1-Flash"])
    _, models, _, _ = usage([event(receipt)])
    assert models == ["MiniMax-M3.1-Flash"]


def test_fixture_flag_alone_is_not_enough():
    receipt = {"model": "fixture:x", "fixture": True, "provider": "openai", "input_tokens": 1, "output_tokens": 1}
    _, models, _, _ = usage([event(receipt)])
    assert models == []
