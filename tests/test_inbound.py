"""handle_inbound routing with an injected fake parser — no Gloo, no real SMS."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.conftest import NOW


def parser_returning(**kwargs):
    defaults = dict(intent="other", confidence=0.95)
    defaults.update(kwargs)
    return lambda text: ParsedMessage(**defaults)


@pytest.fixture
def pastor(make_volunteer):
    return make_volunteer("Pastor Test", pastor=True)


@pytest.fixture
def coordinator(make_volunteer):
    return make_volunteer("Coordinator Test", coordinator=True)


def test_unknown_number_gets_template_reply(session, clock, provider):
    result = handle_inbound(session, clock, provider, "+15559998888", "hello?", parser_returning())
    assert result.routed_to == "unknown_number"
    assert "volunteers" in provider.sent_to("+15559998888")[0].body
    inbound = session.scalar(select(m.Message).where(m.Message.direction == "in"))
    assert inbound.phone == "+15559998888" and inbound.volunteer_id is None


def test_stop_routes_to_opt_out(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    result = handle_inbound(session, clock, provider, vol.phone, "STOP", parser_returning())
    assert result.routed_to == "stop"
    assert vol.sms_opt_in is False


def test_cancel_acks_and_routes_to_fill_agent(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    parser = parser_returning(intent="cancel", confidence=0.92, shift_hint="tomorrow")
    result = handle_inbound(session, clock, provider, vol.phone, "cant make it tmrw!!", parser)
    assert result.routed_to == "fill_agent"
    assert "off the schedule" in provider.sent_to(vol.phone)[0].body


def test_sensitive_cancel_escalates_and_stays_silent(
    session, clock, provider, make_volunteer, pastor
):
    vol = make_volunteer()
    parser = parser_returning(intent="cancel", confidence=0.9, sensitive=True, severity="urgent")
    result = handle_inbound(
        session, clock, provider, vol.phone, "my dad was taken to the ER, can't come", parser
    )
    # Logistics still proceed...
    assert result.routed_to == "fill_agent"
    # ...but the volunteer hears from no robot,
    assert provider.sent_to(vol.phone) == []
    # the pastor is alerted,
    assert len(provider.sent_to(pastor.phone)) == 1
    # and the escalation is open and urgent.
    escalation = session.get(m.Escalation, result.escalation_id)
    assert escalation.category == "sensitive"
    assert escalation.severity == "urgent"
    assert escalation.assigned_to == pastor.id
    assert escalation.related_ids["volunteer_id"] == vol.id


def test_coordinator_yes_approves_and_releases_held_outreach(
    session, clock, provider, gate, make_volunteer, make_shift, coordinator
):
    shift = make_shift("nursery", fill_policy="needs_approval")
    target = make_volunteer()
    held = gate.send(body="Cover nursery Sunday?", purpose="outreach", volunteer=target, role=shift.role)
    approval = session.get(m.Approval, held.approval_id)

    result = handle_inbound(session, clock, provider, coordinator.phone, "YES", parser_returning())
    assert result.routed_to == "approval"
    assert approval.status == "approved"
    assert approval.via == "sms" and approval.decided_by == coordinator.name
    assert provider.sent_to(target.phone)[0].body == "Cover nursery Sunday?"


def test_coordinator_no_rejects(session, clock, provider, gate, make_volunteer, make_shift, coordinator):
    shift = make_shift("nursery", fill_policy="needs_approval")
    target = make_volunteer()
    held = gate.send(body="Cover?", purpose="outreach", volunteer=target, role=shift.role)
    approval = session.get(m.Approval, held.approval_id)

    result = handle_inbound(session, clock, provider, coordinator.phone, "no", parser_returning())
    assert approval.status == "rejected"
    assert provider.sent_to(target.phone) == []


def test_coordinator_free_text_routes_to_admin_agent(session, clock, provider, coordinator):
    result = handle_inbound(
        session, clock, provider, coordinator.phone, "who's serving sunday?", parser_returning()
    )
    assert result.routed_to == "admin_agent"


def test_accept_matches_open_outreach(session, clock, provider, make_volunteer, make_shift):
    vol = make_volunteer()
    shift = make_shift("usher")
    fill = m.FillRequest(shift_id=shift.id, urgency="normal", state="in_progress", created_at=NOW)
    session.add(fill)
    session.flush()
    outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=vol.id, tranche=1)
    session.add(outreach)
    session.flush()

    result = handle_inbound(
        session, clock, provider, vol.phone, "yes!!", parser_returning(intent="accept", confidence=0.97)
    )
    assert result.routed_to == "fill_agent"
    assert outreach.response == "yes"
    assert outreach.responded_at == NOW


def test_accept_without_outreach_asks_for_clarity(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    result = handle_inbound(
        session, clock, provider, vol.phone, "yes", parser_returning(intent="accept", confidence=0.9)
    )
    assert result.routed_to == "clarify"


def test_confirm_marks_next_assignment(session, clock, provider, make_volunteer, make_shift, assign):
    vol = make_volunteer()
    assignment = assign(vol, make_shift("usher"), status="approved")
    result = handle_inbound(
        session, clock, provider, vol.phone, "C", parser_returning(intent="confirm", confidence=0.99)
    )
    assert result.routed_to == "confirmed"
    assert assignment.status == "confirmed"


def test_availability_routes_to_planning(session, clock, provider, make_volunteer):
    result = handle_inbound(
        session, clock, provider, make_volunteer().phone, "2nd and 4th",
        parser_returning(intent="availability", confidence=0.9),
    )
    assert result.routed_to == "planning"


def test_low_confidence_clarifies_then_escalates(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    vague = parser_returning(intent="cancel", confidence=0.4)

    first = handle_inbound(session, clock, provider, vol.phone, "ok", vague)
    assert first.routed_to == "clarify"
    assert "make sure I get this right" in provider.sent_to(vol.phone)[0].body

    clock.advance(timedelta(minutes=10))
    second = handle_inbound(session, clock, provider, vol.phone, "ok", vague)
    assert second.routed_to == "escalated_unclear"
    escalation = session.get(m.Escalation, second.escalation_id)
    assert escalation.category == "unclear"
    assert len(provider.sent_to(vol.phone)) == 1  # no second clarify


def test_parser_failure_escalates_never_guesses(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    broken = lambda text: ParsedMessage(intent="unclear", parse_error=True)
    result = handle_inbound(session, clock, provider, vol.phone, "anything", broken)
    assert result.routed_to == "escalated_unclear"
    assert session.get(m.Escalation, result.escalation_id).category == "system_error"


def test_every_inbound_is_logged(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    handle_inbound(session, clock, provider, vol.phone, "hello", parser_returning(intent="question"))
    logged = session.scalars(select(m.Message).where(m.Message.direction == "in")).all()
    assert len(logged) == 1
    assert logged[0].body == "hello"
    assert logged[0].volunteer_id == vol.id
