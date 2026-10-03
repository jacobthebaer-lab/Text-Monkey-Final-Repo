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
        facts = json.loads(kwargs['input'])
        if 'approved_message' in facts:
            return SimpleNamespace(output_text=facts['approved_message'])
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
    assert care.severity == 'urgent' and care.assigned_to == pastor.id, observed
    assert not provider.sent, 'Care is an internal dashboard record, never an automatic SMS'
    incoming=session.scalar(select(m.Message).where(m.Message.direction=='in',m.Message.phone==phone))
    assert care.related_ids['message_id']==incoming.id

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

@pytest.mark.parametrize('stage',['interests','availability'])
def test_setup_cancellation_creates_review_gap_without_unsolicited_offer(session,clock,provider,make_volunteer,make_shift,assign,stage):
    person=make_volunteer('Synthetic Cancelled Volunteer',prefs={'onboarding_stage':stage})
    helper=make_volunteer('Synthetic First Responder')
    late=make_volunteer('Synthetic Late Responder')
    shift=make_shift('Greeter');booking=assign(person,shift)
    ctx=FillContext(session,clock,provider,FixedModel({'understood':False}))
    cancel=handle_inbound(session,clock,provider,person.phone,"Can't make it Sunday",parser_returning(intent='cancel'),ctx=ctx)
    assert booking.status=='cancelled' and cancel.routed_to=='fill_agent'
    fill=session.scalar(select(m.FillRequest))
    assert fill.state=='in_progress' and fill.cancelled_assignment_id==booking.id
    assert not provider.sent_to(helper.phone) and not provider.sent_to(late.phone)
    assert session.scalar(select(m.Notification).where(m.Notification.purpose=='conversation_suppression'))
    # An unasked YES is not scheduling consent and cannot claim an open slot.
    for candidate in (helper,late):
        answer=handle_inbound(session,clock,provider,candidate.phone,'YES',parser_returning(intent='confirm'),ctx=ctx)
        assert answer.routed_to=='unmatched_reply'
    active=session.scalars(select(m.Assignment).where(m.Assignment.shift_id==shift.id,
        m.Assignment.status.in_(('proposed','approved','confirmed')))).all()
    assert not active and person.preferences['onboarding_stage']==stage
    assert session.scalar(select(m.Outreach)) is not None  # Search state remains available to the coordinator.

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
    assert not provider.sent_to(helper.phone) and not provider.sent_to(pastor.phone)
    assert session.scalar(select(m.FillRequest)).cancelled_assignment_id==booking.id
    care = session.scalar(select(m.Escalation).where(m.Escalation.category == "sensitive"))
    assert care.severity == "urgent" and care.assigned_to == pastor.id

def test_unbound_shift_number_cannot_cancel_a_booking_during_profile_setup(session, clock, provider, make_volunteer, make_shift, assign):
    from datetime import timedelta
    person = make_volunteer("Synthetic Cancelled Volunteer", prefs={"onboarding_stage": "interests"})
    make_volunteer("Synthetic Replacement")
    first = assign(person, make_shift("Greeter"))
    second = assign(person, make_shift("Greeter", starts=clock.now()+timedelta(days=8)))
    ctx = FillContext(session, clock, provider, FixedModel({"understood": False}))
    handle_inbound(session, clock, provider, person.phone, "Can't make it Sunday", parser_returning(intent="cancel"), ctx=ctx)
    assert first.status == second.status == "approved"
    assert session.scalar(select(m.Message).where(m.Message.purpose=='clarify_shift')) is None
    assert session.scalar(select(m.Notification).where(m.Notification.purpose=='cancellation_scope')).state=='pending'
    choice = handle_inbound(session, clock, provider, person.phone, "1", parser_returning(intent="confirm"), ctx=ctx)
    assert choice.routed_to=='cancellation_review'
    assert first.status==second.status=='approved'
    assert person.preferences['onboarding_stage']=='interests'
    assert not provider.sent


