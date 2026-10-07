"""One stateful algorithm cycle with fictional people, clocks and transport."""
import hashlib
import json
from datetime import timedelta
from sqlalchemy import select
from app.agents.fill_agent import FillContext, advance_due
from app.core import algorithm_outreach, reminders
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.test_fill_agent import ScriptedAgentGloo
from tests.test_clyde_algorithm import enable


class CycleGloo(ScriptedAgentGloo):
    def __init__(self):
        self.call_count = 0

    def create_response(self, **kwargs):
        self.call_count += 1
        return super().create_response(**kwargs)


def test_accelerated_cancel_order_tranches_expiry_acceptance_and_due_reminder(
    session, clock, provider, make_volunteer, make_shift, assign, tmp_path
):
    enable(session)
    score, urgency = algorithm_outreach.config(session)
    assert score.k == 100 and urgency.maximum_buffer == .5
    shift = make_shift('Greeter')
    original = make_volunteer('Synthetic Original', prefs={'onboarding_stage': 'complete'})
    initial = assign(original, shift)
    helpers = [make_volunteer(f'Synthetic Helper {i}',
        created_at=clock.now()-timedelta(days=8-i), prefs={'onboarding_stage': 'complete'})
        for i in range(7)]
    assert not session.scalar(select(m.Message))
    gloo = CycleGloo()
    ctx = FillContext(session, clock, provider, gloo, log_dir=tmp_path)
    timeline = []

    def record(stage):
        timeline.append({'stage': stage, 'at': clock.now().isoformat(),
            'sent': len(provider.sent), 'gloo_calls': gloo.call_count,
            'active_assignment_ids': list(session.scalars(select(m.Assignment.id).where(
                m.Assignment.status.in_(('approved', 'confirmed')))))})

    def inbound(person, body, intent):
        return handle_inbound(session, clock, provider, person.phone, body,
            lambda _: ParsedMessage(intent=intent, confidence=.99), ctx=ctx)

    def rows(fill):
        return session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill.id)
            .order_by(m.Outreach.id)).all()

    assert inbound(original, "I can't serve", 'cancel').routed_to == 'fill_agent'
    assert initial.status == 'cancelled'
    fill = session.scalar(select(m.FillRequest).where(m.FillRequest.cancelled_assignment_id == initial.id))
    first = rows(fill)
    assert [row.volunteer_id for row in first] == [v.id for v in helpers[:2]]
    receipt = algorithm_outreach.receipt(session, fill)
    assert receipt.detail['config']['k'] == 100 and receipt.detail['expected_acceptances'] == 2
    for signal in receipt.detail['signals']:
        raw = signal['acceptance_rate']*signal['elapsed_minutes']/signal['average_response_minutes']
        assert abs(signal['score']-10*raw/(raw+100)) < 1e-12
    assert receipt.detail['signals'][0]['score'] == receipt.detail['signals'][1]['score']
    assert all(signal['last_requested_at'] is None for signal in receipt.detail['signals'])
    record('cancelled_and_first_batch_two')
    clock.advance(timedelta(minutes=5))
    assert not advance_due(ctx) and len(rows(fill)) == 2
    assert inbound(helpers[0], f'NO R{first[0].id}', 'decline').notes == ['tranche_sent']
    assert [row.volunteer_id for row in rows(fill)] == [v.id for v in helpers[:3]]
    sent_before = len(provider.sent)
    inbound(helpers[0], f'NO R{first[0].id}', 'decline')
    assert len(rows(fill)) == 3 and len(provider.sent) == sent_before
    winner = rows(fill)[-1]
    assert inbound(helpers[2], f'YES R{winner.id}', 'accept').notes == ['filled']
    booked = session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
        m.Assignment.status == 'confirmed'))
    assert booked.volunteer_id == helpers[2].id and first[1].response == 'revoked'
    record('decline_one_new_request_and_one_winner')
    assert inbound(helpers[2], "I can't serve", 'cancel').routed_to == 'fill_agent'
    assert booked.status == 'cancelled'
    replacement = session.scalar(select(m.FillRequest).where(m.FillRequest.cancelled_assignment_id == booked.id))
    second = rows(replacement)
    assert [row.volunteer_id for row in second] == [v.id for v in helpers[3:5]]
    expiry = replacement.next_action_at
    clock.set_time(expiry-timedelta(microseconds=1))
    assert not advance_due(ctx)
    clock.set_time(expiry)
    assert advance_due(ctx)[0].action == 'tranche_sent'
    assert [row.response for row in second] == ['expired', 'expired']
    assert [row.volunteer_id for row in rows(replacement)] == [v.id for v in helpers[3:7]]
    sent_before = len(provider.sent)
    advance_due(ctx)
    assert len(rows(replacement)) == 4 and len(provider.sent) == sent_before
    assert inbound(helpers[6], 'Sure, I can help', 'confirm').notes == ['filled']
    final = session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
        m.Assignment.status == 'confirmed'))
    assert final.volunteer_id == helpers[6].id
    record('second_cancellation_expiry_batch_two_and_natural_acceptance')
    assert len(session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
        m.Assignment.status.in_(('approved', 'confirmed')))).all()) == 1
    from app.core.policies import PolicyStore
    church_tz = PolicyStore(session).church_tz()
    day_before = (shift.starts_at.astimezone(church_tz)-timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    clock.set_time(day_before-timedelta(hours=7, seconds=1))
    before = len(provider.sent)
    assert reminders.process(ctx)['reminders'] == 0
    assert len(provider.sent) == before
    clock.set_time(day_before)
    assert reminders.process(ctx)['reminders'] == 1
    reminder = session.scalar(select(m.Message).where(m.Message.purpose == 'reminder'))
    assert reminder.volunteer_id == final.volunteer_id
    assert reminder.body == reminders.day_before_copy(final, clock.now().tzinfo)
    calls = gloo.call_count
    session.commit()
    session.expire_all()
    assert reminders.process(ctx)['reminders'] == 0
    assert len(provider.sent) == before+1 and gloo.call_count == calls
    assert all('\u2014' not in message.body for message in provider.sent)
    record('due_exact_reminder_once_across_restart')
    proof = {'transport': 'mock', 'native_calls': 0, 'real_messages': 0,
        'k': score.k, 'church_timezone': str(church_tz), 'batch_sizes': [2, 1, 2, 2],
        'selection_order': [v.id for v in helpers], 'timeline': timeline,
        'reminder_body_sha256': hashlib.sha256(reminder.body.encode()).hexdigest(),
        'reminder_assignment_id': final.id, 'final_active_assignments': 1}
    (tmp_path/'accelerated-cycle.private.json').write_text(json.dumps(proof, indent=2)+'\n')
