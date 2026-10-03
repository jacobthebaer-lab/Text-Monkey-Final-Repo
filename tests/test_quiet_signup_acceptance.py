"""Policy acceptance across actual routes, with fictional senders and mocks."""
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from app.db import models as m
from app.core.signup_copy import WELCOME
from tests.test_concise_signup import PHONE, route
from tests.test_exact_signup_copy import ExactGloo, EXPECTED
from tests.test_adaptive_signup import AdaptiveGloo
from tests.test_independent_text_database import signup_app, submit_queued
from tests.test_mac_messages import mac_app, incoming, post


@pytest.mark.parametrize('identity',['Alex Example','JOIN Alex Example','My name is Alex Example'])
def test_initial_essentials_and_natural_answers_complete_silently(session,clock,provider,identity):
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}));session.flush()
    gloo=ExactGloo()
    assert route(session,clock,provider,'Hello',gloo).routed_to=='signup_invitation'
    assert provider.sent[-1].body==WELCOME
    assert route(session,clock,provider,identity,gloo).routed_to=='onboarding_interests'
    assert provider.sent[-1].body==EXPECTED[1]
    assert route(session,clock,provider,'Anything',gloo).routed_to=='onboarding_availability'
    assert provider.sent[-1].body==EXPECTED[2]
    assert route(session,clock,provider,'Sundays and Wednesdays all day',gloo).routed_to=='onboarding_complete'
    person=session.scalar(select(m.Volunteer))
    assert person.sms_opt_in and person.preferences['onboarding_stage']=='complete'
    assert person.preferences['availability_weekdays']==[6,2] and 'max_per_month' not in person.preferences
    assert [item.body for item in provider.sent]==EXPECTED[:3]
    assert len([f for f in gloo.calls if isinstance(f,dict) and 'approved_message' in f])==3
    assert session.scalar(select(m.Assignment)) is None and session.scalar(select(m.Qualification)) is None


def test_split_name_can_continue_naturally_after_suppressed_repeat(session,clock,provider):
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}));session.flush()
    gloo=AdaptiveGloo()
    route(session,clock,provider,'Hello',gloo)
    assert route(session,clock,provider,'Alex',gloo).routed_to=='signup_intake_suppressed'
    assert len(provider.sent)==1 and session.scalar(select(m.Volunteer)) is None
    assert route(session,clock,provider,'Example',gloo).routed_to=='onboarding_interests'
    person=session.scalar(select(m.Volunteer))
    assert person.name=='Alex Example' and person.sms_opt_in
    assert person.preferences['consent_source']=='sms_name_reply_to_exact_invitation'
    assert len(provider.sent)==2 and provider.sent[-1].body==EXPECTED[1]


def test_fresh_missing_days_question_is_essential_then_completion_is_silent(session,clock,provider,make_volunteer):
    from app.core import onboarding
    from app.core.send_gate import SendGate
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}));session.flush()
    person=make_volunteer('Alex Example',prefs={'onboarding_stage':'availability',
        'signup_minimal_texts':True,'interested_roles':['Greeter']});person.phone=PHONE;session.flush()
    gloo=AdaptiveGloo();gate=SendGate(session,clock,provider)
    assert onboarding.handle(session,clock,gate,person,'Twice a month',gloo)=='onboarding_clarify'
    assert gloo.calls[0]['saved_availability_source']=='saved_profile'
    assert provider.sent[-1].body=='Which days or dates can you serve? You can also say "Flexible".'
    assert person.preferences['onboarding_availability_draft']['max_per_month']==2
    assert onboarding.handle(session,clock,gate,person,'Sundays and Wednesdays all day',gloo)=='onboarding_complete'
    assert person.preferences['max_per_month']==2 and len(provider.sent)==1


def test_http_signup_retains_provenance_and_receipt_dedup_without_completion_queue(signup_app):
    app=signup_app
    stages=[('quiet-hello','Hello','signup_invitation'),('quiet-name','Alex Example','onboarding_interests'),
        ('quiet-roles','Anything','onboarding_availability'),
        ('quiet-days','Sundays and Wednesdays all day','onboarding_complete')]
    with TestClient(app) as client:
        for index,(guid,body,stage) in enumerate(stages):
            data=incoming(guid,body)
            result=post(client,'/mac/inbound',data)
            assert result.status_code==200 and result.json()['intent']==stage
            assert post(client,'/mac/inbound',data).json()['duplicate']
            assert post(client,'/mac/inbound',{**data,'body':'changed'}).status_code==409
            if index<3:submit_queued(client,EXPECTED[index])
            else:assert post(client,'/mac/outbound/pull').json()['messages']==[]
    with app.state.session_factory() as session:
        person=session.scalar(select(m.Volunteer))
        assert person.sms_opt_in and person.preferences['onboarding_stage']=='complete'
        assert len(session.scalars(select(m.Message).where(m.Message.direction=='in')).all())==4
        outputs=session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
        assert len(outputs)==3 and all(row.status=='submitted' for row in outputs)
