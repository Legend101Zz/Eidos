"""Second review regressions, including registered CLI comparisons for every plan shape."""
import ast
import copy
import json
from pathlib import Path

import pytest

from study.exp import compare as C
from study.exp import ledger, novelty
from study.exp.scorecard import canonical, sha256
from study.tests.test_exp_compare import cards

COMMIT = 'a' * 40


def rule_for(name):
    return json.loads((C.RULES / (name + '.json')).read_text())


def declaration(tier, n=8, reps=1, pool='D', provider='offline', **extra):
    return dict(tier=tier, cards=[f'R{i:04d}' for i in range(1, n + 1)],
                replicates=reps, pool=pool, provider=provider, what='fixture', **extra)


def score(d, delta=0):
    s = cards(delta, replicates=d['replicates'], tier=d['tier'])
    s['individual'] = [r for r in s['individual'] if r['card_id'] in d['cards']]
    s['per_card'] = {r['card_id']: {} for r in s['individual']}
    s['pool']['id'] = 'E2' if d['pool'] == 'evaluation_rotation' else d['pool']
    s['provider'] = None if d['provider'] is None else {'id': d['provider']}
    s['build']['commit'] = COMMIT
    for r in s['individual']:
        r.update(pool=d['pool'], mode='autopilot' if d['provider'] is None else 'live', look='initial', tokens=100)
    s['aggregate'].update(live_runs=len(s['individual']), time_seconds=1, quota_weekly_percent=1)
    return s


def register(directory, plan, rule, *, choice=None, vetoes=None, reviews=()):
    directory.mkdir(parents=True, exist_ok=True)
    plan = copy.deepcopy(plan)
    plan.update(schema='eidos-exp-plan/1', id='EXP-900', kind='infra' if rule['id'] == 'R-infra' else
                'enabling' if rule['id'] == 'R-enable' else 'performance', stop_early=[], second_provider=None)
    plan['budget'] = dict(dict(live_runs=200, usd=100, quota_weekly_percent=100), **plan.get('budget', {}))
    plan.setdefault('treatment', dict(incumbent='a', candidate='a'))
    for d in plan['tiers']:
        d.setdefault('what', 'fixture')
    rule = dict(rule, schema='eidos-exp-rule/1')
    choice = dict(dict(schema='eidos-exp-rule-choice/1', rule=rule['id'], version=1, overrides={}, extension=False), **(choice or {}))
    # Overrides/extension may be passed without repeating default keyword names.
    (directory / 'HYPOTHESIS.md').write_text('fixture\n')
    (directory / 'plan.json').write_text(json.dumps(plan))
    (directory / 'rule.json').write_text(json.dumps(choice))
    vetoes = vetoes or rule_for('VETOES')
    entries = []
    data = dict(files={n: sha256((directory / n).read_bytes()) for n in ledger.FILES},
                rule_snapshot=rule, rule_sha256=sha256(canonical(rule)),
                vetoes_snapshot=vetoes, vetoes_sha256=sha256(canonical(vetoes)))
    for kind, value in [('register', data), *[('review', r) for r in reviews]]:
        entries.append(ledger.append(directory / 'ledger.jsonl', entries,
                       dict(type=kind, utc='2026-11-14T00:00:00+00:00', actor='fixture', data=value)))
    return plan


def attest(plan, **checks):
    return dict(experiment=plan['id'], commit=COMMIT, checks=dict(tests_green=True, ci_green=True,
                default_treatment_golden_unchanged=True, guardrails_clean=True,
                practice_generators_no_harm=True, **checks))


def run_registered(tmp_path, name, declarations, before, after, **extra):
    rule = rule_for(name)
    directory = tmp_path / 'EXP-900-fixture'
    plan = register(directory, dict(tiers=declarations, **extra.pop('plan_extra', {})), rule,
                    reviews=[dict(kind='code', commit=COMMIT[:7], verdict='ACCEPT')], **extra)
    return C.compare(before, after, rule, tier=declarations[-1]['tier'], experiment=directory,
                     rule_choice={'plan': plan}, attest=attest(plan))


