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
from pathlib import Path

SSD = Path('/Volumes/Mrigesh SSD/eidos-v1')
DEFAULT_HOME = SSD / 'pilot-1/lab/shared'
DEFAULT_PYTHON = SSD / 'venvs/pilot-1b-110/bin/python'


def home():
    return Path(os.environ.get('EIDOS_STUDY_HOME', DEFAULT_HOME)).resolve()


def python():
    return str(Path(os.environ.get('EIDOS_STUDY_PYTHON', DEFAULT_PYTHON)).absolute())


def environment():
    env = dict(os.environ, EIDOS_HOME=str(home()), OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    for key in list(env):
        if key.endswith(('_API_KEY', '_AUTH_TOKEN', '_OAUTH_TOKEN')) or key in ('EIDOS_TOKEN', 'EIDOS_API_URL', 'PYTHONPATH'):
            env.pop(key, None)
    return env


def cli(*args, timeout=300, stdin=None):
    exe = str(Path(python()).parent / 'lab')
    r = subprocess.run([exe, *map(str, args)], env=environment(), input=stdin,
                       text=True, capture_output=True, timeout=timeout)
    if r.returncode:
        # Never include credential input in exceptions.
        raise RuntimeError(f'lab {args[0]} exited {r.returncode}: {r.stderr[-2000:]}')
    return r.stdout


def client():
    from eidos.client import LabClient
    from eidos.settings import LabConfig
    cfg = LabConfig.load(home())
    return LabClient(cfg.api_url, cfg.token('admin'), client_name='pilot-lane-b', timeout=15)


def guard_disk():
    if shutil.disk_usage('/').free < 12 * 1024**3:
        raise RuntimeError('STOP: internal disk has less than 12 GiB free')
    if SSD not in home().parents:
        raise ValueError('study lab must be on the external SSD')


def start():
    guard_disk()
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
                existing = SSD / 'a8lab/services/temporal/bin'
                if (existing / 'temporal-server').exists():
                    source = str(existing)
            if source:
                args += ['--temporal-source', source]
            cli(*args, timeout=600)
        try:
            c = client()
            c.health()
            c.close()
            running = True
        except Exception:
            running = False
        if not running:
            # Key exists only in memory and lab-start's stdin, never a file or argv.
            key = subprocess.check_output(['/usr/bin/security', 'find-generic-password',
                                          '-a', os.environ['USER'], '-s', 'minimax_api_key', '-w'],
                                         text=True).strip()
            try:
                cli('start', '--credentials-stdin', stdin='OPENAI_API_KEY=' + key + '\n', timeout=300)
            finally:
                key = None
        ensure_mcp()
    return {'home': str(home()), 'python': python(), 'mcp_url': mcp_url()}


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
    cli('stop', '--running-jobs', 'cancel')
    return {'stopped': str(home())}


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
