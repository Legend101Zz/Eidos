"""The Eidos internal investigator, with receipt-based accounting and conservative export."""
from __future__ import annotations

import base64
import json
import shutil
import threading
import time
from pathlib import Path

from . import lab
from .common import ROOT, classify, digest, envelope, ledger, prepare, prompt, usage, utc, write_json

REPRODUCE = '''"""Replay the selected job's exact source, staged inputs and training argv."""
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--out', default='reproduced')
a=p.parse_args()
root=Path(__file__).resolve().parent
out=Path(a.out).resolve()
work=out/'work'
result=out/'outputs'
work.mkdir(parents=True,exist_ok=False)
result.mkdir()
shutil.copytree(root/'candidate'/'source',work,dirs_exist_ok=True)
recipe=json.loads((root/'reproduction.json').read_text())
for item in recipe['inputs']:
    dest=work/item['staged_name']
    dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(root/item['path'],dest)
argv=[x.replace('{work}',str(work)).replace('{out}',str(result)).replace('{seed}',str(recipe['seed']))
      for x in recipe['argv']]
if argv[0]=='python' or Path(argv[0]).name.startswith('python'):
    argv[0]=sys.executable
env=dict(os.environ,OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
for key in list(env):
    if key.endswith('_API_KEY') or key in ('EIDOS_TOKEN','ANTHROPIC_AUTH_TOKEN','PYTHONPATH'):
        env.pop(key,None)
raise SystemExit(subprocess.run(argv,cwd=work,env=env,timeout=recipe['timeout_seconds']).returncode)
'''


def grader_reproduce_command(argv):
    """The selected job's own training argv, in the evaluator's common form: python <code_path> train ...

    Every arm's answer must name candidate.code_path train with --inputs/--out/--seed (study/grade_block.py).
    The recorded job ran `python project.py train --inputs {work} ...` in a staged copy of the same source,
    so only the path and the inputs placeholder are rewritten; reproduce.py remains as extra evidence.
    """
    if len(argv) >= 3 and argv[1] == 'project.py' and argv[2] == 'train':
        rest = [{'{work}': '{inputs}'}.get(x, x) for x in argv[3:]]
        return ['python', 'candidate/source/project.py', 'train', *rest]
    return ['python', 'reproduce.py', '--out', 'reproduced']


# The lab's wall deadline ends this far before the watchdog's, so that a registered deadline is never the later of the two.
WATCHDOG_MARGIN_SECONDS = 5.0


def remaining_wall_seconds(deadline_unix, now=None):
    """Seconds the investigation may run: the watchdog's deadline, as measured right before the investigation starts."""
    return max(1.0, deadline_unix - (time.time() if now is None else now) - WATCHDOG_MARGIN_SECONDS)


def stop_investigation(c, project, inv):
    return c.command('stop_investigation', {'investigation_id': inv, 'running_job_policy': 'cancel'},
                     project=project, key='study-outer-stop:' + inv)


