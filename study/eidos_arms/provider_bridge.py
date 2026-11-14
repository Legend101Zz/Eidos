"""Per-investigation provider connection with an independent hard wall deadline.

This forwards OpenAI chat-completion JSON unchanged to the configured HTTPS endpoint.
It has no provider credential of its own: Authorization arrives from the installed Eidos
investigator and is forwarded only upstream. No request headers/bodies are logged.
An explicitly requested Tier 1 audit records request digests and identifier matches.
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
from socketserver import TCPServer
from urllib.parse import urlparse


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer's getfqdn lookup can exhaust startup and wall deadlines on hosted runners.
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]


class RequestRejected(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__('provider request refused')


def request_mapping(wire, method, path, headers, length, upstream):
    """Validate a request and return only the URL and allowed upstream headers."""
    if wire not in ('openai-chat', 'anthropic-messages'):
        raise ValueError('unknown provider wire')
    if method != 'POST':
        raise RequestRejected(405)
    incoming = {k.lower(): v for k, v in headers.items()}
    parsed = urlparse(path)
    expected = '/v1/messages' if wire == 'anthropic-messages' else '/v1/chat/completions'
    if (parsed.path != expected or parsed.scheme or parsed.netloc or parsed.fragment
            or wire == 'openai-chat' and path != expected):
        raise RequestRejected(404)
    if length <= 0 or length > 8 * 1024**2:
        raise RequestRejected(400)
    if wire == 'openai-chat':
        auth = incoming.get('authorization', '')
        if not auth.startswith('Bearer '):
            raise RequestRejected(400)
        return upstream.rstrip('/') + '/chat/completions', {
            'Authorization': auth, 'Content-Type': 'application/json'}
    if not all(incoming.get(name, '').strip() for name in ('x-api-key', 'anthropic-version')):
        raise RequestRejected(400)
    outgoing = {name: incoming[name] for name in ('x-api-key', 'anthropic-version', 'anthropic-beta')
                if name in incoming}
    outgoing['Content-Type'] = incoming.get('content-type', 'application/json')
    return upstream.rstrip('/') + '/v1/messages' + ('?' + parsed.query if parsed.query else ''), outgoing


def response_headers(wire, headers):
    """Return allowed provider response metadata without logging it."""
    outgoing = {name: headers[name] for name in ('Retry-After', 'Retry-After-Ms')
                if headers.get(name) is not None}
    if wire == 'anthropic-messages':
        outgoing.update({name: value for name, value in headers.items()
                         if name.lower() == 'request-id' or name.lower().startswith('anthropic-ratelimit-')})
    return outgoing


def make_server(upstream, deadline, timeout, *, wire='openai-chat', audit=None, transport=None):
    """Create a loopback relay; injected transports let tests avoid provider calls."""
    import httpx

    parsed = urlparse(upstream)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('configured upstream must be HTTPS without URL credentials')
    if wire not in ('openai-chat', 'anthropic-messages'):
        raise ValueError('unknown provider wire')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if time.time() >= deadline:
                self.send_error(504, 'Investigation wall deadline exhausted')
                return
            try:
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise RequestRejected(400) from None
                url, headers = request_mapping(wire, self.command, self.path, self.headers, length, upstream)
            except RequestRejected as exc:
                self.send_error(exc.status)
                return
            body = self.rfile.read(length)
            if audit is not None:
                audit.scan(body)
            try:
                with httpx.Client(timeout=min(timeout, max(.01, deadline - time.time())), transport=transport) as client:
                    response = client.post(url, content=body, headers=headers)
                self.send_response(response.status_code)
                self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                for name, value in response_headers(wire, response.headers).items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except (httpx.HTTPError, BrokenPipeError, ConnectionResetError):
                # No exception/header/body logging: exceptions can carry request context.
                self.close_connection = True

    return LoopbackServer(('127.0.0.1', 0), Handler)


def serve(upstream, deadline, timeout, state, *, eval_audit_config=None, wire='openai-chat'):
    from .common import write_json

    audit = None
    if eval_audit_config is not None:
        from study.evals.provider_audit import RequestAudit
        audit = RequestAudit(eval_audit_config, state)
    server = make_server(upstream, deadline, timeout, wire=wire, audit=audit)
    suffix = '/v1' if wire == 'openai-chat' else ''
    write_json(state, {'pid': os.getpid(), 'upstream': upstream, 'deadline_unix': deadline,
                       'base_url': f'http://127.0.0.1:{server.server_port}{suffix}', 'wire': wire,
                       'eval_provider_audit': audit is not None,
                       **({'eval_provider_audit_config_sha256': audit.config_sha256} if audit else {})})
    timer = threading.Timer(max(0, deadline - time.time()), lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    server.serve_forever(poll_interval=.1)


def start(run_dir, upstream, deadline, timeout, *, eval_audit_config=None, wire='openai-chat'):
    from . import lab
    state = run_dir / 'provider_bridge.json'
    if state.exists() or state.is_symlink():
        raise RuntimeError('provider deadline bridge state already exists')
    command = [lab.python(), '-m', 'study.eidos_arms.provider_bridge',
               '--upstream', upstream, '--deadline', str(deadline), '--timeout', str(timeout), '--state', str(state)]
    if wire != 'openai-chat':  # Preserve the default bridge argv exactly.
        command.extend(('--wire', wire))
    if eval_audit_config is not None:
        command.extend(('--eval-audit-config', str(eval_audit_config)))
    with (run_dir / 'provider_bridge.log').open('ab') as log:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
                                   env=lab.environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('provider deadline bridge failed; see provider_bridge.log')
            if state.exists():
                receipt = json.loads(state.read_text())
                if receipt.get('pid') != process.pid:
                    raise RuntimeError('provider deadline bridge state has unexpected process identity')
                return process, receipt
            time.sleep(.05)
        raise RuntimeError('provider deadline bridge did not initialize')
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wire', choices=('openai-chat', 'anthropic-messages'), default='openai-chat')
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--deadline', type=float, required=True)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--eval-audit-config', type=Path)
    args = parser.parse_args()
    serve(args.upstream, args.deadline, args.timeout, args.state, eval_audit_config=args.eval_audit_config, wire=args.wire)