@pytest.mark.parametrize('name', ['R-behav', 'R-disc'])
def test_exp_r2_mixed_tier_shapes(tmp_path, name):
    ds = [declaration('T2', 4), declaration('T3', 8, 2)]
    a, b = [score(d) for d in ds], [score(d, .3) for d in ds]
    assert run_registered(tmp_path, name, ds, a, b)['decision'] == 'keep'
    for r in b[1]['individual']:
        r.update(vrg=-1, mrg=-1)
    assert run_registered(tmp_path / 'bad', name, ds, a, b)['decision'] == 'revert'


@pytest.mark.parametrize('reps,new_cards', [(3, False), (2, True)])
def test_exp_r2_extension_shape(tmp_path, reps, new_cards):
    d = declaration('T3', 8, 2)
    ext = dict(cards=[f'R{i:04d}' for i in range(11, 19)] if new_cards else d['cards'], replicates=reps)
    d['extension'] = ext
    initial_a, initial_b = score(d), score(d, .3)
    e = dict(d, **ext)
    a, b = score(e), score(e, .3)
    # score() starts with eight fixture cards; replace names for the new rotation.
    if new_cards:
        for s, delta in ((a, 0), (b, .3)):
            fresh = score(dict(e, cards=d['cards']), delta)
            for r in fresh['individual']:
                r['card_id'] = ext['cards'][int(r['card_id'][1:]) - 1]
            fresh['per_card'] = {r['card_id']: {} for r in fresh['individual']}
            s.update(fresh)
    for s in (a, b):
        for r in s['individual']:
            r['look'] = 'extension'
    result = run_registered(tmp_path, 'R-enable', [d], [initial_a, a], [initial_b, b],
                            choice={'extension': True}, plan_extra={'target': dict(metric='vrg', tier='T3', direction='up', amount=.1)})
    assert result['decision'] == 'keep'
    b['individual'][0]['vrg'] = None
    assert run_registered(tmp_path / 'bad', 'R-enable', [d], [initial_a, a], [initial_b, b],
                          choice={'extension': True}, plan_extra={'target': dict(metric='vrg', tier='T3', direction='up', amount=.1)})['decision'] == 'incomplete'


def test_exp_r2_model_free_rotation_mech(tmp_path):
    ds = [declaration('T1', pool='evaluation_rotation', provider=None), declaration('T2', 4)]
    a, b = [score(d) for d in ds], [score(d, .3) for d in ds]
    assert run_registered(tmp_path, 'R-mech', ds, a, b)['decision'] == 'keep'
    b[0]['individual'][0]['held_out_reads'] = None
    assert run_registered(tmp_path / 'bad', 'R-mech', ds, a, b)['decision'] == 'incomplete'


@pytest.mark.parametrize('name', ['R-infra', 'R-enable'])
def test_exp_r2_single_tier_and_infra(tmp_path, name):
    d = declaration('T1', provider=None) if name == 'R-infra' else declaration('T3')
    a, b = score(d), score(d, .3)
    extra = {'target': dict(metric='vrg', tier='T3', direction='up', amount=.1)} if name == 'R-enable' else {}
    assert run_registered(tmp_path, name, [d], a, b, plan_extra=extra)['decision'] == 'keep'
    b['individual'][0]['incident'] = True
    result = run_registered(tmp_path / 'bad', name, [d], a, b, plan_extra=extra)
    assert result['decision'] == 'incomplete'
    assert 'incident in paired evidence' in result['reasons']


