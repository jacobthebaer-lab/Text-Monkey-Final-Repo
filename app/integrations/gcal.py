"""Read-only Calendar import. Never creates, updates or deletes Google events."""
import hashlib
import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.db import models as m
from app.config import get_settings

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
ACTIVE = {'proposed', 'approved', 'confirmed'}


def client(settings):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    if not settings.google_service_account_json or not settings.google_calendar_id:
        raise ValueError('Calendar ID and service-account JSON file path are required')
    credentials = Credentials.from_service_account_file(settings.google_service_account_json, scopes=SCOPES)
    return build('calendar', 'v3', credentials=credentials, cache_discovery=False)


def event_time(value, tz):
    if 'dateTime' in value:
        dt = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(value.get('timeZone', tz)))
    return datetime.combine(datetime.fromisoformat(value['date']).date(), time(), ZoneInfo(tz))


def source_key(calendar_id, event_id):
    # Google event IDs are only unique within a calendar. Same-calendar imports
    # by different coordinators share one event in the church's schedule.
    return 'gcal:' + hashlib.sha256((calendar_id + '\0' + event_id).encode()).hexdigest()


def sync(ctx, service=None, settings=None, *, namespaced=False, calendar_timezone=None):
    settings = settings or get_settings()
    service = service or client(settings)
    session = ctx.session
    token = None
    counts = {'created': 0, 'updated': 0, 'unknown': 0, 'cancelled': 0, 'protected': 0, 'skipped': 0}
    types = list(session.scalars(select(m.EventType)))

    def flag(event, category, summary):
        if not any(e.related_ids.get('event_id') == event.id and e.summary == summary
                   for e in session.scalars(select(m.Escalation).where(
                       m.Escalation.category == category, m.Escalation.status == 'open'))):
            session.add(m.Escalation(category=category, summary=summary,
                related_ids={'event_id': event.id, 'gcal_event_id': event.gcal_event_id},
                severity='normal', status='open', created_at=ctx.clock.now()))

    while True:
        response = service.events().list(calendarId=settings.google_calendar_id,
            timeMin=ctx.clock.now().isoformat(), timeMax=(ctx.clock.now() + timedelta(weeks=8)).isoformat(),
            singleEvents=True, showDeleted=True, orderBy='startTime', pageToken=token).execute()
        for item in response.get('items', []):
            if not item.get('id'):
                counts['skipped'] += 1
                continue
            key = source_key(settings.google_calendar_id, item['id']) if namespaced else item['id']
            event = session.scalar(select(m.Event).where(m.Event.gcal_event_id == key))
            assigned = event and any(a.status in ACTIVE for sh in event.shifts for a in sh.assignments)
            if item.get('status') == 'cancelled':
                if event:
                    if assigned:
                        counts['protected'] += 1
                        flag(event, 'unclear', f'Calendar cancelled {event.title}; assigned event protected pending human review.')
                    elif event.status != 'cancelled':
                        event.status = 'cancelled'
                        counts['cancelled'] += 1
                        flag(event, 'unclear', f'Calendar cancelled {event.title}; coordinator reviews assignments.')
                continue
            # All-day holidays and personal availability are not timed services.
            if namespaced and ('dateTime' not in item.get('start', {}) or item.get('eventType', 'default') != 'default'):
                counts['skipped'] += 1
                continue
            try:
                tz = calendar_timezone or settings.church_timezone
                start = event_time(item['start'], tz)
                end = event_time(item['end'], tz)
            except (KeyError, ValueError):
                counts['skipped'] += 1
                continue
            if end <= start:
                counts['skipped'] += 1
                continue
            title = (item.get('summary') or 'Untitled event')[:200]
            event_type = next((t for t in types if any(re.search(pattern, title, re.I) for pattern in t.title_patterns)), None)
            type_id = event_type.id if event_type else None
            if event:
                changed = event.starts_at != start or event.ends_at != end or event.event_type_id != type_id or event.status != 'scheduled'
                # Preserve historical assignments when a recipe changes, too.
                history = event.event_type_id != type_id and any(sh.assignments for sh in event.shifts)
                if (changed and assigned) or history:
                    counts['protected'] += 1
                    flag(event, 'unclear', f'Calendar changed {title}; assigned event protected pending human review.')
                    continue
                if event.event_type_id != type_id:
                    for shift in list(event.shifts):
                        session.delete(shift)
                    session.flush()
                    session.expire(event, ['shifts'])
                event.title, event.starts_at, event.ends_at = title, start, end
                event.event_type_id, event.status = type_id, 'scheduled'
                counts['updated'] += 1
            else:
                event = m.Event(gcal_event_id=key, title=title, starts_at=start, ends_at=end,
                                event_type_id=type_id, status='scheduled')
                session.add(event)
                session.flush()
                counts['created'] += 1
            if event_type:
                for recipe in session.scalars(select(m.RoleRecipe).where(m.RoleRecipe.event_type_id == type_id)):
                    existing = {sh.slot_index for sh in event.shifts if sh.role_id == recipe.role_id}
                    for slot in range(recipe.count):
                        if slot not in existing:
                            session.add(m.Shift(event_id=event.id, role_id=recipe.role_id, slot_index=slot))
            else:
                flag(event, 'unknown_event', f'What staffing does {title} need? Coordinator must choose a recipe.')
                counts['unknown'] += 1
            session.flush()
        token = response.get('nextPageToken')
        if not token:
            break
    return counts
