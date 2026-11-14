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
    with pytest.raises(ValueError, match='unknown provider'):
        providers.load('../other')
    with pytest.raises(ValueError, match='unknown provider'):
        providers.load('unknown')
    providers.check()


def test_placeholder_profile_refuses_resolution(tmp_path, monkeypatch):
    data = copy.deepcopy(providers.load('openai-default').data)
    data.update(id='placeholder', served_models=['REPLACE_ME*'])
    data['investigator']['model'] = 'openai:REPLACE_ME'
    (tmp_path / 'placeholder.json').write_text(json.dumps(data))
    monkeypatch.setattr(providers, 'DIRECTORY', tmp_path)
    with pytest.raises(ValueError, match='REPLACE_ME'):
        treatment.resolve(provider='placeholder')
    inspected = providers.load('placeholder', allow_placeholder=True)
    with pytest.raises(ValueError, match='REPLACE_ME'):
        treatment.resolve(provider=inspected)


def test_openai_profile_resolves_and_guards_served_models(monkeypatch):
    resolved = treatment.resolve(provider='openai-default')
    assert resolved.investigator['model'] == 'openai:gpt-5-nano'
    assert resolved.investigator['settings']['price_per_mtok'] == resolved.provider.price_per_mtok == {
        'input': .05, 'output': .40}
    assert resolved.provider.model_usd_per_run == .60
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'openai-default')
    for model, accepted in [('gpt-5-nano-2025-08-07', True), ('gpt-5-mini', False), ('GPT-5-NANO', False)]:
        result = launcher.version_guard([{'status': 'ok', 'model_ids_seen': [model]}])
        assert (result is None) == accepted


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
    registry = tmp_path / 'profiles'
    registry.mkdir()
    data = copy.deepcopy(providers.load(name, allow_placeholder=True).data)
    (registry / (name + '.json')).write_text(json.dumps(data))
    monkeypatch.setattr(providers, 'DIRECTORY', registry)
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', name)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'parent-anthropic')
    monkeypatch.setenv('OPENAI_API_KEY', 'parent-openai')
    monkeypatch.setenv('USER', 'fixture-user')
    binary = tmp_path / 'bin'
    binary.mkdir()
    security = binary / 'security'
    expected_item = 'minimax_api_key' if name == providers.DEFAULT else (
        'openai_api_key' if name == 'openai-default' else 'anthropic_api_key')
    security.write_text('#!/bin/sh\n[ "$*" = "find-generic-password -a fixture-user -s '
                        + expected_item + ' -w" ] || exit 2\nprintf fixture-provider-secret\n')
    security.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.defpath)
    monkeypatch.setattr(lab, 'SECURITY', 'security')
    capture = tmp_path / 'capture.json'
    exe = binary / 'lab'
    program = ('import json,os,sys; from pathlib import Path; '
               f'Path({str(capture)!r}).write_text(json.dumps(dict(argv=sys.argv[1:], '
               'stdin=sys.stdin.read(), secrets={k:os.environ.get(k) for k in '
               "['ANTHROPIC_API_KEY','OPENAI_API_KEY']}))); "
               f"print(json.dumps(dict(phase='running', credentials={data['credential_env']!r} and {{{data['credential_env']!r}: 'stdin'}})))")
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
    expected_line = ('ANTHROPIC_API_KEY=fixture-provider-secret\n' if name.startswith('anthropic-')
                     else 'OPENAI_API_KEY=fixture-provider-secret\n')
    assert observed['stdin'] == expected_line
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
    stop, cli = Mock(return_value={'stopped': True}), Mock(return_value=json.dumps({
        'phase': 'running', 'credentials': {providers.load(requested).credential_env: 'stdin'} if not previous else {}}))
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
    assert guard('minimax-m3.1-flash')
    assert guard('fixture:script')
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'anthropic-haiku-5-5')
    assert guard('claude-haiku-5-5-20261101') is None
    assert guard('claude-sonnet-5-5')
    assert guard('Claude-haiku-5-5')
    fixture = treatment.resolve()
    fixture.investigator['runtime'] = 'fixture'
    monkeypatch.setattr(treatment, 'resolve', lambda: fixture)
    assert guard('fixture:script') is None
    assert guard('fixture:')


