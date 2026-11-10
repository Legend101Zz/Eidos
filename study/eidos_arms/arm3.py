"""Claude bare + lane A's exact Seatbelt/stream runner + project-scoped loopback MCP."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from . import lab
from .common import ROOT, envelope, prepare, prompt, write_json


def lane_a():
    # Only for pre-merge smoke: read lane A's public helpers from its working tree.
    # In the merged controller, no override is needed and these are normal imports.
    override = os.environ.get('EIDOS_STUDY_LANE_A_ROOT')
    if override:
        import study
        study.__path__.append(str(Path(override) / 'study'))
    from study.common import agent_env, sandbox_command
    from study.run_arm import run_stream
    return agent_env, sandbox_command, run_stream


def run(run_dir, meta):
    from .arm4 import validate
    agent_env, sandbox_command, run_stream = lane_a()
    lab.start()
    c = lab.client()
    principal = None
    try:
        project, imported = prepare(c, run_dir, 3)
        method = ROOT / 'study/METHOD.md'
        if not method.exists() and os.environ.get('EIDOS_STUDY_LANE_A_ROOT'):
            method = Path(os.environ['EIDOS_STUDY_LANE_A_ROOT']) / 'study/METHOD.md'
        shutil.copyfile(method, run_dir / 'METHOD.md')
        wall, token_cap = envelope(meta)
        token = secrets.token_urlsafe(48)
        principal = 'agent-' + project
        c.command('create_principal', {'id': principal, 'kind': 'service', 'roles': ['researcher'],
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
        env['EIDOS_TOKEN'] = token
        env['EIDOS_API_URL'] = context['api_url']
        env['ANTHROPIC_API_KEY'] = subprocess.check_output(
            ['/usr/bin/security', 'find-generic-password', '-a', os.environ['USER'],
             '-s', 'minimax_api_key', '-w'], text=True, stderr=subprocess.DEVNULL).strip()
        model = meta.get('model', 'MiniMax-M3.1-Flash').split(':')[-1]
        command = ['claude', '--bare', '-p', prompt(3, run_dir), '--model', model,
                   '--permission-mode', 'bypassPermissions', '--output-format', 'stream-json', '--verbose',
                   '--mcp-config', str(run_dir / 'mcp.json'), '--strict-mcp-config']
        rec = run_stream(run_dir, sandbox_command(run_dir, command), env,
                         wall_s=wall, token_limit=token_cap)
        env.pop('ANTHROPIC_API_KEY', None)
        env.pop('EIDOS_TOKEN', None)
        if rec['status'] == 'ok':
            try:
                validate(run_dir / 'out/answer.json')
            except Exception as exc:
                rec.update(status='invalid_output', incident_reason=f'answer validation: {exc}')
        if len(rec['model_ids_seen']) > 1:
            rec.update(status='incident', incident_reason='model id changed: ' + ', '.join(rec['model_ids_seen']))
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
