"""One human-authorized test exception, expiring independently of any worker."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.db import models as m
from app.integrations.google_voice_demo import sender_fingerprint
from app.integrations.google_voice_policy import google_voice_demo_allowed

KEY = 'google_voice:one_time_quiet_test'
EXPIRES_AT = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
SIGNUP_PURPOSES = ['confirmation', 'reminder', 'signup_reply']
ADMIN_PURPOSES = ['admin_reply', 'coordinator_notify', 'escalation_notify']


def spec_value(spec):
    return {'id': spec.id, 'starts_at': spec.starts_at.isoformat(),
            'expires_at': spec.expires_at.isoformat(), 'continuous': spec.continuous}


def grant(state, session, actor, signup_phone, admin_phone, now):
    provider = state.provider
    phones = [signup_phone, admin_phone]
    if (not google_voice_demo_allowed(state.settings) or provider.transport_name != 'google_voice' or
            len(set(phones)) != 2 or now >= EXPIRES_AT or
            any(not isinstance(phone, str) or not provider.allows(phone) or
                not provider.test_sessions.get(phone) or not provider.test_sessions[phone].active(now) for phone in phones)):
        raise HTTPException(409, 'The one-time test needs its two exact configured cloud sessions before tonight’s deadline.')
    if session.get(m.Policy, KEY):
        raise HTTPException(409, 'This one-time exception was already recorded. It cannot renew or extend itself.')
    value = {'id': uuid4().hex, 'actor': actor, 'at': now.isoformat(), 'expires_at': EXPIRES_AT.isoformat(),
        'authority': 'Authenticated operator attests Jacob authorized this one-time test exception',
        'sender_fingerprint': sender_fingerprint(state.settings),
        'sessions': {phone: spec_value(provider.test_sessions[phone]) for phone in phones},
        'purposes': {signup_phone: SIGNUP_PURPOSES, admin_phone: ADMIN_PURPOSES}}
    session.add(m.Policy(key=KEY, value=value))
    session.add(m.Notification(key='google-quiet-test:' + value['id'], purpose='human_review', state='authorized',
        body='', created_at=now, due_at=now, expires_at=EXPIRES_AT, detail=value))
    session.flush()
    return {'recorded': True, 'expires_at': EXPIRES_AT.isoformat(), 'quiet_hours_changed': False}


def deadline(session, provider, phone, purpose, now, *, approval=None, source=None):
    """Return only the quiet exception boundary. No other delivery authority."""
    if getattr(provider, 'transport_name', None) != 'google_voice' or not google_voice_demo_allowed(provider.settings):
        return None
    row = session.get(m.Policy, KEY)
    value = row.value if row else None
    if not isinstance(value, dict):
        return None
    try:
        start = datetime.fromisoformat(value['at'])
        expires = datetime.fromisoformat(value['expires_at'])
        audit = session.get(m.Notification, 'google-quiet-test:' + value['id'])
        scopes = value['sessions']
        purposes = value['purposes']
        selected = provider.test_sessions.get(phone)
        allowed = {email.strip().lower() for email in provider.settings.superadmin_email_allowlist.split(',') if email.strip()}
        if (not isinstance(value['actor'], str) or value['actor'].lower() not in allowed or
                value['authority'] != 'Authenticated operator attests Jacob authorized this one-time test exception' or
                start.tzinfo is None or expires != EXPIRES_AT or not start <= now < expires or
                not audit or audit.state != 'authorized' or audit.detail != value or audit.expires_at != expires or
                audit.created_at != start or audit.purpose != 'human_review' or
                value['sender_fingerprint'] != sender_fingerprint(provider.settings) or
                not isinstance(scopes, dict) or len(scopes) != 2 or set(scopes) != set(purposes) or
                sorted(purposes.values()) != sorted([SIGNUP_PURPOSES, ADMIN_PURPOSES]) or
                purpose not in purposes.get(phone, []) or not provider.allows(phone) or not selected or
                not selected.active(now) or scopes.get(phone) != spec_value(selected)):
            return None
        # A test-event exception must reference the already authorized private
        # acceptance assignment, never another event merely sharing a purpose.
        if purpose in {'reminder', 'confirmation'}:
            if source is None and approval:
                job = session.get(m.Policy, approval.payload.get('workflow_job_key')) if approval.payload.get('workflow_job_key') else None
                source = job.value.get('source') if job else None
            if not isinstance(source, dict) or source.get('type') != 'assignment' or source.get('purpose') != purpose:
                return None
            assignment = session.get(m.Assignment, source.get('assignment_id'))
            if (not assignment or assignment.volunteer.phone != phone or assignment.shift_id != source.get('shift_id') or
                    assignment.shift.event_id != source.get('event_id') or
                    not any(saved.value.get('assignment_id') == assignment.id and
                        saved.value.get('event_id') == assignment.shift.event_id and saved.value.get('shift_id') == assignment.shift_id
                        for saved in session.scalars(select(m.Policy).where(m.Policy.key.startswith('acceptance:workflow:'))))):
                return None
        return expires
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
