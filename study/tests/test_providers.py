"""Offline provider profiles, lab credentials and study dispatch contracts."""
import copy
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from study import launcher
from study.eidos_arms import arm4, common, lab
from study.exp import providers, treatment


@pytest.fixture(autouse=True)
def default_selection(monkeypatch):
    monkeypatch.delenv('EIDOS_STUDY_PROVIDER', raising=False)
    monkeypatch.delenv('EIDOS_STUDY_TREATMENT', raising=False)
    monkeypatch.delenv('EIDOS_STUDY_LIVE_DISPATCH', raising=False)


def test_default_identity_and_selection(monkeypatch):
    base = json.loads((treatment.ROOT / 'study/eidos_arms/investigator.json').read_text())
    assert treatment.resolve(provider=providers.DEFAULT).investigator == base
    assert providers.load().served_models == ['MiniMax-M3.1-Flash']
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'anthropic-haiku-5-5')
    resolved = treatment.resolve()
    assert resolved.provider.id == 'anthropic-haiku-5-5'
    assert resolved.investigator['model'] == 'anthropic:claude-haiku-5-5'
    assert resolved.investigator['settings']['price_per_mtok'] == {'input': .1, 'output': .5}
    assert treatment.resolve(provider=providers.DEFAULT).investigator == base
    assert treatment.resolve('v2-portfolio').investigator['exploration']['policy'] == 'exploration-policy/3'
    with pytest.raises(ValueError, match='REPLACE_ME'):
        treatment.resolve(provider='openai-default')
    with pytest.raises(ValueError, match='unknown provider'):
        providers.load('../other')
    with pytest.raises(ValueError, match='unknown provider'):
        providers.load('unknown')
    providers.check()


@pytest.mark.parametrize('change', [
    {'extra': 1}, {'schema': 'bad'}, {'credential_env': 'TOKEN'}, {'upstream': 'http://example.com'},
    {'upstream': 'https://user:pass@example.com'}, {'upstream': 'https://'},
    {'wire': 'other'}, {'served_models': []}, {'price_per_mtok': {'input': 1}},
    {'price_per_mtok': {'input': -1, 'output': 2}}, {'model_usd_per_run': .6},
    {'investigator': {'extra': 1}}, {'investigator': {'settings': {'thinking': True}}},
    {'arm2': {'model': 'x', 'anthropic_base_url': 'http://example.com'}},
])
def test_validation_refuses_invalid_profiles(change):
    data = copy.deepcopy(providers.load().data)
    data.update(change)
    with pytest.raises(ValueError):
        providers.validate(data)


def test_profile_before_treatment_overrides(tmp_path, monkeypatch):
    definition = json.loads((treatment.registry_module.TREATMENTS / 'pilot4-v1.json').read_text())
    definition.update(id='test', overrides={'settings': {'output_mode': 'repair'}})
    (tmp_path / 'test.json').write_text(json.dumps(definition))
    monkeypatch.setattr(treatment.registry_module, 'TREATMENTS', tmp_path)
    resolved = treatment.resolve('test', provider='anthropic-haiku-5-5')
    assert resolved.investigator['settings']['output_mode'] == 'repair'
    assert resolved.investigator['model'] == 'anthropic:claude-haiku-5-5'


def test_worker_refuses_provider_drift(monkeypatch):
    original = treatment.resolve()
    meta = {'treatment': original.record(), 'provider': original.provider.record()}
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'anthropic-sonnet-5-5')
    with pytest.raises(ValueError, match='provider'):
        treatment.for_run(meta)


