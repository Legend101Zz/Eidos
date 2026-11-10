"""Standard-library loopback MCP transport; no scientific functions or API bypasses.

python mcp_http.py TOOL '{"project":"...","command_key":"..."}' [--out response.json]
Useful when a provider emits malformed nested arguments in native tool calls.
The bearer token comes only from EIDOS_TOKEN; it is never printed or placed in argv.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def call(url, token, tool, arguments):
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('MCP transport must use loopback HTTP')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
               'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}

    def send(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.headers.get('Mcp-Session-Id'):
                headers['Mcp-Session-Id'] = response.headers['Mcp-Session-Id']
            raw = response.read().decode()
            if not raw.strip():
                return None
            if response.headers.get('Content-Type', '').startswith('text/event-stream'):
                messages = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith('data:')]
                return next(m for m in reversed(messages) if m.get('id') == body.get('id'))
            return json.loads(raw)

    send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'study-loopback-transport', 'version': '1'}}})
    send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    response = send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                     'params': {'name': tool, 'arguments': arguments}})
    if 'error' in response:
        raise RuntimeError(json.dumps(response['error']))
    return response['result']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('tool')
    p.add_argument('arguments', help='JSON object or @path/to/arguments.json')
    p.add_argument('--out', type=Path)
    args = p.parse_args()
    context = json.loads(Path('lab_context.json').read_text())
    value = args.arguments
    if value.startswith('@'):
        value = Path(value[1:]).read_text()
    result = call(context['mcp_url'], os.environ['EIDOS_TOKEN'], args.tool, json.loads(value))
    text = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end='')
    return 1 if result.get('isError') else 0


if __name__ == '__main__':
    raise SystemExit(main())
