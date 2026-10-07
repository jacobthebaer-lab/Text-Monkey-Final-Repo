"""Adversarial control and recipient cases, synthetic data/providers only."""
import pytest
from datetime import timedelta
from sqlalchemy import select

from app.core import confirmations
from app.core.consent_controls import control_action
from app.core.inbound import handle_inbound
from app.core.send_gate import SendStatus
from app.db import models as m


@pytest.mark.parametrize('body', ['Please STOP!', 'Please unsubscribe.', 'Stop texting me now.',
                                "Please don't message me now.", 'Unsubscribe me',
                                'Please STOP, thanks.', 'Please unsubscribe because I moved.'])
@pytest.mark.parametrize('pending_signup', [False, True])
def test_clear_courteous_withdrawals_take_priority_over_schedule_parser(
    session, clock, provider, make_volunteer, body, pending_signup
):
    volunteer = make_volunteer(prefs={'signup_source': 'sms', 'consent_pending': pending_signup})
    def forbidden_parser(text):
        pytest.fail('Actual opt-out cannot reach schedule interpretation')
    result = handle_inbound(session,clock,provider,volunteer.phone,body,forbidden_parser)
    assert result.routed_to == 'stop'
    assert volunteer.sms_opt_in is False
    assert session.get(m.Policy,'sms_opt_out:'+volunteer.phone).value['value'] is True
    assert not provider.sent


@pytest.mark.parametrize('body', ["Please don't STOP", 'Please stop texting me about Production',
                                'If I say please STOP, what happens?', 'She said "Please STOP"',
                                'Please unsubscribe me from Production', "Stop texting me now, I didn't mean that.",
                                'Please STOP, only for Production.', 'Please STOP, if I ask you tomorrow.',
                                'Please unsubscribe because I moved, but keep texting me.',
                                'Please unsubscribe me, that was an example.'])
def test_mentions_negations_and_limited_role_requests_are_not_global_withdrawals(body):
    assert control_action(body) is None


@pytest.mark.parametrize('mode',[False,True])
def test_admin_source_cannot_be_sent_to_a_different_opted_out_volunteer(
    session,gate,provider,make_volunteer,mode
):
    session.info[confirmations.MODE_KEY]=mode
    admin=make_volunteer(coordinator=True)
    other=make_volunteer(opt_in=False)
    existing_approvals = set(session.scalars(select(m.Approval.id)))
    result=gate.send(body='Private coordinator coverage facts.',purpose='coordinator_notify',
                     volunteer=admin,phone=other.phone)
    assert result.status is SendStatus.BLOCKED_POLICY
    assert not provider.sent
    assert not list(session.scalars(select(m.Message)))
    assert set(session.scalars(select(m.Approval.id))) == existing_approvals


def test_legacy_approval_cannot_redirect_saved_volunteer_to_another_phone(
    session,gate,provider,make_volunteer
):
    admin=make_volunteer(coordinator=True)
    other=make_volunteer(opt_in=False)
    approval=m.Approval(kind='send_outreach',status='approved',requested_at=gate.clock.now(),
        payload={'volunteer_id':admin.id,'phone':other.phone,'body':'Private coordinator facts.',
                 'purpose':'coordinator_notify','kind':'template'})
    session.add(approval);session.flush()
    result=gate.send_approved(approval)
    assert result.status is SendStatus.BLOCKED_POLICY
    assert not provider.sent and not list(session.scalars(select(m.Message)))


def test_legacy_approval_with_missing_selected_identity_cannot_resolve_another_phone(
    session, gate, provider, make_volunteer
):
    other = make_volunteer(coordinator=True)
    approval = m.Approval(kind='send_outreach', status='approved', requested_at=gate.clock.now(),
        payload={'volunteer_id': other.id+100, 'phone': other.phone, 'body': 'Private coordinator facts.',
                 'purpose': 'coordinator_notify', 'kind': 'template'})
    session.add(approval)
    session.flush()
    assert gate.send_approved(approval).status is SendStatus.BLOCKED_POLICY
    assert not provider.sent and not list(session.scalars(select(m.Message)))


@pytest.mark.parametrize('selected_volunteer', [True, False])
def test_matching_phone_and_phone_only_resolve_same_consented_recipient(
    session, gate, provider, make_volunteer, selected_volunteer
):
    admin = make_volunteer(coordinator=True)
    result = gate.send(body='Coverage facts.', purpose='coordinator_notify',
        volunteer=admin if selected_volunteer else None, phone=admin.phone)
    assert result.sent
    sent = session.get(m.Message, result.message_id)
    assert sent.volunteer_id == admin.id and sent.phone == admin.phone
    assert len(provider.sent) == 1


