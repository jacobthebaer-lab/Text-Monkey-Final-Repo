"""Shared isolated state across the four jobs and distinct seasonal histories.

Model responses are explicit fixtures; these tests do not attest to Gloo or native SMS.
"""
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.agents import planning_agent, capacity_agent
from app.agents.admin_agent import prepare
from app.core import availability_review, confirmations, reminders, scheduler
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.test_admin_planning_tools import PlanningGloo, tools
from tests.test_capacity_narration import NarrationGloo
from tests.test_fill_agent import ScriptedAgentGloo
from tests.test_planning_composition import CopyGloo
from tests.test_planning_patterns import event

ZONE = ZoneInfo('America/Denver')
OWNER = '11111111-1111-4111-8111-111111111111'


def approve(session, ctx, review):
    assert review.status == 'pending' and confirmations.valid(review, ctx.clock.now())
    result = confirmations.decide(session, ctx.gate, review, approve=True,
        actor='Synthetic coordinator', expected=review.payload['content_hash'],
        now=ctx.clock.now(), ctx=ctx)
    assert review.status == 'approved'
    return result


def approve_texts(session, ctx):
    for review in session.scalars(select(m.Approval).where(
        m.Approval.kind == 'confirm_text', m.Approval.status == 'pending')).all():
        approve(session, ctx, review)


def test_four_jobs_share_one_roster_event_and_reviewed_lifecycle(
    session, clock, provider, make_volunteer, make_shift, tmp_path, record_property
):
    people = [make_volunteer(f'Synthetic helper {i}', prefs={'max_per_month': 3, 'interested_roles': ['Greeter']}) for i in range(2)]
    suppressed = make_volunteer('Synthetic non-consenting person', opt_in=False)
    shift = make_shift('Greeter', starts=datetime(2026, 11, 1, 9, tzinfo=ZONE), minutes=60)
    event_snapshot = (shift.event.id, shift.role_id, shift.starts_at, shift.ends_at)
    session.add(m.Policy(key='algorithm_outreach_enabled', value={'value': True}))
    session.info.update(competition_confirmation_required=True, confirmation_now=clock.now())
    ctx = FillContext(session, clock, provider, CopyGloo(), log_dir=tmp_path)
    parent = availability_review.request_review(session, OWNER, '2026-11', clock.now())
    availability_review.decide(session, parent, OWNER, clock.now(), parent.payload['content_hash'], True)
    collection = session.get(m.Approval, parent.payload['collection_id'])
    collected = planning_agent.collect(ctx, collection)
    assert collected['sent'] == [] and len(collected['reviews']) == 2 and not provider.sent
    approve_texts(session, ctx)
    assert {msg.to for msg in provider.sent} == {person.phone for person in people}
    before = len(provider.sent)
    assert planning_agent.collect(ctx, collection) == {'sent': [], 'reviews': []}
    assert len(provider.sent) == before
    for person in people:
        result = planning_agent.record_availability(ctx, person,
            ParsedMessage(intent='availability', dates=['2026-11-01'], confidence=1),
            'November 1', '2026-11')
        assert result['available'] == ['2026-11-01'] and result['unavailable'] == []
    session.commit(); session.expire_all()
    clock.advance(timedelta(days=3))  # Availability asks must age past the normal outreach cooldown.
    plan = planning_agent.plan_month(ctx, '2026-11')
    assert plan['state'] == 'pending_exact_review' and len(plan['reviews']) == 1, plan
    assert not session.scalar(select(m.Assignment))
    approve(session, ctx, session.get(m.Approval, plan['reviews'][0]))
    original = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id)).one()
    assert original.status == 'approved' and original.volunteer_id in {p.id for p in people}
    assert scheduler.validate(session, '2026-11')['violations'] == []
    clock.set_time(datetime(2026, 10, 31, 8, tzinfo=ZONE))
    assert reminders.process(ctx)['reminders'] == 0  # Exact review holds delivery.
    reminder_review = session.scalars(select(m.Approval).where(
        m.Approval.kind == 'confirm_text', m.Approval.status == 'pending',
        m.Approval.payload['purpose'].as_string() == 'reminder')).one()
    assert 'tomorrow at 9am' in reminder_review.payload['body']
    approve_texts(session, ctx)
    reminder = session.scalars(select(m.Message).where(m.Message.purpose == 'reminder')).one()
    assert reminder.status == 'sent' and 'tomorrow at 9am' in reminder.body
    before = len(provider.sent)
    assert reminders.process(ctx)['reminders'] == 0 and len(provider.sent) == before
    person = session.get(m.Volunteer, original.volunteer_id)
    ctx.gloo = ScriptedAgentGloo()
    outcome = handle_inbound(session, clock, provider, person.phone,
        'Cancel my Greeter shift November 1',
        lambda _: ParsedMessage(intent='cancel', shift_hint='Greeter November 1', confidence=1), ctx=ctx)
    assert outcome.routed_to == 'fill_agent' and original.status == 'cancelled'
    fill = session.scalars(select(m.FillRequest)).one()
    offers = session.scalars(select(m.Outreach)).all()
    assert fill.state == 'waiting_approval' and len(offers) == 1
    helper = next(p for p in people if p.id != person.id)
    assert offers[0].volunteer_id == helper.id and offers[0].message_id is None
    approve_texts(session, ctx)
    assert offers[0].message_id and session.get(m.Message, offers[0].message_id).status == 'sent'
    handle_inbound(session, clock, provider, helper.phone, 'YES',
        lambda _: ParsedMessage(intent='accept', confidence=1), ctx=ctx)
    approve_texts(session, ctx)
    winner = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
        m.Assignment.status == 'confirmed')).one()
    assert winner.volunteer_id == helper.id and fill.state == 'filled'
    assert scheduler.validate(session, '2026-11')['violations'] == []
    before = len(provider.sent)
    ctx.gloo = NarrationGloo()
    flags = capacity_agent.scan(ctx)
    risk = next(f for f in flags if f.type == 'single_point_of_failure' and f.evidence['role_id'] == shift.role_id)
    assert risk.evidence['interested_qualified'] == 2 and risk.evidence['narration']['state'] == 'ready'
    assert len(provider.sent) == before and not provider.sent_to(suppressed.phone)
    assert not session.scalar(select(m.Qualification))
    assert (shift.event.id, shift.role_id, shift.starts_at, shift.ends_at) == event_snapshot
    record_property('feature_ids', json.dumps(['four-jobs', 'monthly-collection', 'plan-publication',
        'literal-reminder', 'cancel-logistics', 'acceptance', 'single-point']))
    record_property('shared_source', json.dumps({'event_id':shift.event.id,'shift_id':shift.id,
        'original_assignment_id':original.id,'replacement_assignment_id':winner.id,
        'collection_id':collection.id,'capacity_flag_id':risk.id}))
    record_property('native_dispatches', 0)


