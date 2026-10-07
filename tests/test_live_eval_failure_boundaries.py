"""Actual 25-case failures, with local privacy and real offer guards intact."""
from datetime import timedelta
from functools import partial

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import confirmations, offer_windows as offers
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.parser import ParsedMessage, parse_inbound
from tests.test_fill_agent import ScriptedAgentGloo, parser_returning
from tests.test_offer_windows import offer_factory

HOSPITAL = "my dad was taken to the hospital, cant come tomorrow"


class PrivateCare:
    def create_response(self, **kwargs):
        pytest.fail("Recognized care details must never leave the local classifier")


@pytest.mark.parametrize("body", [HOSPITAL, "Hospital emergency. Can't come tomorrow.",
    "cant come tomorrow, my dad is in the hospital"])
def test_short_sensitive_logistics_are_local_cancellations(body):
    parsed = parse_inbound(PrivateCare(), body)
    assert parsed.intent == "cancel" and parsed.sensitive and not parsed.parse_error
    assert parsed.raw == {"classification_source": "explicit_sensitive_cancel_backstop"}


@pytest.mark.parametrize("body,severity", [
    ("My mom passed away last night. I cant come tomorrow.", "normal"),
    ("I want to hurt myself. I cant come tomorrow.", "urgent"),
    ("I'm in the hospital, I cannot attend tomorrow.", "normal"),
    ("My father was just admitted to the ICU, can't serve tomorrow.", "normal"),
])
def test_supported_affirmative_care_context_stays_local(body, severity):
    parsed = parse_inbound(PrivateCare(), body)
    assert parsed.intent == "cancel" and parsed.sensitive and not parsed.parse_error
    assert parsed.severity == severity


@pytest.mark.parametrize("body", [
    "My dad is in the hospital, he cant come tomorrow",
    'Hospital says "I cant come tomorrow"',
    "Hospital said 'cant come tomorrow'",
    "Dad in the hospital says I cant come tomorrow",
    "Hospital said, cant come tomorrow",
    "Hospital emergency, maybe cant come tomorrow",
    "If dad is in the hospital, cant come tomorrow",
    "Hospital emergency, cant come tomorrow?",
    "Hospital emergency, unless I recover I cant come tomorrow",
])
def test_care_reports_conditions_and_questions_stay_held(body):
    parsed = parse_inbound(PrivateCare(), body)
    assert parsed.intent == "unclear" and parsed.sensitive and parsed.parse_error


@pytest.mark.parametrize("exact_review", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_hospital_logistics_preserve_booking_scope_and_no_care_reply(
        session, clock, provider, make_volunteer, make_shift, assign, exact_review, ambiguous):
    volunteer = make_volunteer()
    first = assign(volunteer, make_shift(starts=clock.now()+timedelta(hours=23)), status="approved")
    if ambiguous:
        second = assign(volunteer, make_shift(starts=clock.now()+timedelta(hours=24)), status="approved")
    session.flush()
    session.info[confirmations.MODE_KEY] = exact_review
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone, HOSPITAL,
        partial(parse_inbound, PrivateCare()), ctx=ctx)
    assert first.status == ("approved" if ambiguous else "cancelled")
    if ambiguous:
        assert second.status == "approved" and result.routed_to == "cancellation_review"
    assert session.scalar(select(m.Escalation.id).where(m.Escalation.category=="sensitive"))
    assert not provider.sent
    assert not session.scalar(select(m.Message.id).where(m.Message.direction=="out",
        m.Message.volunteer_id==volunteer.id))


@pytest.mark.parametrize("body", [
    "My dad is in the hospital. He texted, can't come tomorrow.",
    "My dad is in the hospital. Can't come tomorrow, that's what he said.",
    "My dad is in the hospital, but it is not true that I can't come tomorrow.",
    "My dad is in the hospital and can't come tomorrow.",
    "My dad is in the hospital, he can't come tomorrow and I can't either.",
    "Can't come tomorrow, that's untrue, my dad is in the hospital.",
    "Message from my dad in the hospital, can't come tomorrow.",
    "My dad in the hospital sent me this, can't come tomorrow.",
    "Can't come tomorrow. That was a lie about being in the hospital.",
])
@pytest.mark.parametrize("exact_review", [False, True])
def test_reported_or_negated_care_clause_preserves_sender_booking(
        session, clock, provider, make_volunteer, make_shift, assign, body, exact_review):
    volunteer = make_volunteer()
    booking = assign(volunteer, make_shift(starts=clock.now()+timedelta(hours=23)), status="approved")
    session.flush()
    session.info[confirmations.MODE_KEY] = exact_review
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, volunteer.phone, body,
        partial(parse_inbound, PrivateCare()), ctx=ctx)
    assert booking.status == "approved"
    assert session.scalar(select(m.Escalation.id).where(m.Escalation.category=="sensitive"))
    assert not provider.sent