def test_exp_r2_registration_and_usage(tmp_path):
    d, rule = declaration('T3'), rule_for('R-behav')
    rule['keep_when'] = rule['keep_when'][1:]
    a, b = score(d), score(d, .3)
    plan = dict(tiers=[d])
    directory = tmp_path / 'EXP-900-fixture'
    plan = register(directory, plan, rule)
    assert C.compare(a, b, rule, tier='T3', rule_choice={'plan': plan})['decision'] == 'incomplete'
    usage = dict(schema='eidos-exp-usage/1', runs=16, usd=2, quota_weekly_percent=1,
                 time_seconds=2, per_variant={'incumbent': {'runs': 8, 'usd': 1}, 'candidate': {'runs': 8, 'usd': 1}})
    a['aggregate']['usd'] = b['aggregate']['usd'] = None
    result = C.compare(a, b, rule, tier='T3', experiment=directory, usage=usage)
    assert result['decision'] == 'keep'
    plan['budget']['live_runs'] = 8
    (directory / 'plan.json').write_text(json.dumps(plan))
    with pytest.raises(ValueError, match='hash mismatch'):
        C.compare(a, b, rule, tier='T3', experiment=directory, usage=usage)


def test_exp_r2_decisive_failure_precedes_pending():
    d = declaration('T3')
    a, b = score(d), score(d, -.3)
    result = C.compare(a, b, rule_for('R-behav'), tier='T3', rule_choice={'plan': dict(tiers=[d], treatment={'incumbent': 'a', 'candidate': 'a'})})
    assert result['decision'] == 'revert'


def test_exp_r2_review_format(tmp_path):
    p = tmp_path / 'review.md'
    for verdict in ['ACCEPT', 'FIX FIRST', 'REJECT']:
        p.write_text(f'VERDICT: {verdict} — commit {COMMIT[:7]}\n')
        assert C._review(p)['verdict'] == verdict


def test_exp_r2_baseline_ast(tmp_path):
    repo = Path(__file__).parents[2]
    cat = novelty.catalog(repo)
    root = tmp_path / 'candidate'
    root.mkdir()
    source = (repo / 'study/exp/cards/baseline_project.py').read_text()
    p = root / 'project.py'
    p.write_text(source + '\n# harmless comment\n')
    assert novelty.fingerprint(root, cat)['class'] == 'baseline'
    p.write_text(source + '\nSEMANTIC_CHANGE = 1\n')
    assert novelty.fingerprint(root, cat)['class'] != 'baseline'


def test_exp_r2_normalization_name_capture():
    assert novelty.normalised_hash(ast.parse('def f(x): return x + local_1_0')) != novelty.normalised_hash(ast.parse('def f(x): return x + x'))


@pytest.mark.parametrize('bad', ['rule', 'plan', 'choice', 'empty_vetoes', 'inline'])
def test_exp_r2_registered_tampering(tmp_path, bad):
    d, rule = declaration('T3'), rule_for('R-behav')
    directory = tmp_path / 'EXP-900-fixture'
    plan = register(directory, dict(tiers=[d]), rule)
    a, b = score(d), score(d, .3)
    if bad == 'rule':
        rule['keep_when'] = rule['keep_when'][1:]
    elif bad in {'plan', 'choice'}:
        p = directory / ('plan.json' if bad == 'plan' else 'rule.json')
        p.write_text(p.read_text() + '\n')
    elif bad == 'empty_vetoes':
        entries = ledger.read_chain(directory / 'ledger.jsonl')
        data = entries[0]['data']
        data['vetoes_snapshot']['keep_when'] = []
        data['vetoes_sha256'] = sha256(canonical(data['vetoes_snapshot']))
        (directory / 'ledger.jsonl').unlink()
        ledger.append(directory / 'ledger.jsonl', [], dict(type='register', utc=entries[0]['utc'], actor='fixture', data=data))
    if bad == 'inline':
        choice = directory / 'inline.json'
        choice.write_text(json.dumps({'plan': plan}))
        x, y, r = [tmp_path / n for n in ['a.json', 'b.json', 'rule.json']]
        for path, value in [(x, a), (y, b), (r, rule)]:
            path.write_text(json.dumps(value))
        with pytest.raises(SystemExit) as exc:
            C.main([str(x), str(y), '--rule', str(r), '--rule-choice', str(choice), '--out', str(tmp_path / 'decision.json')])
        assert exc.value.code == 2
    else:
        with pytest.raises(ValueError):
            C.compare(a, b, rule, experiment=directory, tier='T3')


