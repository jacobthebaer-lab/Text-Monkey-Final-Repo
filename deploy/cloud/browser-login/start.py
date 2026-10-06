"""Private manual desktop only; no cookie import, browser scripting or sends."""
import os
import json
from pathlib import Path
import secrets
import signal
import string
import subprocess
import time


children = []
stopping = False


def stop(_signal=None, _frame=None):
    global stopping
    stopping = True


def launch(arguments):
    # Browser diagnostics may contain private URLs. Never write them to Docker logs.
    child = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children.append(child)
    return child


def prepare_session_restoration(profile=Path('/data/profile')):
    """Set only Chromium's standard persistent startup preference before launch."""
    target = profile / 'Default' / 'Preferences'
    temporary = None
    try:
        if any(path.is_symlink() for path in (profile, target.parent, target)):
            raise ValueError('Unsafe profile path')
        lock = profile / 'SingletonLock'
        if lock.exists() or lock.is_symlink():
            raise ValueError('Browser profile is in use or needs checked recovery')
        prefs = json.loads(target.read_text()) if target.exists() else {}
        if not isinstance(prefs, dict) or ('session' in prefs and not isinstance(prefs['session'], dict)):
            raise ValueError('Invalid preferences')
        if type(prefs.get('session', {}).get('restore_on_startup')) is int and prefs['session']['restore_on_startup'] == 1:
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.parent / ('.voice-session-' + secrets.token_hex(16))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({**prefs, 'session': {**prefs.get('session', {}), 'restore_on_startup': 1}}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except (OSError, ValueError, TypeError):
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RuntimeError('Cloud browser preferences unavailable; profile was not reset.') from None


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.umask(0o077)
    marker = Path('/data/manual-login.active')
    # A crash leaves this fail-closed marker. Only the explicit stop helper clears it.
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        raise SystemExit('Manual login is already active or needs an explicit stop cleanup.')
    prepare_session_restoration()
    password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    Path('/tmp/login-password').write_text(password)
    stored = subprocess.run(['x11vnc', '-storepasswd', password, '/tmp/vnc-password'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if stored.returncode:
        raise RuntimeError('Private desktop authentication unavailable')
    launch(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp', '-ac'])
    for _ in range(100):
        ready = subprocess.run(['xdpyinfo', '-display', ':99'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0
        if ready:
            break
        if stopping or children[0].poll() is not None:
            raise RuntimeError('Private display unavailable')
        time.sleep(0.1)
    else:
        raise RuntimeError('Private display unavailable')
    launch(['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900',
            '-rfbauth', '/tmp/vnc-password', '-forever', '-shared', '-noxdamage',
            '-noclipboard', '-nosel'])
    launch(['websockify', '--web=/usr/share/novnc', '0.0.0.0:6080', '127.0.0.1:5900'])
    # Chromium's sandbox is mandatory. Its namespace syscalls are narrowly allowed
    # by the Compose seccomp profile; never retry with sandbox-disabling switches.
    # No remote debugging, stealth options, credential export or browser automation.
    launch(['/usr/bin/chromium', '--user-data-dir=/data/profile',
            '--no-first-run', '--no-default-browser-check', '--window-size=1280,900',
            'about:blank'])
    print('Private manual desktop ready. Retrieve its temporary password through SSH.', flush=True)
    while not stopping:
        if any(child.poll() is not None for child in children):
            raise RuntimeError('Private desktop process stopped')
        time.sleep(0.2)


if __name__ == '__main__':
    try:
        main()
    finally:
        # Stop Chromium first so it can flush the shared persistent profile.
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
