"""Synthetic OAuth and calendar services. Never access a real Google account."""
import json
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import select
from app.config import Settings
from app.main import create_app
from app.web.texty import admin
from app.integrations import google_calendar_oauth as oauth
from app.integrations.gcal import sync, source_key
from app.integrations.google_calendar_publish import publish
from app.db import models as m
from app.agents.fill_agent import FillContext

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
USER = {'id': OWNER, 'email': 'admin@example.test'}


@pytest.fixture
def calendar_client(tmp_path):
    settings = Settings(database_url='sqlite://', automation_enabled=False,
        google_calendar_client_id='synthetic-client', google_calendar_client_secret='synthetic-secret',
        google_calendar_redirect_uri='https://admin.example.test/api/google-calendar/callback',
        google_calendar_state_dir=str(tmp_path / 'private'), admin_email_allowlist=USER['email'],
        admin_site_url='https://admin.example.test/texty')
    app = create_app(settings)
    app.dependency_overrides[admin] = lambda: USER
    with TestClient(app) as client:
        yield client, app, oauth.Store(settings)


def put_account(store, owner=OWNER):
    store.write('account-' + owner, {'refresh_token': 'synthetic-private-refresh', 'account_email': 'google@example.test',
        'calendar_id': 'church', 'calendar_name': 'Church', 'calendar_timezone': 'America/Denver'})


def fake_exchange(monkeypatch, scopes=None):
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, data):
            assert data['code_verifier'] and data['client_secret'] == 'synthetic-secret'
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'refresh_token': 'synthetic-private-refresh', 'access_token': 'synthetic-private-access',
                'scope': ' '.join(scopes if scopes is not None else oauth.SCOPES)})
        def get(self, url, headers):
            return httpx.Response(200, request=httpx.Request('GET', url), json={'email':'google@example.test','email_verified':True})
    monkeypatch.setattr(oauth.httpx, 'Client', Client)


