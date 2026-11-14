"""One installed study lab, shared by arms 3/4; never uses the source checkout.

Run with the installed interpreter: python -m study.eidos_arms.lab start|stop|status.
The controller starts once before a block and stops after all arms have finished.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(os.environ.get('EIDOS_STUDY_ROOT', '/Volumes/Mrigesh SSD/eidos-v1'))
SECURITY = '/usr/bin/security'  # Injectable only by tests; production never searches PATH.

DEFAULT_HOME = ROOT / 'pilot-1/lab/fixed'
DEFAULT_PYTHON = ROOT / 'venvs/pilot-1-lab/bin/python'


def home():
    return Path(os.environ.get('EIDOS_STUDY_HOME', DEFAULT_HOME)).resolve()


def python():
    return str(Path(os.environ.get('EIDOS_STUDY_PYTHON', DEFAULT_PYTHON)).absolute())


def build_record():
    path = Path(python()).parent.parent / 'pilot-build.json'
    record = json.loads(path.read_text())
    if not record.get('eidos_commit_sha') or record.get('python') != python():
        raise RuntimeError('missing or mismatched fixed-build provenance')
    return record


def environment(profile=None):
    from study.exp.treatment import resolve

    selected = resolve(provider=profile)
    env = dict(os.environ, EIDOS_HOME=str(home()), OMP_NUM_THREADS='1', MKL_NUM_THREADS='1',
               EIDOS_STUDY_TREATMENT=selected.id, EIDOS_STUDY_PROVIDER=selected.provider.id)
    if env.get("EIDOS_STUDY_LIVE_DISPATCH") == "1":
        from study.common import PROMPT_OVERRIDES, refuse_live_prompt_overrides
        refuse_live_prompt_overrides(os.environ)
        for name in PROMPT_OVERRIDES:
            env.pop(name, None)

    from study.private_card_registry import SEAL_ROOT_ENVS

    for key in list(env):
        if key.endswith(('_API_KEY', '_AUTH_TOKEN', '_OAUTH_TOKEN')) or key in SEAL_ROOT_ENVS or key in ('EIDOS_TOKEN', 'EIDOS_API_URL', 'PYTHONPATH', 'EIDOS_EVAL_AUDIT_IDENTIFIERS',
                        'EIDOS_PRIVATE_SEAL_ROOT'):
            env.pop(key, None)
    return env


def cli(*args, timeout=300, stdin=None, profile=None):
    exe = str(Path(python()).parent / 'lab')
    r = subprocess.run([exe, *map(str, args)], env=environment(profile), input=stdin,
                       text=True, capture_output=True, timeout=timeout)
    if r.returncode:
        # Never include credential input in exceptions.
        detail = 'credential input withheld' if stdin is not None else r.stderr[-2000:]
        raise RuntimeError(f'lab {args[0]} exited {r.returncode}: {detail}')
    return r.stdout


def client():
    from eidos.client import LabClient
    from eidos.settings import LabConfig
    cfg = LabConfig.load(home())
    return LabClient(cfg.api_url, cfg.token('admin'), client_name='pilot-lane-b', timeout=15)


def guard_disk():
    minimum_gib = 12.5 if 'p2' in str(home()) or 'pilot-2' in str(home()) else 12  # pilot-2 addendum 2
    if shutil.disk_usage('/').free < minimum_gib * 1024**3:
        raise RuntimeError(f'STOP: internal disk has less than {minimum_gib} GiB free')
    if ROOT not in home().parents:
        raise ValueError('study lab must be under EIDOS_STUDY_ROOT')


def start(profile=None, credentials=True):
    from study.exp.providers import DEFAULT, load

    profile = load(profile)
    guard_disk()
    build_record()
    home().mkdir(parents=True, exist_ok=True)
    with (home() / '.controller.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (home() / 'config/lab.json').exists():
            import importlib.metadata
            if importlib.metadata.version('eidos-lab') != '1.1.0':
                raise RuntimeError('initialize with installed eidos-lab 1.1.0')
            args = ['init', '--home', home(), '--api-port', '28765', '--pg-port', '25544',
                    '--temporal-port', '27633']
            source = os.environ.get('EIDOS_STUDY_TEMPORAL_SOURCE')
            if not source:
                existing = ROOT / 'a8lab/services/temporal/bin'
                if (existing / 'temporal-server').exists():
                    source = str(existing)
            if source:
                args += ['--temporal-source', source]
            cli(*args, timeout=600)
        config = json.loads((home() / 'config/lab.json').read_text())
        if config.get('lab_python') != python():
            raise RuntimeError('lab config uses a different interpreter; choose a fresh EIDOS_STUDY_HOME')
        running = _healthy()
        marker = home() / 'study-provider.json'
        previous = json.loads(marker.read_text()) if marker.exists() else {}
        different = previous.get('provider', DEFAULT) != profile.id
        credential_change = previous.get('credentials', True) != credentials
        supervisor_running = running
        if not running:
            supervisor_running = json.loads(cli('status', '--json')).get('supervisor_running') is True
        if supervisor_running and not (different or credential_change) and not running:
            for _ in range(3):
                time.sleep(.2)
                if _healthy():
                    running = True
                    break
            if not running:
                raise RuntimeError('live lab supervisor remains unhealthy; unchanged credentials, jobs left running')
        if supervisor_running and (different or credential_change):
            _stop_confirmed()
            running = False
        if not running:
            # Key exists only in memory and lab-start's stdin, never a file or argv.
            if credentials:
                key = subprocess.check_output([SECURITY, 'find-generic-password',
                                              '-a', os.environ['USER'], '-s', profile.keychain_item, '-w'],
                                             text=True, stderr=subprocess.DEVNULL).strip()
                try:
                    _start_confirmed(profile, profile.credential_env + '=' + key + '\n', credentials=True)
                finally:
                    key = None
            else:
                _start_confirmed(profile, '', credentials=False)
            marker.write_text(json.dumps({'provider': profile.id, 'started_utc': datetime.now(UTC).isoformat(),
                                          'credentials': credentials}) + '\n')
        ensure_mcp()
    return {'home': str(home()), 'python': python(), 'mcp_url': mcp_url()}


def _healthy():
    try:
        c = client()
        try:
            c.health()
            return True
        finally:
            c.close()
    except Exception:
        return False


def _stop_confirmed():
    for _ in range(2):
        result = stop()
        if (result.get('stopped') is True
                or 'stopped' not in result and result.get('was_running') is False):
            return
    raise RuntimeError('lab stop did not confirm supervisor exit; credentials not changed')


def _start_confirmed(profile, stdin, *, credentials):
    expected = {profile.credential_env: 'stdin'} if credentials else {}
    for attempt in range(2):
        result = json.loads(cli('start', '--credentials-stdin', stdin=stdin, timeout=300, profile=profile))
        if (not result.get('already_running') and result.get('phase') == 'running'
                and result.get('credentials') == expected):
            return
        if attempt == 0:
            _stop_confirmed()
    raise RuntimeError('lab start did not confirm requested credentials were applied')


def mcp_url():
    return 'http://127.0.0.1:' + os.environ.get('EIDOS_STUDY_MCP_PORT', '28766') + '/mcp'


def ensure_mcp():
    port = int(os.environ.get('EIDOS_STUDY_MCP_PORT', '28766'))
    pidfile = home() / 'run/study-mcp.json'
    if pidfile.exists():
        rec = json.loads(pidfile.read_text())
        try:
            os.kill(rec['pid'], 0)
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return
        except (OSError, ProcessLookupError):
            pass
    # Refuse to adopt an unrelated listener.
    probe = socket.socket()
    try:
        probe.bind(('127.0.0.1', port))
    finally:
        probe.close()
    with (home() / 'logs/study-mcp.log').open('ab') as log:
        p = subprocess.Popen([str(Path(python()).parent / 'lab'), 'mcp', '--transport', 'http',
                              '--port', str(port)], env=environment(), stdout=log, stderr=log,
                             start_new_session=True)
    pidfile.write_text(json.dumps({'pid': p.pid, 'started_at': time.time(), 'port': port}))
    for _ in range(100):
        if p.poll() is not None:
            raise RuntimeError('study MCP exited; see logs/study-mcp.log')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.2):
                return
        except OSError:
            time.sleep(.1)
    raise RuntimeError('study MCP did not start')


def stop():
    pidfile = home() / 'run/study-mcp.json'
    if pidfile.exists():
        rec = json.loads(pidfile.read_text())
        # Check recorded command before signalling a persisted PID.
        r = subprocess.run(['ps', '-p', str(rec['pid']), '-o', 'command='], capture_output=True, text=True)
        if 'lab mcp --transport http' in r.stdout and str(rec['port']) in r.stdout:
            os.kill(rec['pid'], signal.SIGTERM)
        pidfile.unlink()
    return json.loads(cli('stop', '--running-jobs', 'cancel'))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['start', 'stop', 'status'])
    args = p.parse_args()
    if args.action == 'start':
        print(json.dumps(start()))
    elif args.action == 'stop':
        print(json.dumps(stop()))
    else:
        print(cli('status', '--json'))
