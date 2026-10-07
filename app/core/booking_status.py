"""Answer booking questions from this sender's persisted schedule, never guesses."""
import re
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core.schedule_messages import shift_facts, describe
from app.core.conversation import scope


def opportunities_requested(body, *, after_cancellation=False):
    text = body.lower().replace("’", "'")
    # A declaration or mixed cancellation still belongs to its record-changing
    # workflow. A question mark later in the message does not erase that intent.
    if not after_cancellation and re.search(r"\bi(?:'m| am| will be) (?:available|unavailable|away|not available)\b|\bi (?:can't|cannot|won't|will not|am unable to) (?:make|attend|come|serve|volunteer|help|cover)\b|(?:^|[.!;,]\s*)i can (?:serve|volunteer|help|cover)\b|\bi (?:need|have|want) to cancel\b|\b(?:please\s+)?cancel\b", text):
        return False
    request = bool('?' in text or re.match(r'^\s*(?:any|what|which|how|where|when|are|is|can|could|would|do)\b', text)
        or re.search(r'\b(?:show|list|tell|find|check) (?:me|my)\b', text))
    return request and bool(re.search(r"\b(?:opportunities|opportunity|openings?)\b|\b(?:other|more|additional|available|open|upcoming)\b.*\b(?:shifts?|roles?|service dates?|ways to (?:help|serve))\b|\b(?:how|when|where) can i (?:help|serve|volunteer)\b|\b(?:can|could) i (?:help|serve|volunteer) (?:more|again)\b", text))


def requested(session, volunteer, body, now):
    text = body.strip().lower().replace("’", "'")
    if opportunities_requested(body):
        return True
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


def snapshot(session, volunteer, now, *, include_opportunities=False, exclude_event_ids=()):
    """All facts that can affect this sender's answer or its delivery proof."""
    assignments = session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id == volunteer.id,
        m.Assignment.status.in_(("proposed", "approved", "confirmed")),
        m.Event.status == "scheduled", m.Shift.ends_at > now,
    ).order_by(m.Shift.starts_at, m.Assignment.id)).all()
    offers = session.scalars(scope(select(m.Outreach).join(m.FillRequest).join(m.Shift).join(m.Event)
        .join(m.Message, m.Outreach.message_id == m.Message.id), session.info.get("mac_test_session")).where(
            m.Outreach.volunteer_id == volunteer.id, m.Outreach.response.in_(("none", "partial")),
            m.FillRequest.state.in_(("open", "in_progress", "waiting_approval", "escalated")),
            m.Event.status == "scheduled", m.Shift.starts_at > now,
            m.Message.phone == volunteer.phone, m.Message.volunteer_id == volunteer.id, m.Message.direction == "out",
            m.Message.status.in_(("sent", "submitted", "uncertain")),
            m.Message.created_at >= now-timedelta(days=14),
        ).order_by(m.Shift.starts_at, m.Outreach.id)).all()
    from app.core import offer_windows
    valid_offers = []
    for offer in offers:
        try:
            if offer_windows.problem(session, offer, now) is None:
                valid_offers.append(offer)
        except (TypeError, KeyError, ValueError):
            continue  # Incomplete historical proof cannot authorize an RSVP.
    facts = {'sender_name': volunteer.name,
        'assignments': [{'assignment_id': a.id, 'status': a.status, **shift_facts(a.shift)} for a in assignments],
        'offers': [{'outreach_id': o.id, 'response': o.response,
            'fill_state': session.get(m.FillRequest, o.fill_request_id).state,
            'message_id': o.message_id, 'message_status': session.get(m.Message, o.message_id).status,
            'offer_window': {'state': offer_windows.metadata(session, o).state,
                'expires_at': offer_windows.metadata(session, o).expires_at.isoformat(),
                'snapshot': offer_windows.metadata(session, o).detail['snapshot']},
            **shift_facts(session.get(m.Shift, session.get(m.FillRequest, o.fill_request_id).shift_id))} for o in valid_offers]}
    if include_opportunities:
        from app.core import eligibility, scheduler
        from app.core.policies import PolicyStore
        from app.core.recurring_availability import global_frequency_limit
        tz = str(PolicyStore(session).church_tz())
        horizon = now + timedelta(days=90)
        try:
            limit = global_frequency_limit(volunteer.preferences)
            needs_review = False
        except (TypeError, ValueError):
            limit, needs_review = None, True
        shifts = session.scalars(select(m.Shift).join(m.Event).where(m.Event.status == 'scheduled',
            m.Shift.starts_at > now, m.Shift.starts_at <= horizon,
            ~m.Shift.coverage_children.any()).order_by(m.Shift.starts_at, m.Shift.id)).all()
        eligible = []
        for shift in shifts:
            if (shift.event_id not in exclude_event_ids and not needs_review and not scheduler.occupied(session, shift) and eligibility.check(session, volunteer, shift, tz)
                    and not scheduler.monthly_problem(session, volunteer, shift, tz)):
                # Multiple slots in the same role/event are one option.
                if not any(x['event_id'] == shift.event_id and x['role_id'] == shift.role_id
                           and x['starts_at'] == shift.starts_at.isoformat() for x in eligible):
                    eligible.append(shift_facts(shift))
        start, end = scheduler.bounds(now.astimezone(PolicyStore(session).church_tz()).strftime('%Y-%m'), tz)
        count = len(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.status.in_((*eligibility.ACTIVE_ASSIGNMENT_STATUSES, 'completed')),
            m.Shift.starts_at >= start, m.Shift.starts_at < end)).all())
        facts['opportunities'] = {'horizon_days': 90, 'current_month': start.strftime('%B %Y'),
            'monthly_limit': limit, 'recorded_this_month': count, 'eligible_open_shifts': eligible[:2]}
        facts['opportunities']['needs_review'] = needs_review
    return facts


def copy_for(facts, tz):
    from app.llm.gloo_client import GlooUnavailableError
    if not facts['sender_name'].strip():
        raise GlooUnavailableError('Saved schedule recipient is missing')
    booked = [a for a in facts['assignments'] if a['status'] in {'approved', 'confirmed'}]
    proposed = [a for a in facts['assignments'] if a['status'] == 'proposed']
    name = facts['sender_name'].split()[0]
    if 'opportunities' in facts:
        opportunity = facts['opportunities']
        body = f"Hi {name}! "
        if opportunity['needs_review']:
            return body + "Your serving preferences need review before I can check additional openings. Could you clarify how often you'd like to serve?"
        if opportunity['monthly_limit'] is not None:
            body += f"Your serving limit is {opportunity['monthly_limit']} per month, with {opportunity['recorded_this_month']} recorded for {opportunity['current_month']}. "
        if opportunity['eligible_open_shifts']:
            body += "Open options that fit your saved rules: " + "; ".join(describe(x, tz) for x in opportunity['eligible_open_shifts']) + ". These are openings, not bookings. Which interests you?"
        else:
            body += "I couldn't find additional open shifts that fit your saved rules in the next 90 days. Would you like to review your availability or serving limit?"
        if len(body) > 600:
            return f"Hi {name}! I found open shifts that fit your saved rules. Your coordinator can confirm their details. No additional shift has been booked."
        return body
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
    if opportunities_requested(incoming.body):
        row.expires_at = incoming.created_at + timedelta(days=2)
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