def test_oauth_single_use_private_and_bound_to_original_admin(calendar_client, monkeypatch):
    client, app, store = calendar_client
    fake_exchange(monkeypatch)
    started = client.post('/api/google-calendar/connect').json()
    query = parse_qs(urlsplit(started['authorization_url']).query)
    assert query['code_challenge_method'] == ['S256']
    assert query['access_type'] == ['offline']
    assert 'calendar.app.created' in query['scope'][0]
    flow = store.read('flow-' + oauth.digest(started['flow_id']))
    assert 'verifier' in flow and 'state_hash' in flow
    assert list(store.root.glob('flow-*'))[0].stat().st_mode & 0o777 == 0o600
    assert client.get('/api/google-calendar/callback?state=wrong&code=synthetic').status_code == 400
    url = '/api/google-calendar/callback?state=' + query['state'][0] + '&code=synthetic'
    result = client.get(url, follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'].endswith('#google-calendar=ready')
    assert not store.read('account-' + OWNER)  # No unauthenticated account replacement.
    assert client.get(url).status_code == 400
    app.dependency_overrides[admin] = lambda: {**USER, 'id':OTHER}
    assert client.post('/api/google-calendar/finish', json={'flow_id': started['flow_id']}).status_code == 400
    app.dependency_overrides[admin] = lambda: USER
    result = client.post('/api/google-calendar/finish', json={'flow_id': started['flow_id']})
    assert result.json()['connected'] and 'synthetic-private' not in result.text
    assert client.get('/api/google-calendar').headers['cache-control'] == 'no-store'
    assert not store.read('flow-' + oauth.digest(started['flow_id']))
    assert client.post('/api/google-calendar/finish', json={'flow_id': started['flow_id']}).status_code == 400


def test_denial_expiry_partial_grant_and_removed_access(calendar_client, monkeypatch):
    client, app, store = calendar_client
    fake_exchange(monkeypatch, ['openid', 'https://www.googleapis.com/auth/userinfo.email'])
    def begin():
        data = client.post('/api/google-calendar/connect').json()
        return data, parse_qs(urlsplit(data['authorization_url']).query)['state'][0]
    data, state = begin()
    assert client.get('/api/google-calendar/callback', params={'state':state,'error':'access_denied'}, follow_redirects=False).headers['location'].endswith('denied')
    assert client.post('/api/google-calendar/finish', json={'flow_id':data['flow_id']}).status_code == 409
    data, state = begin()
    assert client.get('/api/google-calendar/callback', params={'state':state,'code':'synthetic'}).status_code == 502
    data, state = begin()
    key = 'flow-' + oauth.digest(data['flow_id'])
    flow = store.read(key); flow['created'] = time.time() - 1000; store.write(key, flow)
    assert client.get('/api/google-calendar/callback', params={'state':state,'code':'synthetic'}).status_code == 400
    _, state = begin()
    app.state.settings = replace(app.state.settings, admin_email_allowlist='')
    assert client.get('/api/google-calendar/callback', params={'state':state,'code':'synthetic'}).status_code == 403


def test_admin_isolation_disconnect_and_csrf(calendar_client):
    client, app, store = calendar_client
    put_account(store)
    assert client.get('/api/google-calendar').json()['connected']
    assert client.post('/api/google-calendar/disconnect', headers={'Origin':'https://attacker.example'}).status_code == 403
    app.dependency_overrides[admin] = lambda: {**USER, 'id':OTHER}
    assert not client.get('/api/google-calendar').json()['connected']
    assert client.post('/api/google-calendar/sync').status_code == 409
    assert client.post('/api/google-calendar/disconnect').status_code == 200
    assert store.read('account-' + OWNER)
    app.dependency_overrides[admin] = lambda: USER
    client.post('/api/google-calendar/disconnect')
    assert not store.read('account-' + OWNER)
    app.dependency_overrides.clear()
    assert client.get('/api/google-calendar').status_code == 503


def test_selection_validates_account_and_blocks_publish_loop(calendar_client, monkeypatch):
    client, app, store = calendar_client
    put_account(store)
    connection = store.read('account-' + OWNER); connection['publish_calendar_id'] = 'output'; store.write('account-' + OWNER, connection)
    monkeypatch.setattr(oauth, 'service', lambda *a: object())
    monkeypatch.setattr(oauth, 'calendars', lambda api: [{'id':'church','name':'Church','timezone':'America/Denver'}, {'id':'output','name':'Text Monkey','timezone':'UTC'}])
    assert client.post('/api/google-calendar/select', json={'calendar_id':'foreign'}).status_code == 422
    assert client.post('/api/google-calendar/select', json={'calendar_id':'output'}).status_code == 422
    assert client.post('/api/google-calendar/select', json={'calendar_id':'church'}).json()['calendar_name'] == 'Church'


def result(value): return SimpleNamespace(execute=lambda: value)


def test_import_pagination_namespace_all_day_and_protected_cancellation(session, clock, provider, make_shift, make_volunteer):
    shift = make_shift(starts=clock.now() + timedelta(days=2))
    volunteer = make_volunteer()
    event = shift.event
    event.gcal_event_id = source_key('church', 'shared-id')
    session.add(m.Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status='confirmed', source='admin', created_at=clock.now(), updated_at=clock.now()))
    session.flush()
    pages = [
        {'items':[{'id':'shared-id','status':'cancelled'}], 'nextPageToken':'next'},
        {'items':[{'id':'all-day','start':{'date':'2026-10-10'},'end':{'date':'2026-10-11'}},
                  {'id':'other','summary':'Community event','start':{'dateTime':(clock.now()+timedelta(days=1)).isoformat()},'end':{'dateTime':(clock.now()+timedelta(days=1,hours=1)).isoformat()}}]},
    ]
    seen = []
    def listing(**kw):
        seen.append(kw['pageToken']); return result(pages[1 if kw['pageToken'] else 0])
    api = SimpleNamespace(events=lambda:SimpleNamespace(list=listing))
    ctx = FillContext(session, clock, provider, None)
    counts = sync(ctx, api, Settings(google_calendar_id='church'), namespaced=True)
    assert counts['protected'] == counts['skipped'] == counts['created'] == 1
    assert event.status == 'scheduled' and seen == [None, 'next']
    counts = sync(ctx, api, Settings(google_calendar_id='church'), namespaced=True)
    assert counts['created'] == 0
    assert len(list(session.scalars(select(m.Escalation).where(m.Escalation.category=='unclear')))) == 1
    counts = sync(ctx, api, Settings(google_calendar_id='different'), namespaced=True)
    assert counts['created'] == 1 and source_key('church', 'other') != source_key('different', 'other')
    assert provider.sent == []


class PublishAPI:
    def __init__(self): self.remote = {}; self.calls = []; self.fail = False
    def calendars(self): return self
    def events(self): return self
    def insert(self, **kw):
        if 'calendarId' not in kw:
            self.calls.append(('calendar', kw)); return result({'id':'app-output'})
        assert kw['calendarId'] == 'app-output' and kw['sendUpdates'] == 'none'
        assert 'attendees' not in kw['body']
        self.calls.append(('insert', kw))
        if self.fail: raise HttpError(SimpleNamespace(status=503, reason='Synthetic'), b'{}')
        self.remote[kw['body']['id']] = kw['body']; return result(kw['body'])
    def get(self, **kw):
        if kw['eventId'] not in self.remote: raise HttpError(SimpleNamespace(status=404, reason='Synthetic'), b'{}')
        return result(self.remote[kw['eventId']])
    def patch(self, **kw):
        self.calls.append(('patch', kw)); self.remote[kw['eventId']].update(kw['body']); return result({})
    def delete(self, **kw):
        self.calls.append(('delete', kw)); self.remote.pop(kw['eventId'], None); return result({})


