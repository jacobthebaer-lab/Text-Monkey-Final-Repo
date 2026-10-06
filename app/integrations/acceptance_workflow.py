"""One private participant, one reviewed fictional event, one exact reminder.

No scope file and no armed job means no background work. This does not release
provider policy, enable signup, approve messages, or run the general scheduler.
"""
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from app.clock import RealClock
from app.core import confirmations, reminders
from app.core.policies import PolicyStore, in_quiet_hours
from app.db import models as m
from app.integrations.google_voice_client import connector_for, verified_health
from app.integrations.google_voice_demo import restore_demo_scope
from app.integrations.google_voice_policy import google_voice_demo_allowed
from app.integrations.google_voice_runtime import _clock, _tick_lock, dispatch_outbound, is_paused, poll_inbound
from app.integrations.google_voice_signup import unresolved_google_voice_submissions

ENV = 'TEXT_MONKEY_ACCEPTANCE_SCOPE_FILE'
KEY = 'acceptance:workflow:'


def private_scope(state, actor=None):
    path = os.environ.get(ENV)
    if not path:
        raise HTTPException(404, 'The private event test is not configured.')
    try:
        # Open without following a symlink, then validate the actual open inode.
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or
                    metadata.st_uid != os.geteuid() or metadata.st_size > 4096):
                raise ValueError('private file required')
            scope = json.load(handle)
        if (not isinstance(scope, dict) or set(scope) != {'phone', 'admin_email', 'sender_email', 'sender_number', 'timer_available'} or
                not all(isinstance(scope[k], str) for k in ('phone', 'admin_email', 'sender_email', 'sender_number')) or
                not re.fullmatch(r'\+[1-9][0-9]{7,14}', scope['phone']) or
                not isinstance(scope['timer_available'], bool) or not scope['admin_email'] or
                scope['sender_email'] != state.settings.google_voice_expected_email or
                scope['sender_number'] != state.settings.google_voice_expected_number):
            raise ValueError('scope mismatch')
    except (OSError, ValueError, TypeError):
        raise HTTPException(409, 'The private event test configuration needs attention.') from None
    if actor is not None and actor.lower() != scope['admin_email'].lower():
        raise HTTPException(403, 'This account is outside the private event test.')
    if not google_voice_demo_allowed(state.settings):
        raise HTTPException(409, 'The private event test is unavailable in this transport mode.')
    restore_demo_scope(state)
    # Prevent even inbox processing from touching another registered conversation.
    if set(state.provider.phones) != {scope['phone']}:
        raise HTTPException(409, 'The event test requires exactly its one private participant.')
    scope['key'] = KEY + hashlib.sha256(json.dumps({k: scope[k] for k in ('phone', 'admin_email', 'sender_email', 'sender_number')}, sort_keys=True).encode()).hexdigest()
    return scope


def saved(session, scope):
    row = session.get(m.Policy, scope['key'])
    return dict(row.value) if row else {}


def save(session, scope, value):
    row = session.get(m.Policy, scope['key'])
    if row:
        row.value = dict(value)
    else:
        session.add(m.Policy(key=scope['key'], value=dict(value)))
    session.flush()


def target(session, scope):
    person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == scope['phone']))
    if person is None:
        raise HTTPException(409, 'Finish the participant signup before creating an assignment.')
    return person


def assignment(session, scope, value, event_id, assignment_id):
    if (type(event_id) is not int or type(assignment_id) is not int or
            value.get('event_id') != event_id or value.get('assignment_id') != assignment_id):
        raise HTTPException(409, 'Select the saved event and assignment from this test.')
    row = session.get(m.Assignment, assignment_id, populate_existing=True)
    if (not row or row.volunteer_id != target(session, scope).id or row.shift.event_id != event_id or
            row.shift_id != value.get('shift_id') or not row.shift.event.title.startswith('Demo: ')):
        raise HTTPException(409, 'The saved test assignment changed.')
    return row


def reminder_message(session, state, scope, value, event_id, assignment_id, message_id, body_hash):
    row = assignment(session, scope, value, event_id, assignment_id)
    receipt = session.get(m.Policy, f'job:reminder:{row.id}')
    approval = session.get(m.Approval, receipt.value.get('approval_id')) if receipt else None
    message = session.get(m.Message, message_id) if type(message_id) is int else None
    if (not message or not approval or approval.payload.get('message_id') != message.id or
            message.phone != scope['phone'] or message.volunteer_id != row.volunteer_id or
            message.purpose != 'reminder' or message.direction != 'out' or message.status != 'queued' or
            not message.provider_sid.startswith('GV') or confirmations.proof_for(session, message) != approval or
            hashlib.sha256(message.body.encode()).hexdigest() != body_hash):
        raise HTTPException(409, 'Select the exact reviewed reminder. Processed messages cannot be retried.')
    if problem := confirmations.delivery_problem(session, state.provider, approval, _clock(state).now(), message):
        raise HTTPException(409, problem)
    return message, approval


