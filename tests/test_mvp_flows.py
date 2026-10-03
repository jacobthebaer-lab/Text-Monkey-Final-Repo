"""MVP journeys: synthetic people, fake time, mocked delivery only."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from app.agents.fill_agent import FillContext, handle_cancellation
from app.config import Settings
from app.core.inbound import handle_inbound
from app.core.notifications import queue_staffing, flush_due, staffing_snapshot, staffing_snapshots
from app.core.send_gate import SendGate
from app.core import eligibility
from app.db import models as m
from app.jobs import process_due_fill_requests
from tests.conftest import NOW
from tests.test_fill_agent import ScriptedAgentGloo, parser_returning


class ProfileGloo:
    settings = Settings()

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.reply_calls = []

    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        if 'approved_message' in facts:
            self.reply_calls.append(kwargs)
            return SimpleNamespace(output_text=facts['approved_message'])
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(next(self.responses)))


def test_staffing_counts_required_roles_even_before_their_slots_exist(
    session, make_shift, make_volunteer, assign
):
    shift = make_shift('Greeter')
    assign(make_volunteer(), shift)
    event_type = m.EventType(name='Synthetic required-role service', title_patterns=[])
    missing_role = m.Role(name='Synthetic check-in', ministry='Welcome',
        required_qualifications=[], criticality='standard', fill_policy='auto')
    session.add_all([event_type, missing_role]); session.flush()
    shift.event.event_type_id = event_type.id
    session.add_all([
        m.RoleRecipe(event_type_id=event_type.id, role_id=shift.role_id, count=1),
        m.RoleRecipe(event_type_id=event_type.id, role_id=missing_role.id, count=2),
    ])
    other = make_shift('Coffee', starts=NOW+timedelta(days=3))
    session.flush()
    first, second = staffing_snapshots(session, [shift.event, other.event])
    assert first['covered'] == 1 and first['required'] == 3
    assert not first['fully_staffed']
    assert first['gaps'] == [{'role': 'Synthetic check-in', 'open': 2}]
    assert second['covered'] == 0 and second['required'] == 1
    assert staffing_snapshot(session, shift.event) == first


def inbound(ctx, person, text, parser=None, signup=False):
    return handle_inbound(ctx.session, ctx.clock, ctx.provider, getattr(person, 'phone', person), text,
                          parser or parser_returning(intent='confirm'), ctx=ctx, allow_signup=signup)


def setup_fill(session, clock, provider, make_volunteer, make_shift, assign, count=3):
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    original = make_volunteer('Synthetic Original')
    shift = make_shift('Greeter')
    assign(original, shift)
    helpers = [make_volunteer(f'Synthetic Helper {i}') for i in range(count)]
    inbound(ctx, original, "I can't serve", parser_returning(intent='cancel'))
    fill = session.scalar(select(m.FillRequest))
    return ctx, original, shift, helpers, fill


def test_full_text_signup_profile_and_recurring_availability(session, clock, provider, make_shift):
    shift = make_shift('Greeter')
    other = make_shift('Sound', starts=NOW+timedelta(days=2))
    session.add(m.Policy(key='full_text_onboarding', value={'value': True}))
    gloo = ProfileGloo([
        {'signup': True, 'first_name': 'Synthetic', 'last_name': 'Tester'},
        {'understood': True, 'role_ids': [shift.role_id], 'any_role': False},
        {'understood': True, 'weekdays': [6], 'preferred_services': ['sun_9'], 'max_per_month': 2,
         'available_dates': [], 'unavailable_dates': ['2026-10-18']},
    ])
    ctx = FillContext(session, clock, provider, gloo)
    phone = '+12025550190'
    assert inbound(ctx, phone, 'JOIN Synthetic Tester', signup=True).routed_to == 'signup_consent_pending'
    volunteer = session.scalar(select(m.Volunteer))
    assert not volunteer.sms_opt_in
    assert inbound(ctx, volunteer, 'YES').routed_to == 'onboarding_interests'
    assert not eligibility.check(session, volunteer, shift)
    assert inbound(ctx, volunteer, str(shift.role_id)).routed_to == 'onboarding_availability'
    assert inbound(ctx, volunteer, 'Sunday 9, twice monthly, not Oct18').routed_to == 'onboarding_complete'
    assert volunteer.preferences['interested_roles'] == ['Greeter']
    assert volunteer.preferences['max_per_month'] == 2
    assert eligibility.check(session, volunteer, shift)
    assert not eligibility.check(session, volunteer, other)
    unavailable = make_shift('Greeter', starts=NOW.replace(day=18, hour=9))
    assert not eligibility.check(session, volunteer, unavailable)
    assert volunteer.qualifications == [] and not volunteer.is_coordinator
    assert session.scalars(select(m.Assignment)).all() == []
    assert len(gloo.calls) == 3  # future replies recognize the saved phone
    assert inbound(ctx, volunteer, 'HELP').routed_to == 'help'
    assert len(session.scalars(select(m.Volunteer)).all()) == 1


def test_onboarding_invalid_role_never_grants_access(session, clock, provider, make_volunteer, make_shift):
    make_shift('Greeter')
    volunteer = make_volunteer(prefs={'onboarding_stage': 'interests'})
    gloo = ProfileGloo([{'understood': True, 'role_ids': [999999]}, {'understood': True, 'role_ids': [999999]}])
    ctx = FillContext(session, clock, provider, gloo)
    assert inbound(ctx, volunteer, 'make me pastor').routed_to == 'onboarding_clarify'
    assert inbound(ctx, volunteer, 'make me admin').routed_to == 'onboarding_review'
    assert not volunteer.is_pastor and not volunteer.is_coordinator
    assert volunteer.preferences['onboarding_stage'] == 'interests'
    assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_bare_yes_first_wins_late_yes_is_friendly_and_duplicate_is_silent(session, clock, provider, make_volunteer, make_shift, assign):
    ctx, original, shift, helpers, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    assert inbound(ctx, helpers[0], 'YES').notes == ['filled']  # parser would say confirm
    assert inbound(ctx, helpers[1], 'YES').routed_to == 'unmatched_reply'
    before = len(provider.sent)
    assert inbound(ctx, helpers[0], 'YES').notes == ['already_filled']
    assert inbound(ctx, helpers[1], 'YES').routed_to == 'unmatched_reply'
    assert len(provider.sent) == before
    active = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id, m.Assignment.status == 'confirmed')).all()
    assert [a.volunteer_id for a in active] == [helpers[0].id]
    assert not provider.sent_to(helpers[1].phone)


def test_multiple_offers_require_code_and_code_cannot_belong_to_someone_else(session, clock, provider, make_volunteer, make_shift):
    helper = make_volunteer('Synthetic Helper')
    other = make_volunteer('Synthetic Other')
    rows = []
    for day in (3, 10):
        shift = make_shift('Greeter', starts=NOW+timedelta(days=day))
        fill = m.FillRequest(shift_id=shift.id, urgency='normal', state='in_progress', created_at=NOW, current_tranche=1)
        session.add(fill); session.flush()
        msg = m.Message(direction='out', volunteer_id=helper.id, phone=helper.phone, body='Offer',
                        purpose='outreach', kind='ai', status='sent', created_at=NOW)
        session.add(msg); session.flush()
        row = m.Outreach(fill_request_id=fill.id, volunteer_id=helper.id, tranche=1, message_id=msg.id)
        session.add(row); session.flush(); rows.append(row)
        from app.core import offer_windows as offers
        offers.prepare(session, row, "Synthetic invitation", clock.now())
        offers.dispatch(session, row, msg, clock.now())
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    assert inbound(ctx, helper, 'YES').routed_to == 'clarify_offer'
    assert session.scalars(select(m.Assignment)).all() == []
    assert inbound(ctx, other, f'YES R{rows[0].id}').routed_to == 'unmatched_reply'
    assert inbound(ctx, helper, f'YES R{rows[0].id}').notes == ['filled']
    assert session.get(m.FillRequest, rows[1].fill_request_id).state == 'in_progress'


def test_unsent_invitation_cannot_book_a_slot(session, clock, provider, make_volunteer, make_shift):
    vol, shift = make_volunteer(), make_shift('Greeter')
    fill = m.FillRequest(shift_id=shift.id, urgency='normal', state='waiting_approval', created_at=NOW)
    session.add(fill); session.flush()
    row = m.Outreach(fill_request_id=fill.id, volunteer_id=vol.id, tranche=1)
    session.add(row); session.flush()
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    assert inbound(ctx, vol, f'YES R{row.id}').routed_to == 'unmatched_reply'
    assert not session.scalars(select(m.Assignment)).all()


def test_quiet_cancellation_replies_immediately_but_outreach_waits(session, clock, provider, make_volunteer, make_shift, assign):
    clock.set_time(NOW.replace(hour=22))
    ctx, original, shift, helpers, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    assert provider.sent_to(original.phone)  # sender just requested cancellation
    assert fill.state == 'waiting_quiet' and fill.current_tranche == 0
    assert not any(provider.sent_to(h.phone) for h in helpers)
    clock.set_time(NOW.replace(day=2, hour=7))
    outcomes = process_due_fill_requests(ctx)
    assert outcomes[0].action == 'tranche_sent'
    assert len(session.scalars(select(m.Outreach)).all()) == 1


def test_first_yes_at_night_confirms_but_other_closures_defer_and_honor_stop(session, clock, provider, make_volunteer, make_shift, assign):
    clock.set_time(NOW.replace(hour=20, minute=30))
    ctx, _, _, helpers, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    clock.set_time(NOW.replace(hour=22))
    assert inbound(ctx, helpers[0], 'YES').notes == ['filled']
    assert any('confirmed' in x.body for x in provider.sent_to(helpers[0].phone))
    assert not any('filled' in x.body for x in provider.sent_to(helpers[1].phone))
    inbound(ctx, helpers[1], 'STOP')
    clock.set_time(NOW.replace(day=2, hour=7))
    flush_due(ctx)
    assert not any('filled' in x.body for x in provider.sent_to(helpers[1].phone))
    assert not provider.sent_to(helpers[2].phone)


def test_staffing_digest_coalesces_and_only_claims_full_coverage_when_all_slots_filled(session, clock, provider, make_volunteer, make_shift, assign):
    coordinator = make_volunteer(coordinator=True)
    ctx, _, shift, helpers, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    second = m.Shift(event_id=shift.event_id, role_id=shift.role_id, slot_index=2)
    session.add(second); session.flush()
    inbound(ctx, helpers[0], 'YES')
    assert provider.sent_to(coordinator.phone) == []
    clock.advance(timedelta(minutes=5)); flush_due(ctx)
    first = provider.sent_to(coordinator.phone)
    assert len(first) == 1 and 'Still needs cover' in first[0].body
    assign(helpers[1], second, status='confirmed')
    queue_staffing(ctx, shift.event)
    clock.advance(timedelta(minutes=5)); flush_due(ctx)
    assert len(provider.sent_to(coordinator.phone)) == 1
    clock.advance(timedelta(minutes=10)); flush_due(ctx)
    assert len(provider.sent_to(coordinator.phone)) == 2
    assert 'Fully staffed' in provider.sent_to(coordinator.phone)[-1].body
    queue_staffing(ctx, shift.event)
    clock.advance(timedelta(minutes=15)); flush_due(ctx)
    assert len(provider.sent_to(coordinator.phone)) == 2


def test_bound_batches_continue_past_third_batch_without_mass_broadcast(session, clock, provider, make_volunteer, make_shift, assign):
    ctx, _, _, _, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign, count=25)
    for expected in (2, 3, 4):
        clock.set_time(fill.next_action_at)
        process_due_fill_requests(ctx)
        if fill.state == 'waiting_quiet':
            clock.set_time(fill.next_action_at)
            process_due_fill_requests(ctx)
        assert fill.current_tranche == expected
        rows = session.scalars(select(m.Outreach).where(m.Outreach.tranche == expected)).all()
        assert len(rows) == 1
    ids = session.scalars(select(m.Outreach.volunteer_id)).all()
    assert len(ids) == len(set(ids)) == 4


def test_started_shift_rejects_a_yes(session, clock, provider, make_volunteer, make_shift, assign):
    ctx, _, shift, helpers, _ = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    clock.set_time(shift.event.starts_at)
    assert inbound(ctx, helpers[0], 'YES').notes == ['offer_closed']
    assert not session.scalars(select(m.Assignment).where(m.Assignment.status == 'confirmed')).all()


def test_restricted_approval_at_night_is_durable_and_sends_at_opening(session, clock, provider, make_volunteer, make_shift, assign):
    coordinator = make_volunteer(coordinator=True)
    original = make_volunteer('Synthetic Original')
    helper = make_volunteer('Synthetic Qualified', quals=[('safety', 'verified', None)])
    shift = make_shift('Restricted', required=('safety',), fill_policy='needs_approval')
    assign(original, shift)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    inbound(ctx, original, 'cannot make it', parser_returning(intent='cancel'))
    fill = session.scalar(select(m.FillRequest))
    clock.set_time(NOW.replace(hour=22))
    assert inbound(ctx, coordinator, 'YES').routed_to == 'approval'
    assert fill.state == 'waiting_approval'
    assert not provider.sent_to(helper.phone)
    clock.set_time(NOW.replace(day=2, hour=7))
    process_due_fill_requests(ctx)
    assert fill.state == 'in_progress'
    assert len(provider.sent_to(helper.phone)) == 1
    assert session.scalar(select(m.Outreach)).message_id
    process_due_fill_requests(ctx)
    assert len(provider.sent_to(helper.phone)) == 1


def test_original_deadline_does_not_slide_as_start_gets_closer(session, clock, provider, make_volunteer, make_shift, assign):
    from app.agents.fill_agent import escalation_deadline
    ctx, _, shift, _, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign, count=30)
    deadline = shift.event.starts_at-timedelta(minutes=10)
    assert escalation_deadline(fill, shift.event, session) == deadline
    clock.set_time(deadline)
    process_due_fill_requests(ctx)
    assert fill.state == 'escalated'
    assert fill.next_action_at is None
    count = len(provider.sent)
    process_due_fill_requests(ctx)
    assert len(provider.sent) == count


def test_unique_slot_constraint_rejects_another_write_path(session, make_volunteer, make_shift, assign):
    from sqlalchemy.exc import IntegrityError
    import pytest
    a, b = make_volunteer(), make_volunteer()
    shift = make_shift('Greeter')
    assign(a, shift, status='confirmed')
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            assign(b, shift, status='approved')
    assert len(session.scalars(select(m.Assignment)).all()) == 1


def test_admin_filling_slot_during_quiet_wait_prevents_unneeded_asks(session, clock, provider, make_volunteer, make_shift, assign):
    clock.set_time(NOW.replace(hour=22))
    ctx, _, shift, helpers, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    assign(helpers[0], shift, status='confirmed')
    clock.set_time(fill.next_action_at)
    process_due_fill_requests(ctx)
    assert fill.state == 'filled'
    assert not session.scalars(select(m.Outreach)).all()
    assert not any(provider.sent_to(h.phone) for h in helpers)


def test_a_late_yes_at_night_releases_only_that_senders_deferred_closure(session, clock, provider, make_volunteer, make_shift, assign):
    clock.set_time(NOW.replace(hour=20, minute=30))
    ctx, _, _, helpers, _ = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    clock.set_time(NOW.replace(hour=22))
    inbound(ctx, helpers[0], 'YES')
    assert not any('filled' in x.body for x in provider.sent_to(helpers[1].phone))
    assert inbound(ctx, helpers[1], 'YES').routed_to == 'unmatched_reply'
    assert not provider.sent_to(helpers[1].phone)
    assert not any('filled' in x.body for x in provider.sent_to(helpers[2].phone))


def test_natural_acceptance_classified_confirm_still_matches_an_invitation(session, clock, provider, make_volunteer, make_shift, assign):
    ctx, _, _, helpers, _ = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    assert inbound(ctx, helpers[0], 'Sure, I can help', parser_returning(intent='confirm')).notes == ['filled']


def test_gloo_reply_failure_preserves_assignment_and_retries_saved_notification(session, clock, provider, make_volunteer, make_shift, assign):
    from app.llm.gloo_client import GlooUnavailableError
    class UnavailableWriter:
        settings = Settings(gloo_signup_replies=True)
        def create_response(self, **kwargs):
            raise GlooUnavailableError('synthetic outage')
    ctx, _, shift, helpers, fill = setup_fill(session, clock, provider, make_volunteer, make_shift, assign)
    ctx.gloo = UnavailableWriter()
    assert inbound(ctx, helpers[0], 'YES').notes == ['filled']
    assert fill.state == 'filled'
    assert session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id, m.Assignment.status == 'confirmed'))
    pending = session.get(m.Notification, f'winner:{fill.id}:{helpers[0].id}')
    assert pending.state == 'pending' and pending.detail['gloo_attempts'] == 1
    clock.advance(timedelta(minutes=2)); flush_due(ctx)
    clock.advance(timedelta(minutes=2)); flush_due(ctx)
    assert pending.state == 'blocked'
    assert session.scalar(select(m.Escalation).where(m.Escalation.category == 'system_error'))


def test_onboarding_reads_only_the_requested_stage_wrapper(session, clock, provider, make_volunteer, make_shift):
    shift = make_shift('Greeter')
    volunteer = make_volunteer(prefs={'onboarding_stage': 'availability', 'interested_roles': ['Greeter']})
    gloo = ProfileGloo([{'interests': {'role_ids': [999]}, 'availability': {
        'understood': True, 'weekdays': [6], 'preferred_services': ['sun_9'], 'max_per_month': 2,
        'available_dates': [], 'unavailable_dates': []}}])
    ctx = FillContext(session, clock, provider, gloo)
    assert inbound(ctx, volunteer, 'Sundays at 9, twice a month').routed_to == 'onboarding_complete'
    assert volunteer.preferences['interested_roles'] == ['Greeter']
    assert eligibility.check(session, volunteer, shift)


def test_explicit_background_pause_disables_the_real_clock_scheduler(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import apscheduler.schedulers.background
    def forbidden_scheduler(*args, **kwargs):
        raise AssertionError('background scheduler must remain paused')
    monkeypatch.setattr(apscheduler.schedulers.background, 'BackgroundScheduler', forbidden_scheduler)
    application = create_app(Settings(database_url='sqlite://', demo_mode=False, automation_enabled=False))
    with TestClient(application) as client:
        assert client.get('/api/config').json()['automationEnabled'] is False
        assert client.get('/healthz').status_code == 200


def test_approval_rechecks_credentials_and_does_not_wait_on_unsent_asks(session, clock, provider, make_volunteer, make_shift, assign):
    coordinator = make_volunteer(coordinator=True)
    original = make_volunteer('Synthetic Original')
    helper = make_volunteer('Synthetic Qualified', quals=[('safety', 'verified', None)])
    shift = make_shift('Restricted', required=('safety',), fill_policy='needs_approval')
    assign(original, shift)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    inbound(ctx, original, 'cannot make it', parser_returning(intent='cancel'))
    helper.qualifications[0].status = 'expired'
    session.flush()
    response = inbound(ctx, coordinator, 'YES')
    assert any('blocked_eligibility' in note for note in response.notes)
    assert not provider.sent_to(helper.phone)
    fill = session.scalar(select(m.FillRequest))
    assert fill.state == 'in_progress' and fill.next_action_at == clock.now()
    process_due_fill_requests(ctx)
    assert fill.state == 'escalated'


def test_coordinator_status_limit_applies_across_different_events(session, clock, provider, make_volunteer, make_shift):
    coordinator = make_volunteer(coordinator=True)
    first = make_shift('Greeter')
    second = make_shift('Sound', starts=NOW+timedelta(days=6))
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    queue_staffing(ctx, first.event)
    queue_staffing(ctx, second.event)
    clock.advance(timedelta(minutes=5)); flush_due(ctx)
    assert len(provider.sent_to(coordinator.phone)) == 1
    clock.advance(timedelta(minutes=15)); flush_due(ctx)
    assert len(provider.sent_to(coordinator.phone)) == 2
