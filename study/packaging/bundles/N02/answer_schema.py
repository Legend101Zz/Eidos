"""Validate the common answer's structure only; never execute or open artifacts.

Passing does not establish artifact integrity, filesystem confinement, budget
compliance, independent reproduction, or scientific validity.
"""
import argparse
import json
import math
from pathlib import Path, PurePosixPath
import re

STATUSES = {'improvement_claimed', 'no_improvement_found', 'inconclusive', 'invalid', 'abstained'}
CLAIMS = {'checkpoint_improvement', 'recipe_improvement', 'architecture_improvement', 'planted_diagnosis', 'operational_reliability', 'transfer', 'no_positive_claim'}
COST_NUMBERS = {'elapsed_seconds', 'cpu_core_seconds', 'gpu_device_seconds', 'model_input_tokens', 'model_output_tokens', 'model_reasoning_tokens', 'model_calls', 'billed_usd'}
COST_INTEGERS = {'model_input_tokens', 'model_output_tokens', 'model_reasoning_tokens', 'model_calls'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def string(value, field):
    require(isinstance(value, str) and bool(value.strip()), 'expected nonempty string: ' + field)


def strings(value, field):
    require(isinstance(value, list), 'expected list: ' + field)
    for item in value:
        string(item, field)


def number(value, field):
    require(type(value) in (int, float) and math.isfinite(value), 'expected finite number: ' + field)


def path(value, field):
    string(value, field)
    require('\\' not in value and '\x00' not in value and ':' not in value and not PurePosixPath(value).is_absolute() and all(p not in ('', '.', '..') for p in value.split('/')), 'expected relative POSIX artifact path: ' + field)


def digest(value, field):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None, 'expected SHA-256: ' + field)


def fields(value, names, field):
    require(isinstance(value, dict) and set(names) <= value.keys(), 'missing fields in ' + field)


def validate(a):
    required = {'schema_version', 'task_id', 'run_id', 'status', 'claim_type', 'claim', 'scope', 'candidate', 'measurements', 'evidence', 'alternative_explanations', 'abstention', 'cost', 'exposures', 'deviations', 'confidence'}
    fields(a, required, 'answer')
    require(a['schema_version'] == 'eidos-answer/0.2', 'unknown schema')
    require(isinstance(a['status'], str) and a['status'] in STATUSES, 'unknown status')
    require(isinstance(a['claim_type'], str) and a['claim_type'] in CLAIMS, 'unknown claim type')
    for key in ('task_id', 'run_id', 'claim', 'scope'):
        string(a[key], key)
    for key in ('alternative_explanations', 'exposures', 'deviations'):
        strings(a[key], key)
    for key in ('measurements', 'evidence'):
        require(isinstance(a[key], list), 'expected list: ' + key)

    abst = a['abstention']
    fields(abst, {'abstained', 'reason', 'unresolved', 'next_discriminating_test'}, 'abstention')
    require(type(abst['abstained']) is bool, 'abstained must be boolean')
    require(abst['abstained'] == (a['status'] == 'abstained'), 'status and abstention disagree')
    strings(abst['unresolved'], 'abstention.unresolved')
    for key in ('reason', 'next_discriminating_test'):
        if abst[key] is not None:
            string(abst[key], 'abstention.' + key)
    if abst['abstained']:
        require(bool(abst['reason']) and bool(abst['unresolved']) and bool(abst['next_discriminating_test']), 'abstention needs reason, unresolved obligations and next test')
        require(a['claim_type'] == 'no_positive_claim', 'abstention cannot assert a positive claim type')

    candidate = a['candidate']
    if candidate is not None:
        fields(candidate, {'code_path', 'code_sha256', 'model_path', 'model_sha256', 'predictions_path', 'predictions_sha256', 'reproduce_command', 'environment_lock', 'environment_lock_sha256', 'parent_checkpoint', 'restore_class'}, 'candidate')
        for key in ('code_path', 'model_path', 'predictions_path', 'environment_lock'):
            path(candidate[key], 'candidate.' + key)
        for key in ('code_sha256', 'model_sha256', 'predictions_sha256', 'environment_lock_sha256'):
            digest(candidate[key], 'candidate.' + key)
        strings(candidate['reproduce_command'], 'candidate.reproduce_command')
        require(bool(candidate['reproduce_command']), 'empty reproduce command')
        string(candidate['restore_class'], 'candidate.restore_class')
        if candidate['parent_checkpoint'] is not None:
            path(candidate['parent_checkpoint'], 'candidate.parent_checkpoint')

    evidence_paths = set()
    for item in a['evidence']:
        fields(item, {'path', 'sha256', 'role', 'supports', 'limitations'}, 'evidence entry')
        path(item['path'], 'evidence.path')
        digest(item['sha256'], 'evidence.sha256')
        require(item['path'] not in evidence_paths, 'duplicate evidence path')
        evidence_paths.add(item['path'])
        for key in ('role', 'supports', 'limitations'):
            string(item[key], 'evidence.' + key)
    for item in a['measurements']:
        fields(item, {'metric', 'direction', 'split', 'denominator', 'units', 'baseline', 'candidate', 'uncertainty_method', 'evidence_receipt'}, 'measurement')
        for key in ('metric', 'split', 'units', 'uncertainty_method'):
            string(item[key], 'measurement.' + key)
        require(item['direction'] in ('higher_is_better', 'lower_is_better'), 'invalid metric direction')
        require(type(item['denominator']) is int and item['denominator'] > 0, 'denominator must be a positive integer')
        for key in ('baseline', 'candidate'):
            number(item[key], 'measurement.' + key)
        path(item['evidence_receipt'], 'measurement.evidence_receipt')
        require(item['evidence_receipt'] in evidence_paths, 'measurement receipt missing from evidence')
    if a['status'] == 'improvement_claimed':
        require(candidate is not None and bool(a['evidence']) and bool(a['measurements']), 'positive claim needs candidate/evidence/measurement')
        require(a['claim_type'] != 'no_positive_claim', 'positive status requires a positive claim type')

    cost = a['cost']
    fields(cost, COST_NUMBERS | {'gpu_type', 'unknown_fields', 'receipt_paths'}, 'cost')
    strings(cost['unknown_fields'], 'cost.unknown_fields')
    nullable = COST_NUMBERS | {'gpu_type'}
    require(set(cost['unknown_fields']) == {key for key in nullable if cost[key] is None} and len(cost['unknown_fields']) == len(set(cost['unknown_fields'])), 'unknown_fields must name exactly the null cost fields')
    for key in COST_NUMBERS:
        if cost[key] is not None:
            number(cost[key], 'cost.' + key)
            require(cost[key] >= 0, 'negative cost: ' + key)
            if key in COST_INTEGERS:
                require(type(cost[key]) is int, 'cost counter must be integer: ' + key)
    if cost['gpu_type'] is not None:
        string(cost['gpu_type'], 'cost.gpu_type')
    require(isinstance(cost['receipt_paths'], list), 'expected cost receipt list')
    for receipt in cost['receipt_paths']:
        path(receipt, 'cost.receipt_paths')
    if a['confidence'] is not None:
        number(a['confidence'], 'confidence')
        require(0 <= a['confidence'] <= 1, 'invalid confidence')
        string(a.get('confidence_outcome'), 'confidence_outcome')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('answer')
    args = parser.parse_args()
    validate(json.loads(Path(args.answer).read_text()))
    print('structural check passed; artifacts, security, costs and science not independently verified')
