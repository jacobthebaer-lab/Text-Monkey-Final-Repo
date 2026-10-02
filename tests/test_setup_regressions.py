"""Independent acceptance probes derived from PLAN/MVP behavior, no live I/O."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.core import eligibility
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.test_fill_agent import ScriptedAgentGloo, parser_returning


class FixedModel:
    settings = Settings()
    def __init__(self, result):
        self.result = result
        self.fill_agent = ScriptedAgentGloo()
    def create_response(self, **kwargs):
        if kwargs.get("tools"):
            return self.fill_agent.create_response(**kwargs)
        return SimpleNamespace(output_text=json.dumps(self.result))


def test_flexible_retains_specific_date_exclusions_until_explicitly_cleared(session, clock, provider, make_volunteer, make_shift):
    person = make_volunteer("Synthetic Returning Volunteer", prefs={"onboarding_stage": "availability"})
    shift = make_shift("Greeter")
    excluded = shift.event.starts_at.date().isoformat()
    session.add(m.Availability(volunteer_id=person.id, month=excluded[:7], unavailable_dates=[excluded],
                              raw_reply="Previous explicit exclusion", parsed_at=clock.now()))
    session.flush()
    ctx = FillContext(session, clock, provider, FixedModel({"understood": True, "weekdays": [],
        "preferred_services": [], "max_per_month": 2, "available_dates": [], "unavailable_dates": []}))
    result = handle_inbound(session, clock, provider, person.phone, "FLEXIBLE", parser_returning(intent="availability"), ctx=ctx)
    assert result.routed_to == "onboarding_complete"
    verdict = eligibility.check(session, person, shift)
    assert not verdict.eligible and f"said unavailable on {excluded}" in verdict.reasons

@pytest.mark.parametrize("stage", ["unknown", "consent_pending", "interests", "availability", "complete"])
def test_urgent_care_routes_consistently_through_every_signup_stage(session, clock, provider, make_volunteer, stage):
    pastor = make_volunteer("Synthetic Pastor", pastor=True)
    if stage == "unknown":
        phone = "+12025550196"
    else:
        prefs = {"consent_pending": True} if stage == "consent_pending" else {"onboarding_stage": stage}
        person = make_volunteer("Synthetic Newcomer", prefs=prefs)
        phone = person.phone
    ctx = FillContext(session, clock, provider, FixedModel({"sensitive": True, "signup": True}))
    result = handle_inbound(session, clock, provider, phone, "I want to hurt myself",
        lambda _: ParsedMessage(intent="other", sensitive=True, severity="urgent", confidence=1), ctx=ctx, allow_signup=True)
    session.flush()
    care = session.scalar(select(m.Escalation).where(m.Escalation.category == "sensitive"))
    assert care is not None
    assert not provider.sent_to(phone), "Automated replies to a distressed sender must be held"
    observed = {"severity": care.severity, "assigned_to": care.assigned_to,
                "pastor_notifications": len(provider.sent_to(pastor.phone)), "route": result.routed_to}
    assert care.severity == "urgent" and care.assigned_to == pastor.id and provider.sent_to(pastor.phone), observed

def test_existing_volunteer_can_cancel_while_restarting_profile_setup(session, clock, provider, make_volunteer, make_shift, assign):
    person = make_volunteer("Synthetic Serving Volunteer", prefs={"onboarding_stage": "interests"})
    shift = make_shift("Greeter")
    booking = assign(person, shift)
    make_volunteer("Synthetic Replacement")
    ctx = FillContext(session, clock, provider, FixedModel({"understood": False}))
    result = handle_inbound(session, clock, provider, person.phone, "Can't make it Sunday",
                           parser_returning(intent="cancel"), ctx=ctx)
    session.flush()
    assert booking.status == "cancelled", {"route": result.routed_to, "assignment_status": booking.status,
                                          "fill_requests": len(session.scalars(select(m.FillRequest)).all())}


def test_completed_profile_cancellation_control(session, clock, provider, make_volunteer, make_shift, assign):
    person = make_volunteer("Synthetic Serving Volunteer", prefs={"onboarding_stage": "complete"})
    booking = assign(person, make_shift("Greeter"))
    make_volunteer("Synthetic Replacement")
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, person.phone, "Can't make it Sunday", parser_returning(intent="cancel"), ctx=ctx)
    assert booking.status == "cancelled" and result.routed_to == "fill_agent"

@pytest.mark.parametrize("stage", ["interests", "availability"])
def test_setup_does_not_break_cancellation_replacement_confirmation_journey(session, clock, provider, make_volunteer, make_shift, assign, stage):
    person = make_volunteer("Synthetic Cancelled Volunteer", prefs={"onboarding_stage": stage})
    helper = make_volunteer("Synthetic First Responder")
    late = make_volunteer("Synthetic Late Responder")
    shift = make_shift("Greeter")
    booking = assign(person, shift)
    ctx = FillContext(session, clock, provider, FixedModel({"understood": False}))
    cancel = handle_inbound(session, clock, provider, person.phone, "Can't make it Sunday", parser_returning(intent="cancel"), ctx=ctx)
    assert booking.status == "cancelled" and cancel.routed_to == "fill_agent"
    fill = session.scalar(select(m.FillRequest))
    assert fill.state == "in_progress"
    assert provider.sent_to(helper.phone) and provider.sent_to(late.phone)
    won = handle_inbound(session, clock, provider, helper.phone, "YES", parser_returning(intent="confirm"), ctx=ctx)
    assert won.notes == ["filled"]
    closed = handle_inbound(session, clock, provider, late.phone, "YES", parser_returning(intent="confirm"), ctx=ctx)
    assert closed.notes == ["already_filled"]
    sent_before = len(provider.sent)
    duplicate = handle_inbound(session, clock, provider, helper.phone, "YES", parser_returning(intent="confirm"), ctx=ctx)
    assert duplicate.notes == ["already_filled"] and len(provider.sent) == sent_before
    active = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
                            m.Assignment.status.in_(("proposed", "approved", "confirmed")))).all()
    assert [(row.volunteer_id, row.status) for row in active] == [(helper.id, "confirmed")]
    assert fill.state == "filled" and person.preferences["onboarding_stage"] == stage

def test_sensitive_cancellation_during_setup_fills_gap_without_replying_to_sender(session, clock, provider, make_volunteer, make_shift, assign):
    pastor = make_volunteer("Synthetic Pastor", pastor=True)
    person = make_volunteer("Synthetic Cancelled Volunteer", prefs={"onboarding_stage": "interests"})
    helper = make_volunteer("Synthetic Replacement")
    booking = assign(person, make_shift("Greeter"))
    ctx = FillContext(session, clock, provider, FixedModel({"understood": False}))
    result = handle_inbound(session, clock, provider, person.phone, "Can't make Sunday, I want to hurt myself",
                           parser_returning(intent="cancel", sensitive=True, severity="urgent"), ctx=ctx)
    assert result.routed_to == "fill_agent" and booking.status == "cancelled"
    assert not provider.sent_to(person.phone)
    assert provider.sent_to(helper.phone) and provider.sent_to(pastor.phone)
    care = session.scalar(select(m.Escalation).where(m.Escalation.category == "sensitive"))
    assert care.severity == "urgent" and care.assigned_to == pastor.id

def test_which_shift_reply_takes_priority_over_profile_role_numbers(session, clock, provider, make_volunteer, make_shift, assign):
    from datetime import timedelta
    person = make_volunteer("Synthetic Cancelled Volunteer", prefs={"onboarding_stage": "interests"})
    make_volunteer("Synthetic Replacement")
    first = assign(person, make_shift("Greeter"))
    second = assign(person, make_shift("Greeter", starts=clock.now()+timedelta(days=8)))
    ctx = FillContext(session, clock, provider, FixedModel({"understood": False}))
    handle_inbound(session, clock, provider, person.phone, "Can't make it Sunday", parser_returning(intent="cancel"), ctx=ctx)
    assert first.status == second.status == "approved"
    assert session.scalar(select(m.Message).where(m.Message.purpose == "clarify_shift")) is not None
    choice = handle_inbound(session, clock, provider, person.phone, "1", parser_returning(intent="confirm"), ctx=ctx)
    assert choice.routed_to == "fill_agent" and first.status == "cancelled" and second.status == "approved"
