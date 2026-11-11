"""Controller-safe dispatch and receipt/answer utilities for the installed lab worker."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from . import lab

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + '\n')
    tmp.replace(path)


def utc():
    return datetime.now(UTC).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dispatch(arm, run_dir):
    """Always use the recorded fixed wheel build, regardless of lane A controller's interpreter."""
    run_dir = run_dir.resolve()
    r = subprocess.run([lab.python(), '-m', 'study.eidos_arms.worker', '--arm', str(arm),
                        '--run-dir', str(run_dir)], cwd=ROOT, env=lab.environment(), stdin=subprocess.DEVNULL)
    record = run_dir / 'run_record.json'
    if record.exists():
        return json.loads(record.read_text())
    raise RuntimeError(f'arm {arm} worker exited {r.returncode} without a run record')


def prompt(arm, run_dir):
    from study.common import read_json, render_prompt
    return render_prompt(arm, read_json(run_dir / 'arm.json')['card'],
                         base=os.environ.get('EIDOS_STUDY_SMOKE_PROMPT'))


def envelope(meta):
    e = meta.get('envelope', {})
    return int(e.get('wall_s', 1800)), int(e.get('tokens', e.get('model_tokens', 8_000_000)))


def prepare(c, run_dir, arm):
    """One new project per invocation; immutable task bundle is the only scientific input."""
    import uuid

    from study.private_card_registry import PRIVATE_CLASSIFICATION, PRIVATE_REGRESSION

    from eidos.cli.main import builtin_component_dirs, install_component
    from eidos.tasks.bundle import import_bundle
    project = f'pilot-a{arm}-{uuid.uuid4().hex[:16]}'
    c.command('create_project', {'project_id': project, 'budgets': {
        'compute_seconds': 100000000, 'trials': 1000000}}, key='create:' + project)
    c.command('update_profile_policy', {'policy': {'llm_broker': {'enabled': False},
              'auto_freeze_approved_templates': True}}, project=project, key='policy:' + project)
    policy = c.query('get_profile', {}, project=project)['body']['policy']
    if policy.get('llm_broker', {}).get('enabled') is not False:
        raise RuntimeError('study project must have policy.llm_broker explicitly disabled')
    write_json(run_dir / 'lab_policy.json', {'project': project, 'policy': policy})
    imported = import_bundle(c, project, run_dir / 'task', python=os.environ['EIDOS_RESEARCH_PYTHON'])
    from .charters import commit
    imported.update(commit(c, project, imported['task_id'], run_dir))
    card = imported['task_id']
    for directory in builtin_component_dirs():
        # <id>@<release>/ is an earlier release kept installable so pinned references resolve; it is published, not bound.
        name, pinned = directory.name.split('@')[0], '@' in directory.name
        if name in ('eidos.project.command', 'eidos.task.score-classification',
                    'eidos.search.recipe-sweep', 'eidos.diagnose.baseline-errors') or (
                (card in ('D03', 'D06', 'N01', 'N04') or card in PRIVATE_CLASSIFICATION) and name == 'eidos.task.paired-classification-report'):
            r = install_component(c, project, directory, 'default', not pinned)
            if 'component_ref' not in r or r.get('validation') == 'failed':
                raise RuntimeError(f'component installation failed: {r}')
    if card not in ('D01', 'D04', 'D05', 'N02', 'N03') and card not in PRIVATE_REGRESSION:
        from .protocols import prepare as prepare_protocols
        prepare_protocols(c, project, run_dir)
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
            # The project belongs to this run only. Include setup and any explicitly
            # attributed project-budgeted baseline fallback, not just investigation events.
            page = c.query('list_events', {'cursor': cursor, 'horizon': horizon, 'limit': 500}, project=project)
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
        for event in events:
            if event.get('type') == 'decision.runtime_bounded':
                aid = (event.get('body') or {}).get('response_artifact_id')
                if aid:
                    tmp = Path(path).parent / ('.runtime-response-' + aid + '.json')
                    try:
                        c.download(project, aid, tmp)
                        emit('runtime_response', {'artifact_id': aid, 'runtime_outcome':
                            (event.get('body') or {}).get('runtime_outcome'), 'response': json.loads(tmp.read_text())})
                    finally:
                        tmp.unlink(missing_ok=True)
        records = {}
        for kind in ('note', 'claim', 'assessment', 'evidence_plan', 'export', 'finalization_export_receipt'):
            records[kind] = c.query('list_records', {'kind': kind, 'summary': False, 'limit': 1000}, project=project)
            for row in records[kind]:
                emit(kind, row)
        for job in c.query('list_jobs', {}, project=project):
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


