"""Actual source-bound conversational replies use synthetic people and transport."""
from datetime import timedelta
from sqlalchemy import select
import pytest
from app.db import models as m
from app.core import confirmations
from app.core.inbound import handle_inbound
from app.agents.fill_agent import FillContext
from app.llm.parser import ParsedMessage
from tests.test_opportunities_reply import ExactGloo


def receive(session,clock,provider,person,gloo,body,intent='confirm'):
    return handle_inbound(session,clock,provider,person.phone,body,
        lambda _:ParsedMessage(intent=intent,confidence=.99),ctx=FillContext(session,clock,provider,gloo))


@pytest.mark.parametrize('mode',[False,True])
@pytest.mark.parametrize('body',['Okay','Omw',"I'm on my way"])
def test_coordinator_acknowledges_without_approving_or_changing_records(session,clock,provider,make_volunteer,mode,body):
    person=make_volunteer(coordinator=True,prefs={'onboarding_stage':'complete'})
    session.info[confirmations.MODE_KEY]=mode
    gloo=ExactGloo(); calls=[]
    result=handle_inbound(session,clock,provider,person.phone,body,
        lambda text:(calls.append(text) or ParsedMessage(intent='confirm',confidence=.99)),ctx=FillContext(session,clock,provider,gloo))
    assert result.routed_to=='acknowledged' and calls==[body]
    assert len(gloo.calls)==1
    assert not session.scalar(select(m.Assignment))
    if mode:
        approval=session.scalar(select(m.Approval))
        assert approval.kind=='confirm_text' and approval.status=='pending' and not provider.sent
        assert 'No schedule or approval changes' in approval.payload['body']
    else:
        assert not session.scalar(select(m.Approval)) and len(provider.sent)==1
        assert 'No schedule or approval changes' in provider.sent[0].body


@pytest.mark.parametrize('mode',[False,True])
def test_coordinator_free_form_does_not_send_unperformed_admin_claim(session,clock,provider,make_volunteer,monkeypatch,mode):
    person=make_volunteer(coordinator=True,prefs={'onboarding_stage':'complete'})
    session.info[confirmations.MODE_KEY]=mode
    monkeypatch.setattr('app.agents.admin_agent.prepare',lambda *args:{'final_text':'I booked everybody and approved everything!','approval_ids':[],'applied':False})
    gloo=ExactGloo(); receive(session,clock,provider,person,gloo,'Can you change the schedule?','question')
    row=session.scalar(select(m.Notification).where(m.Notification.key.like('ordinary-reply:%')))
    assert row and len(gloo.calls)==1
    assert 'Your text did not approve or change any schedule' in row.body
    assert 'booked everybody' not in row.body and not session.scalar(select(m.Assignment))


@pytest.mark.parametrize('state',['absent','multiple','cancelled_event','valid'])
def test_confirm_reports_only_saved_unique_scheduled_assignment(session,clock,provider,make_volunteer,make_shift,assign,state):
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    bookings=[]
    if state!='absent':
        bookings.append(assign(person,make_shift()))
    if state=='multiple':
        bookings.append(assign(person,make_shift(starts=clock.now()+timedelta(days=10))))
    if state=='cancelled_event':
        bookings[0].shift.event.status='cancelled'
    gloo=ExactGloo(); result=receive(session,clock,provider,person,gloo,'C')
    assert len(provider.sent)==len(gloo.calls)==1
    if state=='valid':
        assert result.routed_to=='confirmed' and bookings[0].status=='confirmed'
        assert "You're confirmed for" in provider.sent[0].body
    else:
        assert result.routed_to=='unmatched_reply'
        assert all(x.status=='approved' for x in bookings)
        assert 'which role or event' in provider.sent[0].body
        assert "You're confirmed" not in provider.sent[0].body


@pytest.mark.parametrize('mode',[False,True])
@pytest.mark.parametrize('body',['No',"No I can't make it"])
def test_subjectless_manual_confirmation_negative_never_selects_a_booking(session,clock,provider,make_volunteer,make_shift,assign,mode,body):
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    booking=assign(person,make_shift())
    session.add(m.Message(direction='out',volunteer_id=person.id,phone=person.phone,
        body='Can you still make it?',purpose='manual',kind='admin',status='sent',created_at=clock.now()-timedelta(minutes=1)))
    session.info[confirmations.MODE_KEY]=mode
    gloo=ExactGloo(); receive(session,clock,provider,person,gloo,body,'confirm')
    assert booking.status=='approved' and not session.scalar(select(m.FillRequest))
    assert len(gloo.calls)==1
    row=session.scalar(select(m.Notification).where(m.Notification.purpose=='signup_reply'))
    assert row and (row.state=='awaiting_approval' if mode else row.state=='sent')
    assert 'confirmed for' not in row.body and 'booking has been cancelled' not in row.body


