"""Publish a church schedule only to the app-created Text Monkey calendar.

No attendees, invitations, text delivery or contact details are exported.
Deterministic Google IDs make a retry after a partial failure idempotent.
"""
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from googleapiclient.errors import HttpError
from app.db import models as m


def publish(session, api, connection, clock, save):
    if not connection.get('publish_calendar_id'):
        created = api.calendars().insert(body={
            'summary': 'Text Monkey', 'timeZone': str(clock.now().tzinfo),
            'description': 'Church event schedule and staffing coverage published by Text Monkey.',
        }).execute()
        connection.update(publish_calendar_id=created['id'], publish_namespace=secrets.token_hex(16), published_events={})
        save(connection)
    calendar_id = connection['publish_calendar_id']
    previous = connection.get('published_events', {})
    versions = connection.setdefault('published_versions', {})

    def remote_id(key):
        source = f"{connection['publish_namespace']}:{key}:{versions.get(key, 0)}"
        return 'tm' + hashlib.sha256(source.encode()).hexdigest()
    # Keep historical Google events while bounding the progress map.
    rows = session.scalars(select(m.Event).where(or_(
        (m.Event.starts_at >= clock.now()) & (m.Event.starts_at < clock.now() + timedelta(weeks=8)),
        m.Event.id.in_([int(i) for i in previous]),
    )).options(selectinload(m.Event.shifts).selectinload(m.Shift.assignments))).all()
    counts = {'published': 0, 'removed': 0}
    current = {str(event.id): event for event in rows}
    # Missing or cancelled local events remove only known app publications.
    for local_id, google_id in list(previous.items()):
        event = current.get(local_id)
        if event and event.status != 'cancelled' and event.ends_at < clock.now():
            del previous[local_id]
            connection['published_events'] = previous
            save(connection)
            continue
        if event is None or event.status == 'cancelled':
            try:
                api.events().delete(calendarId=calendar_id, eventId=google_id, sendUpdates='none').execute()
            except HttpError as exc:
                if exc.resp.status not in (404, 410):
                    raise
            del previous[local_id]
            versions[local_id] = versions.get(local_id, 0) + 1
            counts['removed'] += 1
            connection['published_events'] = previous
            save(connection)
    for event in rows:
        if event.status != 'scheduled' or event.ends_at < clock.now():
            continue
        # Existing publications are updated if their time moved out of the
        # window. New events outside the upcoming window are not exported.
        key = str(event.id)
        if key not in previous and not clock.now() <= event.starts_at < clock.now() + timedelta(weeks=8):
            continue
        google_id = remote_id(key)
        filled = sum(any(a.status in {'approved', 'confirmed'} for a in sh.assignments) for sh in event.shifts)
        body = {'summary': event.title, 'start': {'dateTime': event.starts_at.isoformat()},
                'end': {'dateTime': event.ends_at.isoformat()},
                'description': f'Text Monkey staffing: {filled} of {len(event.shifts)} positions covered. Review assignments in the admin console.'}
        try:
            remote = api.events().get(calendarId=calendar_id, eventId=google_id).execute()
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            if key in previous:
                versions[key] = versions.get(key, 0) + 1
                save(connection)
                google_id = remote_id(key)
            try:
                api.events().insert(calendarId=calendar_id, body={'id': google_id, **body}, sendUpdates='none').execute()
            except HttpError as insert_error:
                if insert_error.resp.status != 409:
                    raise
                api.events().patch(calendarId=calendar_id, eventId=google_id, body=body, sendUpdates='none').execute()
        else:
            if remote.get('status') == 'cancelled':
                # Google keeps deletion tombstones. Use a saved successor ID
                # rather than trying to reuse a deleted remote event ID.
                versions[key] = versions.get(key, 0) + 1
                save(connection)
                google_id = remote_id(key)
                api.events().insert(calendarId=calendar_id, body={'id': google_id, **body}, sendUpdates='none').execute()
            else:
                api.events().patch(calendarId=calendar_id, eventId=google_id, body=body, sendUpdates='none').execute()
        previous[key] = google_id
        connection['published_events'] = previous
        save(connection)
        counts['published'] += 1
    return counts
