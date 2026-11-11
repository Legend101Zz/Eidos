"""Per-investigation provider connection with an independent hard wall deadline.

This forwards OpenAI chat-completion JSON unchanged to the configured HTTPS endpoint.
It has no provider credential of its own: Authorization arrives from the installed Eidos
investigator and is forwarded only upstream. No request headers/bodies are logged.
At the deadline the dedicated process exits, closing its in-flight provider sockets.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def serve(upstream, deadline, timeout, state):
    import httpx
    parsed = urlparse(upstream)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise ValueError('configured upstream must be HTTPS without URL credentials')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if time.time() >= deadline:
                self.send_error(504, 'Investigation wall deadline exhausted')
                return
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            length = int(self.headers.get('Content-Length', '0'))
            auth = self.headers.get('Authorization', '')
            if length <= 0 or length > 8 * 1024**2 or not auth.startswith('Bearer '):
                self.send_error(400)
                return
            body = self.rfile.read(length)
            headers = {'Authorization': auth, 'Content-Type': 'application/json'}
            try:
                with httpx.Client(timeout=min(timeout, max(.01, deadline - time.time()))) as client:
                    response = client.post(upstream.rstrip('/') + '/chat/completions',
                                           content=body, headers=headers)
                self.send_response(response.status_code)
                self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                for name in ('Retry-After', 'Retry-After-Ms'):  # ADR 0023: the provider's backoff hint reaches the runtime
                    if response.headers.get(name) is not None:
                        self.send_header(name, response.headers[name])
                self.send_header('Content-Length', str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except (httpx.HTTPError, BrokenPipeError, ConnectionResetError):
                # No exception/header/body logging: exceptions can carry request context.
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state.write_text(json.dumps({'pid': os.getpid(), 'upstream': upstream, 'deadline_unix': deadline,
                                'base_url': f'http://127.0.0.1:{server.server_port}/v1'}))
    timer = threading.Timer(max(0, deadline - time.time()), lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    server.serve_forever(poll_interval=.1)


def start(run_dir, upstream, deadline, timeout):
    from . import lab
    state = run_dir / 'provider_bridge.json'
    with (run_dir / 'provider_bridge.log').open('ab') as log:
        process = subprocess.Popen([lab.python(), '-m', 'study.eidos_arms.provider_bridge',
            '--upstream', upstream, '--deadline', str(deadline), '--timeout', str(timeout),
            '--state', str(state)], cwd=Path(__file__).resolve().parents[2],
            env=lab.environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('provider deadline bridge failed; see provider_bridge.log')
        if state.exists():
            return process, json.loads(state.read_text())
        time.sleep(.05)
    process.kill()
    process.wait()
    raise RuntimeError('provider deadline bridge did not initialize')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--deadline', type=float, required=True)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    serve(args.upstream, args.deadline, args.timeout, args.state)