def run(run_dir, meta):
    setup_started_at, setup_begin = utc(), time.monotonic()
    lab.start()
    c = lab.client()
    inv = None
    bridge = None
    try:
        project, imported = prepare(c, run_dir, 4)
        investigator = json.loads((Path(__file__).parent / 'investigator.json').read_text())
        wall, token_cap = envelope(meta)
        investigator['turn_deadline_seconds'] = wall
        # Everything in this context comes from the packaged task and common task prompt.
        # Keep source embedded equally; ADR 0016 also enables JSON-mode read requests.
        files = {p.relative_to(run_dir / 'task').as_posix(): p.read_text()
                 for p in sorted((run_dir / 'task').rglob('*'))
                 if p.is_file() and p.suffix in ('.py', '.md', '.json', '.lock', '.txt')}
        files['METHOD.md'] = (ROOT / 'study/METHOD.md').read_text()
        question = prompt(4, run_dir) + '\n\nPackaged task files:\n' + json.dumps(files, ensure_ascii=False)
        begin = time.monotonic()
        deadline_utc = time.time() + wall
        began_at = utc()
        write_json(run_dir / 'setup.json', {'setup_started_at': setup_started_at,
                   'setup_ended_at': began_at, 'setup_wall_s': round(begin - setup_begin, 3),
                   'setup_complete': True})
        from .provider_bridge import start as start_bridge
        bridge, bridge_receipt = start_bridge(run_dir, investigator['settings']['base_url'],
                                              deadline_utc, investigator['settings']['request_timeout'])
        investigator['settings']['base_url'] = bridge_receipt['base_url']
        # ADR 0020: completion state, the conclusion's reserve and the "conclude now" turn, inside the watchdog's envelope.
        investigator['wall_seconds'] = remaining_wall_seconds(deadline_utc)
        investigator['wall_deadline_unix'] = deadline_utc - WATCHDOG_MARGIN_SECONDS
        r = c.command('start_investigation', {
            'question': question, 'charter_id': imported['charter_id'], 'environment': 'default',
            'inputs': imported['artifacts'], 'task': imported['task'], 'investigator': investigator,
            'limits': {'model_tokens': token_cap, 'turns': 10000,
                       'compute_seconds': 100000000, 'trials': 1000000},
            'notes': f'Study deadline unix={deadline_utc}; wall_seconds={wall}. '
                     'Outer controller cancels the investigation and jobs at this deadline.',
        }, project=project)
        inv = r['result']['investigation_id']
        write_json(run_dir / 'investigation.json', {'project': project, 'investigation_id': inv,
                   'deadline_unix': deadline_utc, 'investigator': investigator, 'started_at': utc()})
        done, expired = threading.Event(), threading.Event()
        stop_error = []

        def watchdog():
            if done.wait(max(0, wall - (time.monotonic() - begin))):
                return
            expired.set()
            other = lab.client()
            try:
                stop_investigation(other, project, inv)
            except Exception as exc:
                stop_error.append(str(exc))
            finally:
                other.close()

        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        observed = {}
        try:
            while not expired.is_set():
                lab.guard_disk()
                shown = c.query('get_investigation', {'investigation_id': inv}, project=project)
                observed = shown
                write_json(run_dir / 'investigation_latest.json', shown)
                if shown['status'] != 'active' or shown.get('needs_input'):
                    break
                time.sleep(min(2, max(.05, wall - (time.monotonic() - begin))))
        finally:
            done.set()
            watcher.join(timeout=20)
            # A parked investigator must not resume after this run has returned.
            stop_investigation(c, project, inv)
        shown, events, records = ledger(c, project, inv, run_dir / 'transcript.jsonl')
        tokens, models, billed, receipts = usage(events, run_dir / 'transcript.jsonl')
        uncertain = any(r.get('uncertain') for r in receipts) or (
            expired.is_set() and any(t['status'] == 'open' for t in shown['turns']))
        write_json(run_dir / 'usage_receipts.json', {
            'receipts': receipts, 'tokens': tokens, 'model_ids_seen': models,
            'cache_note': 'Cache breakdown recovered from persisted model responses; input excludes those '
                          'cached tokens. Failed requests without raw responses have unknown cache breakdown.',
            'deadline_stop_errors': stop_error,
            'uncertain_usage': uncertain,
        })
        selected, reason = select_job(c, project, shown, records)
        if selected is None and not expired.is_set() and time.monotonic() - begin < wall - 15:
            # Explicit operational fallback, using only the supplied baseline command.
            selected = baseline(c, project, inv, imported, wall - (time.monotonic() - begin))
            reason = 'operator baseline fallback: investigator produced no exportable locked candidate'
            write_json(run_dir / 'baseline_fallback.json', {'job_id': selected, 'reason': reason})
        conclusion = (shown.get('body') or {}).get('conclusion') or {}
        claim = conclusion.get('summary') or ''
        conclusion_scope = conclusion.get('scope') or {}
        claim_state = conclusion.get('claim_state')
        answer_metadata = conclusion_scope.get('answer') if isinstance(conclusion_scope, dict) else None
        assessments = [r['body'] for r in records['assessment']]
        agent_claim = {'investigator': claim, 'assessor': assessments}
        export_error = None
        try:
            export_answer(c, project, inv, run_dir, meta, selected, reason, claim, assessments, tokens, receipts,
                          usage_uncertain=uncertain, answer_metadata=answer_metadata, claim_state=claim_state)
        except (ValueError, TypeError) as exc:
            export_error = str(exc)
        # Refresh after fallback so the transcript and job receipts include the delivered baseline.
        ledger(c, project, inv, run_dir / 'transcript.jsonl')
        try:
            if export_error:
                raise ValueError(export_error)
            valid = validate(run_dir / 'out/answer.json')
        except (ValueError, OSError) as exc:
            valid = False
            write_json(run_dir / 'validation_error.json', {'error': str(exc)})
        status, incident = classify(observed or shown, receipts, models, expired.is_set(), valid)
        controller_abstention = (shown.get('body') or {}).get('controller_abstention')
        return {'status': status, **({'controller_abstention': controller_abstention} if controller_abstention else {}), 'incident_reason': incident, 'tokens': tokens, 'billed_usd': billed,
                'started_at': began_at,
                'model_ids_seen': models, 'agent_claim': agent_claim, 'transcript': 'transcript.jsonl',
                'project': project, 'investigation_id': inv, 'selected_job': selected,
                'selection_reason': reason, 'investigation_wall_s': round(time.monotonic() - begin, 3),
                'provider_endpoint': bridge_receipt['upstream'], 'provider_outer_kill': True,
                'usage_uncertain': uncertain,
                'evidence_plan_count': len(records['evidence_plan']),
                'assessment_count': len(assessments),
                'investigator_config_sha': digest(Path(__file__).parent / 'investigator.json')}
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.kill()
            bridge.wait(timeout=10)
        if bridge is not None:
            write_json(run_dir / 'provider_bridge_exit.json', {'pid': bridge.pid, 'returncode': bridge.returncode,
                       'deadline_reached': bridge.returncode == 124})
        if inv:
            try:
                stop_investigation(c, project, inv)
            except Exception:
                pass
        c.close()