@pytest.mark.parametrize('unknown', [None, 'x', -1, 10**9])
def test_exp_r2_unknown_heldout_rows(unknown):
    d = declaration('T3')
    a, b = score(d), score(d, .3)
    b['individual'][0]['held_out_reads'] = unknown
    result = C.compare(a, b, rule_for('R-enable'), tier='T3')
    assert result['vetoes'][1]['value'] is None
    assert result['decision'] == 'incomplete'


def test_exp_r2_budget_counts_both_arms(tmp_path):
    d = declaration('T3')
    a, b = score(d), score(d, .3)
    result = run_registered(tmp_path, 'R-enable', [d], a, b,
                            plan_extra=dict(budget=dict(live_runs=8, usd=100, quota_weekly_percent=100),
                            target=dict(metric='vrg', tier='T3', direction='up', amount=.1)))
    assert result['decision'] == 'revert'
    assert result['vetoes'][-1]['value'] is False


def test_exp_r2_latest_ledger_review_and_attestation_binding(tmp_path):
    d, rule = declaration('T3'), rule_for('R-infra')
    directory = tmp_path / 'EXP-900-fixture'
    plan = register(directory, dict(tiers=[d]), rule, reviews=[
        dict(kind='code', commit=COMMIT[:7], verdict='ACCEPT'),
        dict(kind='code', commit=COMMIT[:7], verdict='REJECT')])
    a, b = score(d), score(d)
    checks = attest(plan)
    result = C.compare(a, b, rule, tier='T3', experiment=directory, attest=checks,
                       reviews=[dict(kind='code', commit=COMMIT, verdict='ACCEPT')])
    assert result['decision'] == 'revert'
    entries = ledger.read_chain(directory / 'ledger.jsonl')
    ledger.append(directory / 'ledger.jsonl', entries, dict(type='review', utc=entries[-1]['utc'], actor='fixture',
                  data=dict(kind='code', commit='b' * 40, verdict='ACCEPT')))
    result = C.compare(a, b, rule, tier='T3', experiment=directory, attest=checks)
    assert result['decision'] == 'incomplete'  # N09: a mismatching nonempty commit never passes.
    for field in ['commit', 'experiment']:
        bad = dict(checks, **{field: 'stale'})
        with pytest.raises(ValueError, match='attestation'):
            C.compare(a, b, rule, tier='T3', experiment=directory, attest=bad)


def test_exp_r2_n10_two_prior_extensions_refused():
    d = declaration('T3')
    a, b = score(d), score(d, .3)
    for s in [a, b]:
        for row in s['individual']:
            row['look'] = 'extension'
    d['extension'] = dict(cards=d['cards'], replicates=1)
    with pytest.raises(ValueError, match='second extension'):
        C.compare(a, b, rule_for('R-enable'), tier='T3', previous=[dict(decision='extend')] * 2,
                   rule_choice=dict(plan=dict(tiers=[d], treatment=dict(incumbent='a', candidate='a')), extension=True))


def test_exp_r2_n20_superset_plan_refused():
    d = declaration('T3')
    a, b = score(d), score(d, .3)
    for s in [a, b]:
        extra = dict(s['individual'][0], card_id='R0009')
        s['individual'].append(extra)
        s['per_card']['R0009'] = {}
    with pytest.raises(ValueError, match='card set differs from plan'):
        C.compare(a, b, rule_for('R-behav'), tier='T3',
                   rule_choice=dict(plan=dict(tiers=[d], treatment=dict(incumbent='a', candidate='a'))))


