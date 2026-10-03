"""Role-scoped limits through ranking, actual proposals and virtual planning."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import ranking, scheduler
from app.db import models as m
from tests.conftest import NOW


def role_profile(make_volunteer, greeting, **extra):
    return make_volunteer(prefs={"role_frequency_caps": [{"role_id": greeting.role_id,
        "role_name": greeting.role.name, "max_per_month": 2}], **extra})


@pytest.mark.parametrize('global_limit', ['omitted', None])
def test_coffee_load_does_not_invent_global_cap_or_consume_greeting_cap(
    session, clock, make_volunteer, make_shift, assign, global_limit
):
    greeting = make_shift('Greeter', starts=NOW+timedelta(days=20))
    coffee = make_shift('Coffee', starts=NOW+timedelta(days=21))
    prefs = {} if global_limit == 'omitted' else {'max_per_month': global_limit}
    volunteer = role_profile(make_volunteer, greeting, **prefs)
    for day in (2, 5, 8, 11):
        assign(volunteer, make_shift('Coffee', starts=NOW+timedelta(days=day)), status='confirmed')
    assert [c.volunteer.id for c in ranking.rank_candidates(session, greeting, NOW)] == [volunteer.id]
    assert [c.volunteer.id for c in scheduler.candidates(session, coffee, NOW, 'America/Denver')] == [volunteer.id]
    assert 'assignment_id' in scheduler.propose(session, clock, greeting, volunteer, 'America/Denver')
    assert scheduler.validate(session, '2026-10')['violations'] == []


@pytest.mark.parametrize('profile', ['legacy_default', 'explicit_global'])
def test_true_global_limits_still_apply_across_roles(session, clock, make_volunteer, make_shift, assign, profile):
    greeting = make_shift('Greeter', starts=NOW+timedelta(days=20))
    volunteer = (make_volunteer() if profile == 'legacy_default'
                 else role_profile(make_volunteer, greeting, max_per_month=3))
    for day in (2, 5, 8):
        assign(volunteer, make_shift('Coffee', starts=NOW+timedelta(days=day)), status='confirmed')
    assert not ranking.rank_candidates(session, greeting, NOW)
    assert scheduler.propose(session, clock, greeting, volunteer, 'America/Denver')['error'] == 'monthly maximum reached'


def test_virtual_greeting_cap_counts_own_role_and_month_only(session, make_volunteer, make_shift):
    greeters = [make_shift('Greeter', starts=NOW+timedelta(days=d)) for d in (2, 5, 8)]
    coffee = make_shift('Coffee', starts=NOW+timedelta(days=11))
    next_month = make_shift('Greeter', starts=NOW+timedelta(days=35))
    volunteer = role_profile(make_volunteer, greeters[0])
    choices = [{'shift_id': greeters[0].id, 'volunteer_id': volunteer.id},
               {'shift_id': coffee.id, 'volunteer_id': volunteer.id},
               {'shift_id': next_month.id, 'volunteer_id': volunteer.id}]
    assert scheduler.preview_problem(session, volunteer, greeters[1], choices, 'America/Denver') is None
    choices.append({'shift_id': greeters[1].id, 'volunteer_id': volunteer.id})
    assert 'role-specific monthly maximum' in scheduler.preview_problem(session, volunteer, greeters[2], choices, 'America/Denver')
    other_coffee = make_shift('Coffee', starts=NOW+timedelta(days=14))
    assert scheduler.preview_problem(session, volunteer, other_coffee, choices, 'America/Denver') is None
    assert not list(session.scalars(select(m.Assignment)))


def test_preview_draft_respects_role_cap_without_limiting_other_role(session, clock, make_volunteer, make_shift):
    greeters = [make_shift('Greeter', starts=NOW+timedelta(days=d)) for d in (2, 5, 8)]
    coffees = [make_shift('Coffee', starts=NOW+timedelta(days=d)) for d in (11, 14, 17, 20)]
    volunteer = role_profile(make_volunteer, greeters[0])
    choices = scheduler.preview_draft(session, clock, '2026-10', 'America/Denver')
    selected = {c['shift_id'] for c in choices}
    assert len(selected & {s.id for s in greeters}) == 2
    assert {s.id for s in coffees} <= selected
    assert scheduler.preview_report(session, '2026-10', choices, 'America/Denver')['violations'] == []
    assert not list(session.scalars(select(m.Assignment)))


def test_proposal_and_validation_apply_role_cap_after_cross_role_load(session, clock, make_volunteer, make_shift, assign):
    greeters = [make_shift('Greeter', starts=NOW+timedelta(days=d)) for d in (2, 5, 8)]
    coffee = make_shift('Coffee', starts=NOW+timedelta(days=11))
    volunteer = role_profile(make_volunteer, greeters[0])
    for shift in greeters[:2]:
        assert 'assignment_id' in scheduler.propose(session, clock, shift, volunteer, 'America/Denver')
    assert scheduler.propose(session, clock, greeters[2], volunteer, 'America/Denver')['error'] == 'ineligible'
    assert 'assignment_id' in scheduler.propose(session, clock, coffee, volunteer, 'America/Denver')
    assert scheduler.validate(session, '2026-10')['violations'] == []
    assign(volunteer, greeters[2], status='confirmed')
    report = scheduler.validate(session, '2026-10')
    assert any('role-specific monthly maximum' in reason
        for violation in report['violations'] for reason in violation.get('reasons', []))


def test_actual_fill_acceptance_has_no_invented_global_cap(
    session, clock, provider, make_volunteer, make_shift, assign, tmp_path
):
    from app.agents.fill_agent import FillContext
    from app.core import offer_windows as offers
    from app.core.inbound import handle_inbound
    from tests.test_fill_agent import ScriptedAgentGloo, parser_returning
    greeting = make_shift('Greeter', starts=NOW+timedelta(days=20))
    volunteer = role_profile(make_volunteer, greeting, max_per_month=None)
    for day in (2, 5, 8, 11):
        assign(volunteer, make_shift('Coffee', starts=NOW+timedelta(days=day)), status='confirmed')
    fill = m.FillRequest(shift_id=greeting.id, state='in_progress', urgency='normal',
        current_tranche=1, created_at=clock.now())
    session.add(fill); session.flush()
    message = m.Message(direction='out', volunteer_id=volunteer.id, phone=volunteer.phone,
        body='Synthetic reviewed invitation.', purpose='outreach', kind='ai', status='sent', created_at=clock.now())
    session.add(message); session.flush()
    outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=volunteer.id,
        tranche=1, response='none', message_id=message.id)
    session.add(outreach); session.flush()
    offers.prepare(session, outreach, message.body, clock.now())
    offers.dispatch(session, outreach, message, clock.now())
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo(), log_dir=tmp_path)
    result = handle_inbound(session, clock, provider, volunteer.phone, 'Yes',
        parser_returning(intent='accept'), ctx=ctx)
    assert result.notes == ['filled']
    assignment = scheduler.occupied(session, greeting)
    assert assignment.volunteer_id == volunteer.id and assignment.status == 'confirmed'
    assert fill.state == 'filled'
