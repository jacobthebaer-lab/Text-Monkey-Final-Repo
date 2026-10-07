"""Recovery ordering and failure safety, with fake network/process effects."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('connection_supervisor', Path(__file__).parents[1] / 'tools/public_connection_supervisor.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class FakeHost(tool.Host):
    def __init__(self, config, plan):
        self.config, self.plan = config, plan
        self.calls, self.stopped, self.identities = [], [], {}
        self.local_ok, self.public_ok, self.candidate_ok = True, False, True
        self.auth_ok, self.assets_ok = True, True
        self.old_status = 503
        self.fail_cli = None
        self.pid = 100
        self.identities[99] = {'pid': 99, 'command': ' '.join(self.tunnel_command()), 'started': 'old'}

    def request(self, base, path, *, headers=None, data=None):
        self.calls.append(('request', base, path))
        if base == 'https://old-tunnel.trycloudflare.com':
            if self.old_status != 200:
                return self.old_status, ({'tunnel_not_found': True} if self.old_status == 530 else {})
        ready = self.local_ok if base == self.config['local_origin'] else (
            self.candidate_ok if base.endswith('.trycloudflare.com') else self.public_ok)
        if not ready:
            return 503, {}
        if path == '/api/config':
            return 200, {'name': 'Text Monkey', 'connected': True,
                         'messagingTransport': 'mac_messages', 'macBridgeConnected': True}
        if path == '/api/login':
            return (200, {'access_token': 'synthetic-access-token'}) if self.auth_ok else (401, {})
        assert path == '/api/state'
        return (200, {}) if self.auth_ok and (headers or {}).get('Authorization') else (401, {})

    def asset_hash(self, base, path):
        self.calls.append(('asset', base, path))
        return self.plan['files'][path] if self.assets_ok else 'wrong'

    def process_identity(self, pid):
        return self.identities.get(pid)

    def launch(self, log):
        self.calls.append(('launch',))
        self.identities[self.pid] = {'pid': self.pid, 'command': ' '.join(self.tunnel_command()), 'started': 'new'}
        log.write_text('https://replacement-tunnel.trycloudflare.com\n')
        log.chmod(0o600)
        return self.pid

    def stop(self, identity):
        if identity and self.process_identity(identity['pid']) == identity and self.identified_tunnel(identity):
            self.calls.append(('stop', identity['pid']))
            self.stopped.append(identity['pid'])
            return True
        return False

    def cli(self, args, *, input_value=None, cwd):
        self.calls.append(('cli', args))
        if self.fail_cli and self.fail_cli in args:
            raise tool.Hold('Cloudflare command failed; candidate retained')
        if 'deploy' in args:
            self.public_ok = True
            return 'https://abc123.text-monkey-demo.pages.dev'
        return '4.146.0'

    def sleep(self, seconds):
        pass


@pytest.fixture
def setup(tmp_path):
    upload = tmp_path / 'approved'; upload.mkdir()
    for name in ['index.html', 'app.js', '_worker.js', '_routes.json']:
        (upload / name).write_text('synthetic ' + name)
    plan = {'upload': str(upload), 'files': {p.name: tool.digest(p.read_bytes()) for p in upload.iterdir()}}
    plan_path = tmp_path / 'plan.private.json'; write_private(plan_path, plan)
    bridge = tmp_path / 'bridge.private.json'; write_private(bridge, {'backend_bridge_key': 'synthetic-' * 5})
    login = tmp_path / 'login.private.json'; write_private(login, {'email': 'admin@example.test', 'password': 'synthetic-password'})
    route = tmp_path / 'route.private.json'; write_private(route, {'backend_url': 'https://old-tunnel.trycloudflare.com'})
    pid = tmp_path / 'tunnel.pid'; pid.write_text('99\n'); pid.chmod(0o600)
    executable = tmp_path / 'installed'; executable.write_text('fake')
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    config = {'plan': str(plan_path), 'plan_sha256': tool.digest(plan_path.read_bytes()),
              'bridge_file': str(bridge), 'login_file': str(login),
              'local_origin': 'http://127.0.0.1:60000', 'public_origin': 'https://text-monkey-demo.pages.dev',
              'project': 'text-monkey-demo', 'branch': 'demo', 'account_id': '0' * 32,
              'node': str(executable), 'wrangler': str(executable), 'cloudflared': str(executable),
              'state_dir': str(root), 'route_file': str(route), 'pid_file': str(pid)}
    path = tmp_path / 'config.private.json'; write_private(path, config)
    config, plan, sha = tool.validate(path)
    host = FakeHost(config, plan)
    supervisor = tool.Supervisor(config, plan, sha, host)
    return supervisor, host, path


def test_healthy_connection_has_no_process_or_cloud_mutations(setup):
    supervisor, host, _ = setup
    host.public_ok = True
    assert supervisor.tick(0) == ('healthy', 0)
    assert not any(c[0] in ('launch', 'cli', 'stop') for c in host.calls)


def test_single_bad_probe_does_not_launch(setup):
    supervisor, host, _ = setup
    assert supervisor.tick(0) == ('checking', 1)
    assert not any(c[0] in ('launch', 'cli', 'stop') for c in host.calls)


@pytest.mark.parametrize('status', [200, 401, 403, 429, 0])
def test_public_failure_with_unconfirmed_or_policy_blocked_old_tunnel_does_not_recover(setup, status):
    supervisor, host, _ = setup
    host.old_status = status
    with pytest.raises(tool.Hold, match='unconfirmed'): supervisor.recover()
    assert not any(c[0] in ('launch', 'cli', 'stop') for c in host.calls)


def test_confirmed_tunnel_not_found_is_recoverable(setup):
    supervisor, host, _ = setup
    host.old_status = 530
    assert supervisor.recover() == 'recovered'


@pytest.mark.parametrize('problem', ['local', 'auth'])
def test_local_backend_or_auth_failure_never_launches_or_deploys(setup, problem):
    supervisor, host, _ = setup
    if problem == 'local': host.local_ok = False
    else: host.auth_ok = False
    with pytest.raises(tool.Hold): supervisor.recover()
    assert not any(c[0] in ('launch', 'cli', 'stop') for c in host.calls)


def test_success_orders_readiness_deploy_verification_commit_and_old_stop(setup):
    supervisor, host, _ = setup
    assert supervisor.tick(1) == ('recovered', 0)
    assert host.stopped == [99]
    calls = [c[0] for c in host.calls]
    assert calls.index('launch') < calls.index('cli') < calls.index('asset') < calls.index('stop')
    cloud = [c[1] for c in host.calls if c[0] == 'cli']
    assert len(cloud) == 2 and cloud[0][3] == 'BACKEND_URL'
    assert cloud[1][0:2] == ['pages', 'deploy'] and '--no-bundle' in cloud[1]
    assert not any('BACKEND_BRIDGE_KEY' in args for args in cloud)
    current = tool.private_json(supervisor.current_path)
    assert current['pid'] == 100 and current['receipt']['native_actions'] == 0
    assert current['receipt']['published_assets_verified'] is True
    assert tool.private_json(supervisor.config['route_file'])['backend_url'] == current['backend_url']
    assert Path(supervisor.config['pid_file']).read_text().strip() == '100'
    assert tool.private_json(supervisor.journal_path)['phase'] == 'complete'


def test_unready_candidate_retains_old_and_never_changes_cloud(setup):
    supervisor, host, _ = setup
    host.candidate_ok = False
    with pytest.raises(tool.Hold, match='not ready'): supervisor.recover()
    assert host.stopped == [] and not any(c[0] == 'cli' for c in host.calls)
    assert Path(supervisor.config['pid_file']).read_text().strip() == '99'


@pytest.mark.parametrize('stage', ['put', 'deploy'])
def test_unknown_cloud_failure_resumes_same_candidate_without_duplicate_launch(setup, stage):
    supervisor, host, _ = setup
    host.fail_cli = stage
    with pytest.raises(tool.Hold): supervisor.recover()
    assert host.stopped == [] and Path(supervisor.config['pid_file']).read_text().strip() == '99'
    host.fail_cli = None
    restarted = tool.Supervisor(supervisor.config, supervisor.plan, supervisor.sha, host)
    assert restarted.recover() == 'recovered'
    assert sum(c[0] == 'launch' for c in host.calls) == 1
    assert host.stopped == [99]


def test_public_wrong_bytes_hold_without_stopping_either_tunnel(setup):
    supervisor, host, _ = setup
    host.assets_ok = False
    with pytest.raises(tool.Hold, match='Published assets'): supervisor.recover()
    assert host.stopped == [] and not supervisor.current_path.exists()
    assert Path(supervisor.config['pid_file']).read_text().strip() == '99'


def test_external_route_edit_holds_before_cloud_mutation(setup):
    supervisor, host, _ = setup
    launch = host.launch
    def changed(log):
        result = launch(log)
        write_private(Path(supervisor.config['route_file']), {'backend_url': 'https://operator-change.trycloudflare.com'})
        return result
    host.launch = changed
    with pytest.raises(tool.Hold, match='outside'): supervisor.recover()
    assert not any(c[0] == 'cli' for c in host.calls) and host.stopped == []


def test_pid_reuse_never_stops_different_process(setup):
    supervisor, host, _ = setup
    asset = host.asset_hash
    def replaced(base, path):
        host.identities[99] = {**host.identities[99], 'started': 'reused'}
        return asset(base, path)
    host.asset_hash = replaced
    assert supervisor.recover() == 'recovered'
    assert host.stopped == []


def test_committed_restart_finishes_mirrors_without_redeployment(setup):
    supervisor, host, _ = setup
    finish = supervisor.finish_commit
    supervisor.finish_commit = lambda journal: None
    assert supervisor.recover() == 'recovered'
    assert tool.private_json(supervisor.journal_path)['phase'] == 'committed'
    before = len([c for c in host.calls if c[0] == 'cli'])
    supervisor.finish_commit = finish
    assert supervisor.recover() == 'recovered'
    assert len([c for c in host.calls if c[0] == 'cli']) == before and host.stopped == [99]


def test_committed_restart_with_bad_public_connection_retains_old(setup):
    supervisor, host, _ = setup
    supervisor.finish_commit = lambda journal: None
    supervisor.recover()
    host.public_ok = False
    with pytest.raises(tool.Hold, match='Committed'): supervisor.recover()
    assert host.stopped == []


@pytest.mark.parametrize('change', ['extra', 'bytes', 'symlink', 'plan'])
def test_frozen_manifest_rejects_changes_before_effects(setup, change, tmp_path):
    supervisor, host, path = setup
    upload = Path(supervisor.plan['upload'])
    if change == 'extra': (upload / 'private.env').write_text('secret')
    elif change == 'bytes': (upload / 'app.js').write_text('changed')
    elif change == 'symlink': (upload / 'linked').symlink_to(upload / 'index.html')
    else:
        plan = Path(supervisor.config['plan']); write_private(plan, {**supervisor.plan, 'source': 'changed'})
    with pytest.raises(tool.Hold): tool.validate(path)
    assert host.calls == []


@pytest.mark.parametrize('field,value', [('branch','main'), ('project','other'),
    ('public_origin','https://unapproved.example.test'), ('local_origin','http://192.0.2.1:60000')])
def test_target_and_loopback_are_fixed(setup, field, value):
    _, _, path = setup
    config = tool.private_json(path); config[field] = value; write_private(path, config)
    with pytest.raises(tool.Hold): tool.validate(path)


def test_private_configuration_permissions_and_symlinks_are_rejected(setup, tmp_path):
    _, _, path = setup
    path.chmod(0o644)
    with pytest.raises(tool.Hold): tool.validate(path)
    path.chmod(0o600)
    link = tmp_path / 'config-link'; link.symlink_to(path)
    with pytest.raises(tool.Hold): tool.validate(link)


def test_second_supervisor_cannot_take_writer_lock(tmp_path):
    with tool.exclusive(tmp_path / 'private-state'):
        with pytest.raises(tool.Hold, match='owns the lock'):
            with tool.exclusive(tmp_path / 'private-state'): pass


def test_pending_configuration_change_holds_without_effects(setup):
    supervisor, host, _ = setup
    tool.atomic_json(supervisor.journal_path, {'config_sha256': 'different', 'phase': 'prepared'})
    with pytest.raises(tool.Hold, match='another reviewed'): supervisor.recover()
    assert host.calls == []


def test_deployment_failure_budget_is_bounded_and_preserves_tunnels(setup):
    supervisor, host, _ = setup
    host.fail_cli = 'deploy'
    for _ in range(3):
        with pytest.raises(tool.Hold): supervisor.recover()
    with pytest.raises(tool.Hold, match='retry budget'): supervisor.recover()
    assert sum(c[0] == 'launch' for c in host.calls) == 1
    assert sum(c[0] == 'cli' and 'deploy' in c[1] for c in host.calls) == 3
    assert host.stopped == []


def test_default_cli_is_offline_and_run_requires_exact_digest(setup, monkeypatch, capsys):
    _, _, path = setup
    monkeypatch.setattr('sys.argv', ['supervisor', '--config', str(path)])
    monkeypatch.setattr(tool, 'Host', lambda config: pytest.fail('No host effects are allowed'))
    tool.main()
    assert json.loads(capsys.readouterr().out)['live_actions'] == 0
    monkeypatch.setattr('sys.argv', ['supervisor', '--config', str(path), '--run'])
    with pytest.raises(SystemExit): tool.main()
    output = capsys.readouterr()
    assert 'synthetic-password' not in output.err and 'synthetic-' not in output.err


def test_browser_probe_headers_and_tunnel_error_classification(setup):
    supervisor, _, _ = setup
    host = tool.Host(supervisor.config)
    class Response:
        status = 530
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'<h1>Tunnel not found</h1><p>errorcode:1033</p>'
    class Opener:
        def open(self, request, timeout):
            assert request.get_header('User-agent') == 'Mozilla/5.0'
            assert request.get_header('Cache-control') == 'no-cache'
            return Response()
    host.opener = Opener()
    assert host.request('https://old-tunnel.trycloudflare.com', '/api/config') == (530, {'tunnel_not_found': True})


def test_cloud_cli_uses_stdin_and_existing_oauth_with_pinned_account(setup, monkeypatch):
    supervisor, _, _ = setup
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'wrong-account-private-token')
    monkeypatch.setenv('GLOO_API_KEY', 'unrelated-private-key')
    def run(command, **kwargs):
        assert kwargs['input'] == 'synthetic-secret\n'
        assert not any('synthetic-secret' in arg for arg in command)
        assert kwargs['env']['CLOUDFLARE_ACCOUNT_ID'] == '0' * 32
        assert 'CLOUDFLARE_API_TOKEN' not in kwargs['env'] and 'GLOO_API_KEY' not in kwargs['env']
        assert kwargs['capture_output'] is True and kwargs['text'] is True
        return type('Result', (), {'returncode': 0, 'stdout': 'success'})()
    monkeypatch.setattr(tool.subprocess, 'run', run)
    host = tool.Host(supervisor.config)
    assert host.cli(['pages', 'secret', 'put', 'BACKEND_URL'], input_value='synthetic-secret\n', cwd=supervisor.root) == 'success'


@pytest.mark.parametrize('match', [True, False])
def test_native_host_stop_fences_process_identity_before_sigterm(setup, monkeypatch, match):
    supervisor, fake, _ = setup
    host = tool.Host(supervisor.config)
    identity = fake.identities[99]
    host.process_identity = lambda pid: identity if match else {**identity, 'started': 'reused'}
    killed = []
    monkeypatch.setattr(tool.os, 'kill', lambda pid, sig: killed.append(pid))
    assert host.stop(identity) is match
    assert killed == ([99] if match else [])
