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
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4


CANDIDATE_FAILURE_GRACE_SECONDS = 300
MAX_CANDIDATE_REPLACEMENTS = 1
CANDIDATE_STOP_WAIT_SECONDS = 60


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


def deployment_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r'https://[a-f0-9]{8}\.text-monkey-demo\.pages\.dev', value):
        raise Hold('Invalid immutable Pages deployment origin')
    return value


def manifest_digest(plan):
    return digest(json.dumps(plan['files'], sort_keys=True).encode())


def pages_state(config):
    state = private_json(config['deployment_state_file'])
    if any(state.get(k) != config[k] for k in ('account_id', 'project', 'branch')):
        raise Hold('Shared Pages writer target changed')
    if state.get('generation') != config['deployment_generation'] or state.get('plan_sha256') != config['plan_sha256']:
        raise Hold('Newer Pages source generation requires fresh review')
    origin(state.get('backend_url'), tunnel=True)
    deployment_origin(state.get('deployment_origin'))
    if state.get('status') not in ('ready', 'recovery_pending', 'deployment_pending'):
        raise Hold('Shared Pages deployment state is invalid')
    return state


@contextmanager
def pages_writer_lock(config):
    """Every Pages deploy owner must use this same lock and canonical state."""
    path = Path(config['deployment_lock_file'])
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Hold('Another Pages deployment writer owns the lock') from None
        yield
    finally:
        os.close(fd)