@pytest.mark.parametrize('shape', ['behav', 'disc', 'extension', 'extension_rotation', 'model_free', 'mech', 'enable', 'infra', 'behav_extension', 'disc_extension'])
def test_exp_r2_cli_all_plan_shapes(tmp_path, shape):
    import subprocess
    import sys

    from study.tests.test_exp_scorecard import make_runs, write

    name = {'behav': 'R-behav', 'disc': 'R-disc', 'mech': 'R-mech', 'infra': 'R-infra', 'behav_extension': 'R-behav', 'disc_extension': 'R-disc'}.get(shape, 'R-enable')
    ds = [declaration('T2', 4), declaration('T3', 8, 2)] if shape in {'behav', 'disc', 'behav_extension', 'disc_extension'} else [declaration('T3')]
    if shape == 'mech':
        ds = [declaration('T1', pool='evaluation_rotation', provider=None), declaration('T2', 4)]
    if shape in {'model_free', 'infra'}:
        ds = [declaration('T1', provider=None)]
    extra = dict(target=dict(metric='vrg', direction='up', amount=.1, tier=ds[-1]['tier'])) if name == 'R-enable' else {}
    if shape == 'model_free':
        extra['model_free_suffices'] = True
    batches = [(d, 'initial') for d in ds]
    extension = 'extension' in shape
    if extension:
        ds[-1]['extension'] = dict(cards=[f'R{i:04d}' for i in range(11, 19)] if shape.endswith('rotation') else ds[-1]['cards'], replicates=3)
        batches.append((dict(ds[-1], **ds[-1]['extension']), 'extension'))
    directory = tmp_path / 'EXP-900-fixture'
    rule = rule_for(name)
    plan = register(directory, dict(tiers=ds, **extra), rule, choice={'extension': extension},
                    reviews=[dict(kind='code', commit=COMMIT[:7], verdict='ACCEPT')])
    definition, attestation, usage_path = [tmp_path / p for p in ['definition.json', 'attest.json', 'usage.json']]
    write(definition, rule)
    write(attestation, attest(plan))
    total = sum(len(d['cards']) * d['replicates'] for d, look in batches)
    write(usage_path, dict(schema='eidos-exp-usage/1', runs=2 * total, usd=2 * total * .1,
          quota_weekly_percent=2, time_seconds=50 * total,
          per_variant={side: dict(runs=total, usd=total * .1) for side in ['incumbent', 'candidate']}))
    args = [sys.executable, '-m', 'study.exp.compare', '--rule', str(definition), '--experiment', str(directory),
            '--tier', ds[-1]['tier'], '--attest', str(attestation), '--usage', str(usage_path), '--out', str(tmp_path / 'decision.json')]
    outputs = []
    for i, (d, look) in enumerate(batches):
        pool = tmp_path / f'pool{i}'
        pool.mkdir()
        write(pool / 'pool.json', dict(pool_id='E2' if d['pool'] == 'evaluation_rotation' else d['pool'], commitment='fixture'))
        for side, gain in [('incumbent', .02), ('candidate', .1)]:
            run = tmp_path / f'{side}{i}'
            refs = make_runs(run, [dict(card=c, rep=r, verified=True, gain=gain, extra={'held_out_reads': False})
                                  for c in d['cards'] for r in range(1, d['replicates'] + 1)])
            for c in d['cards']:
                write(pool / 'controller' / c / 'reference.json', refs[c])
                for r in range(1, d['replicates'] + 1):
                    arm = run / 'runs' / f'{c}-r{r}' / 'arm4'
                    record = json.loads((arm / 'run_record.json').read_text())
                    record['eidos_commit_sha'] = COMMIT
                    write(arm / 'run_record.json', record)
                    meta = json.loads((arm / 'arm.json').read_text())
                    meta.update(treatment={'id': 'a'}, provider=None if d['provider'] is None else {'id': d['provider']})
                    write(arm / 'arm.json', meta)
            cat = dict(schema='eidos-exp-catalog/1', commit=COMMIT, missing=['synthetic'], inputs={},
                       parse_errors=[], atoms=[], definition_hashes=[], module_hashes={})
            cat['sha256'] = sha256(canonical(cat))
            cat_path = run / 'catalog.json'
            write(cat_path, cat)
            output = run / 'scorecard.json'
            cmd = [sys.executable, '-m', 'study.exp.scorecard', '--run-dir', str(run), '--pool-root', str(pool),
                   '--catalog', str(cat_path), '--out', str(output), '--tier', d['tier'], '--pool-role', d['pool'],
                   '--look', look, '--mode', 'autopilot' if d['provider'] is None else 'live']
            result = subprocess.run(cmd, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            produced = json.loads(output.read_text())
            assert all(row['tier'] == d['tier'] and row['look'] == look and row['pool'] == d['pool'] for row in produced['individual'])
            args += ['--' + side, str(output)]
            outputs.append(output)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    decision = json.loads((tmp_path / 'decision.json').read_text())
    assert decision['decision'] == 'keep', decision
    assert decision['inputs']['usage'] == sha256(usage_path.read_bytes())
    # A row-backed safety veto stays decisive for every shape through the CLI.
    output = outputs[-1]
    bad = json.loads(output.read_text())
    bad['individual'][0]['false_claims'] = 1
    bad['aggregate']['false_claims'] = 1
    write(output, bad)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'decision.json').read_text())['decision'] == 'revert'