def select_job(c, project, shown, records):
    """Prefer explicit lock, then assessor-confirmed candidate; otherwise the unedited baseline.

    Never choose an unconfirmed search candidate merely because it was the last job.
    """
    jobs = {j['id']: c.query('get_job', {'job_id': j['id']}, project=project)
            for j in shown['jobs'] if j['status'] == 'succeeded'}
    eligible = {jid: j for jid, j in jobs.items()
                if any(o.get('role') == 'model-export' for o in j.get('outputs', []))
                and any(o.get('slot') == 'validation_predictions' for o in j.get('outputs', []))}
    notes = sorted(records['note'], key=lambda r: str(r.get('created_at', '')), reverse=True)
    for note in notes:
        if note['body'].get('kind') != 'candidate_lock':
            continue
        try:
            jid = json.loads(note['body']['text'])['job_id']
            if jid in eligible:
                return jid, 'explicit candidate_lock note ' + note['id']
        except (ValueError, KeyError, TypeError):
            continue
    plans = {p['id']: p for p in records['evidence_plan']}
    for assessment in records['assessment']:
        b = assessment['body']
        if b.get('outcome') != 'protocol-passed' or b.get('conformance') != 'conformant' or b.get('health') != 'intact':
            continue
        plan = (plans.get(b.get('plan_id')) or {}).get('body') or {}
        for arm in plan.get('arms', []):
            if arm.get('role') != 'candidate':
                continue
            for jid in (plan.get('launch') or {}).get('jobs', {}).get(arm['id'], []):
                if jid in eligible:
                    return jid, 'assessor-confirmed candidate ' + assessment['id']
    for jid, job in sorted(eligible.items(), key=lambda kv: str(kv[1].get('created_at', ''))):
        if not job['lock']['inputs'].get('source'):
            return jid, 'reproduced unedited baseline; no locked or confirmed candidate'
    return None, 'no exportable locked candidate or reproduced baseline'


def baseline(c, project, inv, imported, remaining):
    # The investigation is stopped, so this explicitly attributed fallback is project-budgeted.
    wall = max(1, int(remaining - 5))
    r = c.command('run_component', {'component_ref': 'eidos.project.command@1.0.7',
        'inputs': imported['artifacts'], 'environment': 'default', 'purpose': 'reproduction',
        'wall_seconds': wall, 'config': {'argv': ['python', 'project.py', 'train', '--inputs',
        '{work}', '--out', '{out}', '--seed', '{seed}'], 'seed': 11, 'timeout_seconds': wall}},
        project=project, key='study-baseline-fallback:' + inv)
    jid = r['result']['job_id']
    end = time.monotonic() + wall
    while time.monotonic() < end:
        job = c.query('get_job', {'job_id': jid}, project=project)
        if job['status'] == 'succeeded':
            return jid
        if job['status'] in ('failed', 'cancelled', 'pruned'):
            return None
        time.sleep(1)
    c.command('cancel_run', {'job_id': jid}, project=project, key='cancel-fallback:' + inv)
    return None


