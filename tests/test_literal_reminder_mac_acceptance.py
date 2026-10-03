"""Synthetic inbound/queue routes only; no native worker, network model, or real text."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import confirmations
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim
from app.jobs import process_jobs
from tests.test_demo_acceptance_review import acceptance_app, OTHER_PHONE, BRIDGE_TOKEN, pull
from tests.session_fixtures import session_id
from tests.test_exact_day_before_reminder import LITERAL


@pytest.fixture
def reminder_mac(acceptance_app):
    client, app, gloo, clock = acceptance_app
    app.state.settings = replace(app.state.settings, competition_confirmation_required=True)
    app.state.session_factory.configure(info={confirmations.MODE_KEY: True})
    gloo.transform = lambda body: body
    original = gloo.create_response
    def response(**kwargs):
        try:
            facts = json.loads(kwargs['input'])
        except json.JSONDecodeError:
            gloo.calls.append(kwargs)
            return SimpleNamespace(output_text=json.dumps({'intent':'cancel','confidence':1,
                'shift_hint':'tomorrow','sensitive':False}))
        result = original(**kwargs)
        if facts.get('exact_copy'):
            result.output_text = gloo.transform(result.output_text)
        return result
    gloo.create_response = response
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        volunteer = m.Volunteer(name='Clyde Synthetic', phone=OTHER_PHONE, status='active',
            sms_opt_in=True, preferences={}, created_at=clock.now())
        role = m.Role(name='Greeter', ministry='Welcome', required_qualifications=[],
            criticality='standard', fill_policy='auto')
        starts = clock.now()+timedelta(days=1)
        event = m.Event(title='Synthetic service', starts_at=starts,
            ends_at=starts+timedelta(hours=1), status='scheduled')
        shift = m.Shift(event=event, role=role, slot_index=0)
        session.add_all([volunteer, shift]); session.flush()
        app.state.reminder_person_id, app.state.reminder_shift_id = volunteer.id, shift.id
        session.commit()
    yield client, app, gloo, clock


def book(app):
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        row = m.Assignment(volunteer_id=app.state.reminder_person_id, shift_id=app.state.reminder_shift_id,
            status='approved', source='planner', created_at=app.state.clock.now(), updated_at=app.state.clock.now())
        session.add(row); session.flush(); ident = row.id; session.commit()
        return ident


def tick(app):
    with app.state.session_factory() as session:
        result = process_jobs(FillContext(session, app.state.clock, app.state.provider, app.state.gloo))
        reviews = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text',
            m.Approval.status=='pending', m.Approval.payload['purpose'].as_string()=='reminder')).all()
        staged = [(a.id, a.payload['content_hash'], a.payload['body']) for a in reviews]
        session.commit()
        return result, staged


def queued(client, app):
    _, reviews = tick(app)
    assert len(reviews)==1 and reviews[0][2]==LITERAL
    ident, digest, _ = reviews[0]
    result = client.post(f'/api/proposals/{ident}/approve', json={'content_hash':digest})
    assert result.status_code==200, result.text
    return digest


def reschedule(app):
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        event = session.get(m.Shift, app.state.reminder_shift_id).event
        event.starts_at += timedelta(hours=1); event.ends_at += timedelta(hours=1)
        session.commit()


def test_late_booking_stages_one_literal_at_next_tick_and_silence_keeps_booking(reminder_mac):
    _, app, gloo, clock = reminder_mac
    assert tick(app)[1]==[]
    clock.advance(timedelta(minutes=15)); assignment_id=book(app)
    result, reviews=tick(app)
    assert len(reviews)==1 and reviews[0][2]==LITERAL and len(gloo.calls)==1
    assert result['messages']['reminders']==0
    assert tick(app)[1]==reviews and len(gloo.calls)==1
    clock.advance(timedelta(hours=8)); tick(app)
    with app.state.session_factory() as session:
        assert session.get(m.Assignment,assignment_id).status=='approved'
        assert session.scalar(select(m.FillRequest)) is None
        assert session.scalar(select(m.Message)) is None
    assert not app.state.settings.automation_enabled


@pytest.mark.parametrize('defect',['paraphrase','newline','outage'])
def test_literal_model_failures_do_not_create_review_or_seed_message(reminder_mac,defect):
    _,app,gloo,_=reminder_mac; book(app)
    if defect=='outage': gloo.unavailable=True
    else: gloo.transform=lambda body:body+'\n' if defect=='newline' else body.replace('just let me know','tell me')
    assert tick(app)[1]==[] and len(gloo.calls)==1
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None


def test_quiet_hours_hold_before_model_then_require_review(reminder_mac):
    _,app,gloo,clock=reminder_mac; book(app)
    with app.state.session_factory() as session:
        session.add(m.Policy(key='quiet_hours',value={'value':{'start':'09:00','end':'10:30'}}));session.commit()
    assert tick(app)[1]==[] and not gloo.calls
    clock.advance(timedelta(minutes=31))
    assert tick(app)[1][0][2]==LITERAL and len(gloo.calls)==1
    with app.state.session_factory() as session: assert session.scalar(select(m.Message)) is None


@pytest.mark.parametrize('phase',['claim','native_verify'])
def test_changed_shift_blocks_before_native_dispatch(reminder_mac,phase):
    client,app,_,_=reminder_mac;book(app);digest=queued(client,app)
    claim=pull(client).json()['messages'][0] if phase=='native_verify' else None
    reschedule(app)
    if claim:
        response=client.post(f"/mac/outbound/{claim['id']}/verify",
            json={'token':claim['token'],'content_hash':digest},headers={'Authorization':'Bearer '+BRIDGE_TOKEN})
        assert response.status_code==409
    else:
        assert pull(client).json()['messages']==[]
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message.status))==('blocked_policy' if phase=='native_verify' else 'blocked_confirmation')


def test_gloo_interpreted_incoming_cancel_reopens_slot_and_deduplicates(reminder_mac):
    client,app,gloo,_=reminder_mac;assignment_id=book(app);tick(app)
    payload={'guid':'synthetic-literal-cancellation','phone':OTHER_PHONE,
        'body':"I can't make it tomorrow",'session_id':session_id(OTHER_PHONE)}
    headers={'Authorization':'Bearer '+BRIDGE_TOKEN}
    assert client.post('/mac/inbound',json=payload,headers=headers).status_code==200
    assert any(call['input']==payload['body'] for call in gloo.calls)
    assert client.post('/mac/inbound',json=payload,headers=headers).status_code==200
    with app.state.session_factory() as session:
        assert session.get(m.Assignment,assignment_id).status=='cancelled'
        fills=session.scalars(select(m.FillRequest)).all()
        assert len(fills)==1 and fills[0].cancelled_assignment_id==assignment_id
        assert not session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id==app.state.reminder_shift_id,
            m.Assignment.status.in_(('approved','confirmed'))))


@pytest.mark.parametrize('changed',[False,True])
def test_proof_bearing_reminder_retains_native_exact_guard_when_global_mode_off(reminder_mac,changed):
    client,app,_,_=reminder_mac;book(app);digest=queued(client,app)
    app.state.settings=replace(app.state.settings,competition_confirmation_required=False)
    app.state.session_factory.configure(info={confirmations.MODE_KEY:False})
    if changed: reschedule(app)
    messages=pull(client).json()['messages']
    if changed:
        assert messages==[]
    else:
        assert len(messages)==1 and messages[0].get('confirmation_required') is True
        claim=messages[0]
        response=client.post(f"/mac/outbound/{claim['id']}/verify",
            json={'token':claim['token'],'content_hash':digest},headers={'Authorization':'Bearer '+BRIDGE_TOKEN})
        assert response.status_code==200