@pytest.mark.parametrize('pool', ['E1', 'E2', 'E3'])
@pytest.mark.parametrize('provider', [None, 'none (fixture runtime)'])
def test_exp_r2_model_free_provider_and_rotations(tmp_path, pool, provider):
    ds = [declaration('T1', pool='evaluation_rotation', provider=None), declaration('T2', 4)]
    a, b = [score(d) for d in ds], [score(d, .3) for d in ds]
    a[0]['pool']['id'] = b[0]['pool']['id'] = pool
    b[0]['provider'] = provider
    assert run_registered(tmp_path, 'R-mech', ds, a, b)['decision'] == 'keep'


@pytest.mark.parametrize('bad', ['runs', 'negative', 'null_time', 'missing_variant_cost', 'total_cost'])
def test_exp_r2_usage_rejects_bad_or_missing_accounting(tmp_path, bad):
    d = declaration('T3')
    a, b = score(d), score(d, .3)
    rule = rule_for('R-enable')
    directory = tmp_path / 'EXP-900-fixture'
    register(directory, dict(tiers=[d], target=dict(metric='vrg', tier='T3', direction='up', amount=.1)), rule)
    usage = dict(schema='eidos-exp-usage/1', runs=16, usd=2, quota_weekly_percent=1, time_seconds=2,
                 per_variant={side: dict(runs=8, usd=1) for side in ['incumbent', 'candidate']})
    if bad == 'runs':
        usage['runs'] = 8
    elif bad == 'negative':
        usage['usd'] = -1
    elif bad == 'null_time':
        usage['time_seconds'] = None
    elif bad == 'total_cost':
        usage['usd'] = 5
    else:
        usage['per_variant']['candidate']['usd'] = None
        assert C.compare(a, b, rule, tier='T3', experiment=directory, usage=usage)['decision'] == 'incomplete'
        return
    with pytest.raises(ValueError, match='usage|accounting'):
        C.compare(a, b, rule, tier='T3', experiment=directory, usage=usage)


def test_exp_r2_missing_attestation_identity_is_refused(tmp_path):
    d, rule = declaration('T3'), rule_for('R-infra')
    directory = tmp_path / 'EXP-900-fixture'
    register(directory, dict(tiers=[d]), rule)
    with pytest.raises(ValueError, match='attestation'):
        C.compare(score(d), score(d), rule, tier='T3', experiment=directory,
                   attest={'checks': {'tests_green': True}})


@pytest.mark.parametrize('metric,stat,value', [('verified_rate', 'success_count_delta', 0), ('novelty_wins', 'count', 1)])
def test_exp_r2_incident_cannot_make_a_count_clause_decisive(metric, stat, value):
    d = declaration('T3')
    a, b = score(d), score(d)
    for s in [a, b]:
        s['catalog'] = {'sha256': 'same'}
        s['aggregate']['novelty_wins'] = 0
        for row in s['individual']:
            row['novelty_win'] = False
    b['individual'][0].update(incident=True, verified_success=False)
    rule = dict(id='R-behav', version=1, overridable={}, keep_when=[dict(metric=metric, stat=stat, op='>=', value=value, tier='T3')])
    result = C.compare(a, b, rule, tier='T3')
    assert result['decision'] == 'incomplete'
    assert result['clauses'][0]['value'] is None
