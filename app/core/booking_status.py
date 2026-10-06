"""Answer booking questions from this sender's persisted schedule, never guesses."""
import re
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core.schedule_messages import shift_facts, describe
from app.core.conversation import scope


def requested(session, volunteer, body, now):
    text = body.strip().lower().replace("’", "'")
    if re.search(r"\b(?:am i|do i|have i|what am i|when am i)\b.*\b(?:booked|booking|assigned|scheduled|serving|shifts?)\b", text):
        return True
    if re.fullmatch(r"(?:my|any) (?:bookings?|schedule|assignments?|shifts?)[?!.]*", text) or re.search(r"\b(?:which|what) (?:bookings?|assignments?|shifts?) (?:for me|do i|am i)\b|\b(?:what(?:'s| is)|when|show|check)\b.*\bmy (?:bookings?|schedule|assignments?|shifts?)\b", text):
        return True
    if text.rstrip("?!.") not in {"what about now", "and now", "am i now", "anything now"}:
        return False
    from app.core.conversation import scope
    last = session.scalar(scope(select(m.Message), session.info.get("mac_test_session")).where(
        m.Message.volunteer_id == volunteer.id, m.Message.phone == volunteer.phone,
        m.Message.direction == "out", m.Message.created_at >= now-timedelta(hours=24),
    ).order_by(m.Message.id.desc()).limit(1))
    return last is not None and last.purpose == "booking_status"


def snapshot(session, volunteer, now):
    """All facts that can affect this sender's answer or its delivery proof."""
    assignments = session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id == volunteer.id,
        m.Assignment.status.in_(("proposed", "approved", "confirmed")),
        m.Event.status == "scheduled", m.Event.ends_at > now,
    ).order_by(m.Event.starts_at, m.Assignment.id)).all()
    offers = session.scalars(scope(select(m.Outreach).join(m.FillRequest).join(m.Shift).join(m.Event)
        .join(m.Message, m.Outreach.message_id == m.Message.id), session.info.get("mac_test_session")).where(
            m.Outreach.volunteer_id == volunteer.id, m.Outreach.response.in_(("none", "partial")),
            m.FillRequest.state.in_(("open", "in_progress", "waiting_approval", "escalated")),
            m.Event.status == "scheduled", m.Event.starts_at > now,
            m.Message.phone == volunteer.phone, m.Message.volunteer_id == volunteer.id, m.Message.direction == "out",
            m.Message.status.in_(("sent", "submitted", "uncertain")),
            m.Message.created_at >= now-timedelta(days=14),
        ).order_by(m.Event.starts_at, m.Outreach.id)).all()
    from app.core import offer_windows
    valid_offers = []
    for offer in offers:
        try:
            if offer_windows.problem(session, offer, now) is None:
                valid_offers.append(offer)
        except (TypeError, KeyError, ValueError):
            continue  # Incomplete historical proof cannot authorize an RSVP.
    return {'sender_name': volunteer.name,
        'assignments': [{'assignment_id': a.id, 'status': a.status, **shift_facts(a.shift)} for a in assignments],
        'offers': [{'outreach_id': o.id, 'response': o.response,
            'fill_state': session.get(m.FillRequest, o.fill_request_id).state,
            'message_id': o.message_id, 'message_status': session.get(m.Message, o.message_id).status,
            'offer_window': {'state': offer_windows.metadata(session, o).state,
                'expires_at': offer_windows.metadata(session, o).expires_at.isoformat(),
                'snapshot': offer_windows.metadata(session, o).detail['snapshot']},
            **shift_facts(session.get(m.Shift, session.get(m.FillRequest, o.fill_request_id).shift_id))} for o in valid_offers]}


def copy_for(facts, tz):
    from app.llm.gloo_client import GlooUnavailableError
    if not facts['sender_name'].strip():
        raise GlooUnavailableError('Saved schedule recipient is missing')
    booked = [a for a in facts['assignments'] if a['status'] in {'approved', 'confirmed'}]
    proposed = [a for a in facts['assignments'] if a['status'] == 'proposed']
    name = facts['sender_name'].split()[0]
    if booked:
        body = f"Hi {name}! You're booked for {len(booked)} shift(s): "
        body += "; ".join(describe(a, tz) for a in booked[:2]) + "."
        if len(booked) > 2:
            body += f" Plus {len(booked)-2} more; your coordinator can share the full schedule."
    else:
        body = f"Hi {name}! You're not booked for any shifts right now."
    if facts['offers']:
        from datetime import datetime
        offer = facts['offers'][0]
        deadline = datetime.fromisoformat(offer['offer_window']['expires_at']).astimezone(tz).strftime('%a %b %-d, %-I:%M:%S%p %Z')
        body += f" You have {len(facts['offers'])} pending offer(s), not bookings. {describe(offer, tz)}: reply YES R{offer['outreach_id']} or NO R{offer['outreach_id']} by {deadline}."
    if proposed:
        body += f" {len(proposed)} proposed shift(s) await coordinator approval; these are not confirmed bookings."
    return body


def reply(session, clock, gate, volunteer, gloo=None):
    """Keep outages durable without turning a question into later outreach."""
    from app.agents.fill_agent import FillContext
    from app.core.notifications import _dispatch
    reply_id = gate.reply_to_message_id
    key = f'booking-status:{reply_id}'
    row = session.get(m.Notification, key)
    if row is not None:
        return row  # Same source never stages or composes twice.
    incoming = session.get(m.Message, reply_id) if reply_id else None
    if incoming is None:
        return None
    selected = session.info.get('mac_test_session')
    row = m.Notification(key=key, volunteer_id=volunteer.id, purpose='booking_status', body='', state='pending',
        due_at=clock.now(), created_at=clock.now(), expires_at=incoming.created_at+timedelta(minutes=10),
        detail={'reply_id': reply_id, 'session_scope': session_binding(selected)})
    session.add(row); session.flush()
    _dispatch(FillContext(session, clock, gate.provider, gloo, reply_to_message_id=reply_id), row)
    return row


def session_binding(selected):
    return None if selected is None else [selected.id, selected.starts_at.isoformat(), selected.end_iso()]


def prepare(ctx, row, volunteer, now):
    """Each attempt uses the original question and fresh persisted records."""
    from app.core import outbound_conversation
    selected = getattr(ctx.provider, 'test_sessions', {}).get(volunteer.phone)
    if hasattr(ctx.provider, 'allows') and (not ctx.provider.allows(volunteer.phone) or selected is None or not selected.active(now)):
        return None, 'Booking answer needs its active recipient test session'
    if session_binding(selected) != row.detail.get('session_scope'):
        return None, 'Booking question test session changed'
    if selected is not None:
        ctx.session.info['mac_test_session'] = selected
    opted_out = ctx.session.get(m.Policy, 'sms_opt_out:' + volunteer.phone)
    if volunteer.status != 'active' or not volunteer.sms_opt_in or (opted_out and opted_out.value.get('value')):
        return None, 'Booking recipient is no longer active or consenting'
    meta, error = outbound_conversation.metadata(ctx.session, purpose='booking_status', volunteer=volunteer,
        phone=volunteer.phone, now=now, reply_id=row.detail['reply_id'])
    if error:
        return None, error
    return meta, outbound_conversation.problem(ctx.session, purpose='booking_status', volunteer=volunteer,
        phone=volunteer.phone, body='', now=now, meta=meta)
