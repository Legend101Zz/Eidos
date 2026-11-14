"""Public main 806f939 expectations, extracted with git show and AST constants."""
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from eidos.application.handlers import exploration
from eidos.application.handlers.investigations import tool_vocabulary
from eidos.domain.errors import Invalid
from eidos.investigator.context import TOOL_GUIDE, _known_tools, _tool_guide
from eidos.investigator.playbook import PLAYBOOK
from eidos.investigator.pydantic_ai_runtime import PROMPT_REVISION, SYSTEM

# Evaluated only these declarations from git show origin/main:<file>; sha256(value.encode()).
HASHES = {
    "PLAYBOOK": "75845be91c661c8810b9a4648214cf2a3319db50e0e8e1e0e72e738ff05ad253",
    "SYSTEM": "f9a632074d5ce01c86263926e1ac0137d6ca4bd7de45c7ca9ebce7234740ddab",
    "TOOL_GUIDE": "c1371d937c7bd0bfe260c612e5c0c316fb9cebca77e495769a242fcc3d0b4df8",
    "PROMPT_REVISION": "b6ad619c93e48b9222ba7b8ab2e3f4b7faaf76fd644651ae8aed8ad92eb0478d",
}
# sorted(main.EFFECT_TOOLS) + list(main.CONTROL_TOOLS), exactly as main's turn spec.
VOCABULARY = [
    "assess_candidate", "author_component", "author_source", "bind_component", "draft_evidence_plan",
    "export_model", "file_claim", "freeze_evidence_plan", "launch_branch", "launch_evidence_plan", "launch_trial",
    "plan_branch", "plan_trial", "publish_component", "record_note", "retire_component", "run_component",
    "wait_for_jobs", "request_input", "conclude", "stop",
]


def test_default_prompt_text_revision_guide_and_playbook_are_main():
    for name, value in dict(PLAYBOOK=PLAYBOOK, SYSTEM=SYSTEM, TOOL_GUIDE=TOOL_GUIDE, PROMPT_REVISION=PROMPT_REVISION).items():
        assert hashlib.sha256(value.encode()).hexdigest() == HASHES[name]
    assert _tool_guide({}) == TOOL_GUIDE


def test_policy_two_turn_spec_vocabulary_equals_main_and_policy_three_exposes_macro():
    assert tool_vocabulary({}) == VOCABULARY
    assert tool_vocabulary(dict(exploration=dict(policy="exploration-policy/2"))) == VOCABULARY
    assert _known_tools({}) == VOCABULARY
    assert "prepare_candidate" in tool_vocabulary(dict(exploration=dict(policy="exploration-policy/3")))


@pytest.mark.parametrize("policy", ["exploration-policy/2", "exploration-policy/3"])
@pytest.mark.parametrize("phase", ["baseline", "explore", "finalize"])
def test_nameless_lock_keeps_main_semantics_only_under_policy_two(policy, phase):
    # Main check_note returned None for a legacy nameless note, except in finalize
    # where the existing candidate identity is frozen (git show main:.../exploration.py).
    inv = SimpleNamespace(body=dict(investigator=dict(exploration=dict(policy=policy))))
    tx = SimpleNamespace(require=lambda *a, **kw: inv, get_record=lambda *a: None, budgets=lambda *a: {})
    ctx = SimpleNamespace(tx=tx)
    with patch.object(exploration, "_notes", return_value=[]), patch.object(
            exploration, "exploration_state", return_value=dict(phase=phase)):
        if phase == "finalize" or policy == "exploration-policy/3":
            with pytest.raises(Invalid, match="candidate identity" if phase == "finalize" else "names no job"):
                exploration.check_note(ctx, "inv", dict(kind="candidate_lock", text="looks good"))
        else:
            assert exploration.check_note(ctx, "inv", dict(kind="candidate_lock", text="looks good")) is None


def test_legacy_proposal_refusal_cannot_offer_the_v2_tool_in_corrective_feedback():
    from eidos.domain.turns import validate_decision
    proposal = dict(calls=[dict(tool='prepare_candidate', args=dict(source='s', seeds=[11, 23]))])
    with pytest.raises(Invalid, match='unknown or non-effect tool') as caught:
        validate_decision(proposal, max_calls=4)
    assert caught.value.details['allowed'] == VOCABULARY[:-4]
    assert validate_decision(proposal, max_calls=4, allow_prepare_candidate=True)['calls'] == proposal['calls']
