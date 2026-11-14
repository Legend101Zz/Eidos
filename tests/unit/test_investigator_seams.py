"""Runtime extension and prompt-byte compatibility contracts."""
import hashlib
from unittest.mock import Mock

import pytest

from eidos.application.handlers.investigations import start_investigation
from eidos.domain.errors import Invalid
from eidos.investigator.context import _tool_guide
from eidos.investigator.prompt_packs import PROMPT_PACKS, select_prompt_pack
from eidos.investigator.pydantic_ai_runtime import PydanticAIRuntime
from eidos.investigator.runtime import RUNTIMES, DecisionRequest, build_runtime, register_runtime

HASHES = {
    "investigator-prompt/5": ("f9a632074d5ce01c86263926e1ac0137d6ca4bd7de45c7ca9ebce7234740ddab",
                            "75845be91c661c8810b9a4648214cf2a3319db50e0e8e1e0e72e738ff05ad253",
                            "c1371d937c7bd0bfe260c612e5c0c316fb9cebca77e495769a242fcc3d0b4df8"),
    "investigator-prompt/6": ("dacf72c9f49065c3cdb5430a58a12b9e004e4d547bb611f6e148041e3992cb9d",
                            "fcb4412045ef09493a82662631370735349424c43cc81b73e497e5e26368bfb3",
                            "4c0f0d36b80adc57f9f174589ff904432a78c1b5b21a55a9e0c6ea8914626ad7"),
}


@pytest.mark.parametrize("revision", HASHES)
def test_pre_refactor_prompt_hashes(revision):
    pack = PROMPT_PACKS[revision]
    assert tuple(hashlib.sha256(text.encode()).hexdigest() for text in (pack.system, pack.playbook, pack.tool_guide)) == HASHES[revision]


def test_runtime_registry_extension_and_known_names(monkeypatch):
    monkeypatch.setattr("eidos.investigator.runtime.RUNTIMES", dict(RUNTIMES))
    runtime = Mock()
    register_runtime("extension")(lambda config: runtime)
    assert build_runtime(dict(runtime="extension")) is runtime
    assert build_runtime({}).policy_name == "conclude-immediately"
    with pytest.raises(ValueError, match="fixture.*pydantic-ai"):
        build_runtime(dict(runtime="unknown"))


@pytest.mark.parametrize("module", ["os", "eidos.investigator", "eidos.investigator_fake.policy", 1])
def test_policy_module_cannot_escape_investigator_namespace(module):
    with pytest.raises(ValueError, match="policy is code"):
        build_runtime(dict(policy_module=module))


def test_explicit_pack_selects_system_and_guide_and_unknown_refuses_before_model():
    settings = dict(prompt_pack="investigator-prompt/5")
    view = dict(investigation=dict(body=dict(investigator=dict(settings=settings, exploration=dict(policy="exploration-policy/3")))))
    assert select_prompt_pack(settings, view) == ("investigator-prompt/5", PROMPT_PACKS["investigator-prompt/5"])
    assert _tool_guide(view) == PROMPT_PACKS["investigator-prompt/5"].tool_guide
    req = DecisionRequest("t", 0, "", [], {}, settings=dict(prompt_pack="unknown"), view=view)
    with pytest.raises(ValueError, match="unknown prompt pack"):
        PydanticAIRuntime({"_model_object": Mock()}).decide(req)


@pytest.mark.parametrize("investigator", [
    dict(context_policy="unknown"),
    dict(exploration=dict(policy="unknown")),
    dict(settings=dict(prompt_pack="unknown")),
])
def test_start_refuses_unknown_revision_before_writing(investigator):
    ctx = Mock()
    ctx.tx.get_record.return_value = None
    with pytest.raises(Invalid, match="unknown investigator"):
        start_investigation(ctx, dict(question="q", investigator=investigator))
    ctx.tx.put_record.assert_not_called()