@pytest.mark.parametrize('search_target', ['matching', 'unrelated_role', 'occupied_slot', 'duplicate_slot'])
def test_pre_event_no_action_requires_searches_for_each_actual_role_gap(
    session, clock, provider, make_volunteer, make_shift, assign, search_target
):
    from app.core.notifications import queue_pre_event_updates, flush_due
    from tests.test_pre_event_updates import context
    admin = make_volunteer(coordinator=True)
    gap = make_shift('Greeter', starts=clock.now()+timedelta(hours=2))
    target = gap
    if search_target in {'unrelated_role', 'occupied_slot'}:
        target = make_shift('Coffee' if search_target == 'unrelated_role' else 'Greeter',
            criticality='optional', starts=gap.starts_at)
        target.event_id = gap.event_id
        target.slot_index = 1
        if search_target == 'occupied_slot':
            assign(make_volunteer(), target)
    if search_target == 'duplicate_slot':
        extra = make_shift('Greeter', starts=gap.starts_at)
        extra.event_id = gap.event_id
        extra.slot_index = 1
    for _ in range(2 if search_target == 'duplicate_slot' else 1):
        session.add(m.FillRequest(shift_id=target.id, state='in_progress', urgency='high',
            created_at=clock.now()))
    session.flush()
    ctx = context(session, clock, provider)
    queue_pre_event_updates(ctx)
    flush_due(ctx)
    body = provider.sent_to(admin.phone)[0].body
    assert 'Greeter' in body
    assert ('No action needed while those searches continue.' in body) is (search_target == 'matching')