@pytest.mark.parametrize('name', ['minimax-m3.1-flash', 'anthropic-haiku-5-5', 'anthropic-sonnet-5-5', 'openai-default'])
def test_keychain_to_stdin_only(tmp_path, monkeypatch, name):
    # Complete the OpenAI placeholder only inside this isolated fixture registry.
    registry = tmp_path / 'profiles'
    registry.mkdir()
    data = copy.deepcopy(providers.load(name, allow_placeholder=True).data)
    if name == 'openai-default':
        data['investigator']['model'] = 'openai:fixture-openai'
    (registry / (name + '.json')).write_text(json.dumps(data))
    monkeypatch.setattr(providers, 'DIRECTORY', registry)
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', name)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'parent-anthropic')
    monkeypatch.setenv('OPENAI_API_KEY', 'parent-openai')
    monkeypatch.setenv('USER', 'fixture-user')
    binary = tmp_path / 'bin'
    binary.mkdir()
    security = binary / 'security'
    security.write_text('#!/bin/sh\n[ "$1" = find-generic-password ] || exit 2\nprintf fixture-provider-secret\n')
    security.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.defpath)
    monkeypatch.setattr(lab, 'SECURITY', 'security')
    capture = tmp_path / 'capture.json'
    exe = binary / 'lab'
    program = ('import json,os,sys; from pathlib import Path; '
               f'Path({str(capture)!r}).write_text(json.dumps(dict(argv=sys.argv[1:], '
               'stdin=sys.stdin.read(), secrets={k:os.environ.get(k) for k in '
               "['ANTHROPIC_API_KEY','OPENAI_API_KEY']})))")
    exe.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(program) + ' "$@"\n')
    exe.chmod(0o755)
    home = tmp_path / 'lab-home'
    (home / 'config').mkdir(parents=True)
    (home / 'config/lab.json').write_text(json.dumps({'lab_python': str(binary / 'python')}))
    monkeypatch.setattr(lab, 'home', lambda: home)
    monkeypatch.setattr(lab, 'python', lambda: str(binary / 'python'))
    monkeypatch.setattr(lab, 'guard_disk', lambda: None)
    monkeypatch.setattr(lab, 'build_record', lambda: {})
    monkeypatch.setattr(lab, 'ensure_mcp', lambda: None)
    client = Mock()
    client.health.side_effect = RuntimeError('not running')
    monkeypatch.setattr(lab, 'client', lambda: client)
    lab.start()
    observed = json.loads(capture.read_text())
    assert observed['stdin'] == data['credential_env'] + '=fixture-provider-secret\n'
    assert observed['argv'] == ['start', '--credentials-stdin']
    assert all(value is None for value in observed['secrets'].values())
    assert 'fixture-provider-secret' not in json.dumps(observed['argv'])
    for path in home.rglob('*'):
        if path.is_file():
            assert b'fixture-provider-secret' not in path.read_bytes()
    assert json.loads((home / 'study-provider.json').read_text())['provider'] == name


@pytest.mark.parametrize('previous,requested,restarts', [
    (None, providers.DEFAULT, False), (None, 'anthropic-haiku-5-5', True),
    (providers.DEFAULT, 'anthropic-haiku-5-5', True),
    ('anthropic-haiku-5-5', 'anthropic-haiku-5-5', False),
    ('anthropic-haiku-5-5', providers.DEFAULT, True),
])
def test_lab_switch_and_fixture_without_keychain(tmp_path, monkeypatch, previous, requested, restarts):
    monkeypatch.setattr(lab, 'home', lambda: tmp_path)
    monkeypatch.setattr(lab, 'python', lambda: 'fixture-python')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/lab.json').write_text(json.dumps({'lab_python': 'fixture-python'}))
    if previous:
        (tmp_path / 'study-provider.json').write_text(json.dumps({'provider': previous, 'credentials': False}))
    monkeypatch.setattr(lab, 'guard_disk', lambda: None)
    monkeypatch.setattr(lab, 'build_record', lambda: {})
    monkeypatch.setattr(lab, 'ensure_mcp', lambda: None)
    monkeypatch.setattr(lab, 'client', Mock(return_value=Mock()))
    stop, cli = Mock(), Mock()
    monkeypatch.setattr(lab, 'stop', stop)
    monkeypatch.setattr(lab, 'cli', cli)
    keychain = Mock(side_effect=AssertionError('Keychain must not be read'))
    monkeypatch.setattr(lab.subprocess, 'check_output', keychain)
    # A missing marker on a legacy running lab is credential-bearing.
    credentials = False if previous else True
    if previous is None:
        # Separate fixture startup below tests credentials=False on an unhealthy lab.
        monkeypatch.setattr(lab.subprocess, 'check_output', Mock(return_value='fixture-secret'))
    lab.start(requested, credentials=credentials)
    assert stop.called == restarts
    assert [call.args[0] for call in cli.call_args_list] == (['start'] if restarts else [])
    if previous:
        keychain.assert_not_called()