@pytest.mark.parametrize('name', [providers.DEFAULT, 'anthropic-haiku-5-5', 'anthropic-sonnet-5-5', 'openai-default'])
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
        'usage': {'input_tokens': 130, 'output_tokens': 12, 'cache_read_tokens': 20, 'cache_write_tokens': 10}
    }]}}) + '\n')
    tokens, models, usd, _ = common.usage([{'type': 'model.usage', 'body': {'usage': {
        'provider': 'anthropic', 'input_tokens': 130, 'output_tokens': 12,
        'served_models': ['claude-haiku-5-5'], 'usd': .000016}}}], transcript)
    assert tokens == dict(input=100, cache_read=20, cache_write=10, output=12, total=142)
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
    cli = Mock(return_value=json.dumps({'phase': 'running', 'credentials': {}}))
    monkeypatch.setattr(lab, 'cli', cli)
    lab.start(credentials=False)
    keychain.assert_not_called()
    assert cli.call_args.args == ('start', '--credentials-stdin')
    assert cli.call_args.kwargs['stdin'] == ''


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
    assert 'EIDOS_STUDY_PROVIDER' not in env
    assert 'ANTHROPIC_API_KEY' not in env and 'OPENAI_API_KEY' not in env


def isolated_lab(tmp_path, monkeypatch, *, healthy=True, previous=None):
    monkeypatch.setattr(lab, 'home', lambda: tmp_path)
    monkeypatch.setattr(lab, 'python', lambda: 'fixture-python')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/lab.json').write_text(json.dumps({'lab_python': 'fixture-python'}))
    monkeypatch.setattr(lab, 'guard_disk', lambda: None)
    monkeypatch.setattr(lab, 'build_record', lambda: {})
    monkeypatch.setattr(lab, 'ensure_mcp', lambda: None)
    client = Mock()
    if not healthy:
        client.health.side_effect = RuntimeError('not running')
    monkeypatch.setattr(lab, 'client', lambda: client)
    monkeypatch.setattr(lab.subprocess, 'check_output', Mock(return_value='fixture-secret'))
    if previous is not None:
        (tmp_path / 'study-provider.json').write_text(json.dumps(previous))


@pytest.mark.parametrize('answer,healthy', [
    ({'already_running': True, 'credentials': 'not applied: already running'}, True),
    ({'pid': 123, 'credentials': 'not applied: stale supervisor'}, False),
    ({'stopped': False}, True),
    ({'was_running': False, 'stopped': False}, True),
])
def test_switch_rejects_unapplied_credentials(tmp_path, monkeypatch, answer, healthy):
    previous = {'provider': providers.DEFAULT, 'credentials': True}
    isolated_lab(tmp_path, monkeypatch, healthy=healthy, previous=previous)

    binary = tmp_path / 'bin'
    binary.mkdir()
    monkeypatch.setattr(lab, 'python', lambda: str(binary / 'python'))
    (tmp_path / 'config/lab.json').write_text(json.dumps({'lab_python': str(binary / 'python')}))
    calls = tmp_path / 'calls.jsonl'
    script = tmp_path / 'fake_lab.py'
    script.write_text(
        'import json,sys\nfrom pathlib import Path\n'
        f'with Path({str(calls)!r}).open("a") as out: out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        f'answer = {answer!r}\n'
        'if sys.argv[1] == "stop":\n'
        '    print(json.dumps(answer if "stopped" in answer else {"stopped": True}))\n'
        'else:\n    sys.stdin.read()\n    print(json.dumps(answer))\n')
    executable = binary / 'lab'
    executable.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
                          + shlex.quote(str(script)) + ' "$@"\n')
    executable.chmod(0o755)
    with pytest.raises(RuntimeError, match='(stop|start|credentials)'):
        lab.start('anthropic-haiku-5-5')
    assert json.loads((tmp_path / 'study-provider.json').read_text()) == previous
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum(c[0] == 'stop' for c in invocations) == (2 if healthy else 1)
    if answer.get('stopped') is False:
        assert all(c[0] == 'stop' for c in invocations)


