"""Review regressions through real acceptance, launcher receipts, context and study export."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_prepare_candidate_lab as preparation_lab
from temporalio.testing import ActivityEnvironment
from test_incumbent_challengers import RULE, Scene
from test_prepare_candidate_lab import (
    COMMAND,
    client_view,
    fixture_turn,
    run_queued,
    setup_scene,
)

from eidos.application.handlers import exploration
from eidos.domain import exploration as rules
from eidos.domain.assessment import candidate_lock_job_id
from eidos.domain.errors import Invalid
from eidos.domain.promotion import PORTFOLIO_REF, suggested_lock
from eidos.investigator.context import POLICY_V4, pack

pytestmark = pytest.mark.pg
scene = preparation_lab.scene


def decide(scene, inv, acts, calls, index, *, execute=True):
    env = ActivityEnvironment()
    base = dict(project_id=scene['pid'], investigation_id=inv, index=index)
    base['turn_id'] = env.run(acts.begin_turn, base)['turn_id']
    accepted = scene['lab'].cmd(
        scene['lab'].admin, 'accept_decision', dict(turn_id=base['turn_id'], slot=0, usage_recorded=True,
        proposal=dict(calls=calls, control=dict(action='wait_for_jobs'))), project=scene['pid'])['result']
    if execute and accepted['accepted']:
        for continuation in range(12):
            result = env.run(acts.execute_decision, dict(base, decision_id=accepted['decision_id'], continuation=continuation))
            run_queued(scene, inv)
            if not result.get('pending'):
                assert all(row['ok'] for row in result['summary']), result
                break
        else:
            pytest.fail('decision did not finish')
    env.run(acts.complete_turn, dict(base, stop_reason='waiting', control=dict(action='wait_for_jobs')))
    return accepted


def portfolio_source(scene, inv, client, metrics, *, finish=True):
    """A trusted test launcher records chosen evidence; no database edits or mocked domain state."""
    job = client.command('run_component', dict(investigation_id=inv, component_ref=PORTFOLIO_REF,
        environment='numpy', inputs=scene['arts'], config=dict(mode='portfolio', task_type='classification')),
        project=scene['pid'])['result']['job_id']
    base = client_view(scene, inv)['jobs'][0]
    source = client.query('get_artifact', dict(artifact_id=base['inputs']['source']), project=scene['pid'])
    digest, size = scene['lab'].store.put_bytes(Path(scene['lab'].store.path_for(source['digest'])).read_bytes())
    launcher = scene['lab'].principal('r2-launcher-' + job, ['launcher'], [scene['pid']])
    attempt = scene['lab'].cmd(launcher, 'start_attempt', dict(job_id=job), project=scene['pid'])['result']
    binding = dict(job_id=job, attempt_id=attempt['attempt_id'], epoch=attempt['epoch'])
    scene['lab'].cmd(launcher, 'publish_outputs', dict(**binding, manifest=dict(wire='eidos-wire/1', **binding,
        status='succeeded', outputs=[dict(slot='promotion_source', filename='source.json', digest=digest, size=size,
        media_type='application/json', schema='source-bundle/1')])), project=scene['pid'])
    if finish:
        scene['lab'].cmd(launcher, 'finish_attempt', dict(**binding, status='succeeded', receipt=dict(metrics=metrics)), project=scene['pid'])
    output = next(o for o in client.query('get_job', dict(job_id=job), project=scene['pid'])['outputs'] if o['slot'] == 'promotion_source')
    return output['artifact_id']


@pytest.mark.parametrize('bad', ['unknown', 'other-project', 'wrong-slot', 'unfinished', 'misplaced',
                                  'null-config', 'list-config', 'string-config'])
def test_bad_macro_input_is_correctable_coordinator_refusal(scene, bad):
    inv, acts, client = setup_scene(scene, 'classification')
    source = 'art_nonexistent'
    if bad == 'other-project':
        other = scene['pid'] + '-other'
        scene['lab'].cmd(scene['lab'].admin, 'create_project', dict(project_id=other))
        source = client.upload(other, data=b'{"files":{}}', schema='source-bundle/1')['result']['artifact_id']
    elif bad == 'wrong-slot':
        _, view = fixture_turn(scene, inv, acts, 1)
        source = next(o['artifact_id'] for j in view['jobs'] if j['component'] == PORTFOLIO_REF
                      for o in j['outputs'] if o['slot'] == 'portfolio_table')
    elif bad == 'unfinished':
        source = portfolio_source(scene, inv, client, {}, finish=False)
    calls = [dict(tool='prepare_candidate', args=dict(source=source, seeds=[11, 23]))]
    if bad == 'misplaced':
        calls += [dict(tool='record_note', args=dict(kind='observation', text='after macro'))]
    elif bad.endswith('-config'):
        config = {'null-config': None, 'list-config': [], 'string-config': 'portfolio'}[bad]
        calls = [dict(tool='run_component', args=dict(component_ref=PORTFOLIO_REF, inputs=scene['arts'], config=config)),
                 dict(tool='prepare_candidate', args=dict(source={'$ref': 'call[0].outputs.promotion_source'}, seeds=[11, 23]))]
    before = client.query('list_jobs', dict(investigation_id=inv), project=scene['pid'])
    index = 2 if bad == 'wrong-slot' else 1
    refused = decide(scene, inv, acts, calls, index)
    assert not refused['accepted'] and refused['reason'] == 'invalid', refused
    assert refused['error']['code'] == 'INVALID'
    assert len(client.query('list_jobs', dict(investigation_id=inv), project=scene['pid'])) == len(before)
    # The same investigation can take a corrected decision on its next turn.
    good = decide(scene, inv, acts, [dict(tool='record_note', args=dict(kind='observation', text='corrected proposal'))], index + 1)
    assert good['accepted']
    assert client.query('get_record', dict(kind='investigation', id=inv), project=scene['pid'])['status'] == 'active'


@pytest.mark.parametrize('kind', ['classification', 'regression'])
def test_prepared_lock_line_and_nested_copy_through_real_coordinator(scene, kind):
    inv, acts, client = setup_scene(scene, kind)
    _, view = fixture_turn(scene, inv, acts, 1)
    prepared = suggested_lock(view)
    lines = pack(view, [], policy=POLICY_V4)['text'].splitlines()
    label = next(i for i, line in enumerate(lines) if line.startswith('- Prepared lock text ('))
    lock_text = lines[label + 1]  # Copy the entire emitted line verbatim.
    assert candidate_lock_job_id(dict(text=lock_text)) == prepared['lock']['job_id']
    assert rules.parse_rule(json.loads(lock_text)['selection'], policy_id='exploration-policy/3') == prepared['lock']['selection']
    nested_text = json.dumps(prepared)  # The formerly ambiguous, whole candidate fact.
    assert candidate_lock_job_id(dict(text=nested_text)) is None

    env = ActivityEnvironment()
    base = dict(project_id=scene['pid'], investigation_id=inv, index=2)
    base['turn_id'] = env.run(acts.begin_turn, base)['turn_id']
    for slot, text in enumerate((nested_text, lock_text)):
        accepted = client.command('accept_decision', dict(turn_id=base['turn_id'], slot=slot, usage_recorded=True,
            proposal=dict(calls=[dict(tool='record_note', args=dict(kind='candidate_lock', text=text))],
                          control=dict(action='wait_for_jobs'))), project=scene['pid'])['result']
        assert accepted['accepted'], accepted
        result = env.run(acts.execute_decision, dict(base, decision_id=accepted['decision_id']))
        receipts = client.query('get_record', dict(kind='decision_receipts', id=accepted['decision_id']),
                                project=scene['pid'])['body']['receipts']
        (receipt,) = receipts
        current = client_view(scene, inv)
        notes = [n for n in current['pinned_notes'] if n['kind'] == 'candidate_lock']
        if slot == 0:
            assert not receipt['ok'] and receipt['code'] == 'INVALID', receipt
            assert 'top-level job_id' in receipt['error']['message'] and 'nested lock' in receipt['error']['message']
            assert 'Copy only the JSON line' in receipt['error']['next_action']
            assert result['nothing_outstanding'] and result['failed_calls'][0]['code'] == 'INVALID'
            assert notes == [] and current_state(scene, inv)['incumbent'] is None
            assert current['investigation']['status'] == 'active'
            # A correction can use the next slot in the same turn; no terminal failure or silent note.
        else:
            assert receipt['ok'], receipt
            assert len(notes) == 1 and notes[0]['text'] == lock_text
            assert current_state(scene, inv)['incumbent']['job_id'] == prepared['lock']['job_id']
    env.run(acts.complete_turn, dict(base, stop_reason='waiting', control=dict(action='wait_for_jobs')))


@pytest.mark.parametrize('direction,base,near,far', [('higher_is_better', .8, .81, .85), ('lower_is_better', 1., .99, .95)])
def test_margin_relock_uses_recorded_receipts_and_keeps_rule(lab, direction, base, near, far):
    s = Scene(lab)
    inv = s.investigation(exploration=dict(policy='exploration-policy/3', patience_rounds=20))
    rule = dict(RULE, direction=direction, margin=dict(metric='se', multiple=1))
    first = s.job(inv, metrics=dict(accuracy=base, se=.02), config=dict(depth=1))
    s.lock(inv, first, selection=rule)
    challenger = s.job(inv, metrics=dict(accuracy=near, se=.01), config=dict(depth=2))
    with pytest.raises(Invalid, match='margin'):
        s.lock(inv, challenger)
    assert s.locked_job(inv) == first
    better = s.job(inv, metrics=dict(accuracy=far, se=.01), config=dict(depth=3))
    note = s.lock(inv, better)
    assert note['incumbent']['version'] == 2
    assert json.loads(s.notes(inv, 'candidate_lock')[0]['body']['text'])['selection']['margin'] == rule['margin']
    assert s.locked_job(inv) == better


@pytest.mark.parametrize('kind', ['classification', 'regression'])
def test_reprepare_uses_finished_producer_margin_and_regression_sign(scene, kind):
    inv, acts, client = setup_scene(scene, kind, patience_rounds=20)
    _, view = fixture_turn(scene, inv, acts, 1)
    prepared = suggested_lock(view)
    fixture_turn(scene, inv, acts, 2)
    value, se = prepared['validation']['value'], prepared['validation']['se']
    source = portfolio_source(scene, inv, client, dict(validation_chosen=value if kind == 'classification' else -value,
        se_chosen=se, fit_seconds_chosen=.01, checkpoint_bytes_chosen=100, export_eligible=1))
    calls = [dict(tool='prepare_candidate', args=dict(source=source, seeds=[11, 23]))]
    refused = decide(scene, inv, acts, calls, 3)
    assert not refused['accepted'] and 'margin pre-check' in refused['error']['message']
    improved = value + max(3 * se, .1) * (1 if kind == 'classification' else -1)
    source = portfolio_source(scene, inv, client, dict(validation_chosen=improved if kind == 'classification' else -improved,
        se_chosen=se, fit_seconds_chosen=.01, checkpoint_bytes_chosen=100, export_eligible=1))
    calls[0]['args']['source'] = source
    accepted = decide(scene, inv, acts, calls, 4)
    assert accepted['accepted'], accepted
    view = client_view(scene, inv)
    assert suggested_lock(view)['readiness']['readiness_ok'] == 1
    assert suggested_lock(view)['macro']['basis'] == 'explicit-export'


def current_state(scene, inv, **kwargs):
    with scene['lab'].ledger.transaction() as tx:
        tx.project_id = scene['pid']
        ctx = SimpleNamespace(tx=tx, project_id=scene['pid'])
        return exploration.exploration_state(ctx, tx.require('investigation', inv), **kwargs)


def snapshot(scene, inv):
    """Record real ledger projections, including the production exclusions passed by state()."""
    with scene['lab'].ledger.transaction() as tx:
        tx.project_id = scene['pid']
        ctx = SimpleNamespace(tx=tx, project_id=scene['pid'])
        jobs = exploration._jobs(ctx, inv)
        from eidos.application.handlers.promotion import stamps
        provenance = stamps(ctx, inv)
        for job in jobs:
            if job['id'] in provenance:
                job['macro'] = provenance[job['id']]
        log, notes = exploration.turn_log(ctx, inv, jobs)
        return dict(jobs=jobs, round_log=log, note_turns=notes, notes=exploration._notes(ctx, inv))


def test_multi_round_state_preserves_margin_clearing_repreparation_for_lock(scene, tmp_path):
    inv, acts, client = setup_scene(scene, 'classification', patience_rounds=3)
    baseline = client_view(scene, inv)['jobs'][0]
    rule = dict(metric='validation_accuracy', split='validation', direction='higher_is_better', scorer=COMMAND,
                data=dict(inputs=scene['arts']), margin=dict(metric='validation_accuracy_se', multiple=1), reproduce_within=.1)
    lock = dict(job_id=baseline['id'], selection=rule)
    assert decide(scene, inv, acts, [dict(tool='record_note', args=dict(kind='candidate_lock', text=json.dumps(lock)))], 1)['accepted']
    snapshots = [snapshot(scene, inv)]
    recipe = dict(schema='recipe-spec/1', task_type='classification', steps=[dict(op='per_example_center'), dict(op='standardize')],
                  model=dict(family='logistic', params={}))
    for index, mode in ((2, 'refine'), (3, 'rescore')):
        call = dict(tool='run_component', args=dict(component_ref=PORTFOLIO_REF, environment='numpy', inputs=scene['arts'],
            purpose='reproduction' if mode == 'rescore' else 'open-search',
            config=dict(mode=mode, task_type='classification', add=[recipe], scheme='cv-train', cv_folds=2, cv_repeats=1,
                        budget=dict(max_seconds=60))))
        assert decide(scene, inv, acts, [call], index)['accepted']
        snapshots.append(snapshot(scene, inv))
        st = current_state(scene, inv)
        assert st['rounds_without_improvement'] == index - 1 and st['phase'] == 'explore', st
    view = client_view(scene, inv)
    source = next(o['artifact_id'] for j in reversed(view['jobs']) if j['component'] == PORTFOLIO_REF
                  for o in j['outputs'] if o['slot'] == 'promotion_source')
    accepted = decide(scene, inv, acts, [dict(tool='prepare_candidate', args=dict(source=source, seeds=[11, 23]))], 4)
    assert accepted['accepted'], accepted
    view = client_view(scene, inv)
    st = current_state(scene, inv)
    prepared = suggested_lock(dict(view, exploration=st))
    assert rules.clears_margin(dict(value=prepared['validation']['value'], se=prepared['validation']['se']),
                               st['incumbent']['evidence'], rule)
    assert st['phase'] == 'explore' and st['rounds_without_improvement'] == 2, st
    assert prepared['lock']['job_id'] in st['pending_challengers']
    limited = current_state(scene, inv, completion=dict(remaining_seconds=540, conclude_threshold_seconds=270))
    assert limited['stop'] == 'wall-holdback' and limited['phase'] == 'finalize'
    token_limited = current_state(scene, inv, budgets=dict(model_tokens=dict(limit_amount=100, used=85)))
    assert token_limited['stop'] == 'token-holdback' and token_limited['phase'] == 'finalize'
    snapshots.append(snapshot(scene, inv))
    assert decide(scene, inv, acts, [dict(tool='record_note', args=dict(kind='candidate_lock', text=json.dumps(prepared['lock'])))], 5)['accepted']
    view = client_view(scene, inv)
    assert current_state(scene, inv)['incumbent']['version'] == 2
    assert current_state(scene, inv)['rounds_without_improvement'] == 0
    snapshots.append(snapshot(scene, inv))
    # Optional durable regression corpus: only this synthetic test's development ledger.
    import os
    target = Path(os.environ.get('EIDOS_STEP3_LEDGER_FIXTURES', str(tmp_path))) / 'step3-round-ledgers.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshots, default=str, indent=2) + '\n')


@pytest.mark.parametrize('kind', ['classification', 'regression'])
def test_macro_job_select_export_and_real_evaluator_contract(scene, tmp_path, study_arm4, kind):
    from study.fresh_check import candidate_command
    from study.grade_block import reproduction_command

    inv, acts, client = setup_scene(scene, kind)
    fixture_turn(scene, inv, acts, 1)
    _, view = fixture_turn(scene, inv, acts, 2)
    shown, _, records = study_arm4.ledger(client, scene['pid'], inv, tmp_path / 'transcript.jsonl')
    jid, reason = study_arm4.select_job(client, scene['pid'], shown, records)
    assert jid == suggested_lock(view)['lock']['job_id'] and 'candidate_lock' in reason
    (tmp_path / 'task').mkdir()
    (tmp_path / 'task/requirements.lock').write_text('numpy\nscikit-learn\n')
    name = study_arm4.export_answer(client, scene['pid'], inv, tmp_path, dict(card='development'), jid, reason,
        'Candidate pending independent confirmation', [], dict(input=0, output=0, cache_read=0, cache_write=0), [],
        answer_metadata=dict(status='improvement_claimed', claim_type='candidate'),
        claim_state=dict(state='candidate-submitted'))
    out = tmp_path / 'out'
    answer = json.loads((out / 'answer.json').read_text())
    candidate = answer['candidate']
    assert name == 'model.pt' and candidate['model_path'] == 'candidate/model.pt'
    assert (out / candidate['model_path']).is_file()
    workspace = tmp_path / 'evaluator'
    shutil.copytree(out, workspace / 'candidate')
    inputs = workspace / 'inputs'
    inputs.mkdir()
    for port in ('train', 'validation'):
        client.download(scene['pid'], scene['arts'][port], inputs / (port + '.npz'))
    fresh = workspace / 'out'
    command = reproduction_command(candidate['reproduce_command'], candidate['code_path'], inputs, fresh, 11)
    candidate_command(workspace, command, timeout=120)
    comparison = subprocess.run([
        os.environ['EIDOS_NUMPY_PYTHON'], '-c',
        'import sys; from study.fresh_check import equal_predictions; '
        'raise SystemExit(0 if equal_predictions(sys.argv[1], sys.argv[2]) else 1)',
        str(out / candidate['predictions_path']), str(fresh / 'validation_predictions.npz'),
    ], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert comparison.returncode == 0, comparison.stdout + comparison.stderr
