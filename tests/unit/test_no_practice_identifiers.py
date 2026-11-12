"""Prevent practice answers or oracle metadata from reaching production search surfaces."""
import json
from pathlib import Path

from study.practice.guard import identifier_violations

ROOT = Path(__file__).resolve().parents[2]


def identifiers():
    registry = json.loads((ROOT / 'study/practice/registry.json').read_text())
    return ({g['id'] for g in registry} | {f'P{i:04d}' for i in range(1, 61)}
            | {'N01', 'N02', 'N03', 'N04', 'D02', 'D03', 'D04'}
            | {'oracle_mechanism', 'oracle_informative_columns', 'oracle_true_gain', 'oracle_reference_score'})


def test_no_practice_identifiers_in_model_facing_sources():
    paths = [p for directory in ('components', 'src/eidos/investigator', 'study/prompts')
             for p in (ROOT / directory).rglob('*')]
    assert identifier_violations(paths, identifiers()) == []


def test_lint_catches_planted_card_generator_and_oracle_but_allows_operators(tmp_path):
    path = tmp_path / 'operator.py'
    path.write_text('gmm_class = 1\n# P0001 gen-001 oracle_true_gain\n')
    violations = identifier_violations([path], identifiers())
    assert [value[2] for value in violations] == ['P0001', 'gen-001', 'oracle_true_gain']
