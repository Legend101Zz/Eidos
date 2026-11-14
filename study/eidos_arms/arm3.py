"""Claude bare + lane A's exact Seatbelt/stream runner + project-scoped loopback MCP."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from . import lab
from .common import ROOT, envelope, prepare, prompt, write_json


def lane_a():
    # Only for pre-merge smoke: read lane A's public helpers from its working tree.
    # In the merged controller, no override is needed and these are normal imports.
    override = os.environ.get('EIDOS_STUDY_LANE_A_ROOT')
    if override:
        import study
        study.__path__.append(str(Path(override) / 'study'))
    from study import run_arm
    from study.common import agent_env, sandbox_command
    return agent_env, sandbox_command, run_arm


def run(run_dir, meta):
    from eidos.application.scopes import ROLE_SCOPES

    from .arm4 import validate
    agent_env, sandbox_command, stream_module = lane_a()
    from study.exp.treatment import for_run
    provider = for_run(meta).provider
    cli_settings = provider.cli_settings()
    lab.start(profile=provider)
    c = lab.client()
    principal = None
    try:
        project, imported = prepare(c, run_dir, 3)
        method = ROOT / 'study/METHOD.md'
        if not method.exists() and os.environ.get('EIDOS_STUDY_LANE_A_ROOT'):
            method = Path(os.environ['EIDOS_STUDY_LANE_A_ROOT']) / 'study/METHOD.md'
        shutil.copyfile(method, run_dir / 'METHOD.md')
        shutil.copyfile(Path(__file__).parent / 'mcp_http.py', run_dir / 'mcp_http.py')
        wall, token_cap = envelope(meta)
        token = secrets.token_urlsafe(48)
        principal = 'agent-' + project
        # Arm 3 must not launch a second, unaccounted model agent inside the shared lab.
        scopes = [s for s in ROLE_SCOPES['researcher'] if s != 'investigate']
        c.command('create_principal', {'id': principal, 'kind': 'service', 'roles': [], 'extra_scopes': scopes,
                  'project_ids': [project], 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
                  'ttl_seconds': wall + 120}, key='principal:' + project)
        # Claude expands the environment reference; no token is written to this config.
        config = {'mcpServers': {'eidos': {'type': 'http', 'url': lab.mcp_url(),
                  'headers': {'Authorization': 'Bearer ${EIDOS_TOKEN}'}}}}
        write_json(run_dir / 'mcp.json', config)
        cfg = json.loads((lab.home() / 'config/lab.json').read_text())
        context = {'project': project, 'inputs': imported['artifacts'], 'charter_id': imported['charter_id'],
                   'api_url': f"http://127.0.0.1:{cfg['api_port']}", 'mcp_url': lab.mcp_url(),
                   'token_environment': 'EIDOS_TOKEN',
                   'components': c.query('search_components', {'limit': 100}, project=project)}
        write_json(run_dir / 'lab_context.json', context)
        env = agent_env(run_dir)
        # Claude 2.1.318 uses /tmp/claude-$UID unless this explicit override is set.
        # TMPDIR alone does not prevent the Seatbelt-denied startup write.
        env['CLAUDE_CODE_TMPDIR'] = str(run_dir / '.tmp')
        env['TZ'] = 'UTC'
        env['EIDOS_TOKEN'] = token
        env['EIDOS_API_URL'] = context['api_url']
        env['ANTHROPIC_API_KEY'] = subprocess.check_output(
            ['/usr/bin/security', 'find-generic-password', '-a', os.environ['USER'],
             '-s', provider.keychain_item, '-w'], text=True, stderr=subprocess.DEVNULL).strip()
        model = cli_settings['model']
        initial_prompt = prompt(3, run_dir)
        command = ['claude', '--bare', '-p', initial_prompt, '--model', model,
                   '--permission-mode', 'bypassPermissions', '--output-format', 'stream-json', '--verbose',
                   '--include-partial-messages', '--mcp-config', str(run_dir / 'mcp.json'), '--strict-mcp-config']
        sandbox = sandbox_command(run_dir, command)
        # Native Bun/ICU on macOS maps the system timezone database during startup.
        # This is a system path; project/repo/home access rules remain lane A's.
        profile = Path(sandbox[sandbox.index('-f') + 1]).read_text()
        profile = profile.replace('(subpath "/private/etc")',
                                  '(subpath "/private/etc") (subpath "/private/var/db/timezone")')
        profile_path = run_dir / '.agent.sb'
        profile_path.write_text(profile)
        sandbox[sandbox.index('-f') + 1] = str(profile_path)
        from study.common import record_live_delivery
        record_live_delivery(run_dir, meta, initial_prompt, 'claude-prompt')
        from .stream_usage import extend_tracker
        original_tracker = stream_module.UsageTracker
        stream_module.UsageTracker = extend_tracker(original_tracker)
        try:
            # Lane A stops on > threshold; use cap-1 so reaching the exact cap stops too.
            rec = stream_module.run_stream(run_dir, sandbox, env, wall_s=wall, token_limit=token_cap - 1)
        finally:
            stream_module.UsageTracker = original_tracker
        env.pop('ANTHROPIC_API_KEY', None)
        env.pop('EIDOS_TOKEN', None)
        rec['agent_wall_s'] = rec['wall_s']
        if rec['status'] == 'ok':
            try:
                validate(run_dir / 'out/answer.json')
            except Exception as exc:
                rec.update(status='invalid_output', incident_reason=f'answer validation: {exc}')
        # Pilot-3 addendum 3: Claude Code labels its own generated messages (e.g. a printed API error) with the
        # placeholder model id "<synthetic>"; like the launcher's version guard, it never counts as a model change.
        # A run that ends on such an API error is still an incident through run_stream's terminal-error rule.
        served = [m for m in rec['model_ids_seen'] if m != '<synthetic>']
        if len(served) > 1:
            rec.update(status='incident', incident_reason='model id changed: ' + ', '.join(served))
        if rec['model_ids_seen'] and not rec['tokens']['total']:
            rec.update(status='incident', incident_reason='Provider/Claude supplied no usage receipts; '
                       'zero is observed usage, not evidence of zero spend; token envelope could not be verified')
        rec['usage_uncertain'] = rec['status'] in ('timeout', 'token_budget', 'crash', 'incident')
        rec['project'] = project
        return rec
    finally:
        if principal:
            c.command('revoke_principal', {'id': principal}, key='revoke:' + principal)
            jobs = c.query('list_jobs', {'limit': 1000}, project=project)
            for j in jobs:
                if j['status'] not in ('succeeded', 'failed', 'cancelled', 'pruned'):
                    c.command('cancel_run', {'job_id': j['id']}, project=project, key='outer-cancel:' + j['id'])
        c.close()
