"""The Eidos internal investigator, with receipt-based accounting and conservative export."""
from __future__ import annotations

import base64
import json
import math
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import lab
from .common import (PROJECT_COMMAND_BUNDLE_SHA256, PROJECT_COMMAND_REF, ROOT, classify, digest,
                     envelope, ledger, prepare, prompt, usage, utc, write_json)

# The study owns the optional answer shape. The coordinator only understands the generic scope contract.
_ANSWER_TEXT = {'type': 'string', 'pattern': r'\S'}
_NULLABLE_ANSWER_TEXT = {'anyOf': [_ANSWER_TEXT, {'enum': [None]}]}
CONCLUSION_SCOPE_SCHEMA = {
    'type': 'object',
    'properties': {'answer': {
        'type': 'object',
        'properties': {'abstention': {
            'type': 'object',
            'required': ['abstained', 'reason', 'unresolved', 'next_discriminating_test'],
            'properties': {
                'abstained': {'type': 'boolean'},
                'reason': _NULLABLE_ANSWER_TEXT,
                'unresolved': {'type': 'array', 'items': _ANSWER_TEXT},
                'next_discriminating_test': _NULLABLE_ANSWER_TEXT,
            },
            'anyOf': [
                {'type': 'object', 'properties': {'abstained': {'enum': [False]}}},
                {'type': 'object', 'properties': {
                    'abstained': {'enum': [True]},
                    'reason': _ANSWER_TEXT,
                    'unresolved': {'type': 'array', 'minItems': 1},
                    'next_discriminating_test': _ANSWER_TEXT,
                }},
            ],
        }},
    }},
}


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


def observed_deadline(outer_deadline, investigation):
    registered = ((investigation.get('body') or {}).get('wall') or {}).get('deadline_unix')
    if type(registered) not in (int, float) or not math.isfinite(registered):
        return {'registered_lab_deadline_unix': None, 'registered_margin_s': None}
    return {'registered_lab_deadline_unix': registered, 'registered_margin_s': outer_deadline - registered}


def stop_investigation(c, project, inv):
    return c.command('stop_investigation', {'investigation_id': inv, 'running_job_policy': 'cancel'},
                     project=project, key='study-outer-stop:' + inv)


def finalization_read_coverage(receipts):
    reports = [receipt.get("structured_output") or {} for receipt in receipts if receipt.get("phase") == "finalization"]
    if any(report.get("read_rounds_forfeited") is True for report in reports):
        return "forfeited"
    if not reports or any(report.get("read_rounds_forfeited") is not False for report in reports):
        return None
    if all(report.get("finalization_read_round_limit") == 0 for report in reports):
        return "no_read_rounds_available"
    return "not_forfeited"


def investigator_abstention_reason(shown, status):
    conclusion = (shown.get("body") or {}).get("conclusion") or {}
    if status != "ok" or (conclusion.get("claim_state") or {}).get("state") != "abstained":
        return None
    recovery = shown.get("finalization_export_receipt")
    ordinary = shown.get("finalization_phase") or {}
    if recovery:
        if recovery.get("investigator_concluded") is not True or "locked_candidate" not in recovery:
            return None
        locked = recovery["locked_candidate"]
        if locked is not None and (not isinstance(locked, dict)
                or locked.get("status") not in {"admitted", "running", "failed", "cancelled", "pruned"}):
            return None
    elif not ordinary or "incumbent" not in conclusion or conclusion["incumbent"] is not None:
        return None
    triggers = {ordinary.get("trigger"), (shown.get("finalization") or {}).get("trigger")}
    boundary = {"exploration-boundary", "exploration-token-holdback", "ordinary-finalization-time-exhausted"}
    return "no-eligible-lock-at-boundary" if triggers & boundary else "no-eligible-lock-at-finalization"


