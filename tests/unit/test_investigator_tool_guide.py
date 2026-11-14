"""The investigator's tool guide (``eidos.investigator.context.TOOL_GUIDE``) tells a model what it can and cannot do
without held-out data (ADR 0021): a candidate can always be submitted for confirmation, and an assessment over data the
charter does not attest stays exploratory.

Author tests of a constant: no database, no models, no network.
"""

from __future__ import annotations

import re

from eidos.investigator.context import TOOL_GUIDE


def flat(text: str) -> str:
    """The guide with line breaks and indentation collapsed, so a sentence is found wherever it wraps."""
    return re.sub(r"\s+", " ", text)


def test_the_guide_says_plainly_that_a_candidate_can_be_submitted_without_a_held_out_verdict():
    guide = flat(TOOL_GUIDE).lower()
    assert "you can submit a candidate without a held-out verdict" in guide
    assert 'conclude with disposition "candidate"' in guide
    assert "candidate submitted for confirmation" in guide


def test_the_guide_calls_a_submitted_candidate_a_normal_complete_result_and_names_who_decides():
    guide = flat(TOOL_GUIDE)
    assert "the independent evaluator decides" in guide and "normal, complete result" in guide


def test_the_guide_says_an_assessment_without_attested_held_out_data_stays_exploratory():
    guide = flat(TOOL_GUIDE)
    assert "stays exploratory" in guide and "can never confirm a claim" in guide
    assert "results must not exist when a plan is frozen" in guide


def test_the_guide_says_abstaining_or_a_null_takes_precedence_and_that_a_value_is_an_improvement():
    guide = flat(TOOL_GUIDE)
    assert "Abstaining or reporting a null takes precedence over every other field" in guide
    assert 'direction?: "higher_is_better"|"lower_is_better"' in guide and "positive is better" in guide


def test_the_guide_no_longer_offers_the_templates_classification_route_for_existing_predictions():
    assert "classification: {labels:" not in TOOL_GUIDE and "predictions: {baseline:" not in TOOL_GUIDE


def test_the_guide_gives_the_exact_candidate_lock_format():
    guide = flat(TOOL_GUIDE)
    assert 'record_note {kind: "candidate_lock", text: "{\\"job_id\\": \\"<job>\\"}"}' in guide
    assert "exactly one job in evidence_refs" in guide


def test_the_playbook_gives_the_exact_candidate_lock_format():
    from eidos.investigator.playbook import PLAYBOOK

    playbook = flat(PLAYBOOK)
    assert 'record_note with kind "candidate_lock" and text {"job_id": "<job>"}' in playbook
    assert "exactly one job in evidence_refs" in playbook


def test_finalization_context_exposes_evidence_and_conclude_only():
    from eidos.investigator.context import pack
    packed = pack({"investigation": {"id": "i", "status": "active", "body": {"question": "report existing work"}},
                   "horizon": 0, "finalization": {"enabled": True}}, [])
    assert set(packed["known_tools"]) == {"get_job", "inspect_evidence", "describe_component", "conclude", "stop"}
    guide = packed["text"].split("## Tools")[-1]
    assert "calls: []" in guide and "run_component {" not in guide and "author_source {" not in guide
    assert '"rationale": "<why the recorded evidence supports this scoped outcome>"' in guide
    assert '"calls": []' in guide and '"action": "conclude"' in guide
    assert "Missing held-out evidence never passes" in guide


def test_v4_claim_and_manual_lock_examples_agree_with_live_validators():
    import json
    from pathlib import Path

    from eidos.domain.assessment import DISPOSITIONS, derive_claim_state
    from eidos.domain.exploration import parse_rule
    from eidos.domain.turns import validate_decision
    from eidos.investigator.context import TOOL_GUIDE_V4
    from eidos.investigator.playbook import PLAYBOOK_V3

    prompt = (Path(__file__).resolve().parents[2] / 'study/prompts/tools_arm4.v2.md').read_text()
    assert 'copied from the pinned prepared lock JSON' in prompt and 'both job_id and selection' in prompt
    assert 'text containing exactly a JSON object {"job_id"' not in prompt
    assert 'needs no separate scorer' in prompt
    examples = []
    for guide in (TOOL_GUIDE_V4, PLAYBOOK_V3):
        start = guide.index('{"job_id":"<job>"')
        example, _ = json.JSONDecoder().raw_decode(guide[start:])
        examples.append(example)
        rule = parse_rule(example['selection'], policy_id='exploration-policy/3')
        assert rule['margin']['metric'] == rule['metric'] + '_se'
        assert 'prepared lock JSON' in guide and 'job_id-only lock needs a previously recorded rule' in guide
        assert 'without a held-out verdict' in guide and 'disposition "candidate"' in guide
        assert 'omitted args.purpose default to open-search' in guide
        assert 'already-admitted pending round' in guide
        assert 'assess_candidate {success_rule_id, claim_id or claim:' in guide
        assert 'request_input' in guide and 'steering' in guide
        dispositions = re.search(r'disposition: ([^,]+),', guide).group(1)
        assert set(re.findall(r'"([a-z]+)"', dispositions)) == set(DISPOSITIONS)
        for disposition in DISPOSITIONS:
            claim = dict(disposition=disposition, point_estimate=dict(metric='validation_accuracy', value=.05),
                         evidence_refs=['job-evidence'], obligations=['independent confirmation'])
            decision = validate_decision(dict(calls=[], control=dict(action='conclude', summary='visible evidence', claim=claim)), max_calls=6)
            state = derive_claim_state(control=decision['control'], claims=[], assessments=[], plans=[], charter=None)
            if disposition == 'candidate':
                assert state['state'] == 'candidate-submitted' and not state['confirmed']
    assert examples[0] == examples[1]


def test_v4_guide_is_smaller_than_the_previous_guide():
    from eidos.investigator.context import TOOL_GUIDE_V4
    assert len(TOOL_GUIDE_V4) < len(TOOL_GUIDE)