def test_seasonal_reports_do_not_mix_holiday_and_ordinary_history_or_mutate_recipes(
    session, clock, provider, make_volunteer, tmp_path, record_property
):
    coordinator = make_volunteer(coordinator=True)
    kinds = [m.EventType(name=name, title_patterns=[]) for name in ['Holiday gathering', 'Ordinary Sunday']]
    session.add_all(kinds); session.flush()
    sources = {}
    future = {}
    for kind, counts, day in [(kinds[0], [4, 6], 24), (kinds[1], [1, 1], 13)]:
        sources[kind.id] = [event(session, datetime(year, 12, day, 9, tzinfo=ZONE),
            event_type=kind, count=count) for year, count in zip([2024, 2025], counts)]
        future[kind.id] = event(session, datetime(2026, 12, day, 9, tzinfo=ZONE),
            event_type=kind, count=2 if kind == kinds[0] else 1, status='scheduled')
    role = session.scalar(select(m.Role))
    recipes = [m.RoleRecipe(event_type_id=kind.id, role_id=role.id, count=2 if kind == kinds[0] else 1)
        for kind in kinds]
    session.add_all(recipes); session.commit(); session.expire_all()
    before = {row.id:(row.starts_at, row.ends_at, row.event_type_id, tuple(s.id for s in row.shifts))
        for row in session.scalars(select(m.Event))}
    ctx = FillContext(session, clock, provider, PlanningGloo([
        ('read_context', {}), ('seasonal_staffing_report', {'month':'2026-12'})]), log_dir=tmp_path)
    result = prepare(ctx, coordinator, 'Review recorded December staffing')
    assert result['approval_ids'] == []
    report = tools(ctx)['seasonal_staffing_report']
    holiday = next(row for row in report['recommendations'] if row['event_id'] == future[kinds[0].id].id)
    assert holiday['review_difference'] == 3
    assert {sample['event_id'] for sample in holiday['evidence']} == {row.id for row in sources[kinds[0].id]}
    assert not any(row['event_id'] == future[kinds[1].id].id for row in report['recommendations'])
    assert not report['staffing_changed'] and report['assignments_created'] == report['messages_created'] == 0
    assert {row.id:(row.starts_at, row.ends_at, row.event_type_id, tuple(s.id for s in row.shifts))
        for row in session.scalars(select(m.Event))} == before
    assert [session.get(m.RoleRecipe, row.id).count for row in recipes] == [2, 1]
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Message)) and not provider.sent
    record_property('feature_ids', json.dumps(['seasonal-planning', 'event-recipes', 'coordinator-control']))
    record_property('holiday_evidence', json.dumps(holiday, default=str))
    record_property('native_dispatches', 0)


