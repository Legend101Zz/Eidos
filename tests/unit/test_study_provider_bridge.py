"""Both study bridge wires against injected transports; no provider traffic."""
import hashlib
import http.client
import json
import socket
import threading
import time
from unittest.mock import patch

import httpx
import pytest
from study.eidos_arms import provider_bridge as bridge
from study.evals.provider_audit import RequestAudit
from study.tests.test_provider_audit_dispatch import META, audit_fixture


@pytest.mark.parametrize('wire,path,headers,url', [
    ('openai-chat', '/v1/chat/completions', {'Authorization': 'Bearer fixture'}, 'https://example.invalid/v1/chat/completions'),
    ('anthropic-messages', '/v1/messages?beta=true', {'x-api-key': 'fixture', 'anthropic-version': '2023-06-01'}, 'https://example.invalid/v1/messages?beta=true'),
])
def test_forward_body_allowlist_audit_and_timeout(tmp_path, wire, path, headers, url):
    run, config, _ = audit_fixture(tmp_path)
    run.mkdir(parents=True)
    (run / 'arm.json').write_text(json.dumps(META))
    audit = RequestAudit(config, run / 'provider_bridge.json')
    body = json.dumps({'messages': [{'content': 'a' * 64}]}).encode()
    observed = []
    upstream = 'https://example.invalid/v1' if wire == 'openai-chat' else 'https://example.invalid'
    headers = {**headers, 'Content-Type': 'application/json', 'x-not-forwarded': 'private',
               'Cookie': 'private', 'anthropic-beta': 'fixture-beta'}
    if wire == 'anthropic-messages':
        headers['Authorization'] = 'Bearer must-not-forward'
    else:
        headers['x-api-key'] = 'must-not-forward'

    def respond(request):
        observed.append(request)
        return httpx.Response(429, content=b'{"fixture":true}', headers={
            'Retry-After': '3', 'Retry-After-Ms': '3000', 'request-id': 'req_fixture',
            'anthropic-ratelimit-requests-remaining': '0', 'Set-Cookie': 'must-not-forward'})

    deadline = time.time() + 5
    server = bridge.make_server(upstream, deadline, 300, wire=wire, audit=audit,
                                transport=httpx.MockTransport(respond))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        assert response.status == 429 and response.read() == b'{"fixture":true}'
        assert response.getheader('Retry-After') == '3' and response.getheader('Retry-After-Ms') == '3000'
        assert response.getheader('Set-Cookie') is None
        assert response.getheader('request-id') == ('req_fixture' if wire == 'anthropic-messages' else None)
        assert response.getheader('anthropic-ratelimit-requests-remaining') == ('0' if wire == 'anthropic-messages' else None)
        sent = observed[0]
        assert str(sent.url) == url and sent.content == body
        # HTTP framing and httpx defaults are transport-owned, never copied from incoming headers.
        mapped = bridge.request_mapping(wire, 'POST', path, headers, len(body), upstream)[1]
        assert set(k.lower() for k in mapped) == ({'authorization', 'content-type'} if wire == 'openai-chat'
                                                   else {'x-api-key', 'anthropic-version', 'anthropic-beta', 'content-type'})
        for key, value in mapped.items():
            assert sent.headers[key] == value
        assert 'cookie' not in sent.headers and 'x-not-forwarded' not in sent.headers
        assert ('authorization' not in sent.headers) if wire == 'anthropic-messages' else ('x-api-key' not in sent.headers)
        assert 0 < sent.extensions['timeout']['read'] <= 5
        row = json.loads(audit.path.read_text())
        assert row == {'request_sha256': hashlib.sha256(body).hexdigest(), 'matched_identifiers': ['a' * 64]}
        assert 'fixture-beta' not in audit.path.read_text()
        for method, target, auth, status in [('POST', '/wrong', headers, 404),
                                            ('POST', path, {}, 400), ('GET', path, headers, 501)]:
            connection.request(method, target, body=b'{}', headers=auth)
            rejected = connection.getresponse()
            assert rejected.status == status
            rejected.read()
        assert len(observed) == 1
        assert len(audit.path.read_text().splitlines()) == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('wire,path,headers', [
    ('openai-chat', '/v1/chat/completions', {'Authorization': 'Bearer fixture'}),
    ('anthropic-messages', '/v1/messages', {'x-api-key': 'fixture', 'anthropic-version': '2023-06-01'}),
])
def test_invalid_requests_and_expired_deadline(wire, path, headers):
    upstream = 'https://example.invalid'
    for method, target, auth, length, status in [
        ('GET', path, headers, 1, 405), ('POST', '/wrong', headers, 1, 404),
        ('POST', path, {}, 1, 400), ('POST', path, headers, 0, 400),
        ('POST', path, headers, 8 * 1024**2 + 1, 400),
    ]:
        with pytest.raises(bridge.RequestRejected) as error:
            bridge.request_mapping(wire, method, target, auth, length, upstream)
        assert error.value.status == status
    if wire == 'anthropic-messages':
        for missing in ('x-api-key', 'anthropic-version'):
            with pytest.raises(bridge.RequestRejected):
                bridge.request_mapping(wire, 'POST', path, {**headers, missing: ' '}, 1, upstream)
    called = []
    server = bridge.make_server(upstream, time.time() - 1, 300, wire=wire,
                                transport=httpx.MockTransport(lambda r: called.append(r)))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        connection.request('POST', path, body=b'{}', headers=headers)
        assert connection.getresponse().status == 504
        assert called == []
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('wire,path,headers,suffix', [
    ('openai-chat', '/v1/chat/completions', 'Authorization: Bearer fixture', '/v1'),
    ('anthropic-messages', '/v1/messages?beta=true', 'x-api-key: fixture\r\nanthropic-version: 2023-06-01', ''),
])
def test_deadline_process_receipt_and_default_argv(tmp_path, monkeypatch, wire, path, headers, suffix):
    import sys

    from study.eidos_arms import lab

    monkeypatch.setattr(lab, 'python', lambda: sys.executable)
    command = []
    real_popen = bridge.subprocess.Popen

    def popen(argv, **kwargs):
        command.extend(argv)
        return real_popen(argv, **kwargs)

    with patch.object(bridge.subprocess, 'Popen', side_effect=popen):
        process, receipt = bridge.start(tmp_path, 'https://example.invalid/v1' if suffix else 'https://example.invalid',
                                        time.time() + 2, 300, wire=wire)
    try:
        assert receipt['wire'] == wire
        assert receipt['base_url'].endswith(suffix) if suffix else not receipt['base_url'].endswith('/v1')
        assert ('--wire' in command) == (wire != 'openai-chat')
        from urllib.parse import urlsplit
        port = urlsplit(receipt['base_url']).port
        with socket.create_connection(('127.0.0.1', port), timeout=4) as connection:
            # Incomplete body: the process deadline kills the read without sending upstream.
            connection.sendall(f'POST {path} HTTP/1.0\r\n{headers}\r\nContent-Length: 999\r\n\r\n'.encode())
            assert connection.recv(1) == b''
        assert process.wait(timeout=3) == 124
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