@pytest.mark.parametrize('answer',['1','2','1)','1, 2','1 and 2'])
def test_ambiguous_cancellation_numerals_never_mutate_profile_or_bookings(session,clock,provider,make_volunteer,make_shift,assign,answer):
    from datetime import timedelta
    person=make_volunteer('Synthetic Canceller',prefs={'onboarding_stage':'interests','keep':'original'})
    first=assign(person,make_shift('Greeter'))
    second=assign(person,make_shift('Usher',starts=clock.now()+timedelta(days=8)))
    ctx=FillContext(session,clock,provider,FixedModel({'understood':True,'any_role':True,'role_ids':[]}))
    initial=handle_inbound(session,clock,provider,person.phone,"Can't make it Sunday",parser_returning(intent='cancel'),ctx=ctx)
    before=dict(person.preferences)
    assert initial.routed_to=='cancellation_review'
    result=handle_inbound(session,clock,provider,person.phone,answer,
        lambda _:pytest.fail('Unbound number must not reach interpretation'),ctx=ctx)
    assert result.routed_to=='cancellation_review' and person.preferences==before
    assert first.status==second.status=='approved' and not provider.sent
    reviews=session.scalars(select(m.Escalation).where(m.Escalation.category=='cancellation_scope')).all()
    assert len(reviews)==1 and reviews[0].status=='open'


def scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign):
    from datetime import timedelta
    person=make_volunteer('Synthetic Canceller',prefs={'onboarding_stage':'interests','keep':'original'})
    first=assign(person,make_shift('Greeter'))
    second=assign(person,make_shift('Usher',starts=clock.now()+timedelta(days=8)))
    ctx=FillContext(session,clock,provider,FixedModel({'understood':False}))
    result=handle_inbound(session,clock,provider,person.phone,"Can't make it Sunday",parser_returning(intent='cancel'),ctx=ctx)
    assert result.routed_to=='cancellation_review'
    return person,first,second,ctx


@pytest.mark.parametrize('answer',['1','1)','1, 2'])
def test_internal_cancellation_hold_survives_dispatch_before_numeric_reply(session,clock,provider,make_volunteer,make_shift,assign,answer):
    from app.core.notifications import flush_due, _dispatch
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    before=dict(person.preferences)
    hold=session.get(m.Notification,f'cancellation-scope:{person.id}')
    detail=dict(hold.detail)
    ctx.gloo=SimpleNamespace(create_response=lambda **kwargs:pytest.fail('Internal hold must not reach Gloo'))
    assert flush_due(ctx)==0
    _dispatch(ctx,hold)  # The direct dispatch boundary must also preserve internal state.
    assert hold.state=='pending' and hold.detail==detail and not provider.sent
    result=handle_inbound(session,clock,provider,person.phone,answer,
        lambda _:pytest.fail('Unbound numeric reply must not reach interpretation'),ctx=ctx)
    assert result.routed_to=='cancellation_review' and person.preferences==before
    assert first.status==second.status=='approved' and not provider.sent
    assert hold.state=='pending'
    assert len(session.scalars(select(m.Escalation).where(m.Escalation.category=='cancellation_scope')).all())==1


def test_explicit_role_date_resolves_checked_id_without_model_hint_or_profile_change(session,clock,provider,make_volunteer,make_shift,assign):
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    before=dict(person.preferences)
    result=handle_inbound(session,clock,provider,person.phone,'Please cancel Greeter October 4',
        parser_returning(intent='cancel',shift_hint='Usher'),ctx=ctx)
    assert result.routed_to=='fill_agent' and first.status=='cancelled' and second.status=='approved'
    assert person.preferences==before and person.sms_opt_in and not person.is_coordinator and not person.is_pastor
    assert not person.qualifications
    hold=session.get(m.Notification,f'cancellation-scope:{person.id}')
    assert hold.state=='resolved' and hold.detail['assignment_id']==first.id
    assert session.scalar(select(m.Escalation).where(m.Escalation.category=='cancellation_scope')).status=='resolved'
    assert session.scalar(select(m.FillRequest)).cancelled_assignment_id==first.id


@pytest.mark.parametrize('change',['removed','time','closed_event','source_body','source_sender','session'])
def test_stale_cancellation_scope_never_falls_back_to_sole_remaining_booking(session,clock,provider,make_volunteer,make_shift,assign,change):
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    hold=session.get(m.Notification,f'cancellation-scope:{person.id}')
    source=session.get(m.Message,hold.detail['source_message_id'])
    from datetime import timedelta
    if change=='removed':first.status='cancelled'
    elif change=='time':first.shift.event.starts_at+=timedelta(hours=1)
    elif change=='closed_event':first.shift.event.status='cancelled'
    elif change=='source_body':source.body='Unrelated old message'
    elif change=='source_sender':source.phone='+15550009999'
    elif change=='session':hold.detail={**hold.detail,'session_id':'different-session'}
    session.flush()
    result=handle_inbound(session,clock,provider,person.phone,'Please cancel Greeter October 4',parser_returning(intent='cancel'),ctx=ctx)
    assert result.routed_to=='cancellation_review' and second.status=='approved'
    assert first.status==('cancelled' if change=='removed' else 'approved')
    assert person.preferences['onboarding_stage']=='interests' and not provider.sent
    assert hold.state=='pending' and session.scalar(select(m.FillRequest)) is None


