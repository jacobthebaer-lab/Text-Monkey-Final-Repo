"""Validate Gloo-normalized recurring windows; never interpret reply text here.

Windows belong to one volunteer's preferences. They restrict eligibility only;
they cannot supply consent, frequency, qualifications or a booking.
"""
from copy import deepcopy
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import models as m


WINDOW_SCHEMA_INSTRUCTIONS = """
For recurring availability return recurring_windows, a complete merged snapshot
of the sender's role-specific weekday/time/event-context restrictions. Interpret
the reply using Gloo; never encode a time range or group name as preferred_services.
Each window has exactly these fields:
{"weekday":6,"role_ids":[catalogue_id],"role_label":"Greeter","any_role":false,
 "start_time":"08:00","end_time":"10:00","all_day":false,"event_context":null}
Monday=0, Sunday=6. Times are church-local HH:MM, not UTC. End may be 24:00;
start must be earlier than end. Retain an explicitly stated range exactly:
Sunday 8am to 10 means 08:00–10:00, never availability for a 10–11 event.
Do not invent an end time, role, serving frequency, clearance or consent.
Use null/null for unspecified times, all_day=false; all_day=true only when
explicitly stated, with null/null times. Mixed days/roles need separate windows.
For Wednesday coffee for the men's group use Coffee's known role ID and weekday
2, with event_context={"label":"men's group","event_type_ids":[catalogue_id]}.
Only map role/type IDs when supplied catalogue names/context justify that match.
If no known match exists, retain the user's role_label/context label with empty
IDs; these unresolved restrictions will be held, not broadened to any role/group.
event_context=null means no event/group restriction was stated. any_role=true
only for explicit flexibility across roles, with role_ids=[] and role_label=null.
Retain prior recurring_windows for frequency-only or unrelated followups. A
correction replaces only the corrected fact in the returned complete snapshot.
Omit recurring_windows when no window facts changed; [] clears prior windows
only when the sender explicitly removes them. Ordinary all-day/day-only answers
may use a window for explicitly known selected roles; never infer any_role.
""".strip()

WINDOW_FIELDS = {'weekday', 'role_ids', 'role_label', 'any_role', 'start_time',
                 'end_time', 'all_day', 'event_context'}


def _catalogue_ids(catalogue):
    return {item['id'] if isinstance(item, dict) else item.id for item in catalogue}


def _ids(value, known, label):
    if (not isinstance(value, list) or len(value) > 32 or
            not all(type(item) is int and item > 0 and item in known for item in value)):
        raise ValueError(f'Invalid {label} catalogue IDs')
    return list(dict.fromkeys(value))


def _label(value, *, optional=False):
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 120 or
            any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid recurring availability label')
    return value.strip()


def _minutes(value, *, end=False):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        if end and value == '24:00':
            return 1440
        raise ValueError('Time must be church-local HH:MM')
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


def normalize_recurring_windows(windows, roles, event_types=()):
    """Check Gloo's snapshot against catalogues, retaining unresolved labels.

No sender text is interpreted here. Invalid or ambiguous structures raise
ValueError for a targeted clarification; unknown labels do not grant access.
"""
    if not isinstance(windows, list) or len(windows) > 64:
        raise ValueError('Invalid recurring availability windows')
    role_ids, type_ids = _catalogue_ids(roles), _catalogue_ids(event_types)
    normalized = []
    for window in windows:
        if not isinstance(window, dict) or set(window) != WINDOW_FIELDS:
            raise ValueError('Incomplete recurring availability window')
        weekday = window['weekday']
        if type(weekday) is not int or not 0 <= weekday <= 6:
            raise ValueError('Invalid recurring weekday')
        if type(window['any_role']) is not bool or type(window['all_day']) is not bool:
            raise ValueError('Recurring availability flags must be boolean')
        ids = _ids(window['role_ids'], role_ids, 'role')
        label = _label(window['role_label'], optional=window['any_role'])
        if window['any_role'] and (ids or label is not None):
            raise ValueError('Any-role window cannot name a particular role')
        start, end = window['start_time'], window['end_time']
        if start is None and end is None:
            pass  # Unspecified hours remain distinct from explicit all day.
        elif window['all_day'] or start is None or end is None or _minutes(start) >= _minutes(end, end=True):
            raise ValueError('Invalid recurring local time interval')
        context = window['event_context']
        if context is not None:
            if not isinstance(context, dict) or set(context) != {'label', 'event_type_ids'}:
                raise ValueError('Invalid recurring event context')
            context = {'label': _label(context['label']),
                       'event_type_ids': _ids(context['event_type_ids'], type_ids, 'event type')}
        item = {'weekday': weekday, 'role_ids': ids, 'role_label': label,
                'any_role': window['any_role'], 'start_time': start, 'end_time': end,
                'all_day': window['all_day'], 'event_context': context}
        if item not in normalized:
            normalized.append(item)
    return normalized


def merge_recurring_windows(data, previous, roles, event_types=()):
    """Omission preserves prior facts; a supplied list is Gloo's corrected snapshot."""
    windows = data.get('recurring_windows', previous.get('recurring_windows', []))
    return normalize_recurring_windows(deepcopy(windows), roles, event_types)


def _covers(window, shift, start, end):
    if window['weekday'] != start.weekday():
        return False
    if not window['any_role'] and shift.role_id not in window['role_ids']:
        return False
    context = window['event_context']
    if context is not None and shift.event.event_type_id not in context['event_type_ids']:
        return False  # An unresolved context is never every event on that weekday.
    start_min = start.hour * 60 + start.minute + start.second / 60 + start.microsecond / 60000000
    end_min = end.hour * 60 + end.minute + end.second / 60 + end.microsecond / 60000000
    if end.date() != start.date():
        if (end.date() - start.date()).days != 1 or end_min != 0:
            return False
        end_min = 1440
    lower = 0 if window['start_time'] is None else _minutes(window['start_time'])
    upper = 1440 if window['end_time'] is None else _minutes(window['end_time'], end=True)
    return lower <= start_min < end_min <= upper


def recurring_window_reasons(session, preferences, shift, tz='America/Denver'):
    """Return a restrictive reason unless one window covers the whole event."""
    windows = preferences.get('recurring_windows', [])
    if windows == []:
        return []
    try:
        windows = normalize_recurring_windows(windows,
            session.scalars(select(m.Role)).all(), session.scalars(select(m.EventType)).all())
        start = shift.event.starts_at.astimezone(ZoneInfo(tz))
        end = shift.event.ends_at.astimezone(ZoneInfo(tz))
        if shift.event.ends_at <= shift.event.starts_at:
            return ['event has an invalid interval for recurring availability']
        if any(_covers(window, shift, start, end) for window in windows):
            return []
    except (ValueError, TypeError):
        return ['recurring availability needs valid role, context and time mappings']
    return ['outside role-specific recurring availability or unresolved event context']