def test_served_patterns_and_fixture_ids(monkeypatch):
    def guard(model):
        return launcher.version_guard([{'status': 'ok', 'model_ids_seen': [model]}])
    assert guard('MiniMax-M3.1-Flash') is None
    assert guard('MiniMax-M3.1-Flash-new')
    assert guard('fixture:script')
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'anthropic-haiku-5-5')
    assert guard('claude-haiku-5-5-20261101') is None
    assert guard('claude-sonnet-5-5')
    fixture = treatment.resolve()
    fixture.investigator['runtime'] = 'fixture'
    monkeypatch.setattr(treatment, 'resolve', lambda: fixture)
    assert guard('fixture:script') is None
    assert guard('fixture:')


@pytest.mark.parametrize('name', [providers.DEFAULT, 'anthropic-haiku-5-5', 'anthropic-sonnet-5-5'])
@pytest.mark.parametrize('fixture', [False, True])
def test_arm4_plumbs_bridge_prices_and_usd_limit(tmp_path, monkeypatch, name, fixture):
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', name)
    resolved = treatment.resolve()
    if fixture:
        resolved.investigator['runtime'] = 'fixture'
    monkeypatch.setattr(arm4, 'prepare', lambda *a: ('project', {'charter_id': 'charter', 'artifacts': {}, 'task': {}}))
    monkeypatch.setattr(treatment, 'for_run', lambda meta: resolved)
    (tmp_path / 'task').mkdir()
    (tmp_path / 'task/brief.md').write_text('fixture task')
    monkeypatch.setattr(arm4, 'prompt', lambda *a: 'fixture prompt')
    monkeypatch.setattr('study.common.record_live_delivery', lambda *a: None)
    start = Mock()
    monkeypatch.setattr(lab, 'start', start)
    captured = {}

    class StopProbe(Exception):
        pass

    def command(name, payload, **kwargs):
        captured.update(payload)
        raise StopProbe

    monkeypatch.setattr(lab, 'client', lambda: SimpleNamespace(command=command, close=lambda: None))
    from study.eidos_arms import provider_bridge
    bridge = Mock(return_value=(SimpleNamespace(poll=lambda: 0, pid=42, returncode=0), {'base_url': 'http://127.0.0.1:9', 'upstream': resolved.provider.upstream}))
    monkeypatch.setattr(provider_bridge, 'start', bridge)
    with pytest.raises(StopProbe):
        arm4.run(tmp_path, {'envelope': {'wall_s': 30}})
    if fixture:
        bridge.assert_not_called()
        assert start.call_args.kwargs['credentials'] is False
    else:
        assert bridge.call_args.args[1] == resolved.provider.upstream
        assert bridge.call_args.kwargs.get('wire', 'openai-chat') == resolved.provider.wire
        assert captured['investigator']['settings']['base_url'] == 'http://127.0.0.1:9'
    if name == providers.DEFAULT:
        assert 'model_usd' not in captured['limits']
        assert 'price_per_mtok' not in captured['investigator']['settings']
    else:
        assert captured['limits']['model_usd'] == resolved.provider.model_usd_per_run
        assert captured['investigator']['settings']['price_per_mtok'] == resolved.provider.price_per_mtok


def test_anthropic_usage_receipt(tmp_path):
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text(json.dumps({'type': 'model_response', 'data': {'response': [{
        'kind': 'response', 'provider_name': 'anthropic', 'provider_response_id': 'msg_fixture',
        'usage': {'input_tokens': 100, 'output_tokens': 12, 'cache_read_tokens': 0, 'cache_write_tokens': 0}
    }]}}) + '\n')
    tokens, models, usd, _ = common.usage([{'type': 'model.usage', 'body': {'usage': {
        'provider': 'anthropic', 'input_tokens': 100, 'output_tokens': 12,
        'served_models': ['claude-haiku-5-5'], 'usd': .000016}}}], transcript)
    assert tokens == dict(input=100, cache_read=0, cache_write=0, output=12, total=112)
    assert models == ['claude-haiku-5-5'] and usd == .000016