def run(run_dir, meta, *, eval_audit_config=None):
    setup_started_at, setup_begin = utc(), time.monotonic()
    lab.start()
    c = lab.client()
    inv = None
    bridge = None
    try:
        project, imported = prepare(c, run_dir, 4)
        investigator = json.loads((Path(__file__).parent / 'investigator.json').read_text())
        investigator['conclusion_scope_schema'] = CONCLUSION_SCOPE_SCHEMA
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
        wall_start_unix = time.time()
        deadline_utc = wall_start_unix + wall
        began_at = utc()
        write_json(run_dir / 'setup.json', {'setup_started_at': setup_started_at,
                   'setup_ended_at': began_at, 'setup_wall_s': round(begin - setup_begin, 3),
                   'setup_complete': True})
        from .provider_bridge import start as start_bridge
        bridge_start_unix = time.time()
        bridge, bridge_receipt = start_bridge(run_dir, investigator['settings']['base_url'],
                                              deadline_utc, investigator['settings']['request_timeout'],
                                              eval_audit_config=eval_audit_config)
        bridge_ready_unix = time.time()
        if eval_audit_config is not None and bridge_receipt.get('eval_provider_audit') is not True:
            raise RuntimeError('required provider audit did not activate')
        investigator['settings']['base_url'] = bridge_receipt['base_url']
        # ADR 0020: completion state, the conclusion's reserve and the "conclude now" turn, inside the watchdog's envelope.
        investigator['wall_seconds'] = remaining_wall_seconds(deadline_utc)
        investigator['wall_deadline_unix'] = deadline_utc - WATCHDOG_MARGIN_SECONDS
        from study.common import record_live_delivery
        record_live_delivery(run_dir, meta, question, 'investigation-question')
        start_request_unix = time.time()
        r = c.command('start_investigation', {
            'question': question, 'charter_id': imported['charter_id'], 'environment': 'default',
            'inputs': imported['artifacts'], 'task': imported['task'], 'investigator': investigator,
            'limits': {'model_tokens': token_cap, 'turns': 10000,
                       'compute_seconds': 100000000, 'trials': 1000000},
            'notes': f'Study deadline unix={deadline_utc}; wall_seconds={wall}. '
                     'Outer controller cancels the investigation and jobs at this deadline.',
        }, project=project)
        start_response_unix = time.time()
        inv = r['result']['investigation_id']
        investigation_path = run_dir / 'investigation.json'
        deadline_evidence = {
            'wall_start_unix': wall_start_unix,
            'outer_deadline_unix': deadline_utc,
            'requested_lab_deadline_unix': investigator['wall_deadline_unix'],
            'requested_lab_wall_seconds': investigator['wall_seconds'],
            'bridge_startup_s': bridge_ready_unix - bridge_start_unix,
            'start_request_unix': start_request_unix,
            'start_response_unix': start_response_unix,
            **observed_deadline(deadline_utc, {}),
        }
        investigation_record = {'project': project, 'investigation_id': inv, 'deadline_unix': deadline_utc,
                                'investigator': investigator, 'started_at': utc(),
                                'deadline_evidence': deadline_evidence}
        write_json(investigation_path, investigation_record)
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
                if not observed:
                    deadline_evidence.update(observed_deadline(deadline_utc, shown))
                    write_json(investigation_path, investigation_record)
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
        if deadline_evidence['registered_lab_deadline_unix'] is None:
            deadline_evidence.update(observed_deadline(deadline_utc, shown))
            write_json(investigation_path, investigation_record)
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
        provenance = candidate_provenance(c, project, shown, selected)
        write_json(run_dir / 'candidate_provenance.json', provenance)
        conclusion = (shown.get('body') or {}).get('conclusion') or {}
        claim = conclusion.get('summary') or ''
        conclusion_scope = conclusion.get('scope') or {}
        claim_state = conclusion.get('claim_state')
        answer_metadata = conclusion_scope.get('answer') if isinstance(conclusion_scope, dict) else None
        assessments = [r['body'] for r in records['assessment']]
        agent_claim = {'investigator': claim, 'assessor': assessments}
        export_error = None
        checkpoint_basename = None
        try:
            checkpoint_basename = export_answer(c, project, inv, run_dir, meta, selected, reason, claim, assessments, tokens, receipts,
                          usage_uncertain=uncertain, answer_metadata=answer_metadata, claim_state=claim_state,
                          task_artifacts=imported['artifacts'])
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
        return {'status': status, 'finalization_read_coverage': finalization_read_coverage(receipts),
                'investigator_abstention_reason': investigator_abstention_reason(shown, status),
                **({'controller_abstention': controller_abstention} if controller_abstention else {}), 'incident_reason': incident, 'tokens': tokens, 'billed_usd': billed,
                'started_at': began_at,
                'model_ids_seen': models, 'agent_claim': agent_claim, 'transcript': 'transcript.jsonl',
                'project': project, 'investigation_id': inv, 'selected_job': selected,
                'selection_reason': reason, 'candidate_provenance': provenance, 'checkpoint_basename': checkpoint_basename, 'investigation_wall_s': round(time.monotonic() - begin, 3),
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


def candidate_provenance(c, project, shown, selected):
    """Attribute selection from accepted ledger calls, never from an authored claim."""
    if selected is None:
        return dict(job_id=None, preparation='none', basis=None)
    for turn in shown.get('turns') or []:
        detail = c.query('get_turn', dict(turn_id=turn['id']), project=project)
        for decision in detail.get('decisions') or []:
            if not decision.get('accepted') or decision.get('revoked'):
                continue
            calls = (decision.get('body') or {}).get('calls') or []
            try:
                record = c.query('get_record', dict(kind='decision_receipts', id=decision['id']), project=project)
            except Exception as exc:
                if getattr(exc, 'status', None) == 404:
                    continue
                raise
            for receipt in (record.get('body') or {}).get('receipts') or []:
                if receipt.get('ok') and (receipt.get('result') or {}).get('job_id') == selected:
                    index = receipt.get('index')
                    macro = calls[index].get('macro') if isinstance(index, int) and 0 <= index < len(calls) else None
                    return dict(job_id=selected, preparation='prepare_candidate' if macro else 'authored',
                                basis=(macro or {}).get('basis'), decision_id=decision['id'], macro=macro)
    return dict(job_id=selected, preparation='baseline-or-direct', basis=None)


def select_job(c, project, shown, records):
    """Prefer explicit lock, then assessor-confirmed candidate; otherwise the unedited baseline.

    Never choose an unconfirmed search candidate merely because it was the last job.
    """
    jobs = {j['id']: c.query('get_job', {'job_id': j['id']}, project=project)
            for j in shown['jobs'] if j['status'] == 'succeeded'}
    eligible = {jid: j for jid, j in jobs.items()
                if any(o.get('role') == 'model-export' for o in j.get('outputs', []))
                and any(o.get('slot') == 'validation_predictions' for o in j.get('outputs', []))}
    null_result = (((shown.get('body') or {}).get('conclusion') or {}).get('claim_state') or {}).get('state') in ('not-supported', 'null-reported', 'abstained')
    if null_result and ((shown.get('body') or {}).get('investigator', {}).get('exploration') or {}).get('policy') == 'exploration-policy/3':
        eligible = {jid: job for jid, job in eligible.items() if not job['lock']['inputs'].get('source')}
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
    r = c.command('run_component', {'component_ref': PROJECT_COMMAND_REF,
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


_SWEEP_ARTIFACT = re.compile(r'art_([a-f0-9]{16})_evidence_table')
_SWEEP_COMPONENT = 'eidos.search.recipe-sweep@1.1.0'
_SWEEP_BUNDLE_SHA = '22068c870b4463189e8dfde0266a6b695b29858304a71f456d842a870e023e07'
_SWEEP_SCHEMA = 'recipe-sweep-table/1'
_SWEEP_SLOT = 'evidence_table'


def export_sweep_evidence(c, project, inv, out, reference, task_artifacts, authored_evidence=()):
    """Export only the explicitly pinned sweep or portfolio table with verified visible provenance."""
    portfolio = reference.endswith('_portfolio_table')
    component_ref = 'eidos.search.recipe-portfolio@1.0.0' if portfolio else _SWEEP_COMPONENT
    bundle_sha = '4b02e49fd939d0856c16b2adee42ad51249eca19eb27cad4c3c523202dfdc3a0' if portfolio else _SWEEP_BUNDLE_SHA
    schema = 'recipe-portfolio-table/1' if portfolio else _SWEEP_SCHEMA
    slot = 'portfolio_table' if portfolio else _SWEEP_SLOT
    match = re.fullmatch(r'art_([a-f0-9]{16})_portfolio_table', reference) if portfolio else _SWEEP_ARTIFACT.fullmatch(reference)
    splits = ('train', 'validation')
    if not match or not task_artifacts or not all(task_artifacts.get(split) for split in splits):
        raise ValueError('measurement artifact is outside the recipe-sweep export policy')
    job_id = 'job_' + match.group(1)
    try:
        job = c.query('get_job', {'job_id': job_id}, project=project)
        art = c.query('get_artifact', {'artifact_id': reference}, project=project)
        release = c.query('describe_component', {'component_ref': component_ref}, project=project)
    except Exception as exc:
        if getattr(exc, 'status', None) in (403, 404):
            raise ValueError('measurement artifact or its provenance is unavailable') from exc
        raise
    lock = job.get('lock') or {}
    inputs = lock.get('inputs') or {}
    if portfolio and 'task' in inputs:
        splits += ('task',)
    component = lock.get('component') or {}
    release_body = release.get('body') or {}
    config = lock.get('config') or {}
    outputs = [row for row in job.get('outputs', []) if row.get('artifact_id') == reference]
    size, sha = art.get('size'), art.get('digest')
    expected = {'slot': slot, 'role': 'output', 'schema': schema,
                'media_type': 'application/json', 'size': size, 'digest': sha}
    if (
        (job.get('id'), job.get('project_id'), job.get('investigation_id'), job.get('status'))
        != (job_id, project, inv, 'succeeded')
        or lock.get('project_id') != project or lock.get('grants') != []
        or lock.get('output_access_class') != 'project'
        or component.get('ref') != component_ref
        or release.get('id') != component_ref
        or release.get('status') != 'active' or not release_body.get('digest')
        or release_body['digest'] != component.get('digest')
        or release_body.get('bundle_digest') != bundle_sha
        or (component.get('bundle') or {}).get('digest') != bundle_sha
        or (lock.get('outputs') or {}).get(slot) != schema
        or (not portfolio and (set(config) != {'template'} or not isinstance(config['template'], dict)))
        or (portfolio and config.get('mode', 'portfolio') not in ('portfolio', 'refine', 'rescore'))
        or set(inputs) != set(splits) or not all(task_artifacts.get(split) for split in splits)
        or any((inputs[split].get('artifact_id'), inputs[split].get('schema'))
               != (task_artifacts.get(split), 'task-manifest/1' if split == 'task' else 'task-array/1') for split in splits)
        or (art.get('id'), art.get('project_id'), art.get('producer_job'), art.get('origin'), art.get('access_class'))
        != (reference, project, job_id, 'job-output', 'project')
        or not art.get('producer_attempt') or art['producer_attempt'] != job.get('current_attempt')
        or art['producer_attempt'] != (job.get('terminal') or {}).get('attempt_id')
        or type(size) is not int or not 0 < size <= 1024**2
        or not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)
        or any('sha256' in row and row['sha256'] != sha for row in authored_evidence)
        or len(outputs) != 1 or outputs[0].get('attempt_id') != art['producer_attempt']
        or any(outputs[0].get(key) != value for key, value in expected.items())
        or any(art.get(key) != expected[key] for key in ('role', 'schema', 'media_type'))
    ):
        raise ValueError('measurement artifact has unverified or restricted recipe-sweep provenance')
    evidence_dir = out / 'evidence'
    if out.is_symlink() or evidence_dir.is_symlink():
        raise ValueError('unsafe evidence export directory')
    evidence_dir.mkdir(exist_ok=True)
    target = evidence_dir / (reference + '.json')
    if target.exists() or target.is_symlink():
        raise ValueError('measurement artifact export path already exists')
    with tempfile.TemporaryDirectory(dir=out, prefix='.sweep-evidence-') as scratch:
        payload = Path(scratch) / 'table.json'
        c.download(project, reference, payload)
        if payload.is_symlink() or payload.stat().st_size != size or digest(payload) != sha:
            raise ValueError('measurement artifact bytes differ from the registered digest or size')
        table = json.loads(payload.read_bytes())
        if (not isinstance(table, dict) or table.get('schema') != schema
                or (not portfolio and table.get('selection_split') != 'visible-validation')
                or (portfolio and table.get('scheme') not in ('cv-train', 'train-fit-validation-score'))
                or table.get('exploratory') is not True
                or 'claim' not in table or table['claim'] is not None or not isinstance(table.get('recipes'), list)):
            raise ValueError('measurement artifact is outside the visible exploratory export policy')
        payload.rename(target)
    filename = target.relative_to(out).as_posix()
    entry = {'path': filename, 'sha256': sha, 'role': 'investigator-cited-visible-recipe-portfolio' if portfolio else 'investigator-cited-visible-recipe-sweep',
             'supports': 'The cited exploratory portfolio results on supplied training and visible validation' if portfolio else 'The cited exploratory recipe-sweep results on supplied training and visible validation',
             'limitations': 'No held-out or independent confirmation. Controller mapped the cited artifact to these exact bytes.'}
    mapping = {'author': 'study-adapter', 'artifact_id': reference, 'path': filename, 'sha256': sha, 'size': size,
               'producer_job': job_id, 'component_ref': component_ref,
               'investigation_id': inv, 'producer_attempt': art['producer_attempt'],
               'selection_split': table.get('scheme') if portfolio else 'visible-validation', 'authored_evidence': list(authored_evidence),
               'inputs': {split: inputs[split]['artifact_id'] for split in splits}}
    return entry, mapping


def export_model_checkpoint(client, project, job, candidate_dir):
    """Resolve a unique checkpoint name from the pinned producer's bound receipt."""
    from eidos_wire import sha256_file

    producer = job.get("lock", {}).get("component", {})
    if producer.get("ref") != PROJECT_COMMAND_REF or (
            producer.get("bundle") or {}).get("digest") != PROJECT_COMMAND_BUNDLE_SHA256:
        raise ValueError("selected model job lacks the pinned shared producer")
    outputs = job.get("outputs", [])
    models = [output for output in outputs if output.get("role") == "model-export"]
    receipts = [output for output in outputs if output.get("slot") == "receipt"]
    if len(models) != 1 or len(receipts) != 1:
        raise ValueError("selected model job must have exactly one checkpoint and producer receipt")
    model, receipt = models[0], receipts[0]

    def verified_download(output, target):
        client.download(project, output["artifact_id"], target)
        if sha256_file(target) != (output.get("digest"), output.get("size")):
            raise ValueError("downloaded producer artifact differs from bound digest or size")

    receipt_path = candidate_dir.parent / "model_export_receipt.json"
    verified_download(receipt, receipt_path)
    bindings = json.loads(receipt_path.read_bytes()).get("model_exports")
    if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], dict):
        raise ValueError("producer receipt lacks one unambiguous model basename")
    binding = bindings[0]
    basename = binding.get("basename")
    if not isinstance(basename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(?:pt|pth)", basename) or ".." in basename:
        raise ValueError("unsafe original model basename in producer receipt")
    if (binding.get("slot"), binding.get("digest"), binding.get("size")) != (
            model.get("slot"), model.get("digest"), model.get("size")):
        raise ValueError("model basename receipt differs from selected artifact")
    target = candidate_dir / basename
    if any(entry.name.casefold() == basename.casefold() for entry in candidate_dir.iterdir()):
        raise ValueError("model basename collides with another candidate entry")
    verified_download(model, target)
    return target


def export_answer(c, project, inv, run_dir, meta, jid, reason, claim, assessments, tokens, receipts,
                  usage_uncertain=False, answer_metadata=None, claim_state=None, task_artifacts=None):
    out = run_dir / 'out'
    if out.is_symlink():
        raise ValueError('unsafe answer export directory')
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
        checkpoint = export_model_checkpoint(c, project, job, cdir)
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
        pred = next(o['artifact_id'] for o in job['outputs'] if o['slot'] == 'validation_predictions')
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
            'model_path': checkpoint.relative_to(out).as_posix(), 'model_sha256': digest(checkpoint),
            'predictions_path': 'candidate/validation_predictions.npz',
            'predictions_sha256': digest(cdir / 'validation_predictions.npz'),
            'environment_lock': 'requirements.lock', 'environment_lock_sha256': digest(out / 'requirements.lock'),
            'reproduce_command': grader_reproduce_command(recipe['argv']),
            'parent_checkpoint': parent_checkpoint,
            'restore_class': 'checkpoint_continuation' if parent_checkpoint else 'fresh_initialization'}
    for name, role in [('selection_receipt.json', 'candidate-selection'), ('training_receipt.json', 'training'),
                       ('model_export_receipt.json', 'checkpoint-provenance')]:
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
    sweep_mappings = {}
    authored_evidence = answer_metadata.get('evidence')
    authored_evidence = authored_evidence if isinstance(authored_evidence, list) else []
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
        elif isinstance(reference, str) and reference.startswith('art_'):
            if reference not in sweep_mappings:
                cited = [row for row in authored_evidence if isinstance(row, dict) and row.get('path') == reference]
                entry, mapping = export_sweep_evidence(c, project, inv, out, reference, task_artifacts, cited)
                sweep_mappings[reference] = mapping
                evidence.append(entry)
            m['evidence_receipt'] = sweep_mappings[reference]['path']
        measurements.append(m)
    if sweep_mappings:
        write_json(run_dir / 'measurement_receipt_mappings.json', {'mappings': list(sweep_mappings.values())})
    status, claim_type, answer_scope, state_deviations = map_claim_state(claim_state, answer_metadata, candidate, measurements)
    if candidate is None:
        status = 'abstained'
    if status == 'abstained':
        claim_type = 'no_positive_claim'
    abstention = answer_metadata.get('abstention')
    abstention_deviations = []
    if 'abstention' not in answer_metadata or (
            isinstance(abstention, dict) and type(abstention.get('abstained')) is bool
            and abstention['abstained'] != (status == 'abstained')):
        write_json(run_dir / 'abstention_attribution.json', {
            'source': 'study-adapter-fallback',
            'reason': 'authored value conflicts with exported status' if 'abstention' in answer_metadata
                      else 'no authored abstention metadata',
            'authored_abstention': abstention,
            'export_status': status,
        })
        abstention_deviations.append('Abstention metadata authored by the study adapter. See the private attribution receipt.')
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
                       *state_deviations, *abstention_deviations, *answer_metadata.get('deviations', [])],
        'confidence': answer_metadata.get('confidence')}
    if answer['confidence'] is not None:
        answer['confidence_outcome'] = answer_metadata.get('confidence_outcome')
    write_json(out / 'answer.json', answer)
    return checkpoint.name if candidate is not None else None


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
