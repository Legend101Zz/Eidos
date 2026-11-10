"""The Eidos internal investigator, with receipt-based accounting and conservative export."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import threading
import time

from . import lab
from .common import ROOT, classify, digest, envelope, ledger, prepare, prompt, usage, utc, write_json


def stop_investigation(c, project, inv):
    return c.command('stop_investigation', {'investigation_id': inv, 'running_job_policy': 'cancel'},
                     project=project, key='study-outer-stop:' + inv)


def run(run_dir, meta):
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
        # Source is embedded because 1.1.0 JSON output mode has no functional read tools.
        files = {p.relative_to(run_dir / 'task').as_posix(): p.read_text()
                 for p in sorted((run_dir / 'task').rglob('*'))
                 if p.is_file() and p.suffix in ('.py', '.md', '.json', '.lock', '.txt')}
        question = prompt(4, run_dir) + '\n\nPackaged task files:\n' + json.dumps(files, ensure_ascii=False)
        begin = time.monotonic()
        deadline_utc = time.time() + wall
        from .provider_bridge import start as start_bridge
        bridge, bridge_receipt = start_bridge(run_dir, investigator['settings']['base_url'],
                                              deadline_utc, investigator['settings']['request_timeout'])
        investigator['settings']['base_url'] = bridge_receipt['base_url']
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
        uncertain = any(r.get('uncertain') for r in receipts)
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
        assessments = [r['body'] for r in records['assessment']]
        agent_claim = {'investigator': claim, 'assessor': assessments}
        export_answer(c, project, inv, run_dir, meta, selected, reason, claim, assessments, tokens, receipts)
        # Refresh after fallback so the transcript and job receipts include the delivered baseline.
        ledger(c, project, inv, run_dir / 'transcript.jsonl')
        try:
            valid = validate(run_dir / 'out/answer.json')
        except (ValueError, OSError) as exc:
            valid = False
            write_json(run_dir / 'validation_error.json', {'error': str(exc)})
        status, incident = classify(observed or shown, receipts, models, expired.is_set(), valid)
        return {'status': status, 'incident_reason': incident, 'tokens': tokens, 'billed_usd': billed,
                'model_ids_seen': models, 'agent_claim': agent_claim, 'transcript': 'transcript.jsonl',
                'project': project, 'investigation_id': inv, 'selected_job': selected,
                'selection_reason': reason, 'investigation_wall_s': round(time.monotonic() - begin, 3),
                'provider_endpoint': bridge_receipt['upstream'], 'provider_outer_kill': True,
                'usage_uncertain': uncertain,
                'investigator_config_sha': digest(Path(__file__).parent / 'investigator.json')}
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.kill()
            bridge.wait(timeout=10)
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


def export_answer(c, project, inv, run_dir, meta, jid, reason, claim, assessments, tokens, receipts):
    out = run_dir / 'out'
    out.mkdir(exist_ok=True)
    candidate, evidence = None, []
    write_json(out / 'selection_receipt.json', {'job_id': jid, 'reason': reason,
               'investigator_conclusion': claim, 'assessments': assessments})
    if jid:
        job = c.query('get_job', {'job_id': jid}, project=project)
        write_json(out / 'training_receipt.json', job)
        cdir = out / 'candidate'
        cdir.mkdir(exist_ok=True)
        src = (job['lock']['inputs'].get('source') or {}).get('artifact_id')
        if not src:
            src = next(o['artifact_id'] for o in job['outputs'] if o['slot'] == 'source_snapshot')
        bundle_path = out / '.source-bundle.json'
        c.download(project, src, bundle_path)
        files = json.loads(bundle_path.read_text())['files']
        bundle_path.unlink()
        for rel, data in files.items():
            dest = (cdir / rel).resolve()
            if cdir.resolve() not in dest.parents or dest.is_symlink():
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
        shutil.copyfile(run_dir / 'task/requirements.lock', out / 'requirements.lock')
        candidate = {'code_path': 'candidate/project.py', 'code_sha256': digest(cdir / 'project.py'),
            'model_path': 'candidate/model.pt', 'model_sha256': digest(cdir / 'model.pt'),
            'predictions_path': 'candidate/validation_predictions.npz',
            'predictions_sha256': digest(cdir / 'validation_predictions.npz'),
            'environment_lock': 'requirements.lock', 'environment_lock_sha256': digest(out / 'requirements.lock'),
            'reproduce_command': ['python', 'candidate/project.py', 'train', '--inputs', '../task',
                                  '--out', 'reproduced', '--seed', str(job['lock']['config'].get('seed', 11))],
            'parent_checkpoint': None, 'restore_class': 'fresh_initialization'}
    for name, role in [('selection_receipt.json', 'candidate-selection'), ('training_receipt.json', 'training')]:
        if (out / name).exists():
            evidence.append({'path': name, 'sha256': digest(out / name), 'role': role,
                'supports': 'Ledger provenance and conservative candidate selection',
                'limitations': 'Visible validation only; independent evaluator confirmation remains outstanding'})
    cost = {k: None for k in ('elapsed_seconds', 'cpu_core_seconds', 'gpu_device_seconds', 'gpu_type',
                             'model_reasoning_tokens', 'billed_usd')}
    cost.update(model_input_tokens=tokens['input'], model_output_tokens=tokens['output'],
                model_calls=sum(r.get('calls', 0) for r in receipts), receipt_paths=['usage_receipts.json'])
    cost['unknown_fields'] = [k for k, v in cost.items() if v is None]
    write_json(out / 'usage_receipts.json', {'receipts': receipts, 'tokens': tokens})
    answer = {'schema_version': 'eidos-answer/0.2', 'task_id': meta['card'], 'run_id': run_dir.name,
        'status': 'inconclusive' if candidate else 'abstained', 'claim_type': 'no_positive_claim',
        'claim': claim or 'No investigator conclusion was produced. ' + reason,
        'scope': 'Packaged task; visible validation; assessor conclusions preserved in selection_receipt.json.',
        'candidate': candidate, 'measurements': [], 'evidence': evidence,
        'alternative_explanations': ['Training-seed variation', 'Selection on visible validation data'],
        'abstention': {'abstained': candidate is None, 'reason': reason if candidate is None else None,
            'unresolved': ['Independent confirmation on evaluator inputs and fresh training seeds'],
            'next_discriminating_test': 'Run the exported recipe with fresh seeds in the independent evaluator'},
        'cost': cost, 'exposures': ['Packaged task only'],
        'deviations': ['Predictions are visible-validation predictions; evaluator supplies inference inputs separately',
                       'macOS Seatbelt uses the same OS identity; memory limits are best effort',
                       'Eidos 1.1.0 has per-turn deadlines; total wall deadline enforced by stop/cancel watchdog '
                       'and a per-run provider forwarding process that exits at the deadline'],
        'confidence': None}
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
