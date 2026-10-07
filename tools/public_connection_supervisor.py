"""Recover an approved Pages-to-Mac connection, without restarting the backend.

Default invocation validates private configuration offline. Live execution needs
--run and the exact configuration digest printed by that validation. This is an
infrastructure process, never an application scheduler or texting client.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4


class Hold(ValueError):
    """Use fixed messages only; never include credentials or response bodies."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_json(path):
    path = Path(path)
    mode = path.lstat()
    if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
        raise Hold('Private configuration must be a regular mode-0600 file')
    try:
        result = json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise Hold('Invalid private JSON') from None
    if not isinstance(result, dict):
        raise Hold('Private configuration must be an object')
    return result


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.recovery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def origin(value, *, local=False, tunnel=False):
    if not isinstance(value, str):
        raise Hold('Invalid connection origin')
    try:
        parsed = urlsplit(value)
        good = (not parsed.username and not parsed.password and not parsed.query and
                not parsed.fragment and parsed.path in ('', '/') and
                not any(c.isspace() for c in value))
        if local:
            good = good and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and 1024 <= (parsed.port or 0) <= 65535
        else:
            host = parsed.hostname or ''
            good = good and parsed.scheme == 'https' and parsed.port in (None, 443)
            good = good and (bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com', host))
                             if tunnel else host == 'text-monkey-demo.pages.dev')
        if not good:
            raise ValueError()
    except ValueError:
        raise Hold('Invalid connection origin') from None
    return value.rstrip('/')


def manifest_files(plan):
    upload = Path(plan['upload'])
    if not upload.is_absolute() or upload.is_symlink() or not upload.is_dir():
        raise Hold('Approved upload must be an absolute regular directory')
    expected = plan['files']
    if not isinstance(expected, dict) or not {'index.html', '_worker.js', '_routes.json'} <= expected.keys():
        raise Hold('Approved connected upload manifest is incomplete')
    actual = {}
    for path in upload.rglob('*'):
        if path.is_symlink():
            raise Hold('Upload symlinks are forbidden')
        if path.is_file():
            name = path.relative_to(upload).as_posix()
            if any(part.startswith('.') for part in Path(name).parts) or path.suffix in ('.db', '.sqlite', '.env'):
                raise Hold('Private upload files are forbidden')
            actual[name] = digest(path.read_bytes())
    if actual != expected:
        raise Hold('Upload differs from the reviewed manifest')
    return upload, expected