def test_openai_arm_independent_environment(tmp_path, monkeypatch):
    from study import common as agent_common, fresh_check
    registry = tmp_path / 'profiles'
    registry.mkdir()
    data = copy.deepcopy(providers.load('openai-default', allow_placeholder=True).data)
    data['investigator']['model'] = 'openai:fixture-openai'
    (registry / 'openai-default.json').write_text(json.dumps(data))
    monkeypatch.setattr(providers, 'DIRECTORY', registry)
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', 'openai-default')
    env = agent_common.agent_env(tmp_path)
    assert 'EIDOS_STUDY_PROVIDER' not in env
    assert not any(k.endswith('_API_KEY') for k in env)
    monkeypatch.setattr(fresh_check, 'sandbox_command', lambda *a, **kw: ['fixture-command'])
    process = Mock(returncode=0)
    process.communicate.return_value = (b'fixture output', b'')
    monkeypatch.setattr(fresh_check.subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setattr(fresh_check, 'stop_process_tree', lambda p: None)
    assert fresh_check.candidate_command(tmp_path, ['fixture']) == 'fixture output'

    from study.practice import pipeline_c, run_gate
    from study.sandbox import probe
    from study.toolkit import verify

    class EnvironmentBuilt(Exception):
        pass

    def boundary(*args, **kwargs):
        assert kwargs['env'] == agent_common.agent_env(kwargs['cwd'])
        assert 'EIDOS_STUDY_PROVIDER' not in kwargs['env']
        raise EnvironmentBuilt

    monkeypatch.setattr(verify, 'build_toolkit', lambda root: tmp_path)
    monkeypatch.setattr(verify, 'read_json', lambda path: {})
    monkeypatch.setattr(verify, 'sandbox_command', lambda *a, **kw: ['fixture'])
    monkeypatch.setattr(verify.subprocess, 'run', boundary)
    with pytest.raises(EnvironmentBuilt):
        verify.verify(tmp_path)
    # The probe's public isolation targets are all fabricated inside this temp root.
    probe_root = tmp_path / 'probe-root'
    probe_root.mkdir()
    (probe_root / 'README.md').write_text('fixture')
    (tmp_path / 'pilot-1/grading').mkdir(parents=True)
    (tmp_path / '.ssh').mkdir()
    monkeypatch.setattr(probe, 'ROOT', probe_root)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(probe, 'sandbox_command', lambda *a, **kw: ['fixture'])
    with pytest.raises(EnvironmentBuilt):
        probe.probe(tmp_path)

    # Practice flows share candidate_command; stop after its environment is built.
    def practice_boundary(*args, **kwargs):
        assert not any(k.endswith('_API_KEY') for k in kwargs['env'])
        raise EnvironmentBuilt
    monkeypatch.setattr(fresh_check.subprocess, 'Popen', practice_boundary)
    bundle, controller = tmp_path / 'bundle', tmp_path / 'controller'
    bundle.mkdir()
    controller.mkdir()
    (bundle / 'project.py').write_text('# fixture')
    (controller / 'test_inputs.npz').write_bytes(b'fixture')
    monkeypatch.setattr(run_gate, 'require_bundle_registration', lambda *a, **kw: None)
    with pytest.raises(EnvironmentBuilt):
        run_gate.supplied_baseline(tmp_path / 'practice', bundle, controller)
    import numpy as np
    np.savez(controller / 'test_labels.npz', y=[1])
    monkeypatch.setattr(pipeline_c, 'require_bundle_registration', lambda *a, **kw: None)
    with pytest.raises(EnvironmentBuilt):
        pipeline_c.fresh_c(tmp_path / 'practice-c', bundle, controller,
                           {'files': {'project.py': '# fixture'}}, {}, 0, 0)


def test_default_agent_environment_main_golden(tmp_path, monkeypatch):
    from study import common as agent_common
    # Literal shape from git show 85fd308:study/common.py; pin dynamic paths too.
    for key in ('LANG', 'LC_ALL', 'TERM', 'USER'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(agent_common, 'research_python', lambda: '/fixture/python')
    monkeypatch.setattr(agent_common.shutil, 'which', lambda _: '/fixture/node')
    root = str(tmp_path.resolve())
    assert agent_common.agent_env(tmp_path) == {
        'HOME': root + '/.home', 'TMPDIR': root + '/.tmp', 'CLAUDE_CODE_TMPDIR': root + '/.tmp',
        'XDG_CACHE_HOME': root + '/.cache', 'XDG_CONFIG_HOME': root + '/.home',
        'CLAUDE_CONFIG_DIR': root + '/.claude-cfg',
        'PATH': '/fixture:/fixture:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
        'VECLIB_MAXIMUM_THREADS': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'ANTHROPIC_BASE_URL': 'https://api.minimax.io/anthropic',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
    }


@pytest.mark.parametrize('name,item,credential,upstream,wire,served,mode,price,limit,arm2', [
    ('minimax-m3.1-flash', 'minimax_api_key', 'OPENAI_API_KEY', 'https://api.minimax.io/v1',
     'openai-chat', ['MiniMax-M3.1-Flash'], 'json', None, None,
     {'anthropic_base_url': 'https://api.minimax.io/anthropic', 'model': 'MiniMax-M3.1-Flash'}),
    ('anthropic-haiku-5-5', 'anthropic_api_key', 'ANTHROPIC_API_KEY', 'https://api.anthropic.com',
     'anthropic-messages', ['claude-haiku-5-5*'], 'tool', {'input': .1, 'output': .5}, .6,
     {'anthropic_base_url': 'https://api.anthropic.com', 'model': 'claude-haiku-5-5'}),
    ('anthropic-sonnet-5-5', 'anthropic_api_key', 'ANTHROPIC_API_KEY', 'https://api.anthropic.com',
     'anthropic-messages', ['claude-sonnet-5-5*'], 'repair', {'input': 2., 'output': 10.}, 6.,
     {'anthropic_base_url': 'https://api.anthropic.com', 'model': 'claude-sonnet-5-5'}),
    ('openai-default', 'openai_api_key', 'OPENAI_API_KEY', 'https://api.openai.com/v1',
     'openai-chat', ['gpt-5-nano*'], 'json', {'input': .05, 'output': .40}, .60, None),
])
def test_profile_security_golden(name, item, credential, upstream, wire, served, mode, price, limit, arm2):
    profile = providers.load(name, allow_placeholder=True)
    assert (profile.keychain_item, profile.credential_env, profile.upstream, profile.wire,
            profile.served_models, profile.price_per_mtok, profile.model_usd_per_run) == (
                item, credential, upstream, wire, served, price, limit)
    assert profile.data.get('arm2') == arm2
    if name == providers.DEFAULT:
        assert profile.investigator == {}
        assert treatment.resolve().investigator['settings']['output_mode'] == mode
    else:
        assert profile.investigator['settings']['output_mode'] == mode


def test_default_bridge_argv_main_golden(tmp_path, monkeypatch):
    from study.eidos_arms import provider_bridge
    monkeypatch.setattr(lab, 'python', lambda: '/fixture/python')
    monkeypatch.setattr(lab, 'environment', lambda: {})
    process = Mock(pid=42)
    process.poll.return_value = None
    def spawn(argv, **kwargs):
        (tmp_path / 'provider_bridge.json').write_text(json.dumps({'pid': 42}))
        return process
    popen = Mock(side_effect=spawn)
    monkeypatch.setattr(provider_bridge.subprocess, 'Popen', popen)
    provider_bridge.start(tmp_path, providers.load().upstream, 123., 300)
    # Literal argv from git show 85fd308:study/eidos_arms/provider_bridge.py.
    assert popen.call_args.args[0] == [
        '/fixture/python', '-m', 'study.eidos_arms.provider_bridge', '--upstream',
        'https://api.minimax.io/v1', '--deadline', '123.0', '--timeout', '300',
        '--state', str(tmp_path / 'provider_bridge.json')]


@pytest.mark.parametrize('previous_credentials,credentials', [(True, False), (False, True)])
def test_credential_mode_switch_restarts(tmp_path, monkeypatch, previous_credentials, credentials):
    isolated_lab(tmp_path, monkeypatch, previous={'provider': providers.DEFAULT,
                                                'credentials': previous_credentials})
    stop = Mock(return_value={'stopped': True})
    cli = Mock(return_value=json.dumps({'phase': 'running',
        'credentials': {'OPENAI_API_KEY': 'stdin'} if credentials else {}}))
    monkeypatch.setattr(lab, 'stop', stop)
    monkeypatch.setattr(lab, 'cli', cli)
    lab.start(credentials=credentials)
    stop.assert_called_once()
    assert cli.call_args.args == ('start', '--credentials-stdin')
    assert cli.call_args.kwargs['stdin'] == ('OPENAI_API_KEY=fixture-secret\n' if credentials else '')
    assert json.loads((tmp_path / 'study-provider.json').read_text())['credentials'] is credentials


def test_stop_retry_and_start_confirmation(tmp_path, monkeypatch):
    isolated_lab(tmp_path, monkeypatch, previous={'provider': providers.DEFAULT, 'credentials': True})
    calls = []
    def command(action, *args, **kwargs):
        calls.append(action)
        if action == 'stop':
            return json.dumps({'stopped': calls.count('stop') == 2})
        return json.dumps({'phase': 'running', 'credentials': {'ANTHROPIC_API_KEY': 'stdin'}})
    monkeypatch.setattr(lab, 'cli', command)
    lab.start('anthropic-haiku-5-5')
    assert calls == ['stop', 'stop', 'start']
    assert json.loads((tmp_path / 'study-provider.json').read_text())['provider'] == 'anthropic-haiku-5-5'


def test_fixture_required_audit_refuses_before_start(tmp_path, monkeypatch):
    resolved = treatment.resolve()
    resolved.investigator['runtime'] = 'fixture'
    monkeypatch.setattr(treatment, 'for_run', lambda _: resolved)
    start = Mock()
    monkeypatch.setattr(lab, 'start', start)
    with pytest.raises(RuntimeError, match='required provider audit'):
        arm4.run(tmp_path, {}, eval_audit_config=tmp_path / 'audit.json')
    start.assert_not_called()


def test_provider_forwarded_to_worker_and_fallback_record(tmp_path, monkeypatch):
    # Selection from the default (not inherited env) catches omitted forwarding.
    monkeypatch.setattr(launcher, 'prepare', lambda row, path: path.mkdir())
    child = Mock()
    child.poll.return_value = 1
    popen = Mock(return_value=child)
    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    launcher.run_block([dict(arm=4, block_id='N01-r1', task='N01', run=1)], tmp_path, stub=True)
    assert popen.call_args.kwargs['env']['EIDOS_STUDY_PROVIDER'] == 'minimax-m3.1-flash'
    rec = json.loads((tmp_path / 'N01-r1/arm4/run_record.json').read_text())
    assert rec['provider'] == providers.load().record()
    assert lab.environment()['EIDOS_STUDY_PROVIDER'] == 'minimax-m3.1-flash'


@pytest.mark.parametrize('name,model,item', [
    ('minimax-m3.1-flash', 'MiniMax-M3.1-Flash', 'minimax_api_key'),
    ('anthropic-haiku-5-5', 'claude-haiku-5-5', 'anthropic_api_key'),
    ('anthropic-sonnet-5-5', 'claude-sonnet-5-5', 'anthropic_api_key'),
])
def test_arm3_credential_and_model(tmp_path, monkeypatch, name, model, item):
    monkeypatch.syspath_prepend(str(treatment.ROOT / 'src'))
    from study import common as agent_common
    from study.eidos_arms import arm3
    monkeypatch.setenv('EIDOS_STUDY_PROVIDER', name)
    assert agent_common.model_name() == model
    monkeypatch.setattr(lab, 'start', lambda **kw: None)
    monkeypatch.setattr(lab, 'home', lambda: tmp_path)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/lab.json').write_text('{"api_port": 123}')
    c = Mock()
    c.query.return_value = []
    monkeypatch.setattr(lab, 'client', lambda: c)
    monkeypatch.setattr(arm3, 'prepare', lambda *a: ('fixture-project', {'artifacts': {}, 'charter_id': 'charter'}))
    monkeypatch.setattr(arm3, 'prompt', lambda *a: 'fixture prompt')
    monkeypatch.setattr(agent_common, 'record_live_delivery', lambda *a: None)
    sandbox = tmp_path / 'source.sb'
    sandbox.write_text('(subpath "/private/etc")')
    stream = SimpleNamespace(UsageTracker=object)
    def run_stream(root, command, env, **kwargs):
        assert command[command.index('--model') + 1] == model
        assert env['ANTHROPIC_API_KEY'] == 'fixture-arm3-key'
        assert env['ANTHROPIC_BASE_URL'] == providers.load().data['arm2']['anthropic_base_url']
        return dict(status='crash', wall_s=0, model_ids_seen=[], tokens={'total': 0})
    stream.run_stream = run_stream
    monkeypatch.setattr(arm3, 'lane_a', lambda: (agent_common.agent_env,
        lambda root, command: ['sandbox', '-f', str(sandbox), *command], stream))
    keychain = Mock(return_value='fixture-arm3-key')
    monkeypatch.setattr(arm3.subprocess, 'check_output', keychain)
    arm3.run(tmp_path, {})
    assert keychain.call_args.args[0][-3:] == ['-s', item, '-w']


def test_openai_claude_arms_refuse_before_credentials(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(treatment.ROOT / 'src'))
    from study import run_arm
    from study.eidos_arms import arm3
    profile = providers.load('openai-default', allow_placeholder=True)
    (tmp_path / 'arm.json').write_text('{}')
    monkeypatch.setattr(providers, 'load', lambda *a, **kw: profile)
    monkeypatch.setattr(treatment, 'for_run', lambda _: SimpleNamespace(provider=profile))
    keychain = Mock(side_effect=AssertionError('must refuse before Keychain'))
    monkeypatch.setattr(lab.subprocess, 'check_output', keychain)
    for call in (lambda: run_arm.run_claude(tmp_path, 2), lambda: arm3.run(tmp_path, {})):
        with pytest.raises(ValueError, match='openai-default has no arm2 Claude CLI path'):
            call()
    keychain.assert_not_called()


@pytest.mark.parametrize('recovers', [True, False])
def test_unhealthy_live_supervisor_unchanged_mode_never_restarts(tmp_path, monkeypatch, recovers):
    isolated_lab(tmp_path, monkeypatch, previous={'provider': providers.DEFAULT, 'credentials': False})
    health = Mock(side_effect=[False, False, True] if recovers else [False] * 4)
    monkeypatch.setattr(lab, '_healthy', health)
    monkeypatch.setattr(lab.time, 'sleep', lambda _: None)
    calls = []
    def command(action, *args, **kwargs):
        calls.append(action)
        assert action == 'status'
        return json.dumps({'supervisor_running': True})
    monkeypatch.setattr(lab, 'cli', command)
    stop = Mock(side_effect=AssertionError('must not cancel another block'))
    monkeypatch.setattr(lab, 'stop', stop)
    if recovers:
        lab.start(credentials=False)
    else:
        with pytest.raises(RuntimeError, match='unchanged credentials'):
            lab.start(credentials=False)
        assert health.call_count == 4
    assert calls == ['status']
    stop.assert_not_called()
    lab.subprocess.check_output.assert_not_called()


def test_start_confirmation_matches_credential_name_not_just_presence(monkeypatch):
    profile = providers.load('anthropic-haiku-5-5')
    command = Mock(return_value=json.dumps({'phase': 'running', 'credentials': {'OPENAI_API_KEY': 'stdin'}}))
    stop = Mock()
    monkeypatch.setattr(lab, 'cli', command)
    monkeypatch.setattr(lab, '_stop_confirmed', stop)
    with pytest.raises(RuntimeError, match='requested credentials'):
        lab._start_confirmed(profile, 'unused fake input', credentials=True)
    assert command.call_count == 2 and stop.call_count == 1
    command.reset_mock()
    command.return_value = json.dumps({'phase': 'running', 'credentials': {'ANTHROPIC_API_KEY': 'stdin'}})
    lab._start_confirmed(profile, 'unused fake input', credentials=True)
    assert command.call_count == 1