@pytest.mark.parametrize('mode', [False, True])
def test_tomorrow_cancel_uses_original_local_input_day_across_midnight(
    session, clock, provider, make_volunteer, make_shift, assign, mode
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_opportunities_reply import ExactGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    clock.set_time(clock.now().replace(hour=23, minute=59))
    target = assign(volunteer, make_shift('Greeter', starts=clock.now()+timedelta(hours=10)))
    other = assign(volunteer, make_shift('Greeter', starts=clock.now()+timedelta(days=7)))
    session.info[confirmations.MODE_KEY] = mode
    calls = []
    def parser(body):
        calls.append(body)
        clock.advance(timedelta(minutes=2))
        return ParsedMessage(intent='cancel', confidence=.99, shift_hint='wrong-model-hint')
    result = handle_inbound(session, clock, provider, volunteer.phone, "I can't make tomorrow",
        parser, ctx=FillContext(session, clock, provider, ExactGloo()))
    assert result.routed_to == 'fill_agent' and target.status == 'cancelled'
    assert other.status == 'approved' and len(calls) == 1
    assert session.scalar(select(m.FillRequest)).cancelled_assignment_id == target.id


def test_tomorrow_with_two_roles_still_requires_exact_booking(
    session, clock, provider, make_volunteer, make_shift, assign
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_opportunities_reply import ExactGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    rows = [assign(volunteer, make_shift(role, starts=clock.now()+timedelta(days=1)))
            for role in ('Greeter', 'Coffee')]
    result = handle_inbound(session, clock, provider, volunteer.phone, "I can't make tomorrow",
        lambda _: ParsedMessage(intent='cancel', confidence=.99, shift_hint='Greeter'),
        ctx=FillContext(session, clock, provider, ExactGloo()))
    assert result.routed_to == 'cancellation_review'
    assert all(row.status == 'approved' for row in rows)
    assert not session.scalar(select(m.FillRequest))


def test_conflicting_weekday_and_tomorrow_do_not_select_a_booking(
    session, clock, provider, make_volunteer, make_shift, assign
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_opportunities_reply import ExactGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    row = assign(volunteer, make_shift('Greeter', starts=clock.now()+timedelta(days=1)))
    result = handle_inbound(session, clock, provider, volunteer.phone, 'Cancel Greeter Sunday tomorrow',
        lambda _: ParsedMessage(intent='cancel', confidence=.99, shift_hint='tomorrow'),
        ctx=FillContext(session, clock, provider, ExactGloo()))
    assert result.routed_to == 'cancellation_review' and row.status == 'approved'
    assert not session.scalar(select(m.FillRequest))


@pytest.mark.parametrize('mode', [False, True])
def test_month_absence_persists_all_dates_without_cancelling_unrelated_booking(
    session, clock, provider, make_volunteer, make_shift, assign, mode
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_opportunities_reply import ExactGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'max_per_month': 2})
    booking = assign(volunteer, make_shift('Greeter'))
    session.info[confirmations.MODE_KEY] = mode
    calls = []
    def parser(body):
        calls.append(body)
        return ParsedMessage(intent='availability', confidence=.99, dates=['December'])
    body = 'I cannot serve any open shifts in December'
    result = handle_inbound(session, clock, provider, volunteer.phone, body, parser,
        ctx=FillContext(session, clock, provider, ExactGloo()))
    assert result.routed_to == 'availability' and calls == [body]
    assert booking.status == 'approved' and not session.scalar(select(m.FillRequest))
    unavailable = session.scalar(select(m.Availability))
    assert unavailable.month == '2026-12'
    assert unavailable.unavailable_dates == [f'2026-12-{day:02}' for day in range(1, 32)]
    request = volunteer.preferences['serving_requests'][0]
    assert request['status'] == 'recorded_unavailable' and request['text'] == body
    assert session.get(m.Notification, f'cancellation-scope:{volunteer.id}') is None
    assert volunteer.preferences['max_per_month'] == 2


@pytest.mark.parametrize('event_status', ['cancelled', 'completed'])
def test_chronic_gap_evidence_requires_services_that_were_not_cancelled(
    session, clock, provider, make_shift, tmp_path, event_status
):
    from app.agents.fill_agent import FillContext
    from app.agents.capacity_agent import scan
    shifts = [make_shift('Greeter', starts=clock.now()-timedelta(days=days)) for days in (7, 14, 21)]
    for shift in shifts:
        shift.event.status = event_status
    flags = scan(FillContext(session, clock, provider, None, log_dir=tmp_path))
    gaps = [flag for flag in flags if flag.type == 'chronic_gap']
    assert bool(gaps) is (event_status == 'completed')
    if gaps:
        assert set(gaps[0].evidence['shift_ids']) == {shift.id for shift in shifts}
    assert not provider.sent


@pytest.mark.parametrize('mode', [False, True])
def test_natural_confirmation_accepts_only_a_current_delivered_invitation(
    session, clock, provider, make_volunteer, make_shift, mode
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_fill_agent import historical_invitation, ScriptedAgentGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    shift = make_shift('Greeter')
    fill = m.FillRequest(shift_id=shift.id, state='in_progress', urgency='normal', created_at=clock.now())
    session.add(fill)
    session.flush()
    offer = historical_invitation(session, clock, volunteer, fill)
    session.info[confirmations.MODE_KEY] = mode
    result = handle_inbound(session, clock, provider, volunteer.phone, 'Sure, I can help',
        lambda _: ParsedMessage(intent='confirm', confidence=.99),
        ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert result.routed_to == 'fill_agent' and result.notes == ['filled']
    assert offer.response == 'yes' and fill.state == 'filled'
    booked = session.scalar(select(m.Assignment))
    assert booked.volunteer_id == volunteer.id and booked.shift_id == shift.id


@pytest.mark.parametrize('mode', [False, True])
def test_natural_help_without_an_offer_does_not_confirm_a_booking(
    session, clock, provider, make_volunteer, make_shift, assign, mode
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_opportunities_reply import ExactGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    booking = assign(volunteer, make_shift('Greeter'))
    session.info[confirmations.MODE_KEY] = mode
    handle_inbound(session, clock, provider, volunteer.phone, 'Sure, I can help',
        lambda _: ParsedMessage(intent='confirm', confidence=.99),
        ctx=FillContext(session, clock, provider, ExactGloo()))
    assert booking.status == 'approved'
    assert not session.scalar(select(m.FillRequest))


def test_ambiguous_number_gets_its_own_factual_reply_without_replacing_cancel_source(
    session, clock, provider, make_volunteer, make_shift, assign
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_opportunities_reply import ExactGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    bookings = [assign(volunteer, make_shift('Greeter', starts=clock.now()+timedelta(days=days)))
                for days in (3, 10)]
    gloo = ExactGloo()
    ctx = FillContext(session, clock, provider, gloo)
    parser = lambda _: ParsedMessage(intent='cancel', confidence=.99)
    assert handle_inbound(session, clock, provider, volunteer.phone, "I can't make it", parser, ctx=ctx).routed_to == 'cancellation_review'
    hold = session.get(m.Notification, f'cancellation-scope:{volunteer.id}')
    original_id = hold.detail['source_message_id']
    assert handle_inbound(session, clock, provider, volunteer.phone, '2', parser, ctx=ctx).routed_to == 'cancellation_review'
    assert hold.detail['source_message_id'] == original_id
    assert all(booking.status == 'approved' for booking in bookings)
    assert not session.scalar(select(m.FillRequest))
    assert len(provider.sent) == len(gloo.calls) == 2
    assert all('No schedule changes have been made.' in message.body for message in provider.sent)
    from app.core.cancellation_reply import reply
    from app.core.send_gate import SendGate
    from app.core.notifications import flush_due
    latest = session.scalar(select(m.Message).where(m.Message.direction == 'in').order_by(m.Message.id.desc()))
    gate = SendGate(session, clock, provider)
    gate.reply_to_message_id = latest.id
    reply(session, clock, gate, volunteer)
    flush_due(ctx)
    assert len(provider.sent) == len(gloo.calls) == 2


@pytest.mark.parametrize('change', ['expired', 'queued', 'prior_without_scope', 'negated', 'conditional', 'question'])
def test_natural_confirmation_does_not_accept_stale_or_ambiguous_invitation(
    session, clock, provider, make_volunteer, make_shift, change
):
    from app.agents.fill_agent import FillContext
    from app.core import offer_windows
    from app.llm.parser import ParsedMessage
    from tests.test_fill_agent import historical_invitation, ScriptedAgentGloo
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    shift = make_shift('Greeter')
    fill = m.FillRequest(shift_id=shift.id, state='in_progress', urgency='normal', created_at=clock.now())
    session.add(fill)
    session.flush()
    offer = historical_invitation(session, clock, volunteer, fill)
    body = 'Sure, I can help'
    if change == 'expired':
        clock.set_time(offer_windows.metadata(session, offer).expires_at)
    elif change == 'queued':
        session.get(m.Message, offer.message_id).status = 'queued'
    elif change == 'prior_without_scope':
        offer.response = 'expired'
        offer_windows.metadata(session, offer).expires_at = clock.now()-timedelta(seconds=1)
        other = make_shift('Coffee')
        second = m.FillRequest(shift_id=other.id, state='in_progress', urgency='normal', created_at=clock.now())
        session.add(second)
        session.flush()
        historical_invitation(session, clock, volunteer, second)
    elif change == 'negated':
        body = "Sure, I can't help"
    elif change == 'conditional':
        body = 'Sure, I can help if my meeting ends'
    elif change == 'question':
        body = 'Sure, I can help?'
    session.info[confirmations.MODE_KEY] = True
    handle_inbound(session, clock, provider, volunteer.phone, body,
        lambda _: ParsedMessage(intent='confirm', confidence=.99, shift_hint='Greeter'),
        ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert fill.state != 'filled' and not session.scalar(select(m.Assignment))


@pytest.mark.parametrize('route', ['natural', 'explicit', 'direct'])
@pytest.mark.parametrize('change', ['uncertain', 'dispatching', 'wrong_phone', 'wrong_person',
                                  'wrong_body', 'wrong_purpose', 'wrong_metadata_source', 'wrong_metadata_person'])
def test_reply_never_books_from_unverified_or_changed_delivered_source(
    session, clock, provider, make_volunteer, make_shift, route, change
):
    from sqlalchemy import update
    from app.agents.fill_agent import FillContext, on_outreach_reply
    from app.core import offer_windows
    from app.llm.parser import ParsedMessage
    from tests.test_fill_agent import historical_invitation, ScriptedAgentGloo
    person = make_volunteer(prefs={'onboarding_stage': 'complete'})
    other = make_volunteer()
    shift = make_shift('Greeter')
    fill = m.FillRequest(shift_id=shift.id, state='in_progress', urgency='normal', created_at=clock.now())
    session.add(fill)
    session.flush()
    offer = historical_invitation(session, clock, person, fill)
    message = session.get(m.Message, offer.message_id)
    meta = offer_windows.metadata(session, offer)
    message_values = {'uncertain': {'status': 'uncertain'}, 'dispatching': {'status': 'dispatching'},
        'wrong_phone': {'phone': other.phone}, 'wrong_person': {'volunteer_id': other.id},
        'wrong_body': {'body': 'Changed source copy.'}, 'wrong_purpose': {'purpose': 'signup_reply'}}
    if change in message_values:
        # Persist the changed source without refreshing the cached Message.
        # The final decision must read DB evidence, not this identity-map copy.
        session.execute(update(m.Message).where(m.Message.id == message.id)
                        .values(**message_values[change]).execution_options(synchronize_session=False))
        assert message.status == 'sent' and message.phone == person.phone
    else:
        values = {'message_id': None} if change == 'wrong_metadata_source' else {'volunteer_id': other.id}
        session.execute(update(m.Notification).where(m.Notification.key == meta.key)
                        .values(**values).execution_options(synchronize_session=False))
        assert meta.message_id == message.id and meta.volunteer_id == person.id
    session.info[confirmations.MODE_KEY] = True
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    if route == 'direct':
        assert on_outreach_reply(ctx, person, offer, 'accept').action == 'offer_closed'
    else:
        body = 'Sure, I can help' if route == 'natural' else f'YES R{offer.id}'
        handle_inbound(session, clock, provider, person.phone, body,
            lambda _: ParsedMessage(intent='confirm', confidence=.99, shift_hint='Greeter'), ctx=ctx)
    assert not session.scalar(select(m.Assignment))
    assert offer.response != 'yes' and fill.state != 'filled'


@pytest.mark.parametrize('status', ['sent', 'submitted', 'delivered'])
def test_natural_reply_accepts_matching_finalized_dispatch_evidence(
    session, clock, provider, make_volunteer, make_shift, status
):
    from app.agents.fill_agent import FillContext
    from app.llm.parser import ParsedMessage
    from tests.test_fill_agent import historical_invitation, ScriptedAgentGloo
    person = make_volunteer(prefs={'onboarding_stage': 'complete'})
    shift = make_shift('Greeter')
    fill = m.FillRequest(shift_id=shift.id, state='in_progress', urgency='normal', created_at=clock.now())
    session.add(fill)
    session.flush()
    offer = historical_invitation(session, clock, person, fill)
    session.get(m.Message, offer.message_id).status = status
    session.flush()
    session.info[confirmations.MODE_KEY] = True
    result = handle_inbound(session, clock, provider, person.phone, 'Sure, I can help',
        lambda _: ParsedMessage(intent='confirm', confidence=.99),
        ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert result.notes == ['filled']
    assert session.scalar(select(m.Assignment)).volunteer_id == person.id


def test_native_dispatch_preflight_does_not_treat_inflight_source_as_a_reply(
    session, clock, provider, make_volunteer, make_shift
):
    from app.core import offer_windows
    from tests.test_fill_agent import historical_invitation
    person = make_volunteer()
    fill = m.FillRequest(shift_id=make_shift('Greeter').id, state='in_progress',
        urgency='normal', created_at=clock.now())
    session.add(fill)
    session.flush()
    offer = historical_invitation(session, clock, person, fill)
    session.get(m.Message, offer.message_id).status = 'dispatching'
    session.flush()
    assert offer_windows.problem(session, offer, clock.now()) is None
    assert offer_windows.reply_source_problem(session, offer, clock.now()) is not None


@pytest.mark.parametrize('body', ['Sure, I can help', 'YES'])
@pytest.mark.parametrize('change', ['uncertain', 'wrong_phone'])
def test_reply_rechecks_source_after_matching_and_acquiring_decision_locks(
    session, clock, provider, make_volunteer, make_shift, monkeypatch, body, change
):
    from sqlalchemy import update
    from app.agents.fill_agent import FillContext
    from app.core import offer_windows
    from app.llm.parser import ParsedMessage
    from tests.test_fill_agent import historical_invitation, ScriptedAgentGloo
    person = make_volunteer(prefs={'onboarding_stage': 'complete'})
    other = make_volunteer()
    fill = m.FillRequest(shift_id=make_shift('Greeter').id, state='in_progress',
        urgency='normal', created_at=clock.now())
    session.add(fill)
    session.flush()
    offer = historical_invitation(session, clock, person, fill)
    original_lock = offer_windows.lock
    decisions = []
    def changed_source_at_lock(s, outreach):
        result = original_lock(s, outreach)
        values = {'status': 'uncertain'} if change == 'uncertain' else {'phone': other.phone}
        s.execute(update(m.Message).where(m.Message.id == offer.message_id).values(**values)
                  .execution_options(synchronize_session=False))
        decisions.append(outreach.id)
        return result
    monkeypatch.setattr(offer_windows, 'lock', changed_source_at_lock)
    session.info[confirmations.MODE_KEY] = True
    handle_inbound(session, clock, provider, person.phone, body,
        lambda _: ParsedMessage(intent='confirm', confidence=.99),
        ctx=FillContext(session, clock, provider, ScriptedAgentGloo()))
    assert decisions == [offer.id]
    assert not session.scalar(select(m.Assignment))
    assert offer.response != 'yes' and fill.state != 'filled'
