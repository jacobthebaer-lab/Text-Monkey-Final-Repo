"""Source-bound Gloo replies describe saved cancellations or unresolved scope."""
from datetime import timedelta, timezone
import hashlib
from app.db import models as m
from app.core import booking_status
from app.core.schedule_messages import shift_facts, describe


def binding(session, volunteer, key, now):
    from app.core.cancellation_scope import bookings, snapshot
    from app.core.privacy import safe_message_history
    from app.llm.parser import keyword_sensitive
    row = session.get(m.Notification, key)
    if (not row or row.purpose != 'signup_reply' or not key.startswith('cancellation-reply:')
            or row.volunteer_id != volunteer.id or row.state not in {'pending', 'awaiting_approval', 'sent'}
            or not row.expires_at or row.expires_at <= now or volunteer.status != 'active' or not volunteer.sms_opt_in
            or (volunteer.preferences or {}).get('onboarding_stage') not in {None, 'complete'}):
        return None
    selected = session.info.get('mac_test_session')
    if row.detail.get('session_scope') != booking_status.session_binding(selected):
        return None
    # Native and enqueue callers already own provider/session admission. This
    # row independently binds the saved original input and exact current facts.
    source = session.get(m.Message, row.detail['reply_id'])
    if (not source or source.direction != 'in' or source.status != 'received'
            or source.phone != volunteer.phone or source.volunteer_id != volunteer.id
            or source.created_at > now or row.detail['input_hash'] != hashlib.sha256(source.body.encode()).hexdigest()
            or keyword_sensitive(source.body) or not safe_message_history(session, [source])
            or (selected and (not selected.active(now) or source.purpose != 'test:'+selected.id
                             or source.created_at < selected.starts_at))):
        return None
    assignment_id = row.detail.get('assignment_id')
    facts = {'reply_id': source.id, 'input_hash': row.detail['input_hash'], 'session_scope': row.detail['session_scope'],
             'name': volunteer.name, 'phone': volunteer.phone, 'volunteer_id': volunteer.id,
             'assignment_id': assignment_id, 'preferences': volunteer.preferences,
             'bookings': snapshot(bookings(session, volunteer, now))}
    if assignment_id is not None:
        assignment = session.get(m.Assignment, assignment_id)
        if (not assignment or assignment.volunteer_id != volunteer.id or assignment.status != 'cancelled'
                or assignment.updated_at.astimezone(timezone.utc).isoformat() != row.detail['cancelled_at']):
            return None
        facts['cancelled'] = {'updated_at': assignment.updated_at.astimezone(timezone.utc).isoformat(), **shift_facts(assignment.shift)}
        if booking_status.opportunities_requested(source.body, after_cancellation=True):
            facts['options'] = booking_status.snapshot(session, volunteer, now, include_opportunities=True,
                exclude_event_ids=(assignment.shift.event_id,))
    else:
        hold = session.get(m.Notification, f'cancellation-scope:{volunteer.id}')
        if (not hold or hold.state != 'pending' or hold.detail.get('source_message_id') != source.id
                or hold.detail.get('source_body_hash') != row.detail['input_hash']):
            return None
        facts['review_scope'] = hold.detail
    return facts


def copy_for(facts, tz):
    name = facts['name'].split()[0]
    if facts['assignment_id'] is None:
        return f"Thanks, {name}! No schedule changes have been made. Which date and role should I cancel? Your coordinator can review the request."
    body = f"Hi {name}! Your {describe(facts['cancelled'], tz)} booking has been cancelled."
    if 'options' in facts:
        options = booking_status.copy_for(facts['options'], tz)
        body += ' ' + options.removeprefix(f'Hi {name}! ')
        if len(body) > 600:
            body = f"Hi {name}! Your {describe(facts['cancelled'], tz)} booking has been cancelled. Your coordinator can share the additional service options. No additional shift has been booked."
    return body


def reply(session, clock, gate, volunteer, *, assignment=None):
    from app.agents.fill_agent import FillContext
    from app.core.notifications import _dispatch
    incoming = session.get(m.Message, gate.reply_to_message_id) if gate.reply_to_message_id else None
    if not incoming:
        return None  # Internal/admin changes are not a sender conversation.
    key = f'cancellation-reply:{incoming.id}'
    existing = session.get(m.Notification, key)
    if existing:
        return existing
    session.flush()
    row = m.Notification(key=key, volunteer_id=volunteer.id, purpose='signup_reply', body='', state='pending',
        due_at=clock.now(), created_at=clock.now(), expires_at=incoming.created_at+timedelta(days=2),
        detail={'reply_id': incoming.id, 'input_hash': hashlib.sha256(incoming.body.encode()).hexdigest(),
                'session_scope': booking_status.session_binding(session.info.get('mac_test_session')),
                'assignment_id': assignment.id if assignment else None,
                'cancelled_at': assignment.updated_at.astimezone(timezone.utc).isoformat() if assignment else None,
                'conversation': {'cancellation_reply': key}})
    session.add(row); session.flush()
    _dispatch(FillContext(session,clock,gate.provider,getattr(gate,'gloo',None),reply_to_message_id=incoming.id),row)
    return row