CONFIRMATION_PENDING = 'pending independent confirmation'
DEFAULT_SCOPE = 'Packaged task; visible validation; assessor conclusions preserved in selection_receipt.json.'


def map_claim_state(claim_state, metadata, candidate, measurements):
    """Map a conclusion's evidence-derived claim state (ADR 0021) onto the eidos-answer/0.2 status fields.

    Returns (status, claim_type, scope, deviations). A conclusion without a claim state (recorded before ADR 0021)
    keeps the model's own fields. The independent evaluator still decides: this states what the lab evidence shows.
    """
    status = metadata.get('status', 'inconclusive' if candidate else 'abstained')
    claim_type = metadata.get('claim_type', 'no_positive_claim')
    scope = metadata.get('scope', DEFAULT_SCOPE)
    if not isinstance(claim_state, dict) or claim_state.get('state') in (None, 'no-claim'):
        return status, claim_type, scope, []
    state = claim_state['state']
    # An abstention or a null the model reported wins over any other field, including an older claim state.
    if status in ('abstained', 'no_improvement_found'):
        return status, 'no_positive_claim', scope, [
            f'Model status {status!r} kept: an abstention or null takes precedence over the claim state {state!r}']
    estimate = (claim_state.get('point_estimate') or {}).get('value')
    positive = isinstance(estimate, (int, float)) and not isinstance(estimate, bool) and estimate > 0
    deviations = [f'Status mapped from the conclusion claim state {state!r} (ADR 0021); model status was '
                  f'{metadata.get("status")!r}; the independent evaluator still decides']
    if state in ('protocol-passed', 'candidate-submitted') and candidate is not None and not measurements:
        return 'inconclusive', 'no_positive_claim', scope, deviations + [
            f'Claim state {state!r} but no recorded measurement, so no improvement is claimed in this answer']
    if state == 'protocol-passed':
        return 'improvement_claimed', 'recipe_improvement', (
            f"Protocol-passed under {claim_state.get('success_rule_ref')} (assessment {claim_state.get('assessment_id')}); "
            f"{scope}"), deviations
    if state == 'candidate-submitted':
        if not positive:
            return 'inconclusive', 'no_positive_claim', scope, deviations + [
                'Candidate submitted without a positive point estimate; no improvement is claimed']
        return 'improvement_claimed', 'recipe_improvement', f'{scope}; {CONFIRMATION_PENDING}', deviations
    if state in ('not-supported', 'null-reported'):
        return 'no_improvement_found', 'no_positive_claim', scope, deviations
    if state == 'abstained':
        return 'abstained', 'no_positive_claim', scope, deviations
    return status, claim_type, scope, deviations + [f'Unknown claim state {state!r}; model status kept']