def test_openai_has_no_claude_cli_path():
    with pytest.raises(ValueError, match='no arm2 Claude CLI path'):
        providers.load('openai-default', allow_placeholder=True).cli_settings()


def test_fixture_start_never_reads_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, 'home', lambda: tmp_path)
    monkeypatch.setattr(lab, 'python', lambda: 'fixture-python')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/lab.json').write_text(json.dumps({'lab_python': 'fixture-python'}))
    monkeypatch.setattr(lab, 'guard_disk', lambda: None)
    monkeypatch.setattr(lab, 'build_record', lambda: {})
    monkeypatch.setattr(lab, 'ensure_mcp', lambda: None)
    client = Mock()
    client.health.side_effect = RuntimeError('not running')
    monkeypatch.setattr(lab, 'client', lambda: client)
    keychain = Mock(side_effect=AssertionError('Keychain must not be read'))
    monkeypatch.setattr(lab.subprocess, 'check_output', keychain)
    cli = Mock()
    monkeypatch.setattr(lab, 'cli', cli)
    lab.start(credentials=False)
    keychain.assert_not_called()
    assert cli.call_args.args == ('start',)
    assert 'stdin' not in cli.call_args.kwargs


@pytest.mark.parametrize('name', [providers.DEFAULT, 'anthropic-haiku-5-5', 'anthropic-sonnet-5-5'])
def test_claude_cli_uses_profile_settings(tmp_path, monkeypatch, name):
    from study import run_arm
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', name)
    profile = providers.load()
    (tmp_path / 'arm.json').write_text(json.dumps({'card': 'N01'}))
    monkeypatch.setattr(run_arm, 'agent_env', lambda root: {})
    monkeypatch.setattr('study.toolkit.build_toolkit.build_toolkit', lambda *a, **kw: None)
    monkeypatch.setattr(run_arm, 'render_prompt', lambda *a: 'fixture prompt')
    monkeypatch.setattr(run_arm, 'record_live_delivery', lambda *a: None)
    monkeypatch.setattr(run_arm, 'sandbox_command', lambda root, command: command)
    secret = 'fixture-cli-key'
    keychain = Mock(return_value=secret)
    monkeypatch.setattr(run_arm.subprocess, 'check_output', keychain)
    stream = Mock(return_value={'status': 'crash'})
    monkeypatch.setattr(run_arm, 'run_stream', stream)
    run_arm.run_claude(tmp_path, 2)
    assert keychain.call_args.args[0][-2:] == [profile.keychain_item, '-w']
    command, env = stream.call_args.args[1:]
    assert command[command.index('--model') + 1] == profile.cli_settings()['model']
    assert env['ANTHROPIC_API_KEY'] == secret and secret not in command


def test_provider_bound_prompt_receipts(tmp_path, monkeypatch):
    from study.common import record_prompt
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'anthropic-haiku-5-5')
    root = tmp_path / 'N01-r1/arm4'
    root.mkdir(parents=True)
    resolved = treatment.resolve()
    meta = dict(arm=4, card='N01', run_index=1, block_id='N01-r1',
                charter_amendment_commit='a' * 40, charter_amendment_sha256='b' * 64,
                provider=resolved.provider.record(), treatment=resolved.record())
    record_prompt(root, meta)
    receipts = list((root.parent / 'prompt-receipts').glob('*.json'))
    assert receipts and all(json.loads(path.read_text())['provider'] == resolved.provider.record() for path in receipts)


@pytest.mark.parametrize('name', [providers.DEFAULT, 'anthropic-haiku-5-5', 'anthropic-sonnet-5-5'])
def test_claude_environment_uses_profile_base_url(tmp_path, monkeypatch, name):
    from study.common import agent_env
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', name)
    profile = providers.load()
    env = agent_env(tmp_path)
    assert env['ANTHROPIC_BASE_URL'] == profile.cli_settings()['anthropic_base_url']
    assert env['EIDOS_STUDY_PROVIDER'] == name
    assert 'ANTHROPIC_API_KEY' not in env and 'OPENAI_API_KEY' not in env
