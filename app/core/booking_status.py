"""Answer booking questions from this sender's persisted schedule, never guesses."""
import re
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core.signup_responder import compose_signup_reply
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


def reply(session, clock, gate, volunteer, gloo=None):
    now = clock.now()
    assignments = session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id == volunteer.id,
        m.Assignment.status.in_(("proposed", "approved", "confirmed")),
        m.Event.status == "scheduled", m.Event.ends_at > now,
    ).order_by(m.Event.starts_at, m.Assignment.id)).all()
    booked = [a for a in assignments if a.status in {"approved", "confirmed"}]
    proposed = [a for a in assignments if a.status == "proposed"]
    offers = session.scalars(scope(select(m.Outreach).join(m.FillRequest).join(m.Shift).join(m.Event)
        .join(m.Message, m.Outreach.message_id == m.Message.id), session.info.get("mac_test_session")).where(
            m.Outreach.volunteer_id == volunteer.id, m.Outreach.response.in_(("none", "partial")),
            m.FillRequest.state.in_(("open", "in_progress", "waiting_approval", "escalated")),
            m.Event.status == "scheduled", m.Event.starts_at > now,
            m.Message.phone == volunteer.phone, m.Message.volunteer_id == volunteer.id, m.Message.direction == "out",
            m.Message.status.in_(("sent", "submitted", "uncertain")),
            m.Message.created_at >= now-timedelta(days=14),
        ).order_by(m.Event.starts_at, m.Outreach.id)).all()
    tz = gate.policies.church_tz()

    def describe(shift):
        return f"{shift.role.name[:60]} on {shift.event.starts_at.astimezone(tz).strftime('%a %b %-d, %-I:%M%p %Z')}"

    name = volunteer.name.split()[0]
    if booked:
        body = f"Hi {name}! You're booked for {len(booked)} shift(s): "
        body += "; ".join(describe(a.shift) for a in booked[:2]) + "."
        if len(booked) > 2:
            body += f" Plus {len(booked)-2} more; your coordinator can share the full schedule."
    else:
        body = f"Hi {name}! You're not booked for any shifts right now."
    if offers:
        offer = offers[0]
        shift = session.get(m.Shift, session.get(m.FillRequest, offer.fill_request_id).shift_id)
        body += f" You have {len(offers)} pending offer(s), not bookings. {describe(shift)}: reply YES R{offer.id} or NO R{offer.id}."
    if proposed:
        body += f" {len(proposed)} proposed shift(s) await coordinator approval; these are not confirmed bookings."
    if not booked and not offers and not proposed:
        body += " We'll send the details when a matching shift is available."
    rendered = compose_signup_reply(session, clock, gloo, body, (body,), volunteer=volunteer)
    return gate.send(body=rendered, purpose="booking_status", volunteer=volunteer)