def snapshot(state, actor):
    scope = private_scope(state, actor)
    with state.session_factory() as session:
        value = saved(session, scope)
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == scope['phone']))
        event = session.get(m.Event, value.get('event_id')) if value.get('event_id') else None
        reviews = []
        ids = [value.get(k) for k in ('event_review', 'shift_review', 'assignment_review')]
        receipt = session.get(m.Policy, f"job:reminder:{value['assignment_id']}") if value.get('assignment_id') else None
        if receipt:
            ids.append(receipt.value.get('approval_id'))
        message = None
        for review_id in ids:
            a = session.get(m.Approval, review_id) if review_id else None
            if a:
                reviews.append({'id': a.id, 'status': a.status, 'kind': a.kind,
                    'content_hash': a.payload['content_hash'], 'reason': a.payload.get('reason'),
                    'after': a.payload.get('after'), 'body': a.payload.get('body'),
                    'expires_at': a.payload.get('expires_at')})
                if a.payload.get('message_id'):
                    row = session.get(m.Message, a.payload['message_id'])
                    if row:
                        message = {'id': row.id, 'body': row.body, 'status': row.status,
                            'body_hash': hashlib.sha256(row.body.encode()).hexdigest()}
        return {'participant': person.name if person else 'Signup not completed',
            'zone': str(PolicyStore(session).church_tz()),
            'roles': [{'id': r.id, 'name': r.name} for r in session.scalars(select(m.Role).order_by(m.Role.name))],
            'event': {'id': event.id, 'title': event.title, 'starts_at': event.starts_at.isoformat(),
                'ends_at': event.ends_at.isoformat()} if event else None,
            'assignment_id': value.get('assignment_id'), 'reviews': reviews, 'message': message,
            'reminder_state': receipt.value.get('state') if receipt else None,
            'timer_available': scope['timer_available'], 'timer': value.get('timer', {'enabled': False}),
            'delivery_verified': False}


def fresh_intake(state, scope):
    if unresolved_google_voice_submissions(state):
        raise HTTPException(409, 'A Google Voice submission has an unknown outcome. Review it before continuing.')
    with state.session_factory() as session:
        if is_paused(session):
            raise HTTPException(409, 'Cloud texting is paused.')
    connector = connector_for(state)
    health = connector.intake(phone=scope['phone'])
    connected = verified_health(health, state.settings, state.provider)
    state.google_voice_status = {'connected': connected, 'checked_monotonic': time.monotonic(),
                                'last_checked_at': _clock(state).now().isoformat()}
    if not connected:
        raise HTTPException(409, 'The cloud account or participant inbox could not be verified.')
    poll_inbound(state, connector)  # applies fresh STOP/cancellation before reminder checks
    if unresolved_google_voice_submissions(state):
        raise HTTPException(409, 'A Google Voice submission needs delivery review.')
    return connector


def dispatch_exact(state, scope, data):
    if not state.settings.live_sms or not state.settings.gloo_api_key:
        raise HTTPException(409, 'Live texting and Gloo must be ready.')
    connector = fresh_intake(state, scope)
    with state.session_factory() as session:
        reminder_message(session, state, scope, saved(session, scope), **data)
    dispatch_outbound(state, connector, message_id=data['message_id'], expected_body_hash=data['body_hash'])


def stop_service(state):
    worker = getattr(state, 'acceptance_scheduler', None)
    state.acceptance_scheduler = None
    if worker:
        from apscheduler.schedulers import SchedulerNotRunningError
        try:
            worker.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass


def start_service(state):
    try:
        scope = private_scope(state)
        with state.session_factory() as session:
            timer = saved(session, scope).get('timer', {})
        if (not scope['timer_available'] or not timer.get('enabled') or
                not isinstance(_clock(state), RealClock) or getattr(state, 'acceptance_scheduler', None)):
            return
    except HTTPException:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    worker = BackgroundScheduler()
    worker.add_job(tick, 'interval', seconds=15, args=[state], id='exact_test_reminder', max_instances=1, coalesce=True)
    state.acceptance_scheduler = worker
    worker.start()


def tick(state):
    # Real time only. Test fixtures may replace RealClock with their deterministic implementation.
    if not isinstance(_clock(state), RealClock) or not _tick_lock.acquire(blocking=False):
        return
    try:
        scope = private_scope(state)
        if not scope['timer_available']:
            return
        with state.session_factory() as session:
            value = saved(session, scope)
            timer = value.get('timer', {})
            if not timer.get('enabled') or _clock(state).now() < datetime.fromisoformat(timer['due_at']):
                return
            # Check before intake too; quiet time cannot submit a reviewed text.
            policies = PolicyStore(session)
            message = session.get(m.Message, timer.get('message_id'))
            approval = confirmations.proof_for(session, message) if message else None
            from app.integrations.google_voice_quiet_test import deadline as quiet_test_deadline
            if (in_quiet_hours(_clock(state).now().astimezone(policies.church_tz()), *policies.quiet_hours()) and
                    not quiet_test_deadline(session, state.provider, scope['phone'], 'reminder', _clock(state).now(), approval=approval)):
                return
        try:
            dispatch_exact(state, scope, {k: timer[k] for k in ('event_id', 'assignment_id', 'message_id', 'body_hash')})
            with state.session_factory() as session:
                outcome = session.get(m.Message, timer['message_id']).status
        except Exception:
            outcome = 'held'
        # One attempt only, even for an unknown connector response or policy hold.
        with state.session_factory() as session:
            value = saved(session, scope)
            value['timer'] = {**timer, 'enabled': False, 'state': outcome,
                              'finished_at': _clock(state).now().isoformat()}
            save(session, scope, value)
            session.commit()
    except HTTPException:
        return
    finally:
        _tick_lock.release()
