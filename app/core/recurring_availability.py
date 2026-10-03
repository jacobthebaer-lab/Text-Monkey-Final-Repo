"""Validate Gloo-normalized recurring windows; never interpret reply text here.

Windows belong to one volunteer's preferences. They restrict eligibility only;
they cannot supply consent, frequency, qualifications or a booking.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.db import models as m


WINDOW_SCHEMA_INSTRUCTIONS = """
For recurring availability return recurring_windows, a complete merged snapshot
of the sender's role-specific weekday/time/event-context restrictions. Interpret
the reply using Gloo; never encode a time range or group name as preferred_services.
Each newly returned window must include these fields, including time_mode:
{"weekday":6,"role_ids":[catalogue_id],"role_label":"Greeter","any_role":false,
 "time_mode":"clock","start_time":"08:00","end_time":"10:00","all_day":false,"event_context":null}
Monday=0, Sunday=6. Times are church-local HH:MM, not UTC. End may be 24:00;
start must be earlier than end. Retain an explicitly stated range exactly:
Sunday 8am to 10 means 08:00–10:00, never availability for a 10–11 event.
Do not invent an end time, role, serving frequency, clearance or consent.
Use null/null for unspecified times, all_day=false; these hours remain unknown
and held for scheduling, even after a frequency answer. all_day=true only when
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
Set time_mode="event" ONLY for explicit willingness to follow a named
group's event schedule (for example "coffee whenever the men's group meets").
Use a named role, any_role=false, event_context with that named group, null/null
times and all_day=false. Retain unknown group IDs as []; they remain ineligible
until mapped. This is not unknown numeric hours or availability for every event.
Declare time_mode="clock" for numeric or unknown clock hours, and
time_mode="event" for explicit named-group schedule following. Do not omit the
mode in new output. A known catalogue group still requires event mode when the
sender follows its schedule. Historical saved windows can omit the mode; those
are clock windows. Do not infer event mode from a mere group mention. Preserve
other role windows and date exclusions.
Return role_frequency_caps as a merged list of
{"role_id":catalogue_id,"role_name":"exact catalogue name","max_per_month":2}
ONLY for explicitly role-scoped frequency. Greeting twice a month caps Greeting,
not Coffee or all serving. Do not put this value in global max_per_month;
global frequency stays unknown/null unless separately supplied. A frequency-only
followup preserves event-mode/windows/exclusions and untouched role caps. Omit
role_frequency_caps if unchanged; [] clears caps only on an explicit correction.
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
        if not isinstance(window, dict) or set(window) not in (WINDOW_FIELDS, WINDOW_FIELDS | {'time_mode'}):
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
        mode = window.get('time_mode', 'clock')
        if type(mode) is not str or mode not in {'clock', 'event'}:
            raise ValueError('Invalid recurring time mode')
        if mode == 'event' and (window['any_role'] or len(ids) > 1 or context is None or
                                window['all_day'] or start is not None or end is not None):
            raise ValueError('Event-follow availability requires a named role and group, without clock hours')
        item = {'weekday': weekday, 'role_ids': ids, 'role_label': label,
                'any_role': window['any_role'], 'start_time': start, 'end_time': end,
                'all_day': window['all_day'], 'event_context': context}
        if 'time_mode' in window:
            item['time_mode'] = mode
        if item not in normalized:
            normalized.append(item)
    return normalized


def merge_recurring_windows(data, previous, roles, event_types=()):
    """Omission preserves prior facts; a supplied list is Gloo's corrected snapshot."""
    windows = data.get('recurring_windows', previous.get('recurring_windows', []))
    return normalize_recurring_windows(deepcopy(windows), roles, event_types)