from tests.test_mac_messages import mac_app, PHONE, post, exact_manual_gloo  # noqa: E402,F401
from fastapi.testclient import TestClient


@pytest.mark.parametrize('change',['unchanged','source','cancelled','schedule'])
def test_confirmed_reply_native_claim_requires_actual_saved_facts(mac_app,change):
    clock=mac_app.state.clock; gloo=exact_manual_gloo(mac_app)
    with mac_app.state.session_factory() as session:
        person=session.scalar(select(m.Volunteer)); person.preferences={'onboarding_stage':'complete'}
        role=m.Role(name='Greeter',ministry='Welcome',required_qualifications=[],criticality='normal',fill_policy='auto')
        session.add(role); session.flush()
        event=m.Event(title='Sunday Service',starts_at=clock.now()+timedelta(days=3),ends_at=clock.now()+timedelta(days=3,hours=1),status='scheduled')
        shift=m.Shift(event=event,role=role,slot_index=0)
        session.add(shift);session.flush()
        booking=m.Assignment(shift_id=shift.id,volunteer_id=person.id,status='approved',source='planner',created_at=clock.now(),updated_at=clock.now())
        session.add(booking);session.flush()
        session.info['mac_test_session']=mac_app.state.provider.test_sessions[PHONE]
        result=receive(session,clock,mac_app.state.provider,person,gloo,'C')
        assert result.routed_to=='confirmed' and booking.status=='confirmed'
        if change=='source':session.scalar(select(m.Message).where(m.Message.direction=='in')).body='No'
        elif change=='cancelled':booking.status='cancelled'
        elif change=='schedule':event.starts_at+=timedelta(hours=1)
        session.commit()
    with TestClient(mac_app) as client:
        batch=post(client,'/mac/outbound/pull').json()['messages']
        assert bool(batch)==(change=='unchanged')
        if batch:
            assert len(batch)==1 and "You're confirmed for" in batch[0]['body']
            assert post(client,f"/mac/outbound/{batch[0]['id']}/verify",{'token':batch[0]['token']}).status_code==200


def test_commitment_scope_no_gets_clarification_without_resolving_either_branch(session,clock,provider,make_volunteer,make_shift,assign):
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    booking=assign(person,make_shift())
    session.add(m.Notification(key=f'offer-reply:{person.id}:local',volunteer_id=person.id,
        purpose='offer_reply_scope',state='commitment_scope',body='',created_at=clock.now(),due_at=clock.now(),
        expires_at=clock.now()+timedelta(minutes=10),detail={'kind':'commitment','assignment_ids':[booking.id],'outreach_ids':[]}))
    gloo=ExactGloo(); result=receive(session,clock,provider,person,gloo,'NO','decline')
    assert result.routed_to=='clarify_commitment' and booking.status=='approved'
    assert len(provider.sent)==len(gloo.calls)==1
    assert 'which role or event' in provider.sent[0].body


@pytest.mark.parametrize('mode',[False,True])
def test_actual_explicit_confirmation_replies_from_saved_state_in_both_modes(session,clock,provider,make_volunteer,make_shift,assign,mode):
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    booking=assign(person,make_shift())
    session.info[confirmations.MODE_KEY]=mode
    gloo=ExactGloo(); result=receive(session,clock,provider,person,gloo,'Confirm my shift')
    assert result.routed_to=='confirmed' and booking.status=='confirmed'
    assert len(gloo.calls)==1
    row=session.scalar(select(m.Notification).where(m.Notification.key.like('ordinary-reply:%')))
    assert "You're confirmed for" in row.body
    assert row.state==('awaiting_approval' if mode else 'sent')


def test_confirmation_does_not_skip_already_confirmed_other_booking(session,clock,provider,make_volunteer,make_shift,assign):
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    first=assign(person,make_shift(),status='confirmed')
    second=assign(person,make_shift(starts=clock.now()+timedelta(days=10)))
    gloo=ExactGloo(); result=receive(session,clock,provider,person,gloo,'C')
    assert result.routed_to=='unmatched_reply' and first.status=='confirmed' and second.status=='approved'
    assert len(provider.sent)==1 and 'which role or event' in provider.sent[0].body



def test_saved_confirmation_ack_is_not_a_prior_clarifying_question(session,clock,provider,make_volunteer,make_shift,assign):
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    assign(person,make_shift())
    gloo=ExactGloo(); receive(session,clock,provider,person,gloo,'Confirm my shift')
    result=receive(session,clock,provider,person,gloo,'Where should I go?','question')
    assert result.routed_to=='clarify' and not session.scalar(select(m.Escalation))
    assert len(provider.sent)==2