def test_publish_idempotent_partial_retry_updates_and_removes(session, clock, make_shift):
    event = make_shift(starts=clock.now()+timedelta(days=2)).event
    api = PublishAPI(); connection = {}; receipts = []
    def save(value): receipts.append(json.loads(json.dumps(value)))
    api.fail = True
    with pytest.raises(HttpError): publish(session, api, connection, clock, save)
    assert connection['publish_calendar_id'] == 'app-output'
    api.fail = False
    assert publish(session, api, connection, clock, save)['published'] == 1
    first = next(iter(api.remote))
    assert publish(session, api, connection, clock, save)['published'] == 1
    assert list(api.remote) == [first]
    assert sum(name=='calendar' for name, _ in api.calls) == 1
    event.title = 'Changed service'; event.starts_at += timedelta(days=100); event.ends_at += timedelta(days=100)
    publish(session, api, connection, clock, save)
    assert api.remote[first]['summary'] == 'Changed service'
    event.status = 'cancelled'
    assert publish(session, api, connection, clock, save)['removed'] == 1
    assert not api.remote and not connection['published_events']


def test_import_failure_rolls_back_new_events_and_does_not_record_success(calendar_client, monkeypatch):
    client, app, store = calendar_client
    put_account(store)
    from datetime import datetime, timezone
    start = datetime.now(timezone.utc) + timedelta(days=1)
    item = {'id':'one','summary':'Community event','start':{'dateTime':start.isoformat()},'end':{'dateTime':(start+timedelta(hours=1)).isoformat()}}
    def listing(**kw):
        if kw['pageToken']:
            raise HttpError(SimpleNamespace(status=503, reason='Synthetic'), b'{}')
        return result({'items':[item],'nextPageToken':'next'})
    monkeypatch.setattr(oauth, 'service', lambda *a: SimpleNamespace(events=lambda:SimpleNamespace(list=listing)))
    response = client.post('/api/google-calendar/sync')
    assert response.status_code == 502 and 'synthetic-private' not in response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Event)) is None
        assert session.scalar(select(m.Escalation)) is None
    assert not store.read('account-' + OWNER).get('last_sync_at')


def test_publish_endpoint_retains_progress_without_false_success(calendar_client, monkeypatch):
    client, app, store = calendar_client
    put_account(store)
    from datetime import datetime, timezone
    with app.state.session_factory() as session:
        start = datetime.now(timezone.utc)+timedelta(days=1)
        session.add(m.Event(title='Synthetic service', starts_at=start, ends_at=start+timedelta(hours=1), status='scheduled'))
        session.commit()
    api = PublishAPI(); api.fail = True
    monkeypatch.setattr(oauth, 'service', lambda *a: api)
    assert client.post('/api/google-calendar/publish').status_code == 502
    saved = store.read('account-' + OWNER)
    assert saved['publish_calendar_id'] == 'app-output' and not saved.get('last_publish_at')
    api.fail = False
    response = client.post('/api/google-calendar/publish')
    assert response.status_code == 200 and response.json()['publish_counts']['published'] == 1
    assert 'synthetic-private' not in response.text and 'published_events' not in response.text


def test_changed_assigned_event_is_held_and_unassigned_cancellation_deduplicates(session, clock, provider, make_shift, make_volunteer):
    shift = make_shift(); event = shift.event
    event.gcal_event_id = source_key('church','assigned')
    volunteer = make_volunteer()
    session.add(m.Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status='approved', source='admin', created_at=clock.now(), updated_at=clock.now()))
    free = m.Event(title='Unassigned', starts_at=event.starts_at, ends_at=event.ends_at, status='scheduled', gcal_event_id=source_key('church','free'))
    session.add(free); session.flush()
    original = event.starts_at
    items = [{'id':'assigned', 'summary':'Moved service', 'start':{'dateTime':(original+timedelta(hours=1)).isoformat()}, 'end':{'dateTime':(event.ends_at+timedelta(hours=1)).isoformat()}}, {'id':'free','status':'cancelled'}]
    api = SimpleNamespace(events=lambda:SimpleNamespace(list=lambda **kw:result({'items':items})))
    ctx = FillContext(session, clock, provider, None)
    first = sync(ctx, api, Settings(google_calendar_id='church'), namespaced=True)
    second = sync(ctx, api, Settings(google_calendar_id='church'), namespaced=True)
    assert first['protected'] == first['cancelled'] == 1 and second['cancelled'] == 0
    assert event.starts_at == original and free.status == 'cancelled'
    assert len(list(session.scalars(select(m.Escalation)))) == 2
    assert provider.sent == []


def test_restoring_cancelled_event_uses_successor_google_id(session, clock, make_shift):
    event = make_shift(starts=clock.now()+timedelta(days=2)).event
    api = PublishAPI(); connection = {}
    publish(session, api, connection, clock, lambda _:None)
    original = next(iter(api.remote))
    event.status = 'cancelled'
    publish(session, api, connection, clock, lambda _:None)
    # Google may retain a deletion tombstone under the original ID.
    api.remote[original] = {'id':original,'status':'cancelled'}
    event.status = 'scheduled'
    publish(session, api, connection, clock, lambda _:None)
    successor = connection['published_events'][str(event.id)]
    assert successor != original and api.remote[original]['status'] == 'cancelled'
    assert api.remote[successor]['summary'] == event.title

    del api.remote[successor]  # A manual Google deletion must also recover.
    publish(session, api, connection, clock, lambda _:None)
    assert connection['published_events'][str(event.id)] != successor