def export_answer(c, project, inv, run_dir, meta, jid, reason, claim, assessments, tokens, receipts,
                  usage_uncertain=False, answer_metadata=None, claim_state=None):
    out = run_dir / 'out'
    out.mkdir(exist_ok=True)
    candidate, evidence = None, []
    if answer_metadata is None:
        answer_metadata = {}
    if not isinstance(answer_metadata, dict):
        raise ValueError('conclusion scope.answer must be an object')
    for field in ('measurements', 'exposures', 'deviations'):
        if field in answer_metadata and not isinstance(answer_metadata[field], list):
            raise ValueError('conclusion answer.' + field + ' must be an array')
    write_json(out / 'selection_receipt.json', {'job_id': jid, 'reason': reason,
               'investigator_conclusion': claim, 'assessments': assessments, 'claim_state': claim_state})
    if jid:
        job = c.query('get_job', {'job_id': jid}, project=project)
        write_json(out / 'training_receipt.json', job)
        cdir = out / 'candidate'
        cdir.mkdir(exist_ok=True)
        source_dir = cdir / 'source'
        source_dir.mkdir(exist_ok=True)
        src = (job['lock']['inputs'].get('source') or {}).get('artifact_id')
        if not src:
            src = next(o['artifact_id'] for o in job['outputs'] if o['slot'] == 'source_snapshot')
        bundle_path = out / '.source-bundle.json'
        c.download(project, src, bundle_path)
        files = json.loads(bundle_path.read_text())['files']
        bundle_path.unlink()
        for rel, data in files.items():
            dest = (source_dir / rel).resolve()
            if source_dir.resolve() not in dest.parents or dest.is_symlink():
                raise ValueError('source bundle path escapes candidate directory')
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, dict):
                dest.write_bytes(base64.b64decode(data['base64']))
            else:
                dest.write_text(data)
        model = next(o['artifact_id'] for o in job['outputs'] if o.get('role') == 'model-export')
        pred = next(o['artifact_id'] for o in job['outputs'] if o['slot'] == 'validation_predictions')
        c.download(project, model, cdir / 'model.pt')
        c.download(project, pred, cdir / 'validation_predictions.npz')
        shutil.copyfile(run_dir / 'task/requirements.lock', out / 'task-requirements.lock')
        installed = job['lock']['environment'].get('installed') or {}
        if installed:
            (out / 'requirements.lock').write_text(''.join(f'{name}=={version}\n'
                                                  for name, version in sorted(installed.items())))
        else:
            shutil.copyfile(run_dir / 'task/requirements.lock', out / 'requirements.lock')
        recipe = {'argv': job['lock']['config']['argv'],
                  'seed': job['lock']['config'].get('seed', job['lock'].get('seed') or 0),
                  'timeout_seconds': job['lock']['config'].get('timeout_seconds', 600), 'inputs': []}
        inputs_dir = out / 'reproduction_inputs'
        inputs_dir.mkdir(exist_ok=True)
        parent_checkpoint = None
        extensions = {'application/x-npz': '.npz', 'application/json': '.json', 'text/csv': '.csv',
                      'text/plain': '.txt'}
        for port, binding in job['lock']['inputs'].items():
            if port == 'source':
                continue
            name = (job['lock']['config'].get('files') or {}).get(port)
            if not name:
                suffix = '.pt' if binding.get('schema') == 'model-file/1' else extensions.get(binding.get('media_type'), '')
                name = port + suffix
            # Match the project's staging rule without permitting an export path escape.
            if Path(name).name != name or name in ('', '.', '..') or '/' in port or '\\' in port:
                raise ValueError('unsafe reproduction input name')
            target = inputs_dir / (port + Path(name).suffix)
            c.download(project, binding['artifact_id'], target)
            if binding.get('schema') == 'model-file/1' and parent_checkpoint is None:
                parent_checkpoint = target.relative_to(out).as_posix()
            recipe['inputs'].append({'path': target.relative_to(out).as_posix(),
                                    'staged_name': name, 'sha256': digest(target)})
        write_json(out / 'reproduction.json', recipe)
        (out / 'reproduce.py').write_text(REPRODUCE)
        candidate = {'code_path': 'candidate/source/project.py', 'code_sha256': digest(source_dir / 'project.py'),
            'model_path': 'candidate/model.pt', 'model_sha256': digest(cdir / 'model.pt'),
            'predictions_path': 'candidate/validation_predictions.npz',
            'predictions_sha256': digest(cdir / 'validation_predictions.npz'),
            'environment_lock': 'requirements.lock', 'environment_lock_sha256': digest(out / 'requirements.lock'),
            'reproduce_command': grader_reproduce_command(recipe['argv']),
            'parent_checkpoint': parent_checkpoint,
            'restore_class': 'checkpoint_continuation' if parent_checkpoint else 'fresh_initialization'}
    for name, role in [('selection_receipt.json', 'candidate-selection'), ('training_receipt.json', 'training')]:
        if (out / name).exists():
            evidence.append({'path': name, 'sha256': digest(out / name), 'role': role,
                'supports': 'Ledger provenance and conservative candidate selection',
                'limitations': 'Visible validation only; independent evaluator confirmation remains outstanding'})
    for name in ('reproduction.json', 'reproduce.py'):
        if (out / name).exists():
            evidence.append({'path': name, 'sha256': digest(out / name), 'role': 'reproduction',
                'supports': 'Exact selected-job argv, source snapshot and staged input replay',
                'limitations': 'Requires the recorded Python environment; one training seed'})
    cost = {k: None for k in ('elapsed_seconds', 'cpu_core_seconds', 'gpu_device_seconds', 'gpu_type',
                             'model_reasoning_tokens', 'billed_usd')}
    cost.update(model_input_tokens=tokens['input'] + tokens['cache_read'] + tokens['cache_write'],
                model_output_tokens=tokens['output'],
                model_calls=sum(r.get('calls', 0) for r in receipts), receipt_paths=['usage_receipts.json'])
    if usage_uncertain:
        for field in ('model_input_tokens', 'model_output_tokens', 'model_calls'):
            cost[field] = None
    cost['unknown_fields'] = [k for k, v in cost.items() if v is None]
    write_json(out / 'usage_receipts.json', {'receipts': receipts, 'tokens': tokens,
               'usage_uncertain': usage_uncertain})
    measurements = []
    for measurement in answer_metadata.get('measurements', []):
        if not isinstance(measurement, dict):
            raise ValueError('conclusion measurements must contain objects')
        m = dict(measurement)
        reference = m.get('evidence_receipt', '')
        if isinstance(reference, str) and reference.startswith('job_'):
            try:
                record = c.query('get_job', {'job_id': reference}, project=project)
            except Exception as exc:
                if getattr(exc, 'status', None) == 404:
                    raise ValueError('measurement cites an unknown job: ' + reference) from exc
                raise
            filename = 'evidence/' + reference + '.json'
            write_json(out / filename, record)
            m['evidence_receipt'] = filename
            if not any(e['path'] == filename for e in evidence):
                evidence.append({'path': filename, 'sha256': digest(out / filename),
                    'role': 'investigator-cited-job', 'supports': 'Ledger receipt cited by investigator measurement',
                    'limitations': 'Investigator interpretation; confirmation and selection roles remain as recorded'})
        measurements.append(m)
    status, claim_type, answer_scope, state_deviations = map_claim_state(claim_state, answer_metadata, candidate, measurements)
    if candidate is None:
        status = 'abstained'
    if status == 'abstained':
        claim_type = 'no_positive_claim'
    abstention = answer_metadata.get('abstention')
    if not isinstance(abstention, dict) or abstention.get('abstained') != (status == 'abstained'):
        abstention = {
            'abstained': status == 'abstained', 'reason': reason if status == 'abstained' else None,
            'unresolved': ['Independent confirmation on evaluator inputs and fresh training seeds'],
            'next_discriminating_test': 'Run the exported recipe with fresh seeds in the independent evaluator'}
    answer = {'schema_version': 'eidos-answer/0.2', 'task_id': meta['card'], 'run_id': run_dir.name,
        'status': status, 'claim_type': claim_type,
        'claim': answer_metadata.get('claim') or claim or 'No investigator conclusion was produced. ' + reason,
        'scope': answer_scope,
        'candidate': None if status == 'abstained' else candidate, 'measurements': measurements, 'evidence': evidence,
        'alternative_explanations': answer_metadata.get('alternative_explanations', ['Training-seed variation', 'Selection on visible validation data']),
        'abstention': abstention,
        'cost': cost, 'exposures': ['Packaged task only', *answer_metadata.get('exposures', [])],
        'deviations': ['Predictions are visible-validation predictions; evaluator supplies inference inputs separately',
                       'requirements.lock records actual registered packages; original supplied lock retained as task-requirements.lock',
                       'macOS Seatbelt uses the same OS identity; memory limits are best effort',
                       'Eidos 1.1.0 has per-turn deadlines; total wall deadline enforced by stop/cancel watchdog '
                       'and a per-run provider forwarding process that exits at the deadline',
                       *state_deviations, *answer_metadata.get('deviations', [])],
        'confidence': answer_metadata.get('confidence')}
    if answer['confidence'] is not None:
        answer['confidence_outcome'] = answer_metadata.get('confidence_outcome')
    write_json(out / 'answer.json', answer)


def validate(path):
    import importlib.util
    validator = ROOT / 'research/public/development/validate_answer.py'
    spec = importlib.util.spec_from_file_location('pilot_answer_validator', validator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = json.loads(path.read_text())
    module.validate(answer)
    out = path.parent.resolve()

    def artifact(rel, expected=None):
        target = (out / rel).resolve()
        if out not in target.parents or not target.is_file():
            raise ValueError(f'answer artifact is missing or escapes out/: {rel}')
        if expected is not None and digest(target) != expected:
            raise ValueError(f'answer artifact hash mismatch: {rel}')

    candidate = answer.get('candidate')
    if candidate:
        for name in ('code', 'model', 'predictions'):
            artifact(candidate[name + '_path'], candidate[name + '_sha256'])
        artifact(candidate['environment_lock'], candidate['environment_lock_sha256'])
        if candidate.get('parent_checkpoint'):
            artifact(candidate['parent_checkpoint'])
    for evidence in answer['evidence']:
        artifact(evidence['path'], evidence['sha256'])
    for receipt in answer['cost']['receipt_paths']:
        artifact(receipt)
    return True
