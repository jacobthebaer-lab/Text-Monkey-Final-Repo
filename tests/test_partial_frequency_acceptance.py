"""Independent recorded-input and window-bound checks; synthetic records only."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import onboarding
from app.core.recurring_availability import recurring_window_reasons
from app.core.send_gate import SendGate
from app.db import models as m
from app.integrations.test_sessions import TestSession as RecipientSession
from tests.test_adaptive_signup import AdaptiveGloo, WINDOWS
from tests.test_concise_signup import PHONE
from tests.test_nullable_signup_frequency import snapshot


@pytest.mark.parametrize('defect', ['sender', 'body', 'stage', 'session'])
def test_recorded_recovery_binding_cannot_substitute_another_input(
        session, clock, provider, make_volunteer, snapshot, defect):
    _, data, _ = snapshot
    person = make_volunteer('Synthetic Example', prefs={'onboarding_stage':'availability',
        'signup_minimal_texts':True, 'interested_roles':['Greeter']})
    person.phone = PHONE
    body = 'Synthetic Sunday 8 to 10 greeting and Wednesday coffee'
    incoming = m.Message(phone=PHONE, volunteer_id=person.id, direction='in', status='received',
        kind='inbound', body=body, created_at=clock.now())
    if defect == 'sender': incoming.phone = '+12025550191'
    if defect == 'body': incoming.body = 'A different recorded answer'
    if defect == 'session':
        session.info['mac_test_session'] = RecipientSession('a'*32,
            clock.now()-timedelta(minutes=1), clock.now()+timedelta(minutes=10))
        incoming.provider_sid = 'MAC'+'b'*32+':1'
    run = m.AgentRun(agent='onboarding', trigger='Synthetic availability', started_at=clock.now())
    session.add_all([incoming, run]); session.flush()
    step = m.AgentStep(run_id=run.id, step_no=1, type='decision', created_at=clock.now(),
        result={'stage':'interests' if defect=='stage' else 'availability', 'extraction':deepcopy(data)})
    session.add(step); session.flush()
    original = deepcopy(step.result)
    session.info['verified_onboarding_source'] = {'incoming_id':incoming.id, 'step_id':step.id}
    gloo = AdaptiveGloo()
    result = onboarding.handle(session, clock,
        SendGate(session, clock, provider, reply_to_message_id=incoming.id), person,
        body, gloo, recorded_step_id=step.id)
    assert result == 'onboarding_review' and not provider.sent and not gloo.calls
    assert 'onboarding_availability_draft' not in person.preferences
    assert step.result == original
    assert session.scalars(select(m.Message).where(m.Message.direction=='in')).all() == [incoming]


def test_nullable_frequency_never_expands_validated_serving_windows(
        session, clock, snapshot, make_shift):
    previous, data, roles = snapshot
    before = deepcopy(previous)
    draft = onboarding.validated_availability(data, previous, clock.now().date(), roles=roles)
    assert previous == before and draft['recurring_windows'] == WINDOWS
    assert draft['frequency_known'] is False and draft['max_per_month'] is None
    sunday = (clock.now()+timedelta(days=3)).replace(hour=10, minute=0)
    ten_to_eleven = make_shift('Greeter', starts=sunday, minutes=60)
    assert recurring_window_reasons(session, {'recurring_windows':draft['recurring_windows']},
        ten_to_eleven)
    assert draft['recurring_windows'][0]['end_time'] == '10:00'
    assert draft['recurring_windows'][1]['start_time'] is None
    assert draft['recurring_windows'][1]['end_time'] is None
    assert draft['recurring_windows'][1]['all_day'] is False
