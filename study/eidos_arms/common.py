"""Controller-safe dispatch and receipt/answer utilities for the installed lab worker."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from . import lab

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + '\n')
    tmp.replace(path)


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dispatch(arm, run_dir):
    """Always use installed 1.1.0, regardless of lane A controller's interpreter."""
    run_dir = run_dir.resolve()
    r = subprocess.run([lab.python(), '-m', 'study.eidos_arms.worker', '--arm', str(arm),
                        '--run-dir', str(run_dir)], cwd=ROOT, env=lab.environment(), stdin=subprocess.DEVNULL)
    record = run_dir / 'run_record.json'
    if record.exists():
        return json.loads(record.read_text())
    raise RuntimeError(f'arm {arm} worker exited {r.returncode} without a run record')


def prompt(arm, run_dir):
    base = ROOT / 'study/prompts/task.md'
    # Production must read the merged lane A prompt; smoke can explicitly supply a non-study prompt.
    if os.environ.get('EIDOS_STUDY_SMOKE_PROMPT'):
        base = Path(os.environ['EIDOS_STUDY_SMOKE_PROMPT'])
    text = base.read_text()
    paragraph = (ROOT / f'study/prompts/tools_arm{arm}.md').read_text().strip()
    if '{{TOOLS_PARAGRAPH}}' not in text:
        raise ValueError('base prompt must contain {{TOOLS_PARAGRAPH}}')
    return text.replace('{{TOOLS_PARAGRAPH}}', paragraph)


def envelope(meta):
    e = meta.get('envelope', {})
    return int(e.get('wall_s', 1800)), int(e.get('tokens', e.get('model_tokens', 8_000_000)))


def prepare(c, run_dir, arm):
    """One new project per invocation; immutable task bundle is the only scientific input."""
    from eidos.tasks.bundle import import_bundle
    from eidos.cli.main import install_component, builtin_component_dirs
    import uuid
    project = f'pilot-a{arm}-{uuid.uuid4().hex[:16]}'
    c.command('create_project', {'project_id': project, 'budgets': {
        'compute_seconds': 100000000, 'trials': 1000000}}, key='create:' + project)
    imported = import_bundle(c, project, run_dir / 'task', python=os.environ['EIDOS_RESEARCH_PYTHON'])
    for directory in builtin_component_dirs():
        if directory.name in ('eidos.project.command', 'eidos.task.score-classification'):
            r = install_component(c, project, directory, 'default', True)
            if 'component_ref' not in r or r.get('validation') == 'failed':
                raise RuntimeError(f'component installation failed: {r}')
    write_json(run_dir / 'lab_import.json', {'project': project, **imported})
    return project, imported


def ledger(c, project, inv, path):
    """Export authorized ledger events, turn decisions, model responses and scientific records."""
    events = []
    cursor, horizon = 0, None
    with Path(path).open('w') as f:
        def emit(kind, body):
            f.write(json.dumps({'type': kind, 'data': body}, default=str) + '\n')
        while True:
            page = c.query('list_events', {'investigation_id': inv, 'cursor': cursor,
                           'horizon': horizon, 'limit': 500}, project=project)
            horizon = page['horizon']
            for event in page['events']:
                events.append(event)
                emit('ledger_event', event)
            if not page['events'] or not page['more']:
                break
            cursor = page['next_cursor']
        shown = c.query('get_investigation', {'investigation_id': inv}, project=project)
        emit('investigation', shown)
        for turn in shown['turns']:
            detail = c.query('get_turn', {'turn_id': turn['id']}, project=project)
            emit('turn', detail)
            for dec in detail.get('decisions', []):
                aid = (dec.get('body') or {}).get('response_artifact_id')
                if aid:
                    tmp = Path(path).parent / ('.response-' + dec['id'] + '.json')
                    try:
                        c.download(project, aid, tmp)
                        emit('model_response', {'artifact_id': aid, 'response': json.loads(tmp.read_text())})
                    finally:
                        tmp.unlink(missing_ok=True)
        records = {}
        for kind in ('note', 'claim', 'assessment', 'evidence_plan', 'export'):
            records[kind] = c.query('list_records', {'kind': kind, 'summary': False, 'limit': 1000}, project=project)
            for row in records[kind]:
                emit(kind, row)
        for job in shown['jobs']:
            emit('job', c.query('get_job', {'job_id': job['id']}, project=project))
    return shown, events, records


def usage(events, transcript=None):
    """Use model.usage receipts once, including failed requests; never double-count decisions.

    Eidos aggregate input_tokens includes cached OpenAI input. Recover its cache breakdown
    from persisted model-response receipts; failures without a raw response remain uncertain.
    """
    tokens = dict(input=0, cache_read=0, cache_write=0, output=0, total=0)
    models, receipts, usd = set(), [], []
    for e in events:
        if e['type'] != 'model.usage':
            continue
        u = (e.get('body') or {}).get('usage') or {}
        receipts.append(u)
        tokens['input'] += int(u.get('input_tokens') or 0)
        tokens['output'] += int(u.get('output_tokens') or 0)
        models.update(u.get('served_models') or [])
        models.update(r['model'] for r in u.get('per_request', []) if r.get('model'))
        if u.get('usd') is not None:
            usd.append(u['usd'])
    tokens['total'] = tokens['input'] + tokens['output']
    if transcript:
        seen = set()
        for line in Path(transcript).read_text().splitlines():
            row = json.loads(line)
            if row['type'] != 'model_response':
                continue
            for message in row['data']['response']:
                if not isinstance(message, dict) or message.get('kind') != 'response':
                    continue
                ident = message.get('provider_response_id') or hashlib.sha256(
                    json.dumps(message, sort_keys=True).encode()).hexdigest()
                if ident in seen:
                    continue
                seen.add(ident)
                u = message.get('usage') or {}
                tokens['cache_read'] += int(u.get('cache_read_tokens') or 0)
                tokens['cache_write'] += int(u.get('cache_write_tokens') or 0)
        cached = tokens['cache_read'] + tokens['cache_write']
        if cached > tokens['input']:
            raise ValueError('raw cache counters exceed ledger input total')
        tokens['input'] -= cached
    return tokens, sorted(models), (sum(usd) if receipts and len(usd) == len(receipts) else None), receipts


def classify(shown, receipts, models, timed_out=False, answer_valid=True):
    failures = [r for r in receipts if r.get('failed_request') or r.get('error')]
    text = json.dumps(failures)
    if len(models) > 1:
        return 'incident', 'model id changed: ' + ', '.join(models)
    import re
    if failures and (re.search(r'\b(?:429|5\d\d)\b', text) or any(
            x in text.lower() for x in ('quota', 'rate limit', 'insufficient credit')) or (
            not timed_out and any(x in text.lower() for x in ('connection', 'timeout')))):
        return 'incident', 'provider/infrastructure failure: ' + text[:1800]
    if timed_out:
        return 'timeout', None
    if any(t.get('stop_reason') == 'budget-exhausted' for t in shown.get('turns', [])) or (
            'budget-exhausted' in str(shown.get('needs_input', ''))):
        return 'token_budget', None
    if failures or shown.get('needs_input') or any(t.get('stop_reason') == 'error' for t in shown.get('turns', [])):
        return 'crash', 'investigator failed or requires operator input: ' + text[:1800]
    if not answer_valid:
        return 'invalid_output', None
    return 'ok', None
