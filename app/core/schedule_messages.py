"""Saved scheduling facts only; general signup keeps its contextual writer."""
from datetime import timezone
from app.core.signup_responder import compose_signup_reply


def current_assignment(session, assignment_id):
    """Reload each required row, holding if a source was deleted in flight."""
    from app.db import models as m
    row = session.get(m.Assignment, assignment_id, populate_existing=True)
    if row is None:
        return None
    shift = session.get(m.Shift, row.shift_id, populate_existing=True)
    if shift is None:
        return None
    event = session.get(m.Event, shift.event_id, populate_existing=True)
    role = session.get(m.Role, shift.role_id, populate_existing=True)
    if event is None or role is None:
        return None
    session.expire(row, ['shift'])
    session.expire(shift, ['event', 'role'])
    return row


def shift_facts(shift):
    from app.llm.gloo_client import GlooUnavailableError
    if shift is None or shift.event is None or shift.role is None or not shift.event.title or not shift.role.name:
        raise GlooUnavailableError('Saved schedule facts are incomplete')
    return {**({'parent_shift_id':shift.parent_shift_id} if shift.parent_shift_id is not None else {}), 'shift_id': shift.id, 'event_id': shift.event_id, 'event_title': shift.event.title,
            'role_id': shift.role_id, 'role_name': shift.role.name,
            'starts_at': shift.starts_at.astimezone(timezone.utc).isoformat(),
            'ends_at': shift.ends_at.astimezone(timezone.utc).isoformat()}


def describe(facts, tz):
    from datetime import datetime
    when = datetime.fromisoformat(facts['starts_at']).astimezone(tz).strftime('%a %b %-d, %-I:%M%p %Z')
    if facts.get('parent_shift_id') is not None:
        when += ' to ' + datetime.fromisoformat(facts['ends_at']).astimezone(tz).strftime('%a %b %-d, %-I:%M%p %Z')
    return f"{facts['role_name']} at {facts['event_title']} on {when}"


def confirmation_copy(assignment, tz):
    from app.llm.gloo_client import GlooUnavailableError
    if assignment is None or assignment.volunteer is None or not assignment.volunteer.name.strip():
        raise GlooUnavailableError('Saved schedule recipient is missing')
    state = 'confirmed' if assignment.status == 'confirmed' else 'scheduled'
    return f"Hi {assignment.volunteer.name.split()[0]}! You're {state} for {describe(shift_facts(assignment.shift), tz)}. Thank you!"


def compose(session, clock, gloo, body, volunteer, context):
    # The model must successfully return this person's verified factual copy.
    # Exact equality prevents new location/arrival/booking claims, rather than
    # trying to recognize every possible fabricated claim with a word blacklist.
    from app.core.message_style import outbound_style_problem
    from app.llm.gloo_client import GlooUnavailableError
    if outbound_style_problem(body) or not 0 < len(body) <= 600:
        raise GlooUnavailableError('Saved schedule copy requires review before composition')
    return compose_signup_reply(session, clock, gloo, body, (body,), volunteer=volunteer,
        require_gloo=True, exact_copy=True, factual_context=context)
