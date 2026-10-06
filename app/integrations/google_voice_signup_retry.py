"""Bounded read-only connection recovery under an unchanged signup grant."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import models as m
from app.integrations.google_voice_demo import scope_fingerprint, sender_fingerprint
from app.integrations.google_voice_runtime import _clock, is_paused

WAITING = 'waiting_connection'
KEY = 'google_voice:signup_authorization'
LIMIT = 5
WINDOW = timedelta(minutes=10)


def binding(state, value):
    return {'authorization_id': value.get('id'),
        'sender_fingerprint': sender_fingerprint(state.settings),
        'scope_fingerprint': scope_fingerprint(state.provider.test_sessions)}


def current(session, state, value, owner):
    return bool(value and value.get('enabled') is True and value.get('state') in {'enabled', WAITING}
        and value.get('sender_fingerprint') == owner['sender_fingerprint']
        and binding(state, value) == owner and not is_paused(session))


def retry_valid(retry, owner, now):
    try:
        first, until, next_at = (datetime.fromisoformat(retry[k]) for k in ('first_at', 'until', 'next_at'))
        return (all(retry.get(k) == v for k, v in owner.items())
            and retry['reason_code'] in {'connector_starting', 'connector_connection_unavailable'}
            and type(retry['attempts']) is int and 1 <= retry['attempts'] <= LIMIT
            and all(t.tzinfo is not None for t in (first, until, next_at))
            and until - first == WINDOW and first <= next_at <= until and first <= now < until)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def eligibility(session, state, owner):
    """False means fail closed; None means backoff, True permits a probe."""
    row = session.get(m.Policy, KEY)
    value = row.value if row else {}
    now = _clock(state).now()
    if not current(session, state, value, owner):
        return False
    if value['state'] == 'enabled':
        return True
    retry = value.get('connection_retry')
    if not retry_valid(retry, owner, now):
        return False
    return True if now >= datetime.fromisoformat(retry['next_at']) else None


def wait(state, owner, reason_code):
    now = _clock(state).now()
    with state.session_factory() as session:
        row = session.scalar(select(m.Policy).where(m.Policy.key == KEY).with_for_update())
        value = row.value if row else {}
        if not current(session, state, value, owner):
            return False
        previous = value.get('connection_retry') if value['state'] == WAITING else None
        if value['state'] == WAITING and not retry_valid(previous, owner, now):
            return False
        attempts = previous['attempts'] + 1 if previous else 1
        if attempts > LIMIT:
            return False
        first = datetime.fromisoformat(previous['first_at']) if previous else now
        until = first + WINDOW
        next_at = min(now + timedelta(seconds=15 * 2 ** (attempts - 1)), until)
        row.value = {**value, 'state': WAITING,
            'reason': 'Cloud connection is starting. Authorized signup waits for verified readiness.',
            'connection_retry': {**owner, 'reason_code': reason_code, 'attempts': attempts,
                'first_at': first.isoformat(), 'until': until.isoformat(), 'next_at': next_at.isoformat()}}
        session.commit()
    state.google_voice_status = {}
    return True


def complete(state, owner):
    with state.session_factory() as session:
        if eligibility(session, state, owner) is not True:
            return False
        row = session.get(m.Policy, KEY)
        row.value = {k: v for k, v in {**row.value, 'state': 'enabled', 'reason': ''}.items()
                     if k != 'connection_retry'}
        session.commit()
    return True


def constructor_health(health, state):
    """Only the exact fresh-connector state, never generic UI/auth failures."""
    return bool(isinstance(health, dict) and health.get('demo_mode') is True
        and health.get('scope_fingerprint') == scope_fingerprint(state.provider.test_sessions)
        and health.get('state') == 'reconnect_required' and health.get('reason_code') == 'session_not_verified'
        and health.get('ready') is False and health.get('identity_verified') is False
        and health.get('expected_identity_match') is False and health.get('identity_fingerprint') is None)