def test_new_service_label_without_checked_window_cannot_complete_intake(
    session, clock, provider, make_volunteer, make_shift, tmp_path
):
    from types import SimpleNamespace
    from app.config import Settings
    shift = make_shift('Greeter')
    person = make_volunteer(prefs={'onboarding_stage':'availability', 'interested_roles':['Greeter']})

    class OmittedWindowGloo:
        settings = Settings(gloo_signup_replies=True)
        def create_response(self, **kwargs):
            facts = json.loads(kwargs['input'])
            if 'approved_message' in facts:
                return SimpleNamespace(output_text=facts['approved_message'], usage=None)
            return SimpleNamespace(output_text=json.dumps({'understood':True, 'sensitive':False,
                'availability_known':True, 'frequency_known':True, 'weekdays':[6],
                'all_day':False, 'preferred_services':['sun_9'], 'max_per_month':2,
                'available_dates':[], 'unavailable_dates':[]}), usage=None)

    ctx = FillContext(session, clock, provider, OmittedWindowGloo(), log_dir=tmp_path)
    result = handle_inbound(session, clock, provider, person.phone,
        'Sundays 9-10am, twice a month',
        lambda _: ParsedMessage(intent='availability', confidence=1), ctx=ctx)
    session.commit(); session.expire_all()
    assert result.routed_to == 'onboarding_clarify'
    assert person.preferences['onboarding_stage'] == 'availability'
    assert not person.preferences.get('preferred_services')
    assert not person.preferences.get('recurring_windows')
    assert not any('preferences are saved' in row.body for row in provider.sent)
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Availability))
    run = session.scalar(select(m.AgentRun).where(m.AgentRun.agent == 'onboarding'))
    assert run.outcome == 'needs_clarification'


@pytest.mark.parametrize('body,prior,current,complete', [
    ('Sundays 9-10am, twice a month', True, False, False),
    ('Sundays at 9am, twice a month', False, False, True),
    ('Twice a month', True, False, True),
    ('Sundays 9-10am, twice a month', True, True, True),
])
def test_explicit_window_guard_preserves_prior_bounds_and_legacy_service_choices(
    session, clock, provider, make_volunteer, make_shift, tmp_path, body, prior, current, complete
):
    from types import SimpleNamespace
    from app.config import Settings
    from tests.test_recurring_availability import window
    role = make_shift('Greeter').role
    old = window(role, start='09:00', end='11:00')
    prefs = {'onboarding_stage':'availability', 'interested_roles':['Greeter']}
    if prior:
        prefs.update(recurring_windows=[old], availability_weekdays=[6],
            availability_all_day=False, max_per_month=2)
    person = make_volunteer(prefs=prefs)
    extraction = {'understood':True, 'sensitive':False, 'availability_known':True,
        'frequency_known':True, 'weekdays':[6], 'all_day':False,
        'preferred_services':['sun_9'], 'max_per_month':2, 'available_dates':[], 'unavailable_dates':[]}
    if current:
        extraction['recurring_windows'] = [window(role, start='09:00', end='10:00')]

    class WindowGloo:
        settings = Settings(gloo_signup_replies=True)
        def create_response(self, **kwargs):
            facts = json.loads(kwargs['input'])
            return SimpleNamespace(output_text=facts['approved_message'] if 'approved_message' in facts
                else json.dumps(extraction), usage=None)

    result = handle_inbound(session, clock, provider, person.phone, body,
        lambda _: ParsedMessage(intent='availability', confidence=1),
        ctx=FillContext(session, clock, provider, WindowGloo(), log_dir=tmp_path))
    session.commit(); session.expire_all()
    assert result.routed_to == ('onboarding_complete' if complete else 'onboarding_clarify')
    if prior:
        assert person.preferences['recurring_windows'] == (
            extraction['recurring_windows'] if current else [old])
    else:
        assert person.preferences['preferred_services'] == ['sun_9']
    if not complete:
        assert person.preferences['onboarding_stage'] == 'availability'
        assert not any('preferences are saved' in row.body for row in provider.sent)
    assert not session.scalar(select(m.Assignment))
