"""Standard-library loopback MCP transport; no scientific functions or API bypasses.

python mcp_http.py TOOL '{"project":"...","command_key":"..."}' [--out response.json]
Useful when a provider emits malformed nested arguments in native tool calls.
The bearer token comes only from EIDOS_TOKEN; it is never printed or placed in argv.

One MCP session is reused (ADR 0025): within a process by ``Session``, and across invocations through a small state file
(``.mcp_session.json`` beside ``lab_context.json``, mode 0600) holding the server's session id, never the token. A
session the server no longer knows (HTTP 404) is re-initialized once and the request re-sent; the server did not
process it. Native MCP clients are unaffected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

PROTOCOL_VERSION = '2025-11-25'
STATE_FILE = '.mcp_session.json'


class Session:
    """One MCP session over loopback streamable HTTP, initialized on first use and reused for every call."""

    def __init__(self, url, token, state_path=None, timeout=60):
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('MCP transport must use loopback HTTP')
        self.url, self.timeout = url, timeout
        self.state_path = Path(state_path) if state_path else None
        self.headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL_VERSION}
        # the state file is bound to this server and token (by digest), so another lab or credential never reuses it
        self._owner = hashlib.sha256((url + '\n' + token).encode()).hexdigest()[:32]
        self.session_id = self._load()
        self.initializations = 0
        self._next_id = 1

    # -- state --------------------------------------------------------------------------------------------------------
    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return None
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None
        return state.get('session_id') if state.get('owner') == self._owner else None

    def _save(self):
        if not self.state_path:
            return
        if self.session_id is None:
            self.state_path.unlink(missing_ok=True)
            return
        tmp = self.state_path.with_name(self.state_path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            json.dump({'owner': self._owner, 'session_id': self.session_id}, fh)
        os.replace(tmp, self.state_path)

    # -- wire ---------------------------------------------------------------------------------------------------------
    def _send(self, body):
        headers = dict(self.headers)
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            if response.headers.get('Mcp-Session-Id'):
                self.session_id = response.headers['Mcp-Session-Id']
            raw = response.read().decode()
            if not raw.strip():
                return None
            if response.headers.get('Content-Type', '').startswith('text/event-stream'):
                messages = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith('data:')]
                return next(m for m in reversed(messages) if m.get('id') == body.get('id'))
            return json.loads(raw)

    def _id(self):
        self._next_id += 1
        return self._next_id

    def initialize(self):
        self.session_id = None
        self._send({'jsonrpc': '2.0', 'id': self._id(), 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
            'clientInfo': {'name': 'study-loopback-transport', 'version': '2'}}})
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.initializations += 1
        self._save()

    def request(self, method, params):
        """Send one request on the session, initializing it first if needed. A session the server no longer knows is
        re-initialized once."""
        for attempt in (0, 1):
            if not self.session_id:
                self.initialize()
            try:
                response = self._send({'jsonrpc': '2.0', 'id': self._id(), 'method': method, 'params': params})
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and attempt == 0:  # expired or unknown session: not processed, safe to re-send
                    self.session_id = None
                    continue
                raise
            if 'error' in response:
                raise RuntimeError(json.dumps(response['error']))
            return response['result']
        raise AssertionError('unreachable')

    def call(self, tool, arguments):
        return self.request('tools/call', {'name': tool, 'arguments': arguments})


def call(url, token, tool, arguments, state_path=None):
    return Session(url, token, state_path).call(tool, arguments)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('tool')
    p.add_argument('arguments', help='JSON object or @path/to/arguments.json')
    p.add_argument('--out', type=Path)
    p.add_argument('--new-session', action='store_true', help='Initialize a fresh MCP session.')
    args = p.parse_args()
    context = json.loads(Path('lab_context.json').read_text())
    value = args.arguments
    if value.startswith('@'):
        value = Path(value[1:]).read_text()
    session = Session(context['mcp_url'], os.environ['EIDOS_TOKEN'], Path(STATE_FILE))
    if args.new_session:
        session.session_id = None
    result = session.call(args.tool, json.loads(value))
    if 'structuredContent' in result:  # the text content repeats it; print the payload once (ADR 0025)
        result = {k: v for k, v in result.items() if k != 'content'}
    text = json.dumps(result, separators=(',', ':')) + '\n'
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end='')
    return 1 if result.get('isError') else 0


if __name__ == '__main__':
    raise SystemExit(main())