def validate(path):
    config = private_json(path)
    required = {'plan', 'plan_sha256', 'bridge_file', 'login_file', 'local_origin',
                'public_origin', 'project', 'branch', 'account_id', 'node', 'wrangler',
                'cloudflared', 'state_dir', 'route_file', 'pid_file'}
    if set(config) != required or config['project'] != 'text-monkey-demo' or config['branch'] != 'demo':
        raise Hold('Configuration must bind only the existing approved Pages project and branch')
    if not isinstance(config['account_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', config['account_id']):
        raise Hold('Pin the existing Cloudflare account')
    for field in required - {'plan_sha256', 'local_origin', 'public_origin', 'project', 'branch', 'account_id'}:
        if not isinstance(config[field], str) or not Path(config[field]).is_absolute():
            raise Hold('Configuration paths must be absolute')
    config['local_origin'] = origin(config['local_origin'], local=True)
    config['public_origin'] = origin(config['public_origin'])
    plan_path = Path(config['plan'])
    plan = private_json(plan_path)
    if digest(plan_path.read_bytes()) != config['plan_sha256']:
        raise Hold('Recovery plan changed after review')
    manifest_files(plan)
    bridge = private_json(config['bridge_file'])
    login = private_json(config['login_file'])
    if not isinstance(bridge.get('backend_bridge_key'), str) or len(bridge['backend_bridge_key']) < 32:
        raise Hold('Existing private bridge secret is required')
    if not all(isinstance(login.get(k), str) and login[k] for k in ('email', 'password')):
        raise Hold('Existing private administrator login is required')
    for field in ('node', 'wrangler', 'cloudflared'):
        p = Path(config[field])
        if not p.is_file():
            raise Hold('Required installed executable is unavailable')
    if Path(config['state_dir']).is_symlink():
        raise Hold('State directory cannot be a symlink')
    for field in ('route_file', 'pid_file'):
        if Path(config[field]).is_symlink():
            raise Hold('Active connection files cannot be symlinks')
    return config, plan, digest(Path(path).read_bytes())


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Host:
    """All process/network effects live here so synthetic tests never invoke them."""
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirects())

    def request(self, base, path, *, headers=None, data=None):
        req = Request(base + path, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache', **(headers or {})},
                      data=json.dumps(data).encode() if data is not None else None)
        try:
            response = self.opener.open(req, timeout=15)
        except HTTPError as error:
            response = error
        except (URLError, OSError, TimeoutError) as error:
            return 0, {'dns_failure': isinstance(getattr(error, 'reason', error), socket.gaierror)}
        with response:
            body = response.read(2_000_001)
            try:
                result = json.loads(body) if len(body) <= 2_000_000 else None
            except (ValueError, UnicodeError):
                result = None
            if response.status == 530 and (b'tunnel not found' in body.lower() or re.search(rb'error\s*code\s*:\s*1033', body.lower())):
                result = {'tunnel_not_found': True}
            return response.status, result

    def asset_hash(self, base, path):
        try:
            with self.opener.open(Request(base + '/' + path, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'}), timeout=15) as response:
                if response.status != 200:
                    return None
                hasher = hashlib.sha256()
                while chunk := response.read(65536):
                    hasher.update(chunk)
                return hasher.hexdigest()
        except (HTTPError, URLError, OSError, TimeoutError):
            return None

    def process_identity(self, pid):
        if type(pid) is not int or pid <= 1:
            return None
        command = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, timeout=5)
        start = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, timeout=5)
        if command.returncode or start.returncode:
            return None
        return {'pid': pid, 'command': command.stdout.decode().strip(), 'started': start.stdout.decode().strip()}

    def tunnel_command(self):
        return [self.config['cloudflared'], '--config', '/dev/null', '--no-autoupdate', 'tunnel', '--url',
                self.config['local_origin'], '--protocol', 'http2']

    def identified_tunnel(self, identity):
        command = self.tunnel_command()
        # The already approved tunnel predates the explicit empty config flag.
        return identity and identity['command'] in (' '.join(command), ' '.join([command[0], *command[3:]]))

    def launch(self, log):
        fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as output:
            process = subprocess.Popen(self.tunnel_command(), stdout=output, stderr=output,
                                       stdin=subprocess.DEVNULL, start_new_session=True,
                                       env={k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'TMPDIR') if k in os.environ})
        return process.pid

    def stop(self, identity):
        if identity and self.process_identity(identity['pid']) == identity:
            if self.identified_tunnel(identity):
                os.kill(identity['pid'], signal.SIGTERM)
                return True
        return False

    def cli(self, args, *, input_value=None, cwd):
        # OAuth from the existing user's Wrangler configuration; never inherit a
        # different API token, account, .env or Workers deployment configuration.
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'TMPDIR', 'XDG_CONFIG_HOME') if k in os.environ}
        env.update(CLOUDFLARE_ACCOUNT_ID=self.config['account_id'], CI='true', WRANGLER_SEND_METRICS='false')
        try:
            result = subprocess.run([self.config['node'], self.config['wrangler'], *args],
                input=input_value, text=True, capture_output=True, timeout=180, cwd=cwd, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise Hold('Cloudflare command outcome is unknown; candidate retained') from None
        if result.returncode:
            raise Hold('Cloudflare command failed; candidate retained')
        return result.stdout

    def sleep(self, seconds):
        time.sleep(seconds)


class Supervisor:
    def __init__(self, config, plan, config_sha256, host):
        self.config, self.plan, self.sha, self.host = config, plan, config_sha256, host
        self.root = Path(config['state_dir'])
        self.journal_path = self.root / 'recovery.private.json'
        self.current_path = self.root / 'current.private.json'

    def save(self, journal, phase):
        journal['phase'] = phase
        journal['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.journal_path, journal)

    def bridge_headers(self, token=None):
        headers = {'X-Texty-Bridge': private_json(self.config['bridge_file'])['backend_bridge_key']}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        return headers

    def health(self, base, *, direct=False):
        headers = self.bridge_headers() if direct else {}
        status, data = self.host.request(base, '/api/config', headers=headers)
        if status != 200 or not isinstance(data, dict) or data.get('name') != 'Text Monkey' or data.get('connected') is not True:
            return False
        if data.get('messagingTransport') != 'mac_messages' or data.get('macBridgeConnected') is not True:
            return False
        return self.host.request(base, '/api/state', headers=headers)[0] == 401

    def login(self):
        credentials = private_json(self.config['login_file'])
        headers = {**self.bridge_headers(), 'Content-Type': 'application/json'}
        status, data = self.host.request(self.config['local_origin'], '/api/login', headers=headers,
            data={k: credentials[k] for k in ('email', 'password')})
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get('access_token'), str) or not data['access_token']:
            raise Hold('Existing administrator authentication is unavailable')
        return data['access_token']

    def authenticated(self, base, token, *, direct=False):
        headers = self.bridge_headers(token) if direct else {'Authorization': 'Bearer ' + token}
        status, data = self.host.request(base, '/api/state', headers=headers)
        return status == 200 and isinstance(data, dict)

    def connection_snapshot(self):
        route = Path(self.config['route_file']); pid_file = Path(self.config['pid_file'])
        value = private_json(route)
        url = origin(value['backend_url'], tunnel=True)
        if not pid_file.is_file() or pid_file.is_symlink():
            raise Hold('Active tunnel PID is unavailable')
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            raise Hold('Active tunnel PID is invalid') from None
        identity = self.host.process_identity(pid)
        if identity and not self.host.identified_tunnel(identity):
            raise Hold('Active PID is not the identified tunnel to this backend')
        return {'backend_url': url, 'pid': pid, 'identity': identity,
                'route_sha256': digest(route.read_bytes()), 'pid_sha256': digest(pid_file.read_bytes())}

    def require_unchanged(self, journal):
        old = journal['old']
        if (digest(Path(self.config['route_file']).read_bytes()) != old['route_sha256'] or
                digest(Path(self.config['pid_file']).read_bytes()) != old['pid_sha256']):
            raise Hold('Active connection changed outside this recovery; review required')

    def require_old_tunnel_failure(self, journal):
        # Public policy/WAF failures are not evidence of a broken tunnel. Repeat
        # direct probes with the same browser UA and existing bridge secret.
        for attempt in range(2):
            status, data = self.host.request(journal['old']['backend_url'], '/api/config', headers=self.bridge_headers())
            failed = status in (502, 503, 504) or (
                status == 530 and isinstance(data, dict) and data.get('tunnel_not_found') is True) or (
                status == 0 and isinstance(data, dict) and data.get('dns_failure') is True)
            if not failed:
                raise Hold('Old tunnel failure is unconfirmed; public access policy may be responsible')
            if attempt == 0:
                self.host.sleep(2)

    def snapshot_upload(self, journal):
        upload, expected = manifest_files(self.plan)
        destination = self.root / ('upload-' + journal['id'])
        if destination.exists():
            manifest_files({**self.plan, 'upload': str(destination)})
            return destination
        destination.mkdir(mode=0o700)
        for name, expected_hash in expected.items():
            data = (upload / name).read_bytes()
            if digest(data) != expected_hash:
                raise Hold('Upload changed while snapshotting')
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        manifest_files({**self.plan, 'upload': str(destination)})
        return destination

    def candidate(self, journal, token):
        candidate = journal.get('candidate')
        if candidate:
            if self.host.process_identity(candidate['pid']) != candidate['identity']:
                raise Hold('Pending replacement tunnel disappeared; review required')
            url = candidate.get('backend_url')
            if url and self.health(url, direct=True) and self.authenticated(url, token, direct=True):
                return url
            log = Path(candidate['log'])
        else:
            log = self.root / ('tunnel-' + journal['id'] + '.private.log')
            pid = self.host.launch(log)
            identity = self.host.process_identity(pid)
            journal['candidate'] = {'pid': pid, 'identity': identity, 'log': str(log)}
            self.save(journal, 'candidate_started')
            if not identity or identity['command'] != ' '.join(self.host.tunnel_command()):
                raise Hold('Replacement tunnel process did not verify')
        for _ in range(30):
            text = log.read_text(errors='replace')[-65536:]
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
            if urls:
                url = origin(urls[-1], tunnel=True)
                if self.health(url, direct=True) and self.authenticated(url, token, direct=True):
                    journal['candidate']['backend_url'] = url
                    self.save(journal, 'candidate_ready')
                    return url
            self.host.sleep(2)
        raise Hold('Replacement tunnel is not ready; old connection preserved')

    def commit_connection(self, journal):
        # One atomically replaced canonical record binds PID, route and receipt.
        # The old compatibility files are recoverable mirrors, not the commit.
        receipt = {'config_sha256': self.sha, 'plan_sha256': self.config['plan_sha256'],
                   'asset_manifest_sha256': digest(json.dumps(self.plan['files'], sort_keys=True).encode()),
                   'public_authenticated_state_status': 200, 'public_unauthenticated_state_status': 401,
                   'published_assets_verified': True,
                   'local_backend_restarted': False, 'native_actions': 0}
        value = {'id': journal['id'], **journal['candidate'], 'receipt': receipt}
        atomic_json(self.current_path, value)
        self.save(journal, 'committed')
        self.finish_commit(journal)

    def finish_commit(self, journal):
        current = private_json(self.current_path)
        if current['id'] != journal['id'] or current['receipt']['config_sha256'] != self.sha:
            raise Hold('Canonical recovery record changed; review required')
        candidate = journal['candidate']
        # Permit restart after either mirror was already written, but never
        # overwrite a different operator's route or PID.
        route_path, pid_path = Path(self.config['route_file']), Path(self.config['pid_file'])
        route = private_json(route_path)
        pid_bytes = pid_path.read_bytes()
        if digest(route_path.read_bytes()) != journal['old']['route_sha256'] and route.get('backend_url') != candidate['backend_url']:
            raise Hold('Route mirror changed outside recovery')
        if digest(pid_bytes) != journal['old']['pid_sha256'] and pid_bytes.strip() != str(candidate['pid']).encode():
            raise Hold('PID mirror changed outside recovery')
        atomic_json(route_path, {'backend_url': candidate['backend_url']})
        # Keep the existing plain integer PID format via the same atomic writer.
        atomic_json(pid_path, candidate['pid'])
        stopped = self.host.stop(journal['old']['identity'])
        journal['old_tunnel_stopped'] = stopped
        self.save(journal, 'complete')

    def recover(self):
        if self.journal_path.exists():
            journal = private_json(self.journal_path)
            if journal.get('config_sha256') != self.sha:
                raise Hold('Pending recovery belongs to another reviewed configuration')
            if journal['phase'] == 'complete':
                journal = None
        else:
            journal = None
        if not self.health(self.config['local_origin'], direct=True):
            raise Hold('Local backend or Mac bridge is unavailable; no restart attempted')
        token = self.login()
        if not self.authenticated(self.config['local_origin'], token, direct=True):
            raise Hold('Local authenticated API is unavailable')
        if journal and journal['phase'] == 'committed':
            if not self.health(self.config['public_origin']) or not self.authenticated(self.config['public_origin'], token):
                raise Hold('Committed connection is not healthy; old tunnel preserved')
            self.finish_commit(journal)
            return 'recovered'
        if journal is None:
            journal = {'id': uuid4().hex, 'config_sha256': self.sha, 'old': self.connection_snapshot()}
            self.save(journal, 'prepared')
        self.require_unchanged(journal)
        if not journal.get('candidate'):
            self.require_old_tunnel_failure(journal)
        upload = self.snapshot_upload(journal)
        url = self.candidate(journal, token)
        self.require_unchanged(journal)
        if journal.get('cloud_attempts', 0) >= 3:
            raise Hold('Recovery deployment retry budget exhausted; review required')
        journal['cloud_attempts'] = journal.get('cloud_attempts', 0) + 1
        self.save(journal, 'secret_pending')
        self.host.cli(['pages', 'secret', 'put', 'BACKEND_URL', '--project-name', self.config['project']],
                      input_value=url + '\n', cwd=self.root)
        self.save(journal, 'deploy_pending')
        self.require_unchanged(journal)
        manifest_files({**self.plan, 'upload': str(upload)})
        output = self.host.cli(['pages', 'deploy', str(upload), '--project-name', self.config['project'],
                               '--branch', self.config['branch'], '--no-bundle'], cwd=self.root)
        deployment = re.findall(r'https://[a-z0-9-]+\.text-monkey-demo\.pages\.dev', output)
        if not deployment:
            raise Hold('Cloudflare deployment identity is unavailable; candidate retained')
        journal['deployment_origin'] = deployment[-1]
        self.save(journal, 'verification_pending')
        for _ in range(15):
            origins = (journal['deployment_origin'], self.config['public_origin'])
            if all(self.health(base) and self.authenticated(base, token) for base in origins):
                # Routing and header files are deployment inputs, not served UI.
                assets = {k: v for k, v in self.plan['files'].items() if not k.startswith('_')}
                if any(self.host.asset_hash(base, name) != expected for base in origins for name, expected in assets.items()):
                    raise Hold('Published assets differ from the approved upload; tunnels retained')
                self.require_unchanged(journal)
                self.commit_connection(journal)
                return 'recovered'
            self.host.sleep(2)
        raise Hold('Public authenticated API did not verify; candidate and old tunnel retained')

    def tick(self, failures):
        pending = private_json(self.journal_path) if self.journal_path.exists() else {}
        if pending and pending.get('phase') != 'complete':
            return self.recover(), 0
        if self.health(self.config['public_origin']):
            return 'healthy', 0
        if failures + 1 < 2:
            return 'checking', failures + 1
        return self.recover(), 0


@contextmanager
def exclusive(root):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise Hold('Recovery state directory must be private mode 0700')
    fd = os.open(root / 'supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Hold('Another connection supervisor owns the lock') from None
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--approved-config-sha256')
    args = parser.parse_args()
    try:
        config, plan, sha = validate(args.config)
        if not args.run:
            print(json.dumps({'status': 'offline_validated', 'config_sha256': sha, 'live_actions': 0}))
            return
        if args.approved_config_sha256 != sha:
            raise Hold('Live execution requires the exact reviewed configuration digest')
        with exclusive(config['state_dir']):
            supervisor = Supervisor(config, plan, sha, Host(config))
            if not re.search(r'\b4\.146\.0\b', supervisor.host.cli(['--version'], cwd=supervisor.root)):
                raise Hold('Use the reviewed Wrangler 4.146.0 installation')
            failures, last_status, hold_count = 0, None, 0
            while True:
                try:
                    # Refuse plan/config changes, including during a long run.
                    _, _, fresh = validate(args.config)
                    if fresh != sha:
                        raise Hold('Reviewed supervisor configuration changed')
                    status, failures = supervisor.tick(failures)
                except (Hold, OSError, KeyError, TypeError, ValueError) as error:
                    status, failures = 'held', 0
                    atomic_json(supervisor.root / 'last-hold.private.json', {
                        'at': datetime.now(timezone.utc).isoformat(), 'status': 'held',
                        'reason': str(error) if isinstance(error, Hold) else 'Private recovery state is invalid or unavailable',
                        'config_sha256': sha, 'backend_restarted': False,
                    })
                if status != last_status:
                    print(json.dumps({'status': status}), flush=True)
                    last_status = status
                hold_count = hold_count + 1 if status == 'held' else 0
                supervisor.host.sleep(min(300, 30 * 2 ** min(hold_count, 4)) if hold_count else 30)
    except (Hold, OSError, KeyError, TypeError, ValueError):
        parser.exit(1, 'Connection supervisor validation failed; no secrets printed.\n')
    except KeyboardInterrupt:
        print(json.dumps({'status': 'supervisor_stopped', 'active_tunnel_preserved': True}))


if __name__ == '__main__':
    main()
