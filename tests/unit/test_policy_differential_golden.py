"""Output hashes computed from main 806f939, with the reviewer's fixed probes."""

import hashlib
import json
from pathlib import Path

from tests.unit._policy_differential_cases import context_cases, state_cases

from eidos.domain import exploration
from eidos.investigator import context

GOLDEN = json.loads((Path(__file__).parent / "fixtures/policy_main_806f939.json").read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def test_context_policy_three_outputs_equal_main():
    for name, (view, catalog, limit) in context_cases().items():
        assert (
            digest(context.pack(view, catalog, policy=context.POLICY_ID, char_limit=limit))
            == GOLDEN["context"][name]
        )


def test_exploration_policy_two_outputs_equal_main():
    assert digest([exploration.state(**kw) for kw in state_cases()]) == GOLDEN["state"]
