"""Existing fill winners notify once; suppressed notifications never recompose."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.agents import fill_agent
from app.agents.fill_agent import FillContext
from app.core import notifications, offer_windows as offers
from app.db import models as m
from tests.test_fill_agent import ScriptedAgentGloo, historical_invitation


class CountingGloo(ScriptedAgentGloo):
    def __init__(self):
        self.calls = 0
        self.before_response = None

    def create_response(self, **kwargs):
        self.calls += 1
        if self.before_response:
            self.before_response()
        return super().create_response(**kwargs)


@pytest.mark.parametrize('already_won', [False, True])
def test_recorded_fill_winner_notice_is_bound_and_deduped_across_callers(
    session, clock, provider, make_volunteer, make_shift, already_won
):
    volunteer = make_volunteer()
    shift = make_shift(starts=clock.now()+timedelta(days=3))
    fill = m.FillRequest(shift_id=shift.id, urgency='normal', state='in_progress', current_tranche=1, created_at=clock.now())
    session.add(fill); session.flush()
    # Historical offer already delivered before notification-first policy.
    outreach = historical_invitation(session, clock, volunteer, fill)
    assert offers.reply_source_problem(session, outreach, clock.now()) is None
    if already_won:
        assignment = m.Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status='confirmed', source='fill',
                                  created_at=clock.now(), updated_at=clock.now())
        session.add(assignment); outreach.response='yes'; fill.state='filled'; session.flush()
    gloo=CountingGloo(); ctx=FillContext(session,clock,provider,gloo)
    outcome=fill_agent.on_outreach_reply(ctx,volunteer,outreach,'accept')
    assert outcome.action == ('already_filled' if already_won else 'filled')
    assignment=session.scalar(select(m.Assignment))
    winner=session.get(m.Notification,f'winner:{fill.id}:{volunteer.id}')
    assert winner.state=='sent' and winner.detail['conversation']['assignment_id']==assignment.id
    assert len(provider.sent)==1 and gloo.calls==1
    # Repeated YES and planner notification paths share the gate's assignment key.
    fill_agent.on_outreach_reply(ctx,volunteer,outreach,'accept')
    duplicate=notifications.deliver(ctx,key=f'planner-scheduled:{assignment.id}', body='Same placement, different wording.',
        purpose='confirmation',volunteer=volunteer,conversation={'assignment_id':assignment.id,'notice':'scheduled'})
    assert duplicate.state=='blocked_policy'
    assert len(provider.sent)==1 and gloo.calls==1
    assert len(list(session.scalars(select(m.Assignment))))==1


def test_suppression_is_terminal_without_any_gloo_calls(session,clock,provider,make_volunteer):
    volunteer=make_volunteer();gloo=CountingGloo();ctx=FillContext(session,clock,provider,gloo)
    row=notifications.deliver(ctx,key='routine:one',body='Checking on your request.',purpose='thanks',volunteer=volunteer)
    assert row.state=='blocked_policy' and row.detail['reason']
    for _ in range(3):
        clock.advance(timedelta(minutes=5))
        notifications.deliver(ctx,key='routine:one',body='Checking again.',purpose='thanks',volunteer=volunteer)
        notifications.flush_due(ctx)
    assert not provider.sent and gloo.calls==0


def test_schedule_changed_during_composition_is_held_without_delivery(session,clock,provider,make_volunteer,make_shift):
    volunteer=make_volunteer();shift=make_shift(starts=clock.now()+timedelta(days=3))
    assignment=m.Assignment(shift_id=shift.id,volunteer_id=volunteer.id,status='confirmed',source='fill',
                            created_at=clock.now(),updated_at=clock.now())
    session.add(assignment);session.flush();gloo=CountingGloo();ctx=FillContext(session,clock,provider,gloo)
    def reschedule():
        shift.event.starts_at += timedelta(hours=1)
        shift.event.ends_at += timedelta(hours=1)
        session.flush()
    gloo.before_response=reschedule
    row=notifications.deliver(ctx,key='winner:changed',body='Original scheduled time.',purpose='confirmation',volunteer=volunteer,
        conversation={'assignment_id':assignment.id,'notice':'scheduled'})
    assert row.state=='blocked_policy' and not provider.sent and gloo.calls==1
    notifications.flush_due(ctx)
    assert gloo.calls==1


def test_internal_admin_notifications_still_compose_and_deliver(session,clock,provider,make_volunteer):
    admin=make_volunteer(coordinator=True);gloo=CountingGloo();ctx=FillContext(session,clock,provider,gloo)
    row=notifications.deliver(ctx,key='admin:status',body='Internal coverage gap.',purpose='coordinator_notify',volunteer=admin)
    assert row.state=='sent' and len(provider.sent)==1 and gloo.calls==1
