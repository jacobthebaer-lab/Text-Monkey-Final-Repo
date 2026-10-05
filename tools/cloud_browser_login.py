"""Explicit operator handoff for a private manual browser, never transport enablement."""
import argparse
import json
from pathlib import Path
import re
import subprocess


class LoginError(Exception):
    pass


class Login:
    def __init__(self, env_file, project, repo=None):
        path = Path(env_file).resolve()
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise LoginError('Select the existing private environment file with mode 0600.')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project):
            raise LoginError('Use the exact existing Compose project name.')
        root = Path(repo or Path(__file__).resolve().parents[1]).resolve()
        self.environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                            'CLOUD_ENV_FILE': str(path)}
        self.command = ['docker', 'compose', '--project-name', project, '--env-file', str(path),
                        '-f', str(root / 'deploy/cloud/compose.yaml'),
                        '-f', str(root / 'deploy/cloud/compose.login.yaml'),
                        '--profile', 'manual-login']

    def run(self, *arguments):
        result = subprocess.run([*self.command, *arguments], env=self.environment,
                                capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise LoginError('Docker operation failed; inspect it privately through SSH.')
        return result.stdout

    def states(self):
        output = self.run('ps', '--all', '--format', 'json').strip()
        rows = json.loads(output) if output.startswith('[') else [json.loads(line) for line in output.splitlines()]
        return {row['Service']: row['State'] for row in rows}

    @staticmethod
    def active(state):
        return state is not None and state not in {'exited', 'dead'}

    def require_volume(self):
        config = json.loads(self.run('config', '--format', 'json'))
        name = config['volumes']['voice-data']['name']
        result = subprocess.run(['docker', 'volume', 'inspect', name], env=self.environment,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise LoginError('The existing voice-data volume is missing; refuse a new login profile.')

    def action(self, action):
        if action == 'build':
            self.run('build', 'browser-login')
            return 'Private login image built; no runtime started.'
        self.require_volume()
        states = self.states()
        if action == 'start':
            if self.active(states.get('google-voice')):
                raise LoginError('Stop the connector first; it must never share an open browser profile.')
            if self.active(states.get('browser-login')):
                raise LoginError('The manual login browser is already active.')
            self.run('up', '-d', '--no-deps', '--wait', '--wait-timeout', '60', 'browser-login')
            return 'Manual desktop started on VM loopback port 6080 only. Use the SSH tunnel and password action.'
        if action == 'password':
            if states.get('browser-login') != 'running':
                raise LoginError('The manual login browser is not running.')
            return self.run('exec', '-T', 'browser-login', 'cat', '/tmp/login-password').strip()
        if action == 'stop':
            self.run('stop', '-t', '30', 'browser-login')
            if self.active(self.states().get('browser-login')):
                raise LoginError('Manual browser still active; leave the profile locked.')
            self.run('run', '--rm', '--no-deps', '--entrypoint', 'python3', 'browser-login', '-c',
                     "from pathlib import Path; Path('/data/manual-login.active').unlink(missing_ok=True)")
            return 'Manual browser stopped and its login marker cleared. Connector remains stopped.'
        if action == 'resume':
            if self.active(states.get('browser-login')):
                raise LoginError('Stop the manual browser before restarting the connector.')
            self.run('run', '--rm', '--no-deps', '--entrypoint', 'python3', 'browser-login', '-c',
                     "from pathlib import Path; import sys; sys.exit(Path('/data/manual-login.active').exists())")
            self.run('up', '-d', '--no-deps', 'google-voice')
            return 'Connector restarted with its existing settings. Verify cloud sign-in explicitly in the admin UI.'
        raise LoginError('Unknown action.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'start', 'password', 'stop', 'resume'))
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--project-name', required=True)
    options = parser.parse_args()
    try:
        print(Login(options.env_file, options.project_name).action(options.action))
    except (LoginError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
        # Never echo Compose output, private paths, configuration or credentials on failure.
        raise SystemExit(str(error) if isinstance(error, LoginError) else 'Private login operation failed.')


if __name__ == '__main__':
    main()
