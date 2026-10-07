"""Admin-owned, Google Calendar OAuth, stored outside the database.

Private files need a persistent encrypted host volume in production. Credentials
never enter frontend responses, application tables, or repository evidence.
"""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from fastapi import HTTPException

SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.app.created']
TOKEN_URL = 'https://oauth2.googleapis.com/token'
FLOW_TTL = 900


def configured(settings):
    uri = urlsplit(settings.google_calendar_redirect_uri)
    return bool(settings.google_calendar_client_id and settings.google_calendar_client_secret
                and uri.hostname and not uri.username and not uri.password
                and uri.path == '/api/google-calendar/callback' and not uri.query and not uri.fragment
                and (uri.scheme == 'https' or (uri.scheme == 'http' and uri.hostname in {'127.0.0.1', 'localhost'})))


def owner(user):
    try:
        return str(UUID(user['id']))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(403, 'A verified coordinator identity is required.') from None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, settings):
        self.root = Path(settings.google_calendar_state_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    @contextmanager
    def lock(self):
        # Serialize across workers as well as threads, including imports.
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Expired flows, including incomplete token exchanges, are private
            # temporary material and must not accumulate on disk.
            for path in self.root.glob('flow-*.json'):
                if path.stat().st_mtime < time.time() - FLOW_TTL:
                    path.unlink(missing_ok=True)
            yield
        finally:
            os.close(fd)

    def path(self, key):
        return self.root / (key + '.json')

    def read(self, key):
        try:
            return json.loads(self.path(key).read_text())
        except FileNotFoundError:
            return None

    def write(self, key, value):
        temporary = self.root / ('.tmp-' + secrets.token_hex(16))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(value, file)
            os.replace(temporary, self.path(key))
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def clear_flows(self, owner_id):
        for path in self.root.glob('flow-*.json'):
            if json.loads(path.read_text()).get('owner') == owner_id:
                path.unlink(missing_ok=True)


def start(store, settings, user):
    if not configured(settings):
        raise HTTPException(503, 'Google Calendar needs owner configuration before accounts can connect.')
    flow_id, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    store.clear_flows(owner(user))
    store.write('flow-' + digest(flow_id), {
        'owner': owner(user), 'admin_email': user['email'].lower(), 'state_hash': digest(state),
        'verifier': verifier, 'created': time.time(), 'status': 'pending',
    })
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': settings.google_calendar_client_id, 'redirect_uri': settings.google_calendar_redirect_uri,
        'response_type': 'code', 'scope': ' '.join(SCOPES), 'access_type': 'offline',
        'prompt': 'consent select_account', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
    })
    return {'authorization_url': url, 'flow_id': flow_id}


def complete_callback(store, settings, state, code, denied=False):
    if not isinstance(state, str) or not 20 <= len(state) <= 200:
        raise HTTPException(400, 'Calendar connection expired. Start again from Settings.')
    match = next(((path, json.loads(path.read_text())) for path in store.root.glob('flow-*.json')
                  if secrets.compare_digest(json.loads(path.read_text()).get('state_hash', ''), digest(state))), None)
    if not match:
        raise HTTPException(400, 'Calendar connection expired. Start again from Settings.')
    path, flow = match
    if flow['status'] != 'pending' or flow['created'] < time.time() - FLOW_TTL:
        raise HTTPException(400, 'Calendar connection expired. Start again from Settings.')
    from app.web.texty import allowed_emails
    if flow['admin_email'] not in allowed_emails(settings):
        store.delete(path.stem)
        raise HTTPException(403, 'Coordinator access was removed.')
    # Consume state before any external call. Failed/denied flows cannot replay.
    flow['status'] = 'failed'
    flow.pop('state_hash')
    store.write(path.stem, flow)
    if denied:
        return False
    if not isinstance(code, str) or not 1 <= len(code) <= 4096:
        raise HTTPException(400, 'Google did not provide a valid authorization code.')
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(TOKEN_URL, data={
                'client_id': settings.google_calendar_client_id, 'client_secret': settings.google_calendar_client_secret,
                'redirect_uri': settings.google_calendar_redirect_uri, 'grant_type': 'authorization_code',
                'code': code, 'code_verifier': flow['verifier'],
            })
            response.raise_for_status()
            tokens = response.json()
            if not tokens.get('refresh_token') or not tokens.get('access_token'):
                raise ValueError('Missing offline authorization')
            if not set(SCOPES).issubset(set(tokens.get('scope', '').split())):
                raise ValueError('Incomplete permission grant')
            identity = client.get('https://openidconnect.googleapis.com/v1/userinfo',
                                  headers={'Authorization': 'Bearer ' + tokens['access_token']})
            identity.raise_for_status()
            account = identity.json()
            if not account.get('email_verified') or not account.get('email'):
                raise ValueError('Unverified Google identity')
        flow.update(status='ready', refresh_token=tokens['refresh_token'], account_email=account['email'])
        flow.pop('verifier', None)
        store.write(path.stem, flow)
        return True
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Google Calendar could not connect. Start again from Settings.') from None


def finish(store, user, flow_id):
    if not isinstance(flow_id, str) or not 20 <= len(flow_id) <= 100:
        raise HTTPException(400, 'Start connecting Google Calendar from this browser tab.')
    key = 'flow-' + digest(flow_id)
    flow = store.read(key)
    if not flow or flow['owner'] != owner(user) or flow['created'] < time.time() - FLOW_TTL:
        raise HTTPException(400, 'Calendar connection expired. Start again from Settings.')
    if flow['status'] != 'ready':
        raise HTTPException(409, 'Google authorization is incomplete. Start again from Settings.')
    # Only the original authenticated tab can finalize the callback. The callback
    # has no bearer and does not change an existing connection on its own.
    previous = store.read('account-' + owner(user)) or {}
    publication = {k: v for k, v in previous.items() if k.startswith('publish') or k == 'last_publish_at'} if previous.get('account_email') == flow['account_email'] else {}
    store.write('account-' + owner(user), {
        'refresh_token': flow['refresh_token'], 'account_email': flow['account_email'],
        'calendar_id': '', 'calendar_name': '', 'last_sync_at': None, 'counts': None, **publication,
    })
    store.delete(key)


def service(settings, connection):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    credentials = Credentials(token=None, refresh_token=connection['refresh_token'],
        token_uri=TOKEN_URL, client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret, scopes=SCOPES)
    return build('calendar', 'v3', credentials=credentials, cache_discovery=False)


def calendars(api):
    result, page = [], None
    while True:
        response = api.calendarList().list(minAccessRole='reader', maxResults=250, pageToken=page).execute()
        result.extend({'id': c['id'], 'name': c.get('summaryOverride') or c.get('summary', c['id']),
                       'timezone': c.get('timeZone', 'UTC'), 'primary': bool(c.get('primary'))}
                      for c in response.get('items', []) if not c.get('deleted'))
        page = response.get('nextPageToken')
        if not page:
            return result