def test_legacy_question_number_never_cancels_reordered_current_booking(session,clock,provider,make_volunteer,make_shift,assign):
    person=make_volunteer(prefs={'onboarding_stage':'interests'})
    booking=assign(person,make_shift('Greeter'))
    session.add(m.Message(direction='out',volunteer_id=person.id,phone=person.phone,body='Historical numbered question.',
        purpose='clarify_shift',kind='ai',status='sent',created_at=clock.now()));session.flush()
    ctx=FillContext(session,clock,provider,FixedModel({'understood':True,'any_role':True}))
    result=handle_inbound(session,clock,provider,person.phone,'1)',lambda _:pytest.fail('Must not interpret old number'),ctx=ctx)
    assert result.routed_to=='cancellation_review' and booking.status=='approved'
    assert person.preferences=={'onboarding_stage':'interests'} and not provider.sent


def test_stop_precedes_cancellation_hold_and_changes_only_consent(session,clock,provider,make_volunteer,make_shift,assign):
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    result=handle_inbound(session,clock,provider,person.phone,'STOP',lambda _:pytest.fail('STOP must be local'),ctx=ctx)
    assert result.routed_to=='stop' and not person.sms_opt_in
    assert first.status==second.status=='approved' and person.preferences['keep']=='original'
    assert session.get(m.Notification,f'cancellation-scope:{person.id}').state=='pending'
    assert not provider.sent


def test_next_cancellation_episode_reopens_one_internal_review(session,clock,provider,make_volunteer,make_shift,assign):
    from datetime import timedelta
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    handle_inbound(session,clock,provider,person.phone,'Please cancel Greeter October 4',parser_returning(intent='cancel'),ctx=ctx)
    third=assign(person,make_shift('Production',starts=clock.now()+timedelta(days=9)))
    handle_inbound(session,clock,provider,person.phone,"Can't make it Friday",parser_returning(intent='cancel'),ctx=ctx)
    reviews=session.scalars(select(m.Escalation).where(m.Escalation.category=='cancellation_scope')).all()
    assert len(reviews)==1 and reviews[0].status=='open'
    assert second.status==third.status=='approved'
    assert session.get(m.Notification,f'cancellation-scope:{person.id}').state=='pending'


def test_booking_change_during_interpretation_keeps_cancellation_held(session,clock,provider,make_volunteer,make_shift,assign):
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    def raced_parse(body):
        second.status='cancelled';session.flush()
        return ParsedMessage(intent='cancel',confidence=1,shift_hint='Greeter')
    result=handle_inbound(session,clock,provider,person.phone,'Please cancel Greeter October 4',raced_parse,ctx=ctx)
    assert result.routed_to=='cancellation_review' and first.status=='approved'
    assert session.scalar(select(m.FillRequest)) is None and not provider.sent


def test_checked_assignment_helper_rejects_changed_scope_and_foreign_recipient(session,clock,provider,make_volunteer,make_shift,assign):
    from app.agents.fill_agent import cancel_recorded_assignment
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    hold=session.get(m.Notification,f'cancellation-scope:{person.id}')
    second.status='cancelled';session.flush()
    result=cancel_recorded_assignment(ctx,person,first.id,expected_scope=hold.detail['bookings'])
    assert result.action=='cancellation_scope_changed' and first.status=='approved'
    other=make_volunteer('Other Synthetic Sender')
    result=cancel_recorded_assignment(ctx,other,first.id)
    assert result.action=='cancellation_scope_changed' and first.status=='approved'


def test_sensitive_ambiguous_cancellation_is_internal_and_retains_both_bookings(session,clock,provider,make_volunteer,make_shift,assign):
    person,first,second,ctx=scoped_cancellation(session,clock,provider,make_volunteer,make_shift,assign)
    result=handle_inbound(session,clock,provider,person.phone,"I can't serve Sunday, I want to hurt myself",
        parser_returning(intent='cancel',sensitive=True,severity='urgent'),ctx=ctx)
    assert result.routed_to=='cancellation_review' and result.escalation_id
    assert first.status==second.status=='approved' and not provider.sent
    care=session.get(m.Escalation,result.escalation_id)
    assert care.category=='sensitive' and care.severity=='urgent'
    assert care.related_ids['message_id']==session.scalar(select(m.Message.id).where(m.Message.body=="I can't serve Sunday, I want to hurt myself"))
