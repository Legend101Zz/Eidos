"""Analytical controls and malformed records; no Eidos run data or artifact execution."""
from copy import deepcopy
from analyze_results import analyze
from validate_answer import validate, COST_NUMBERS


def require(value, message):
    if not value:
        raise AssertionError(message)


def rejects(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except ValueError:
        return
    raise AssertionError('malformed input accepted')


def design_for(families):
    return [dict(family=f, task=str(t), run=0, arm=a, primary_eligible=True) for f,n in families for t in range(n) for a in [1,2,3,4]]


d = design_for([('large', 9), ('small', 1)])
r = [dict(x, verified_success=(x['arm'] == 4 and x['family'] == 'small'), status='valid', billed_usd=0) for x in d]
a = analyze(d, r, bootstrap=100)
require(a['primary_4_minus_2'] == .5, 'families must receive equal weight')
r = [dict(x, verified_success=(x['arm'] == 4), status='valid', billed_usd=0) for x in d]
a = analyze(d, r, bootstrap=100)
require(a['primary_4_minus_2'] == 1 and a['primary_95_percentile_interval'] == [1, 1], 'known unit contrast')
r = [x for x in r if not (x['family'] == 'small' and x['arm'] == 4)]
a = analyze(d, r, bootstrap=100)
require(a['primary_4_minus_2'] == .5 and a['missing_results_scored_zero'] == 1, 'missing assignment remains zero')
r = [dict(x, verified_success=True, status='valid', billed_usd=0) for x in d]
require(analyze(d, r, bootstrap=100)['primary_4_minus_2'] == 0, 'equal arms have zero contrast')
analysis_rejections = 0
for change in ({'primary_eligible': 'false'}, {'primary_eligible': 0}, {'arm': True}, {'arm': 1.0}):
    bad = deepcopy(d)
    bad[0].update(change)
    rejects(analyze, bad, r, bootstrap=10)
    analysis_rejections += 1
for change in ({'arm': True}, {'arm': 1.0}, {'billed_usd': -1}, {'billed_usd': float('nan')}, {'billed_usd': float('inf')}, {'billed_usd': True}, {'verified_success': 1}, {'status': 'invalid'}):
    bad = deepcopy(r)
    bad[0].update(change)
    rejects(analyze, d, bad, bootstrap=10)
    analysis_rejections += 1
rejects(analyze, d, r, bootstrap=0)
analysis_rejections += 1
unknown = deepcopy(r)
unknown[0]['billed_usd'] = None
require(analyze(d, unknown, bootstrap=10)['unknown_billed_cost_entries'] == 1, 'null cost remains unknown')

cost = {key: None for key in COST_NUMBERS | {'gpu_type'}}
cost.update(unknown_fields=sorted(cost), receipt_paths=[])
base = dict(schema_version='eidos-answer/0.2', task_id='D12', run_id='control', status='abstained', claim_type='no_positive_claim', claim='No supported positive claim.', scope='This attempted run only.', candidate=None, measurements=[], evidence=[], alternative_explanations=[], abstention=dict(abstained=True, reason='Confirmation missing', unresolved=['confirmation'], next_discriminating_test='independent evaluation'), cost=cost, exposures=[], deviations=[], confidence=None)
require(validate(base), 'well-formed abstention rejected')
positive = deepcopy(base)
positive.update(status='improvement_claimed', claim_type='recipe_improvement', claim='The validation metric improved.', candidate=dict(code_path='candidate/project.py', code_sha256='0'*64, model_path='candidate/model.pt', model_sha256='1'*64, predictions_path='candidate/predictions.npz', predictions_sha256='2'*64, reproduce_command=['python', 'candidate/project.py'], environment_lock='requirements.lock', environment_lock_sha256='3'*64, parent_checkpoint=None, restore_class='fresh_initialization'), measurements=[dict(metric='accuracy', direction='higher_is_better', split='validation', denominator=100, units='fraction', baseline=.7, candidate=.8, uncertainty_method='not estimated; independent confirmation pending', evidence_receipt='validation.json')], evidence=[dict(path='validation.json', sha256='4'*64, role='validation', supports='candidate selection', limitations='not independent confirmation')], abstention=dict(abstained=False, reason=None, unresolved=['independent confirmation'], next_discriminating_test='paired fresh training runs'))
require(validate(positive), 'well-formed claim rejected')
calibrated = deepcopy(positive)
calibrated.update(confidence=.6, confidence_outcome='Independent confirmation meets the prespecified gain and guardrails.')
require(validate(calibrated), 'named confidence rejected')

# Each mutation defeats a distinct structural obligation, not a scientific test.
mutations = [
    ('schema_version', 'eidos-answer/0.1'),
    ('status', 'verified_improvement'),
    ('status', []),
    ('claim_type', 'no_positive_claim'),
    ('candidate', {}),
    ('candidate', None),
    ('candidate.code_sha256', 'bad'),
    ('candidate.predictions_path', '../outside.npz'),
    ('candidate.environment_lock', '/etc/passwd'),
    ('candidate.reproduce_command', 'python candidate.py'),
    ('candidate.reproduce_command', []),
    ('measurements', [None]),
    ('measurements', []),
    ('measurements.0.denominator', True),
    ('measurements.0.denominator', 0),
    ('measurements.0.baseline', float('nan')),
    ('measurements.0.candidate', float('inf')),
    ('measurements.0.evidence_receipt', 'missing.json'),
    ('measurements.0.direction', 'up'),
    ('evidence', [None]),
    ('evidence', []),
    ('evidence.0.sha256', 'not a digest'),
    ('evidence.0.path', '../../receipt.json'),
    ('cost', {}),
    ('cost.elapsed_seconds', -1),
    ('cost.billed_usd', float('inf')),
    ('cost.model_calls', 1.5),
    ('cost.unknown_fields', []),
    ('cost.receipt_paths', ['../receipt.json']),
    ('abstention.abstained', True),
    ('abstention.unresolved', 'confirmation'),
    ('confidence', float('nan')),
    ('confidence', True),
    ('confidence', 1.1),
    ('confidence', .5),
]
for dotted, value in mutations:
    bad = deepcopy(positive)
    parts = dotted.split('.')
    target = bad
    for key in parts[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    target[parts[-1]] = value
    # Isolate numeric-cost validation from null-field bookkeeping.
    if dotted.startswith('cost.') and parts[-1] in COST_NUMBERS and value is not None:
        bad['cost']['unknown_fields'].remove(parts[-1])
    rejects(validate, bad)
for change in ({'reason': None}, {'unresolved': []}, {'next_discriminating_test': None}, {'abstained': False}):
    bad = deepcopy(base)
    bad['abstention'].update(change)
    rejects(validate, bad)
print(f'5 analytical estimator controls, {analysis_rejections} malformed-analysis controls, 3 valid-answer controls and {len(mutations)+4} malformed-answer controls passed; synthetic inputs only.')