def normalize_role_frequency_caps(caps, roles):
    """Role-specific caps cannot silently become a global serving frequency."""
    if not isinstance(caps, list) or len(caps) > 32:
        raise ValueError('Invalid role frequency caps')
    catalogue = {r['id'] if isinstance(r, dict) else r.id:
                 r['name'] if isinstance(r, dict) else r.name for r in roles}
    result, seen = [], set()
    for cap in caps:
        if not isinstance(cap, dict) or set(cap) != {'role_id', 'role_name', 'max_per_month'}:
            raise ValueError('Incomplete role frequency cap')
        rid, name, maximum = cap['role_id'], cap['role_name'], cap['max_per_month']
        if (type(rid) is not int or rid not in catalogue or rid in seen or
                type(name) is not str or name != catalogue[rid] or
                type(maximum) is not int or not 1 <= maximum <= 8):
            raise ValueError('Invalid role frequency mapping or limit')
        result.append({'role_id': rid, 'role_name': name, 'max_per_month': maximum})
        seen.add(rid)
    return result


def merge_role_frequency_caps(data, previous, roles):
    return normalize_role_frequency_caps(deepcopy(data.get('role_frequency_caps',
                                         previous.get('role_frequency_caps', []))), roles)


def global_frequency_limit(preferences, legacy_default=3):
    """No invented global cap for role-cap data; old profiles retain their default.

Ranking/planning consumers must check for None before comparing an all-role
count. This helper does not reinterpret or remove a supplied global cap.
"""
    if 'max_per_month' not in preferences:
        return None if 'role_frequency_caps' in preferences else legacy_default
    limit = preferences['max_per_month']
    if limit is None and 'role_frequency_caps' in preferences:
        return None
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('Invalid global serving frequency')
    return limit


def role_frequency_reasons(session, volunteer, shift, tz='America/Denver', exclude_assignment_id=None):
    preferences = volunteer.preferences or {}
    if 'role_frequency_caps' not in preferences:
        return []
    try:
        caps = normalize_role_frequency_caps(preferences['role_frequency_caps'],
                                             session.scalars(select(m.Role)).all())
    except (ValueError, TypeError):
        return ['role-specific frequency needs a valid role mapping and limit']
    cap = next((c for c in caps if c['role_id'] == shift.role_id), None)
    if cap is None:
        return []
    local = shift.event.starts_at.astimezone(ZoneInfo(tz))
    start = datetime(local.year, local.month, 1, tzinfo=ZoneInfo(tz))
    end = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=ZoneInfo(tz))
    count = session.scalar(select(func.count(m.Assignment.id)).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id == volunteer.id, m.Shift.role_id == shift.role_id,
        m.Assignment.status.in_(('proposed', 'approved', 'confirmed', 'completed')),
        m.Assignment.id != exclude_assignment_id if exclude_assignment_id is not None else True,
        m.Event.starts_at >= start, m.Event.starts_at < end))
    if count >= cap['max_per_month']:
        return [f"role-specific monthly maximum reached: {cap['role_name']}"]
    return []


def _covers(window, shift, start, end):
    if window['weekday'] != start.weekday():
        return False
    if not window['any_role'] and shift.role_id not in window['role_ids']:
        return False
    context = window['event_context']
    if context is not None and shift.event.event_type_id not in context['event_type_ids']:
        return False  # An unresolved context is never every event on that weekday.
    if window.get('time_mode') == 'event':
        return True  # Exact matching mapped event supplies its interval, not all-day hours.
    if not window['all_day'] and window['start_time'] is None:
        return False  # A resolved group type does not supply missing serving hours.
    start_min = start.hour * 60 + start.minute + start.second / 60 + start.microsecond / 60000000
    end_min = end.hour * 60 + end.minute + end.second / 60 + end.microsecond / 60000000
    if end.date() != start.date():
        if (end.date() - start.date()).days != 1 or end_min != 0:
            return False
        end_min = 1440
    if window['all_day']:
        return True  # Actual elapsed order is checked in UTC, including a repeated hour.
    if start.utcoffset() != end.utcoffset():
        return False  # Local endpoints cannot prove coverage through a clock change.
    lower = _minutes(window['start_time'])
    upper = _minutes(window['end_time'], end=True)
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
        if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
            return ['event has an invalid interval for recurring availability']
        if any(_covers(window, shift, start, end) for window in windows):
            return []
    except (ValueError, TypeError):
        return ['recurring availability needs valid role, context and time mappings']
    return ['outside role-specific recurring availability or unresolved hours/event context']
