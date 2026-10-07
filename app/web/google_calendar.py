"""Google Calendar settings for authenticated coordinators and app-calendar publication."""
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, ConfigDict

from app.clock import RealClock
from app.agents.fill_agent import FillContext
from app.integrations import google_calendar_oauth as oauth
from app.integrations.gcal import sync
from app.web.texty import admin, bridge
from app.web.routes import db

class FinishConnection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    flow_id: str = Field(min_length=20, max_length=100)


class SelectCalendar(BaseModel):
    model_config = ConfigDict(extra='forbid')
    calendar_id: str = Field(min_length=1, max_length=1024)


def same_origin(request: Request):
    origin = request.headers.get('origin')
    if request.method != 'GET' and origin:
        site = urlsplit(request.app.state.settings.admin_site_url)
        allowed = {f'{site.scheme}://{site.netloc}', str(request.base_url).rstrip('/')}
        if origin not in allowed:
            raise HTTPException(403, 'Cross-origin calendar changes are blocked.')


router = APIRouter(prefix='/api/google-calendar', dependencies=[Depends(same_origin)])


def private(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'


def public_status(settings, connection):
    return {'configured': oauth.configured(settings), 'connected': bool(connection),
            'account_email': (connection or {}).get('account_email', ''),
            'calendar_id': (connection or {}).get('calendar_id', ''),
            'calendar_name': (connection or {}).get('calendar_name', ''),
            'last_sync_at': (connection or {}).get('last_sync_at'),
            'counts': (connection or {}).get('counts'),
            'publish_calendar_id': (connection or {}).get('publish_calendar_id', ''),
            'last_publish_at': (connection or {}).get('last_publish_at'),
            'publish_counts': (connection or {}).get('publish_counts')}


def connection_for(store, user):
    connection = store.read('account-' + oauth.owner(user))
    if not connection:
        raise HTTPException(409, 'Connect your Google account first.')
    return connection


def google_call(fn):
    from google.auth.exceptions import GoogleAuthError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error
    try:
        return fn()
    except (GoogleAuthError, HttpError, HttpLib2Error, OSError, ValueError, KeyError):
        raise HTTPException(502, 'Google Calendar is unavailable or access expired. Retry or reconnect your account.') from None


@router.get('')
def status(request: Request, response: Response, user=Depends(admin)):
    private(response)
    settings = request.app.state.settings
    store = oauth.Store(settings)
    with store.lock():
        return public_status(settings, store.read('account-' + oauth.owner(user)))


@router.post('/connect')
def connect(request: Request, response: Response, user=Depends(admin)):
    private(response)
    store = oauth.Store(request.app.state.settings)
    with store.lock():
        return oauth.start(store, request.app.state.settings, user)


@router.get('/callback')
def callback(request: Request, state: str = '', code: str = '', error: str = ''):
    # The public callback accepts no credentials from Google other than a
    # single-use state and code. Cloudflare forwards it through the usual bridge.
    request.scope['query_string'] = b''  # Do not log OAuth codes or state in backend access logs.
    bridge(request)
    settings = request.app.state.settings
    if not oauth.configured(settings):
        raise HTTPException(503, 'Google Calendar is not configured.')
    store = oauth.Store(settings)
    with store.lock():
        success = oauth.complete_callback(store, settings, state, code, denied=bool(error))
    return RedirectResponse(settings.admin_site_url.split('#')[0] + '#google-calendar=' + ('ready' if success else 'denied'),
        status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.post('/finish')
def finish(data: FinishConnection, request: Request, response: Response, user=Depends(admin)):
    private(response)
    store = oauth.Store(request.app.state.settings)
    with store.lock():
        oauth.finish(store, user, data.flow_id)
        return public_status(request.app.state.settings, connection_for(store, user))


@router.get('/calendars')
def calendars(request: Request, response: Response, user=Depends(admin)):
    private(response)
    store = oauth.Store(request.app.state.settings)
    with store.lock():
        connection = connection_for(store, user)
        return {'calendars': google_call(lambda: oauth.calendars(oauth.service(request.app.state.settings, connection)))}


@router.post('/select')
def select_calendar(data: SelectCalendar, request: Request, response: Response, user=Depends(admin)):
    private(response)
    store = oauth.Store(request.app.state.settings)
    with store.lock():
        connection = connection_for(store, user)
        rows = google_call(lambda: oauth.calendars(oauth.service(request.app.state.settings, connection)))
        if data.calendar_id == connection.get('publish_calendar_id'):
            raise HTTPException(422, 'Choose a source calendar other than the published Text Monkey calendar.')
        selected = next((row for row in rows if row['id'] == data.calendar_id), None)
        if not selected:
            raise HTTPException(422, 'Choose a calendar available to your connected account.')
        if connection['calendar_id'] != selected['id']:
            connection.update(last_sync_at=None, counts=None)
        connection.update(calendar_id=selected['id'], calendar_name=selected['name'], calendar_timezone=selected['timezone'])
        store.write('account-' + oauth.owner(user), connection)
        return public_status(request.app.state.settings, connection)


@router.post('/sync')
def import_events(request: Request, response: Response, user=Depends(admin), session=Depends(db)):
    private(response)
    settings = request.app.state.settings
    store = oauth.Store(settings)
    with store.lock():
        connection = connection_for(store, user)
        if not connection['calendar_id']:
            raise HTTPException(409, 'Choose your church calendar before importing events.')
        # Use current wall time, even when the message demo clock is pinned.
        ctx = FillContext(session, RealClock(settings.church_timezone), request.app.state.provider, request.app.state.gloo)
        source_key = 'source-' + oauth.digest(connection['calendar_id'])
        tracked_events = store.read(source_key) or {}
        counts = google_call(lambda: sync(ctx, service=oauth.service(settings, connection),
            settings=replace(settings, google_calendar_id=connection['calendar_id']), namespaced=True,
            calendar_timezone=connection.get('calendar_timezone'), tracked_events=tracked_events))
        session.commit()  # Receipts are saved only after the import transaction succeeds.
        store.write(source_key, tracked_events)
        connection.update(last_sync_at=datetime.now(timezone.utc).isoformat(), counts=counts)
        store.write('account-' + oauth.owner(user), connection)
        return public_status(settings, connection)


@router.post('/disconnect')
def disconnect(request: Request, response: Response, user=Depends(admin)):
    private(response)
    store = oauth.Store(request.app.state.settings)
    with store.lock():
        # Forget only this admin's authorization. Imported shared events remain.
        store.delete('account-' + oauth.owner(user))
        store.clear_flows(oauth.owner(user))
    return public_status(request.app.state.settings, None)


@router.post('/publish')
def publish_events(request: Request, response: Response, user=Depends(admin), session=Depends(db)):
    private(response)
    from app.integrations.google_calendar_publish import publish
    settings = request.app.state.settings
    store = oauth.Store(settings)
    with store.lock():
        connection = connection_for(store, user)
        save = lambda value: store.write('account-' + oauth.owner(user), value)
        counts = google_call(lambda: publish(session, oauth.service(settings, connection), connection,
                                            RealClock(settings.church_timezone), save))
        connection.update(last_publish_at=datetime.now(timezone.utc).isoformat(), publish_counts=counts)
        save(connection)
        return public_status(settings, connection)
