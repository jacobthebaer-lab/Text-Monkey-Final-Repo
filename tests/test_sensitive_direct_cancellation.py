"""Literal care logistics can cancel a booking without exporting private context."""
from datetime import timedelta
from functools import partial

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import confirmations
from app.core.cancellation_scope import route
from app.core.inbound import handle_inbound
from app.core.send_gate import SendGate
from app.db import models as m
from app.llm.parser import parse_inbound, sensitive_cancellation_clause
from tests.test_fill_agent import ScriptedAgentGloo


class PrivateCare:
    def create_response(self, **kwargs):
        pytest.fail('Care details must stay local')


@pytest.mark.parametrize('body', [
    'I cannot serve Sunday because I am in the hospital',
    'I cannot attend Sunday because my dad was taken to the ER',
    'my dad was taken to the ER, can’t come',
])
@pytest.mark.parametrize('exact_review', [False, True])
def test_direct_medical_absence_cancels_only_booking_locally(
        session, clock, provider, make_volunteer, make_shift, assign, body, exact_review):
    person = make_volunteer()
    booking = assign(person, make_shift(), status='approved')
    session.info[confirmations.MODE_KEY] = exact_review
    parsed = parse_inbound(PrivateCare(), body)
    assert parsed.intent == 'cancel' and parsed.sensitive and not parsed.parse_error
    handle_inbound(session, clock, provider, person.phone, body,
        partial(parse_inbound, PrivateCare()), ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert booking.status == 'cancelled'
    assert session.scalar(select(m.Escalation.id).where(m.Escalation.category == 'sensitive'))
    source = session.scalar(select(m.Message).where(m.Message.direction == 'in'))
    assert source.body == body and source.volunteer_id == person.id and source.phone == person.phone
    assert not provider.sent_to(person.phone)


@pytest.mark.parametrize('body', [
    'It is not true that I cannot serve Sunday because I am in the hospital',
    'I cannot serve Sunday because I am not in the hospital',
    'If I cannot serve Sunday because I am in the hospital, ask someone else',
    'I cannot serve Sunday because I am in the hospital, that is what my dad said',
    'He said "I cannot serve Sunday because I am in the hospital"',
    "I cannot serve Sunday because I am in the hospital, but I didn't mean that",
])
@pytest.mark.parametrize('exact_review', [False, True])
def test_because_is_not_authority_for_reported_or_negated_cancellation(
        session, clock, provider, make_volunteer, make_shift, assign, body, exact_review):
    person = make_volunteer()
    booking = assign(person, make_shift(), status='approved')
    session.info[confirmations.MODE_KEY] = exact_review
    assert sensitive_cancellation_clause(body) is None
    handle_inbound(session, clock, provider, person.phone, body,
        partial(parse_inbound, PrivateCare()), ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert booking.status == 'approved' and not session.scalar(select(m.FillRequest.id))
    assert not provider.sent_to(person.phone)


@pytest.mark.parametrize('exact_review', [False, True])
@pytest.mark.parametrize('scope', ['multiple', 'wrong_day', 'prior_hold'])
def test_medical_logistics_keep_ambiguous_or_mismatched_booking_scope(
        session, clock, provider, make_volunteer, make_shift, assign, exact_review, scope):
    person = make_volunteer()
    first = assign(person, make_shift(starts=clock.now()+timedelta(hours=23)), status='approved')
    body = 'my dad was taken to the ER, can’t come'
    if scope == 'multiple':
        second = assign(person, make_shift(starts=clock.now()+timedelta(hours=24)), status='approved')
    elif scope == 'wrong_day':
        body = 'I cannot serve Sunday because I am in the hospital'
    else:
        session.add(m.Notification(key=f'cancellation-scope:{person.id}', purpose='cancellation_scope',
            volunteer_id=person.id, body='', state='pending', due_at=clock.now(), created_at=clock.now(),
            detail={'source_message_id': -1, 'bookings': []}))
    session.flush()
    session.info[confirmations.MODE_KEY] = exact_review
    result = handle_inbound(session, clock, provider, person.phone, body,
        partial(parse_inbound, PrivateCare()), ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert result.routed_to == 'cancellation_review' and first.status == 'approved'
    if scope == 'multiple':
        assert second.status == 'approved'
    assert not session.scalar(select(m.FillRequest.id)) and not provider.sent_to(person.phone)


def test_optional_clause_cannot_substitute_for_recorded_care_body(
        session, clock, provider, make_volunteer, make_shift, assign):
    person = make_volunteer()
    booking = assign(person, make_shift(), status='approved')
    body = "Message from my dad in the hospital, can't come"
    source = m.Message(direction='in', status='received', kind='inbound', volunteer_id=person.id,
        phone=person.phone, body=body, created_at=clock.now())
    session.add(source); session.flush()
    held = route(session, clock, SendGate(session, clock, provider), person, source,
        partial(parse_inbound, PrivateCare()), FillContext(session, clock, provider, ScriptedAgentGloo()),
        instruction=True, sensitive_clause="can't come")
    assert held[0] == 'cancellation_review' and booking.status == 'approved'
    assert source.body == body and not provider.sent