@contextmanager
def source_deployment(config):
    """Root integration: reserve before CLI; explicitly finalize verified source.

    The yielded ready record supplies the current backend URL. Exceptions or an
    omitted finalization leave deployment_pending, blocking stale recovery.
    """
    with pages_writer_lock(config):
        state = pages_state(config)
        if state['status'] != 'ready':
            raise Hold('Pending Pages writer must be reconciled before source deployment')
        writer = uuid4().hex
        atomic_json(config['deployment_state_file'], {**state, 'status': 'deployment_pending', 'writer_id': writer})
        def finalize(plan_path, verified_origin):
            fresh = pages_state(config)
            if fresh.get('writer_id') != writer or fresh['status'] != 'deployment_pending':
                raise Hold('Source deployment reservation changed')
            plan = private_json(plan_path)
            manifest_files(plan)
            atomic_json(config['deployment_state_file'], {
                **state, 'status': 'ready', 'generation': uuid4().hex,
                'plan_sha256': digest(Path(plan_path).read_bytes()),
                'asset_manifest_sha256': manifest_digest(plan),
                'deployment_origin': deployment_origin(verified_origin),
            })
        yield state, finalize


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
                'cloudflared', 'state_dir', 'route_file', 'pid_file', 'pages_secrets_file',
                'deployment_lock_file', 'deployment_state_file', 'deployment_generation'}
    if set(config) != required or config['project'] != 'text-monkey-demo' or config['branch'] != 'demo':
        raise Hold('Configuration must bind only the existing approved Pages project and branch')
    if not isinstance(config['account_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', config['account_id']):
        raise Hold('Pin the existing Cloudflare account')
    for field in required - {'plan_sha256', 'local_origin', 'public_origin', 'project', 'branch', 'account_id', 'deployment_generation'}:
        if not isinstance(config[field], str) or not Path(config[field]).is_absolute():
            raise Hold('Configuration paths must be absolute')
    config['local_origin'] = origin(config['local_origin'], local=True)
    config['public_origin'] = origin(config['public_origin'])
    plan_path = Path(config['plan'])
    plan = private_json(plan_path)
    if digest(plan_path.read_bytes()) != config['plan_sha256']:
        raise Hold('Recovery plan changed after review')
    manifest_files(plan)
    if not isinstance(config['deployment_generation'], str) or not re.fullmatch(r'[a-f0-9]{32}', config['deployment_generation']):
        raise Hold('Pin the reviewed Pages source generation')
    shared = pages_state(config)
    if shared.get('asset_manifest_sha256') != manifest_digest(plan):
        raise Hold('Reviewed Pages asset provenance changed')
    if Path(config['deployment_state_file']).parent != Path(config['deployment_lock_file']).parent:
        raise Hold('Shared deployment lock must live beside its canonical state')
    secrets = private_json(config['pages_secrets_file'])
    origin(secrets.get('BACKEND_URL'), tunnel=True)
    bridge = private_json(config['bridge_file'])
    login = private_json(config['login_file'])
    if not isinstance(bridge.get('backend_bridge_key'), str) or len(bridge['backend_bridge_key']) < 32:
        raise Hold('Existing private bridge secret is required')
    if secrets.get('BACKEND_BRIDGE_KEY') != bridge['backend_bridge_key']:
        raise Hold('Existing Pages and backend bridge secrets differ')
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
            try:
                base = origin(base)
            except Hold:
                base = deployment_origin(base)
            approved = urlsplit(base)
            if not isinstance(path, str) or not path or path.startswith('/') or any(p in ('.', '..', '') for p in path.split('/')):
                return None

            def checked_target(location, current):
                # Validate before urljoin/urlsplit can discard control characters.
                if (not isinstance(location, str) or not location or '\\' in location or
                        any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in location) or
                        re.search(r'%(?![0-9a-fA-F]{2})|%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)', location, re.I)):
                    raise ValueError()
                target = urlsplit(urljoin(current, location))
                if (target.scheme != 'https' or target.hostname != approved.hostname or
                        target.port not in (None, 443) or target.username is not None or
                        target.password is not None or target.fragment):
                    raise ValueError()
                return urlunsplit(('https', approved.hostname, target.path or '/', target.query, ''))

            url = checked_target('/' + quote(path, safe='/'), base)
            visited = set()
            for hop in range(6):
                if url in visited:
                    return None
                visited.add(url)
                # Each hop is a fresh anonymous GET. The API opener still denies
                # all redirects; asset verification follows only checked targets.
                request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'})
                try:
                    response = self.opener.open(request, timeout=15)
                except HTTPError as error:
                    response = error
                with response:
                    checked_target(response.geturl(), url)
                    if response.status in (301, 302, 303, 307, 308):
                        locations = response.headers.get_all('Location', [])
                        if hop == 5 or len(locations) != 1:
                            return None
                        url = checked_target(locations[0], url)
                        continue
                    if response.status != 200:
                        return None
                    hasher = hashlib.sha256()
                    while chunk := response.read(65536):
                        hasher.update(chunk)
                    return hasher.hexdigest()
        except (Hold, HTTPError, URLError, OSError, TimeoutError, ValueError, TypeError):
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

    def now_utc(self):
        return datetime.now(timezone.utc)

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
        base = self.checked_probe_origin(base, direct=direct)
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
        base = self.checked_probe_origin(base, direct=direct)
        headers = self.bridge_headers(token) if direct else {'Authorization': 'Bearer ' + token}
        status, data = self.host.request(base, '/api/state', headers=headers)
        return status == 200 and isinstance(data, dict)

    def checked_probe_origin(self, base, *, direct):
        if direct:
            return origin(base, local=True) if base == self.config['local_origin'] else origin(base, tunnel=True)
        return self.config['public_origin'] if base == self.config['public_origin'] else deployment_origin(base)

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
                'route_sha256': digest(route.read_bytes()), 'pid_sha256': digest(pid_file.read_bytes()),
                'secrets_sha256': digest(Path(self.config['pages_secrets_file']).read_bytes()),
                'secrets_other_sha256': digest(json.dumps({k: v for k, v in private_json(self.config['pages_secrets_file']).items()
                    if k != 'BACKEND_URL'}, sort_keys=True).encode())}

    def require_unchanged(self, journal):
        old = journal['old']
        if (digest(Path(self.config['route_file']).read_bytes()) != old['route_sha256'] or
                digest(Path(self.config['pid_file']).read_bytes()) != old['pid_sha256']):
            raise Hold('Active connection changed outside this recovery; review required')
        if digest(Path(self.config['pages_secrets_file']).read_bytes()) != old['secrets_sha256']:
            raise Hold('Private Pages secrets mirror changed outside recovery')
        self.require_generation(journal)

    def require_generation(self, journal):
        shared = pages_state(self.config)
        if shared.get('asset_manifest_sha256') != manifest_digest(self.plan):
            raise Hold('Shared Pages asset provenance changed')
        if shared['status'] == 'recovery_pending' and shared.get('recovery_id') == journal['id']:
            if shared['backend_url'] != journal['old']['backend_url']:
                raise Hold('Saved recovery route differs from its canonical reservation')
            return shared
        if shared['status'] == 'ready' and journal.get('phase') in ('committed', 'complete') and shared.get('connection_revision') == journal['id']:
            return shared
        raise Hold('Shared Pages deployment reservation changed; review required')

    def require_old_tunnel_failure(self, journal):
        # Public policy/WAF failures are not evidence of a broken tunnel. Repeat
        # direct probes with the same browser UA and existing bridge secret.
        for attempt in range(2):
            status, data = self.host.request(origin(journal['old']['backend_url'], tunnel=True), '/api/config', headers=self.bridge_headers())
            failed = status in (502, 503, 504) or (
                status == 530 and isinstance(data, dict) and data.get('tunnel_not_found') is True) or (
                status == 0 and isinstance(data, dict) and data.get('dns_failure') is True)
            if not failed:
                raise Hold('Old tunnel failure is unconfirmed; public access policy may be responsible')
            if attempt == 0:
                self.host.sleep(2)

    def original_connection_verified(self, journal, token, shared):
        """Verify the original route and reviewed source without any mutations."""
        if digest(Path(self.config['plan']).read_bytes()) != self.config['plan_sha256']:
            raise Hold('Reviewed recovery plan changed')
        manifest_files(self.plan)
        old = journal['old']
        if (not old.get('identity') or self.host.process_identity(old['pid']) != old['identity']
                or not self.host.identified_tunnel(old['identity'])):
            raise Hold('Original tunnel process identity changed')
        routes = ((old['backend_url'], True), (self.config['public_origin'], False),
                  (deployment_origin(shared['deployment_origin']), False))
        if not all(self.health(base, direct=direct) and self.authenticated(base, token, direct=direct)
                   for base, direct in routes):
            return False
        assets = {k: v for k, v in self.plan['files'].items() if not k.startswith('_')}
        if any(self.host.asset_hash(base, name) != expected
               for base, direct in routes if not direct for name, expected in assets.items()):
            raise Hold('Original published assets differ from the reviewed source')
        self.require_unchanged(journal)
        return True

    def aborted_snapshot(self, journal):
        """Reconcile only the exact known local ready transition, never an attempt."""
        if (journal.get('config_sha256') != self.sha
                or not isinstance(journal.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', journal['id'])
                or set(journal) != {'id', 'config_sha256', 'old', 'phase', 'updated_at', 'abort_receipt_sha256'}):
            raise Hold('Original connection abort identity changed')
        path = self.root / ('aborted-' + journal['id'] + '.private.json')
        receipt = private_json(path)
        prepared = receipt.get('prepared_journal', {})
        if (digest(path.read_bytes()) != journal.get('abort_receipt_sha256')
                or receipt.get('phase') != 'aborted'
                or prepared.get('phase') != 'prepared'
                or set(prepared) != {'id', 'config_sha256', 'old', 'phase', 'updated_at'}
                or any(prepared.get(k) != journal.get(k) for k in ('id', 'config_sha256', 'old'))
                or receipt.get('ready_pages_state') != {**receipt.get('pending_pages_state', {}),
                    'status': 'ready', 'aborted_recovery_id': journal['id']}):
            raise Hold('Original connection abort receipt changed')
        shared = pages_state(self.config)
        if shared not in (receipt['pending_pages_state'], receipt['ready_pages_state']):
            raise Hold('Original connection abort reservation changed')
        return receipt, shared

    def abort_prepared(self, journal, token):
        # No candidate, upload snapshot, cloud attempt or retired resource may
        # be inferred away merely because the original route now answers.
        if (journal['phase'] != 'prepared'
                or set(journal) != {'id', 'config_sha256', 'old', 'phase', 'updated_at'}
                or any((self.root / name).exists() for name in
                       ('upload-' + journal['id'], 'tunnel-' + journal['id'] + '.private.log'))):
            return False
        shared = self.require_generation(journal)
        if not self.original_connection_verified(journal, token, shared):
            return False
        ready = {**shared, 'status': 'ready', 'aborted_recovery_id': journal['id']}
        receipt = {'phase': 'aborted', 'reason': 'Original connection freshly verified before any attempt',
                   'prepared_journal': dict(journal), 'pending_pages_state': shared,
                   'ready_pages_state': ready, 'native_actions': 0, 'cloud_mutations': 0}
        path = self.root / ('aborted-' + journal['id'] + '.private.json')
        if path.exists():
            if private_json(path) != receipt:
                raise Hold('Historical original connection abort receipt changed')
        else:
            atomic_json(path, receipt)
        journal['abort_receipt_sha256'] = digest(path.read_bytes())
        self.save(journal, 'aborted')
        # A crash here leaves an exact, independently resumable local commit.
        self.finish_abort(journal, token)
        return True

    def finish_abort(self, journal, token):
        receipt, shared = self.aborted_snapshot(journal)
        if shared == receipt['ready_pages_state']:
            return
        if not self.original_connection_verified(journal, token, shared):
            raise Hold('Original connection no longer verifies; abort reservation retained')
        atomic_json(self.config['deployment_state_file'], receipt['ready_pages_state'])

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

    def candidate_log(self, journal):
        candidate = journal['candidate']
        log = self.root / ('tunnel-' + journal['id'] + '.private.log')
        if candidate.get('log') != str(log):
            raise Hold('Saved candidate log is outside its recovery')
        mode = log.lstat()
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
            raise Hold('Saved candidate log must be a regular private file')
        return log

    def saved_candidate_url(self, journal):
        candidate = journal['candidate']
        log = self.candidate_log(journal)
        urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', log.read_text(errors='replace')[-65536:])
        if not urls:
            raise Hold('Saved candidate tunnel origin is unavailable')
        url = origin(urls[-1], tunnel=True)
        if candidate.get('backend_url') is not None and origin(candidate['backend_url'], tunnel=True) != url:
            raise Hold('Saved candidate origin differs from its process log')
        return url

    def require_candidate_identity(self, journal):
        candidate = journal['candidate']
        identity = candidate.get('identity')
        if (not isinstance(identity, dict) or type(candidate.get('pid')) is not int or
                candidate['pid'] <= 1 or identity.get('pid') != candidate['pid'] or
                not isinstance(identity.get('started'), str) or not identity['started'] or
                identity.get('command') != ' '.join(self.host.tunnel_command()) or
                self.host.process_identity(candidate['pid']) != identity):
            raise Hold('Pending replacement tunnel disappeared or changed; review required')

    def require_candidate(self, journal, token):
        self.require_candidate_identity(journal)
        url = self.saved_candidate_url(journal)
        if not self.health(url, direct=True) or not self.authenticated(url, token, direct=True):
            raise Hold('Saved replacement tunnel is unavailable; no cloud mutation attempted')
        return url

    def launch_candidate(self, journal, log):
        # Unknown Popen results must not create another tunnel on a later tick.
        self.save(journal, 'candidate_launch_pending')
        pid = self.host.launch(log)
        identity = self.host.process_identity(pid)
        journal['candidate'] = {'pid': pid, 'identity': identity, 'log': str(log),
                                'started_at': self.host.now_utc().isoformat()}
        self.save(journal, 'candidate_launch_pending')
        if not identity or identity.get('pid') != pid or identity.get('command') != ' '.join(self.host.tunnel_command()):
            raise Hold('Replacement tunnel process did not verify; unknown launch requires review')
        self.require_unchanged(journal)
        self.save(journal, 'candidate_started')

    def candidate_registration_failure(self, log):
        text = log.read_text(errors='replace')[-65536:]
        rejected = text.rfind('Register tunnel error from server side error="Unauthorized: Tunnel not found"')
        if rejected < 0 or text.rfind('Registered tunnel connection') > rejected:
            return False
        # Any new successful registration invalidates earlier DNS failure
        # probes, even when the resolver is still propagating the new route.
        return {'registrations': tuple(line for line in text.splitlines() if 'Registered tunnel connection' in line)}

    def candidate_failure_confirmed(self, journal, log, url):
        if journal['phase'] != 'candidate_started' or journal.get('deployment_origin') is not None:
            return False
        try:
            started = datetime.fromisoformat(journal['candidate'].get('started_at', journal['updated_at']))
            if started.tzinfo is None or (self.host.now_utc() - started).total_seconds() < CANDIDATE_FAILURE_GRACE_SECONDS:
                return False
        except (ValueError, TypeError, KeyError):
            raise Hold('Candidate age evidence is invalid; no retirement attempted') from None
        evidence = self.candidate_registration_failure(log)
        if not evidence:
            return False
        for attempt in range(2):
            status, data = self.host.request(url, '/api/config', headers=self.bridge_headers())
            failed = isinstance(data, dict) and ((status == 0 and data.get('dns_failure') is True) or
                     (status == 530 and data.get('tunnel_not_found') is True))
            if not failed:
                return False
            if attempt == 0:
                self.host.sleep(2)
        return evidence if self.candidate_registration_failure(log) == evidence else False

    def replace_failed_candidate(self, journal, log, url, evidence):
        if self.candidate_registration_failure(log) != evidence:
            return False
        count = journal.get('candidate_replacements', 0)
        if type(count) is not int or not 0 <= count < MAX_CANDIDATE_REPLACEMENTS:
            raise Hold('Candidate replacement budget exhausted; owner reconciliation required')
        self.require_unchanged(journal)
        self.require_candidate_identity(journal)
        if self.candidate_registration_failure(log) != evidence:
            return False
        suffix = journal['id'] + '-' + str(count + 1)
        archive = self.root / ('failed-candidate-' + suffix + '.private.json')
        archived_log = self.root / ('failed-candidate-' + suffix + '.private.log')
        if archive.exists() or archived_log.exists():
            raise Hold('Candidate retirement archive already exists; no replay attempted')
        atomic_json(archive, {'journal': journal, 'failure': 'Repeated DNS/1033 and server registration not-found'})
        # Stop once, only the verified failed candidate. The bound tunnel remains.
        self.save(journal, 'candidate_retire_pending')
        candidate = journal['candidate']
        identity = candidate['identity']
        # Recheck after private writes, directly before the stop. If a
        # registration arrived, preserve the archive as cancellation history
        # and resume readiness without consuming a replacement or signaling.
        if self.candidate_registration_failure(log) != evidence:
            deferred = self.root / ('cancelled-retirement-' + journal['id'] + '-' + uuid4().hex + '.private.json')
            os.replace(archive, deferred)
            journal.setdefault('candidate_retirement_deferrals', []).append({
                'archive': str(deferred), 'reason': 'Fresh registration invalidated candidate failure evidence'})
            self.save(journal, 'candidate_started')
            return False
        if not self.host.stop(identity):
            raise Hold('Failed candidate identity changed before retirement')
        for _ in range(CANDIDATE_STOP_WAIT_SECONDS * 5):
            current = self.host.process_identity(candidate['pid'])
            if current is None:
                break
            if current != identity:
                raise Hold('Retired PID identity changed; no further signal attempted')
            self.host.sleep(0.2)
        else:
            raise Hold('Candidate graceful stop is pending; no launch attempted')
        self.require_unchanged(journal)
        os.replace(log, archived_log)
        journal.setdefault('candidate_history', []).append({**candidate, 'archived_log': str(archived_log),
            'failed_origin': url, 'stop_verified': True, 'failure': 'Repeated DNS/1033 and server registration not-found'})
        journal.pop('candidate')
        journal['candidate_replacements'] = count + 1
        self.launch_candidate(journal, log)

    def candidate(self, journal, token):
        candidate = journal.get('candidate')
        if candidate:
            self.require_candidate_identity(journal)
            log = self.candidate_log(journal)
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', log.read_text(errors='replace')[-65536:])
            if urls:
                url = self.saved_candidate_url(journal)
                evidence = self.candidate_failure_confirmed(journal, log, url)
                if evidence:
                    self.replace_failed_candidate(journal, log, url, evidence)
        else:
            log = self.root / ('tunnel-' + journal['id'] + '.private.log')
            self.launch_candidate(journal, log)
        for _ in range(30):
            text = log.read_text(errors='replace')[-65536:]
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
            if urls:
                url = self.saved_candidate_url(journal)
                if self.health(url, direct=True) and self.authenticated(url, token, direct=True):
                    journal['candidate']['backend_url'] = url
                    self.save(journal, 'secret_updated' if journal['phase'] == 'secret_updated' else 'candidate_ready')
                    return url
            self.host.sleep(2)
        raise Hold('Replacement tunnel is not ready; old connection preserved')

    def commit_connection(self, journal):
        # One atomically replaced canonical record binds PID, route and receipt.
        # The old compatibility files are recoverable mirrors, not the commit.
        receipt = {'config_sha256': self.sha, 'plan_sha256': self.config['plan_sha256'],
                   'asset_manifest_sha256': manifest_digest(self.plan),
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
        url = origin(candidate['backend_url'], tunnel=True)
        self.require_generation(journal)
        # Permit restart after either mirror was already written, but never
        # overwrite a different operator's route or PID.
        route_path, pid_path = Path(self.config['route_file']), Path(self.config['pid_file'])
        route = private_json(route_path)
        pid_bytes = pid_path.read_bytes()
        if digest(route_path.read_bytes()) != journal['old']['route_sha256'] and route.get('backend_url') != candidate['backend_url']:
            raise Hold('Route mirror changed outside recovery')
        if digest(pid_bytes) != journal['old']['pid_sha256'] and pid_bytes.strip() != str(candidate['pid']).encode():
            raise Hold('PID mirror changed outside recovery')
        secrets_path = Path(self.config['pages_secrets_file'])
        secrets = private_json(secrets_path)
        if digest(json.dumps({k: v for k, v in secrets.items() if k != 'BACKEND_URL'}, sort_keys=True).encode()) != journal['old']['secrets_other_sha256']:
            raise Hold('Other private Pages secret fields changed outside recovery')
        if digest(secrets_path.read_bytes()) != journal['old']['secrets_sha256'] and secrets.get('BACKEND_URL') != url:
            raise Hold('Pages secrets mirror changed outside recovery')
        atomic_json(route_path, {'backend_url': url})
        # Keep the existing plain integer PID format via the same atomic writer.
        atomic_json(pid_path, candidate['pid'])
        atomic_json(secrets_path, {**secrets, 'BACKEND_URL': url})
        shared = self.require_generation(journal)
        atomic_json(self.config['deployment_state_file'], {
            **shared, 'status': 'ready', 'backend_url': url,
            'deployment_origin': deployment_origin(journal['deployment_origin']),
            'connection_revision': journal['id'],
        })
        stopped = self.host.stop(journal['old']['identity'])
        journal['old_tunnel_stopped'] = stopped
        self.save(journal, 'complete')

    def recover(self):
        with pages_writer_lock(self.config):
            return self._recover()

    def _recover(self):
        if self.journal_path.exists():
            journal = private_json(self.journal_path)
            if journal.get('config_sha256') != self.sha:
                raise Hold('Pending recovery belongs to another reviewed configuration')
            if journal['phase'] == 'complete':
                journal = None
            elif journal['phase'] == 'aborted':
                receipt, shared = self.aborted_snapshot(journal)
                if shared == receipt['ready_pages_state']:
                    journal = None
        else:
            journal = None
        # Validate all durable destinations before constructing secret headers.
        if journal:
            if not isinstance(journal.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', journal['id']):
                raise Hold('Invalid durable recovery identity')
            origin(journal['old']['backend_url'], tunnel=True)
            if journal['phase'] in ('candidate_launch_pending', 'candidate_retire_pending'):
                raise Hold('Unknown candidate launch or retirement outcome requires reconciliation; no repeat attempted')
            candidate = journal.get('candidate', {})
            if candidate:
                self.require_candidate_identity(journal)
                self.candidate_log(journal)
            if candidate.get('backend_url') is not None:
                origin(candidate['backend_url'], tunnel=True)
                self.saved_candidate_url(journal)
            if journal.get('deployment_origin') is not None:
                deployment_origin(journal['deployment_origin'])
            self.require_generation(journal)
            if journal['phase'] in ('secret_pending', 'deploy_pending'):
                raise Hold('Unknown cloud mutation outcome requires reconciliation; no repeat attempted')
            if journal['phase'] not in ('prepared', 'aborted', 'candidate_started', 'candidate_ready', 'secret_updated', 'verification_pending', 'committed'):
                raise Hold('Unrecognized recovery phase requires review')
        if not self.health(self.config['local_origin'], direct=True):
            raise Hold('Local backend or Mac bridge is unavailable; no restart attempted')
        token = self.login()
        if not self.authenticated(self.config['local_origin'], token, direct=True):
            raise Hold('Local authenticated API is unavailable')
        if journal and journal['phase'] == 'aborted':
            self.finish_abort(journal, token)
            return 'healthy'
        if journal and journal['phase'] == 'committed':
            self.require_candidate(journal, token)
            if not self.health(self.config['public_origin']) or not self.authenticated(self.config['public_origin'], token):
                raise Hold('Committed connection is not healthy; old tunnel preserved')
            self.finish_commit(journal)
            return 'recovered'
        if journal is None:
            shared = pages_state(self.config)
            if shared['status'] != 'ready' or shared.get('asset_manifest_sha256') != manifest_digest(self.plan):
                raise Hold('Another Pages deployment is pending or asset provenance changed')
            journal = {'id': uuid4().hex, 'config_sha256': self.sha, 'old': self.connection_snapshot()}
            if shared['backend_url'] != journal['old']['backend_url']:
                raise Hold('Canonical Pages route and active tunnel differ')
            self.save(journal, 'prepared')
            atomic_json(self.config['deployment_state_file'], {**shared, 'status': 'recovery_pending', 'recovery_id': journal['id']})
        self.require_unchanged(journal)
        if journal['phase'] == 'verification_pending':
            return self.verify_deployment(journal, token)
        if not journal.get('candidate'):
            if self.abort_prepared(journal, token):
                return 'healthy'
            self.require_old_tunnel_failure(journal)
        upload = self.snapshot_upload(journal)
        url = self.candidate(journal, token)
        self.require_unchanged(journal)
        if journal['phase'] != 'secret_updated':
            self.save(journal, 'secret_pending')
            self.host.cli(['pages', 'secret', 'put', 'BACKEND_URL', '--project-name', self.config['project']],
                          input_value=url + '\n', cwd=self.root)
            self.save(journal, 'secret_updated')
        self.save(journal, 'deploy_pending')
        self.require_unchanged(journal)
        manifest_files({**self.plan, 'upload': str(upload)})
        output = self.host.cli(['pages', 'deploy', str(upload), '--project-name', self.config['project'],
                               '--branch', self.config['branch'], '--no-bundle'], cwd=self.root)
        deployment = re.findall(r'https://[a-f0-9]{8}\.text-monkey-demo\.pages\.dev', output)
        if not deployment:
            raise Hold('Cloudflare deployment identity is unavailable; candidate retained')
        journal['deployment_origin'] = deployment_origin(deployment[-1])
        self.save(journal, 'verification_pending')
        return self.verify_deployment(journal, token)

    def verify_deployment(self, journal, token):
        self.require_candidate(journal, token)
        immutable = deployment_origin(journal['deployment_origin'])
        for _ in range(15):
            origins = (immutable, self.config['public_origin'])
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
        if pending.get('phase') == 'aborted':
            _, shared = self.aborted_snapshot(pending)
            if shared['status'] == 'ready':
                pending = {}
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
