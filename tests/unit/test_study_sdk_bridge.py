"""Real pinned SDK clients through a bridge process to a loopback HTTP upstream."""
import asyncio
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize('via_model', [False, True])
def test_anthropic_sdk_through_bridge_process(tmp_path, monkeypatch, via_model):
    from anthropic import AsyncAnthropic
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters
    from study.exp.treatment import resolve

    from eidos.investigator.pydantic_ai_runtime import build_model

    seen = []
    reply = dict(id='msg_fixture', type='message', role='assistant', model='claude-haiku-5-5',
                 content=[dict(type='text', text='loopback response')], stop_reason='end_turn',
                 stop_sequence=None, usage=dict(input_tokens=3, output_tokens=2))

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append((self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    # Keep production's HTTPS-only validation. This test-only transport maps that
    # URL onto the real loopback HTTP fake, with no external sockets or DNS.
    state = tmp_path / 'port'
    script = '''
import sys, time
from pathlib import Path
import httpx
from study.eidos_arms.provider_bridge import make_server
def forward(request):
    with httpx.Client(trust_env=False) as client:
        return client.post(str(request.url).replace('https:', 'http:', 1),
                           content=request.content, headers=request.headers)
server = make_server('https://127.0.0.1:' + sys.argv[1], time.time() + 60, 10,
                     wire='anthropic-messages', transport=httpx.MockTransport(forward))
Path(sys.argv[2]).write_text(str(server.server_port))
server.serve_forever()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(upstream.server_port), str(state)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(200):
            if state.exists() or process.poll() is not None:
                break
            time.sleep(.01)
        assert state.exists(), process.communicate(timeout=2)[1].decode()
        url = 'http://127.0.0.1:' + state.read_text()
        monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-sdk-key')
        if via_model:
            cfg = resolve(provider='anthropic-haiku-5-5').investigator
            model = build_model({**cfg['settings'], 'model': cfg['model'], 'base_url': url})
            client = model.client
        else:
            client = AsyncAnthropic(api_key='fixture-sdk-key', base_url=url, max_retries=0)
        assert client.max_retries == 0
        emitted = []

        async def capture(request):
            emitted.append(await request.aread())

        client._client.event_hooks['request'].append(capture)

        async def call():
            try:
                if via_model:
                    result = await model.request([ModelRequest(parts=[UserPromptPart('hi')])],
                                                 {'max_tokens': 16}, ModelRequestParameters())
                    assert result.parts[0].content == 'loopback response'
                    assert result.model_name == reply['model']
                else:
                    result = await client.messages.create(model='claude-haiku-5-5', max_tokens=16,
                                                          messages=[dict(role='user', content='hi')])
                    assert result.model_dump(exclude_unset=True) == reply
            finally:
                await client.close()

        asyncio.run(call())
        assert len(seen) == len(emitted) == 1
        path, headers, body = seen[0]
        assert path == '/v1/messages' and body == emitted[0]
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers['x-api-key'] == 'fixture-sdk-key'
        assert headers['anthropic-version'] == '2026-10-21'
        assert headers['content-type'] == 'application/json'
        assert set(headers) == {'x-api-key', 'anthropic-version', 'content-type', 'host',
                                'accept', 'accept-encoding', 'connection', 'user-agent', 'content-length'}
    finally:
        process.terminate()
        process.communicate(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