INCIDENT_RE = re.compile(
    r"\b429\b|(?:http|status|error|code)[\s\"':=_-]{0,12}5\d\d\b|"
    r"\b5\d\d\s+(?:internal|server|error)|internal server error|service unavailable|bad gateway|gateway timeout|"
    r"rate[ _-]?limit|overloaded|quota|insufficient.{0,20}(?:credit|balance)|"
    r"(?:credit|balance).{0,20}(?:insufficient|exhausted)|usage limit|out of credits|capacity exceeded",
    re.I,
)


def _provider_incident(receipt, timed_out=False):
    """True only for provider 429/5xx/quota-type failures (same text rule as arms 1-3) or a local bridge
    connection failure before the deadline; never for token counts or a plain request timeout."""
    failed = receipt.get('failed_request') or {}
    code = failed.get('status_code')
    if isinstance(code, int) and (code == 429 or code >= 500):
        return True
    message = ' '.join(str(x) for x in (receipt.get('error'), failed.get('error')) if x)
    if INCIDENT_RE.search(message):
        return True
    return (not timed_out) and 'connection' in message.lower() and 'timeout' not in message.lower()


def classify(shown, receipts, models, timed_out=False, answer_valid=True):
    failures = [r for r in receipts if r.get('failed_request') or r.get('error')]
    text = json.dumps(failures)
    if len(models) > 1:
        return 'incident', 'model id changed: ' + ', '.join(models)
    # Addendum 2: classify only error strings and HTTP status codes with the same rule as arms 1-3
    # (study/run_arm.py incident_text). The old search over the whole failure JSON matched token
    # counts such as "output_tokens": 541 as a 5xx status. A request timeout without a provider
    # status is the system's own behaviour, not an incident.
    # Pilot-2 addendum 1 (symmetric with arms 1-3): a recovered provider failure never decides the run.
    # A concluded investigation with a valid answer is ok; a provider failure is an incident only if the
    # investigation ended without concluding (timeouts and budget stops keep their own status).
    # Pilot-3 addendum 2: the provider failure must be the terminal event, as for arms 1-3 (whose final
    # result must itself be the API error). An earlier failure the investigator recovered from, followed by
    # successful model calls, never turns a later behavioural stop (e.g. refused proposals) into an incident.
    # Admission/closure can emit zero-call bookkeeping after the last provider request.
    last = next((r for r in reversed(receipts) if r.get('calls') or r.get('failed_request') or r.get('error')), {})
    controller = (shown.get('body') or {}).get('controller_abstention') or {}
    terminal = controller.get('failure') or {}
    terminal_cause = terminal.get('error_type') or terminal.get('reason')
    provider = terminal.get('provider') or {}
    provider_failure = (
        controller.get('code') == 'provider-unavailable' or
        provider.get('status') == 429 or
        bool(last.get('failed_request') or last.get('error')) and _provider_incident(last, timed_out)
    )
    if timed_out or terminal_cause == 'wall-deadline-reached':
        return 'timeout', None
    if terminal_cause == 'budget-exhausted' or any(
            t.get('stop_reason') == 'budget-exhausted' for t in shown.get('turns', [])) or (
            'budget-exhausted' in str(shown.get('needs_input', ''))):
        return 'token_budget', None
    if terminal_cause in {'max-turns-reached', 'turn-budget-exhausted'}:
        return 'crash', 'investigation turn limit reached: ' + terminal_cause
    # Scientific completion is determined by the final state and exported answer,
    # not by recovered request/turn errors. A controller fallback alone is not a conclusion.
    if shown.get('status') == 'concluded':
        return ('ok' if answer_valid else 'invalid_output'), None
    if provider_failure:
        return 'incident', 'provider/infrastructure failure: ' + text[:1800]
    return 'crash', 'investigator ended without a conclusion or requires operator input: ' + text[:1800]
