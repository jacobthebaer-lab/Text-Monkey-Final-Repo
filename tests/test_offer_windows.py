"""Synthetic clocks/transports; no network, runtime or schema changes."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agents import fill_agent
from app.agents.fill_agent import FillContext
from app.core import offer_windows as offers
from app.core.inbound import handle_inbound
from app.core.send_gate import SendGate, SendStatus, SendOutcome
from app.db import models as m
from tests.test_fill_agent import ScriptedAgentGloo, parser_returning


@pytest.fixture
def offer_factory(session, clock, provider, make_volunteer, make_shift):
    def create(*, lead=timedelta(hours=6), volunteer=None, shift=None):
        volunteer = volunteer or make_volunteer()
        shift = shift or make_shift(starts=clock.now()+lead)
        fill = m.FillRequest(shift_id=shift.id, urgency="normal", state="in_progress",
                             current_tranche=1, created_at=clock.now())
        session.add(fill); session.flush()
        outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=volunteer.id, tranche=1)
        session.add(outreach); session.flush()
        # Historical submitted invitation receipt, never a new SendGate request.
        # Exercise dispatch bookkeeping directly without contacting a transport.
        meta = offers.prepare(session, outreach, "Could you serve this shift?", clock.now())
        if meta is None:
            # Stage a genuine earlier review before moving to the late boundary.
            prior = clock.now()-timedelta(hours=1)
            meta = offers.prepare(session, outreach, "Could you serve this shift?", prior)
        message = m.Message(direction="out", volunteer_id=volunteer.id, phone=volunteer.phone,
            body=meta.body, purpose="outreach", kind="ai", status="sent",
            provider_sid="SYNTHETIC-HISTORICAL", created_at=clock.now())
        session.add(message); session.flush()
        issue = offers.dispatch(session, outreach, message, offers.decision_time(session, clock))
        if issue:
            session.delete(message)
            outcome = SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason=issue)
        else:
            outreach.message_id = message.id
            outcome = SendOutcome(SendStatus.SENT, message_id=message.id)
        return volunteer, shift, fill, outreach, outcome
    return create


@pytest.mark.parametrize("lead,minutes", [(1440,120), (360,60), (120,20), (60,10), (30,5), (15,2.5), (12,2)])
def test_defaults_and_exact_local_deadline(session, clock, offer_factory, lead, minutes):
    _, _, fill, outreach, result = offer_factory(lead=timedelta(minutes=lead))
    meta = offers.metadata(session, outreach)
    assert result.sent and meta.state == "offer_active"
    assert meta.expires_at == clock.now()+timedelta(minutes=minutes)
    assert fill.next_action_at == meta.expires_at
    assert "Reply yes or no by" in meta.body and "UTC-0600" in meta.body
    assert "Offer R" not in meta.body


@pytest.mark.parametrize("lead", [11.999, 10, 1, 0, -1])
def test_too_late_creates_one_task_no_offer_or_roster_changes(session, clock, provider, offer_factory, lead):
    _, shift, fill, outreach, result = offer_factory(lead=timedelta(minutes=lead))
    assert not result.sent and provider.sent == []
    assert fill.state == "escalated"
    assert outreach.response == "revoked"
    offers.task_once(session, fill, clock.now(), "Duplicate tick")
    assert len(session.scalars(select(m.Escalation)).all()) == 1
    assert not session.scalars(select(m.Assignment)).all()


def test_custom_policy_and_malformed_policy_fail_closed(session, clock):
    session.add(m.Policy(key="offer_response_window", value={"value":{
        "max_minutes":30, "min_minutes":3, "lead_time_divisor":12, "cutoff_minutes":20}}))
    session.flush()
    assert offers.deadline_for(session, clock.now()+timedelta(hours=4), clock.now()) == clock.now()+timedelta(minutes=20)
    assert offers.deadline_for(session, clock.now()+timedelta(minutes=22), clock.now()) is None
    row = session.get(m.Policy, "offer_response_window")
    row.value = {"value":{"lead_time_divisor":0}}
    with pytest.raises(ValueError):
        offers.deadline_for(session, clock.now()+timedelta(hours=1), clock.now())


@pytest.mark.parametrize("offset,accepted", [(-1,True), (0,False), (1,False)])
def test_acceptance_boundary_uses_decision_time(session, clock, provider, offer_factory, offset, accepted):
    volunteer, shift, fill, outreach, _ = offer_factory()
    deadline = offers.metadata(session, outreach).expires_at
    clock.set_time(deadline+timedelta(microseconds=offset))
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    outcome = fill_agent.on_outreach_reply(ctx, volunteer, outreach, "accept")
    winner = session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id))
    assert bool(winner) == accepted
    assert outcome.action == ("filled" if accepted else "offer_closed")
    assert outreach.response == ("yes" if accepted else "expired")


def test_expiry_is_offer_only_sequential_and_idempotent(session, clock, provider, make_volunteer, make_shift, assign, offer_factory):
    original, first, second = [make_volunteer() for _ in range(3)]
    shift = make_shift(starts=clock.now()+timedelta(hours=6))
    cancelled = assign(original, shift, status="cancelled")
    unrelated = assign(first, make_shift("Other", starts=clock.now()+timedelta(days=2)), status="confirmed")
    _, _, fill, first_offer, _ = offer_factory(volunteer=first, shift=shift)
    fill.cancelled_assignment_id = cancelled.id
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    assert first_offer.volunteer_id == first.id
    clock.set_time(fill.next_action_at)
    assert fill_agent.advance_due(ctx)[0].action == "offer_blocked"
    current = session.scalars(select(m.Outreach).where(m.Outreach.id != first_offer.id)).all()
    assert len(current) == 1 and current[0].volunteer_id == second.id
    assert current[0].response == "blocked" and current[0].message_id is None
    assert first_offer.response == "expired" and first_offer.responded_at is None
    assert unrelated.status == "confirmed" and first.sms_opt_in
    before = len(provider.sent)
    assert fill_agent.advance_due(ctx)[0].action == "escalated"
    assert fill_agent.advance_due(ctx) == []
    assert len(provider.sent) == before
    assert fill_agent.on_outreach_reply(ctx, first, first_offer, "accept").action == "offer_closed"
    assert fill.current_tranche == 2


def test_duplicate_yes_and_reordered_no_do_not_cancel_winner(session, clock, provider, offer_factory):
    volunteer, shift, fill, outreach, _ = offer_factory()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    assert fill_agent.on_outreach_reply(ctx, volunteer, outreach, "accept").action == "filled"
    before = len(provider.sent)
    assert fill_agent.on_outreach_reply(ctx, volunteer, outreach, "accept").action == "already_filled"
    assert fill_agent.on_outreach_reply(ctx, volunteer, outreach, "decline").action == "accepted_offer_requires_explicit_cancellation"
    assert len(provider.sent) == before
    assert session.scalar(select(m.Assignment)).status == "confirmed"
    assert outreach.response == "yes"


def test_expired_old_yes_does_not_bind_to_a_new_offer(session, clock, provider, offer_factory):
    volunteer, _, _, old, _ = offer_factory()
    clock.set_time(offers.metadata(session, old).expires_at)
    offers.close(session, old, "expired", clock.now())
    # Preserve the existing contact cooldown; do not introduce global penalties.
    clock.advance(timedelta(days=1))
    _, _, fill, current, result = offer_factory(volunteer=volunteer)
    assert result.sent
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    parser = parser_returning(intent="accept")
    response = handle_inbound(session, clock, provider, volunteer.phone, "YES", parser, ctx=ctx)
    assert response.routed_to == "clarify_offer"
    assert not session.scalars(select(m.Assignment)).all()
    pending = session.get(m.Notification, f"offer-reply:{volunteer.id}:local")
    assert pending.message_id is None and not provider.sent
    # Suppressed clarifiers cannot turn another bare YES into sender authority.
    again = handle_inbound(session, clock, provider, volunteer.phone, "YES", parser, ctx=ctx)
    assert again.routed_to == "clarify_offer" and current.response == "none"
    scoped = handle_inbound(session, clock, provider, volunteer.phone, f"YES R{current.id}", parser, ctx=ctx)
    assert scoped.notes == ["filled"] and current.response == "yes" and old.response == "expired"


def test_stop_closes_offer_and_continues_without_cancelling_assignment(session, clock, provider, offer_factory, make_shift, assign, make_volunteer):
    volunteer, shift, fill, outreach, _ = offer_factory()
    assignment = assign(volunteer, make_shift("Other", starts=clock.now()+timedelta(days=2)), status="confirmed")
    make_volunteer()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone, "STOP", parser_returning(), ctx=ctx)
    assert result.routed_to == "stop" and outreach.response == "blocked"
    assert assignment.status == "confirmed" and not volunteer.sms_opt_in
    fill_agent.advance_due(ctx)
    assert fill.current_tranche == 2


def test_material_shift_change_revokes_and_reissues(session, clock, provider, offer_factory, make_volunteer):
    volunteer, shift, fill, outreach, _ = offer_factory()
    make_volunteer()
    shift.event.starts_at += timedelta(hours=1)
    shift.event.ends_at += timedelta(hours=1)
    session.flush()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    assert fill_agent.advance_due(ctx)[0].action == "offer_blocked"
    assert outreach.response == "revoked" and fill.current_tranche == 2
    assert fill_agent.on_outreach_reply(ctx, volunteer, outreach, "accept").action == "offer_closed"


def test_unknown_legacy_deadline_fails_closed(session, clock, provider, offer_factory):
    volunteer, _, _, outreach, _ = offer_factory()
    session.delete(offers.metadata(session, outreach)); session.flush()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    assert fill_agent.on_outreach_reply(ctx, volunteer, outreach, "accept").action == "offer_closed"
    assert not session.scalars(select(m.Assignment)).all()


def test_dst_elapsed_time_and_fold_copy(session):
    denver = ZoneInfo("America/Denver")
    # 01:30 repeats on the fall-back day. Absolute lead time is 60 minutes.
    now = datetime(2026,11,1,1,30,tzinfo=denver,fold=0)
    start = datetime(2026,11,1,1,30,tzinfo=denver,fold=1)
    deadline = offers.deadline_for(session, start, now)
    assert deadline == now.astimezone(timezone.utc)+timedelta(minutes=10)
    assert "MDT (UTC-0600)" in offers.copy_with_deadline(session, "Serve?", deadline)
    spring_now = datetime(2027,3,14,1,30,tzinfo=denver)
    spring_start = datetime(2027,3,14,3,30,tzinfo=denver)
    assert offers.deadline_for(session, spring_start, spring_now) == spring_now.astimezone(timezone.utc)+timedelta(minutes=10)


def test_exact_review_refreshes_changed_dispatch_deadline(session, clock, provider, offer_factory):
    from app.core import confirmations
    volunteer, _, fill, outreach, result = offer_factory()
    message = session.get(m.Message, result.message_id)
    meta = offers.metadata(session, outreach)
    # A recorded legacy review/queue awaits its first dispatch, not a new send.
    meta.state = "offer_review"
    meta.detail = {k:v for k,v in meta.detail.items() if k != "dispatched_at"}
    message.status = "queued"
    old = confirmations.stage(session, clock.now(), {"phone":volunteer.phone,
        "volunteer_id":volunteer.id, "purpose":"outreach", "body":message.body, "kind":"ai",
        "outreach_id":outreach.id, "fill_request_id":fill.id, "transport":"mock_or_twilio"})
    old_body, digest = old.payload["body"], old.payload["content_hash"]
    clock.advance(timedelta(minutes=10))
    issue = offers.dispatch(session, outreach, message, offers.decision_time(session, clock), exact=True)
    assert "fresh exact review" in issue and message.status == "blocked_confirmation"
    assert message.body == old_body and old.payload["content_hash"] == digest
    assert meta.body != old_body and meta.state == "offer_review"
    assert meta.expires_at == offers.deadline_for(session, session.get(m.Shift, fill.shift_id).event.starts_at, clock.now())
    assert len(session.scalars(select(m.Escalation)).all()) == 1 and not provider.sent


def test_response_policy_has_one_visible_second_boundary(session, clock):
    when = clock.now()+timedelta(microseconds=123456)
    deadline = offers.deadline_for(session, when+timedelta(hours=6), when)
    assert deadline.microsecond == 0
    assert deadline-when <= timedelta(hours=1)


def test_delayed_role_approval_uses_dispatch_time(session, clock, provider, make_volunteer, make_shift):
    volunteer = make_volunteer()
    shift = make_shift(fill_policy="needs_approval", starts=clock.now()+timedelta(hours=6))
    fill = m.FillRequest(shift_id=shift.id, urgency="normal", state="in_progress", current_tranche=1, created_at=clock.now())
    session.add(fill); session.flush()
    outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=volunteer.id, tranche=1)
    session.add(outreach); session.flush()
    meta = offers.prepare(session, outreach, "Could you serve?", clock.now())
    # Existing restricted-role human approval, retained as a source receipt.
    approval = m.Approval(kind="send_outreach", status="pending", requested_at=clock.now(),
        payload={"outreach_id":outreach.id,"fill_request_id":fill.id,"volunteer_id":volunteer.id,
                 "phone":volunteer.phone,"role_id":shift.role_id,"body":meta.body})
    session.add(approval);session.flush()
    fill.state = "waiting_approval"
    clock.advance(timedelta(minutes=20))
    approval.status = "approved"
    message = m.Message(direction="out",volunteer_id=volunteer.id,phone=volunteer.phone,
        body=meta.body,purpose="outreach",kind="ai",status="sent",created_at=clock.now())
    session.add(message);session.flush()
    assert offers.dispatch(session, outreach, message, offers.decision_time(session, clock)) is None
    outreach.message_id = message.id
    assert not provider.sent
    assert meta.detail["dispatched_at"] == clock.now().astimezone(timezone.utc).isoformat()
    assert meta.expires_at == clock.now()+timedelta(minutes=56, seconds=40)


def test_volunteer_is_not_offered_two_vacancies(session, clock, provider, offer_factory):
    volunteer, _, _, first, _ = offer_factory()
    _, _, _, second, result = offer_factory(volunteer=volunteer)
    assert result.status == SendStatus.BLOCKED_ELIGIBILITY
    assert second.message_id is None and offers.metadata(session, first).state == "offer_active"
    assert len(session.scalars(select(m.Message)).all()) == 1 and not provider.sent


def test_offer_cancellation_clarifies_existing_commitment(session, clock, provider, offer_factory, make_shift, assign):
    volunteer, _, _, outreach, _ = offer_factory()
    assignment = assign(volunteer, make_shift("Other", starts=clock.now()+timedelta(days=2)), status="confirmed")
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone, "I can't make it", parser_returning(intent="cancel"), ctx=ctx)
    assert result.routed_to == "clarify_commitment"
    assert assignment.status == "confirmed" and outreach.response == "none"


def test_synchronous_unknown_transport_is_held_for_reconciliation(session, clock, provider, offer_factory):
    class UnknownTransport:
        def send(self, *args):
            pytest.fail("A recorded uncertain attempt must never be retried")
    volunteer, shift, fill, outreach, result = offer_factory()
    message = session.get(m.Message, result.message_id)
    message.status = "uncertain"
    meta = offers.metadata(session,outreach)
    meta.state, fill.state = "offer_uncertain", "escalated"
    offers.task_once(session,fill,clock.now(),"Historical delivery needs reconciliation")
    session.commit();session.expire_all()
    ctx = FillContext(session,clock,UnknownTransport(),ScriptedAgentGloo())
    assert fill_agent.advance_due(ctx) == []
    assert offers.problem(session,outreach,clock.now()) == "offer is closed"
    assert offers.delivery_hold(session,volunteer_id=volunteer.id,shift_id=shift.id)
    assert len(session.scalars(select(m.Escalation)).all()) == 1
    assert len(session.scalars(select(m.Outreach)).all()) == 1 and not provider.sent


def test_cancellation_hint_for_invitation_never_cancels_another_day(session, clock, provider, offer_factory, make_shift, assign):
    volunteer, shift, _, outreach, _ = offer_factory()
    booking = assign(volunteer,make_shift("Other",starts=clock.now()+timedelta(days=2)),status="confirmed")
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    result = handle_inbound(session,clock,provider,volunteer.phone,"Can't make Thursday",parser_returning(intent="cancel",shift_hint="Thursday"),ctx=ctx)
    assert result.routed_to == "clarify_commitment" and booking.status == "confirmed"
    assert outreach.response == "none"


def test_explicit_role_and_day_scope_a_natural_reply(session, clock, provider, offer_factory):
    volunteer, shift, _, old, _ = offer_factory()
    offers.close(session,old,"expired",clock.now())
    clock.advance(timedelta(days=1))
    _, new_shift, _, current, result = offer_factory(volunteer=volunteer)
    assert result.sent
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    response = handle_inbound(session,clock,provider,volunteer.phone,"Yes, usher on Friday",parser_returning(intent="accept",shift_hint="usher Friday"),ctx=ctx)
    assert response.routed_to == "fill_agent" and current.response == "yes" and old.response == "expired"


def test_unknown_delivery_holds_sender_even_after_contact_cooldown(session, clock, provider, offer_factory):
    volunteer, _, fill, outreach, _ = offer_factory()
    meta = offers.metadata(session,outreach)
    meta.state,fill.state = "offer_uncertain","escalated"
    clock.advance(timedelta(days=2))
    _, _, _, other, result = offer_factory(volunteer=volunteer)
    assert result.status == SendStatus.BLOCKED_ELIGIBILITY and other.message_id is None


@pytest.mark.parametrize("reply,intent", [("YES","accept"),("NO","decline"),("Sure, I can help","confirm"),("No sorry","decline")])
def test_commitment_question_persists_and_cannot_be_answered_with_general_yes_no(
        session, clock, provider, offer_factory, make_shift, assign, reply, intent):
    volunteer, _, _, outreach, _ = offer_factory()
    booking = assign(volunteer,make_shift("Other",starts=clock.now()+timedelta(days=2)),status="confirmed")
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    asked = handle_inbound(session,clock,provider,volunteer.phone,"Can't make it",parser_returning(intent="cancel"),ctx=ctx)
    assert asked.routed_to == "clarify_commitment"
    session.commit();session.expire_all()
    answer = handle_inbound(session,clock,provider,volunteer.phone,reply,parser_returning(intent=intent),ctx=ctx)
    assert answer.routed_to == "clarify_commitment"
    assert outreach.response == "none" and booking.status == "confirmed"
    assert len(session.scalars(select(m.Assignment)).all()) == 1


def test_commitment_question_can_explicitly_decline_only_its_invitation(session,clock,provider,offer_factory,make_shift,assign):
    volunteer, _, _, outreach, _ = offer_factory()
    booking = assign(volunteer,make_shift("Other",starts=clock.now()+timedelta(days=2)),status="confirmed")
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    handle_inbound(session,clock,provider,volunteer.phone,"Can't make it",parser_returning(intent="cancel"),ctx=ctx)
    answer = handle_inbound(session,clock,provider,volunteer.phone,"Decline the invitation",parser_returning(intent="decline"),ctx=ctx)
    assert answer.routed_to == "fill_agent" and outreach.response == "no"
    assert booking.status == "confirmed"


def test_held_commitment_question_also_blocks_bare_yes(session,clock,provider,offer_factory,make_shift,assign):
    volunteer, _, _, outreach, _ = offer_factory()
    booking = assign(volunteer,make_shift("Other",starts=clock.now()+timedelta(days=2)),status="confirmed")
    session.info["competition_confirmation_required"] = True
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    handle_inbound(session,clock,provider,volunteer.phone,"Can't make it",parser_returning(intent="cancel"),ctx=ctx)
    pending = session.scalar(select(m.Notification).where(m.Notification.purpose=="offer_reply_scope"))
    assert pending.message_id is None and pending.detail["approval_id"] is None
    assert pending.state == "commitment_scope" and not provider.sent
    answer = handle_inbound(session,clock,provider,volunteer.phone,"YES",parser_returning(intent="accept"),ctx=ctx)
    assert answer.routed_to == "clarify_commitment" and outreach.response == "none"
    assert booking.status == "confirmed"


@pytest.mark.parametrize("days", [15,90,365])
def test_old_reply_beyond_lookback_requires_new_offer_clarification(session,clock,provider,offer_factory,days):
    volunteer, _, _, old, _ = offer_factory()
    offers.close(session,old,"expired",clock.now())
    clock.advance(timedelta(days=days))
    _, _, _, current, result = offer_factory(volunteer=volunteer)
    assert result.sent
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    answer = handle_inbound(session,clock,provider,volunteer.phone,"YES",parser_returning(intent="accept"),ctx=ctx)
    assert answer.routed_to == "clarify_offer" and current.response == "none"
    assert not session.scalars(select(m.Assignment)).all()
    assert handle_inbound(session,clock,provider,volunteer.phone,"YES",parser_returning(intent="accept"),ctx=ctx).routed_to == "clarify_offer"
    clarified = handle_inbound(session,clock,provider,volunteer.phone,f"YES R{current.id}",parser_returning(intent="accept"),ctx=ctx)
    assert clarified.notes == ["filled"] and old.response == "expired"


def test_old_session_offer_is_a_metadata_only_ambiguity_guard(session,clock,provider,offer_factory):
    from app.integrations.test_sessions import TestSession
    volunteer, _, _, old, _ = offer_factory()
    offers.close(session,old,"expired",clock.now())
    old_message = session.get(m.Message,old.message_id)
    old_message.body = "Private old-session body must not be repeated"
    clock.advance(timedelta(days=30))
    _, _, _, current, result = offer_factory(volunteer=volunteer)
    selected = TestSession('a'*32,clock.now()-timedelta(minutes=1),clock.now()+timedelta(hours=1))
    session.get(m.Message,current.message_id).provider_sid = selected.outbound_prefix+"synthetic"
    session.info["mac_test_session"] = selected
    ctx = FillContext(session,clock,provider,ScriptedAgentGloo())
    answer = handle_inbound(session,clock,provider,volunteer.phone,"YES",parser_returning(intent="accept"),ctx=ctx)
    assert answer.routed_to == "clarify_offer" and current.response == "none"
    scope = session.get(m.Notification, f"offer-reply:{volunteer.id}:{selected.id}")
    assert scope.message_id is None and not provider.sent
    assert "Private old-session" not in scope.body and "Private old-session" not in str(scope.detail)