@pytest.mark.parametrize("hint", ["10:30", "until 10:30", "only til 10:30am"])
def test_partial_window_is_not_an_event_identity_hint(
        session, clock, provider, offer_factory, hint):
    volunteer, _, _, outreach, _ = offer_factory()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone,
        "I can but only til 10:30", parser_returning(intent="partial", confidence=.95,
            shift_hint=hint, partial_window="until 10:30"), ctx=ctx)
    assert result.routed_to == "fill_agent" and outreach.response == "partial"
    assert not session.scalar(select(m.Assignment.id))
    assert result.parsed.shift_hint == hint  # The model evidence is retained.


@pytest.mark.parametrize("hint,body", [
    ("Saturday", "I can but only til 10:30"),
    ("nursery", "I can but only til 10:30"),
    ("2027-01-01", "I can but only til 10:30"),
    ("10:30", "I can serve Saturday but only til 10:30"),
    ("10:30", "Jen can but only til 10:30"),
    ("10:30pm", "I can but only til 10:30am"),
    ("11:30", "I can but only til 10:30"),
])
def test_wrong_event_or_person_hint_is_never_erased(
        session, clock, provider, offer_factory, hint, body):
    volunteer, _, _, outreach, _ = offer_factory()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, volunteer.phone, body,
        parser_returning(intent="partial", confidence=.95,
            shift_hint=hint, partial_window="until 10:30"), ctx=ctx)
    assert outreach.response == "none"
    assert not session.scalar(select(m.Assignment.id))


def test_partial_window_without_an_invitation_never_creates_a_response(
        session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, volunteer.phone, "I can but only til 10:30",
        parser_returning(intent="partial", confidence=.95,
            shift_hint="10:30", partial_window="until 10:30"), ctx=ctx)
    assert not session.scalar(select(m.Outreach.id))
    assert not session.scalar(select(m.Assignment.id))


@pytest.mark.parametrize("guard", ["uncertain", "wrong_phone", "prior_offer"])
def test_partial_window_cannot_bypass_dispatch_or_ambiguity(
        session, clock, provider, offer_factory, guard):
    volunteer, _, _, outreach, sent = offer_factory()
    message = session.get(m.Message, sent.message_id)
    if guard == "prior_offer":
        offers.close(session, outreach, "expired", clock.now())
        _, _, _, current, _ = offer_factory(volunteer=volunteer)
    elif guard == "uncertain":
        message.status = "uncertain"
    else:
        message.phone = "+15550100199"
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, volunteer.phone, "I can but only til 10:30",
        parser_returning(intent="partial", confidence=.95,
            shift_hint="10:30", partial_window="until 10:30"), ctx=ctx)
    assert outreach.response != "partial"
    if guard == "prior_offer":
        assert current.response == "none"
    assert not session.scalar(select(m.Assignment.id))


def test_two_competing_current_sources_still_require_clarification(
        session, clock, provider, offer_factory):
    volunteer, _, _, first, _ = offer_factory()
    offers.close(session, first, "expired", clock.now())
    _, _, _, second, _ = offer_factory(volunteer=volunteer)
    # A controlled legacy/concurrency state, never provider delivery evidence:
    # both source-bound historical fixtures are individually valid and current.
    first.response = "none"
    metadata = offers.metadata(session, first)
    metadata.state = "offer_active"
    metadata.detail = {key:value for key,value in metadata.detail.items() if key!='closed_at'}
    session.flush()
    for outreach in (first, second):
        assert offers.problem(session, outreach, clock.now()) is None
        assert offers.reply_source_problem(session, outreach, clock.now()) is None
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone, "I can but only til 10:30",
        parser_returning(intent="partial", confidence=.95,
            shift_hint="10:30", partial_window="until 10:30"), ctx=ctx)
    assert result.routed_to == "clarify_offer"
    assert first.response == second.response == "none"
    assert not session.scalar(select(m.Assignment.id))
