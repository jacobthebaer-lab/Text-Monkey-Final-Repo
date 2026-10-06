"""Read-only fictional event proposals from unambiguous saved Sunday settings.

No database, people, consent, assignments, AI, transport or timer is accessed.
This deliberately narrow preview is not a general church calendar parser.
"""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo


def preview(details, sunday, duration_minutes, role_id):
    if not isinstance(details, dict):
        raise ValueError('Provide saved church details as a JSON object.')
    zone = ZoneInfo(details['timezone'])
    day = date.fromisoformat(sunday)
    if day.weekday() != 6:
        raise ValueError('Choose an explicit Sunday date for the saved Sunday services.')
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 720:
        raise ValueError('Provide an explicit service duration of 1 to 720 minutes.')
    if type(role_id) is not int or role_id <= 0:
        raise ValueError('Choose an existing role ID; this preview creates no roles.')
    saved = details.get('service_times')
    match = re.fullmatch(r'\s*Sundays?\s*:?\s*(.+?)\s*', saved or '', re.IGNORECASE)
    if not match:
        raise ValueError('Saved service times must explicitly name Sunday and AM/PM times.')
    tokens = re.split(r'\s*(?:/|,|&|\band\b)\s*', match[1], flags=re.IGNORECASE)
    if not 1 <= len(tokens) <= 4:
        raise ValueError('Preview one to four explicit Sunday service times.')
    times = []
    for token in tokens:
        value = re.fullmatch(r'(1[0-2]|[1-9])(?::([0-5][0-9]))?\s*(am|pm)', token, re.IGNORECASE)
        if not value:
            raise ValueError('Ambiguous service time. Use explicit times such as Sunday 9AM / 11AM.')
        hour = int(value[1]) % 12 + (12 if value[3].lower() == 'pm' else 0)
        times.append(time(hour, int(value[2] or 0)))
    if len(set(times)) != len(times):
        raise ValueError('Duplicate service times need correction before preview.')
    source = {'timezone': str(zone), 'service_times': saved, 'sunday': sunday,
              'duration_minutes': duration_minutes, 'role_id': role_id}
    events = []
    for service_time in sorted(times):
        start = datetime.combine(day, service_time, zone)
        # Never guess an offset for an ambiguous or nonexistent local time.
        if (start.utcoffset() != start.replace(fold=1).utcoffset() or
                start.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != start.replace(tzinfo=None)):
            raise ValueError('This local time crosses a clock-change ambiguity. Choose an explicit reviewed event instead.')
        end = (start.astimezone(timezone.utc) + timedelta(minutes=duration_minutes)).astimezone(zone)
        events.append({'title': f'Demo: Sunday service {service_time:%H:%M}',
            'event_type_id': None, 'gcal_event_id': None, 'status': 'scheduled',
            'starts_at': start.isoformat(), 'ends_at': end.isoformat(),
            'role_slots': [{'role_id': role_id, 'slot_index': 0}],
            'admin_update_window_opens_at': (start.astimezone(timezone.utc)-timedelta(hours=3)).astimezone(zone).isoformat(),
            'volunteer_reminder_local_date': (day-timedelta(days=1)).isoformat()})
    return {'fictional': True, 'read_only': True, 'state': 'requires_exact_record_review',
        'source': source, 'source_hash': hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
        'events': events, 'assignments': [], 'messages': [],
        'delivery_verified': False, 'scheduler_verified': False}
