import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('cloud_browser_login', Path(__file__).parents[1] / 'tools/cloud_browser_login.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def login(tmp_path, monkeypatch):
    path = tmp_path / 'private.env'
    path.write_text('')
    path.chmod(0o600)
    instance = module.Login(path, 'isolated-test')
    monkeypatch.setattr(instance, 'require_volume', lambda: None)
    return instance


@pytest.mark.parametrize('state', ['running', 'restarting', 'created', 'paused'])
def test_start_refuses_any_active_connector(login, monkeypatch, state):
    monkeypatch.setattr(login, 'states', lambda: {'google-voice': state})
    monkeypatch.setattr(login, 'run', lambda *args: pytest.fail('No mutation allowed'))
    with pytest.raises(module.LoginError, match='Stop the connector'):
        login.action('start')


def test_resume_refuses_active_manual_browser(login, monkeypatch):
    monkeypatch.setattr(login, 'states', lambda: {'browser-login': 'running'})
    monkeypatch.setattr(login, 'run', lambda *args: pytest.fail('No mutation allowed'))
    with pytest.raises(module.LoginError, match='Stop the manual browser'):
        login.action('resume')


def test_stop_does_not_clear_marker_while_browser_active(login, monkeypatch):
    monkeypatch.setattr(login, 'states', lambda: {'browser-login': 'running'})
    calls = []
    monkeypatch.setattr(login, 'run', lambda *args: calls.append(args))
    with pytest.raises(module.LoginError, match='leave the profile locked'):
        login.action('stop')
    assert calls == [('stop', '-t', '30', 'browser-login')]


def test_stop_clears_marker_only_after_confirmed_stopped(login, monkeypatch):
    monkeypatch.setattr(login, 'states', lambda: {'browser-login': 'exited'})
    calls = []
    monkeypatch.setattr(login, 'run', lambda *args: calls.append(args))
    assert 'Connector remains stopped' in login.action('stop')
    assert calls[0] == ('stop', '-t', '30', 'browser-login')
    assert calls[1][:6] == ('run', '--rm', '--no-deps', '--entrypoint', 'python3', 'browser-login')
    assert 'manual-login.active' in calls[1][-1]


def test_missing_private_volume_prevents_runtime_start(login, monkeypatch):
    def missing():
        raise module.LoginError('volume missing')
    monkeypatch.setattr(login, 'require_volume', missing)
    monkeypatch.setattr(login, 'run', lambda *args: pytest.fail('No mutation allowed'))
    with pytest.raises(module.LoginError, match='volume missing'):
        login.action('start')


def test_shell_enablement_and_remote_docker_settings_are_not_inherited(login, monkeypatch):
    monkeypatch.setenv('GOOGLE_VOICE_ENABLED', 'true')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://public.example:2375')
    assert set(login.environment) == {'PATH', 'CLOUD_ENV_FILE'}


def test_loose_environment_permissions_are_rejected(tmp_path):
    path = tmp_path / 'private.env'
    path.write_text('')
    path.chmod(0o644)
    with pytest.raises(module.LoginError, match='0600'):
        module.Login(path, 'isolated-test')


def test_seccomp_policy_remains_default_deny_with_bounded_namespace_additions():
    policy = module.json.loads((Path(__file__).parents[1] / 'deploy/cloud/chromium-seccomp.json').read_text())
    assert policy['defaultAction'] == 'SCMP_ACT_ERRNO'
    extra = policy['syscalls'][-5:]
    assert [entry['names'] for entry in extra] == [['clone'], ['unshare'], ['chroot'], ['setns'], ['clone']]
    for entry in extra[:2]:
        assert entry['args'] == [{'index': 0, 'value': 268435456, 'valueTwo': 268435456, 'op': 'SCMP_CMP_MASKED_EQ'}]
    assert extra[-1]['args'] == [{'index': 0, 'value': 536870929, 'op': 'SCMP_CMP_EQ'}]
    assert not any(entry.get('action') == 'SCMP_ACT_ALLOW' and not entry.get('includes')
                   and any(name in entry['names'] for name in ['mount', 'bpf', 'keyctl'])
                   for entry in policy['syscalls'])
