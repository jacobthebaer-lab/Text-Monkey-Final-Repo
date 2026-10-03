"""Typed window facts survive a nullable frequency contradiction; no real sends."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core import onboarding
from app.core.signup_copy import ensure_exact_role_menu
from app.core.send_gate import SendGate
from app.db import models as m
from tests.test_adaptive_signup import AdaptiveGloo,WINDOWS
from tests.test_concise_signup import PHONE

@pytest.fixture
def snapshot(session,clock):
    ensure_exact_role_menu(session)
    previous={'availability_known':False,'frequency_known':False,'weekdays':[],
        'all_day':False,'preferred_services':[],'max_per_month':None,'available_dates':[],
        'unavailable_dates':[]}
    data={**previous,'understood':True,'availability_known':True,'frequency_known':True,
        'weekdays':[6,2],'recurring_windows':deepcopy(WINDOWS)}
    roles=session.scalars(select(m.Role)).all()
    return previous,data,roles

def test_null_frequency_keeps_valid_windows_and_stays_unknown(session,clock,snapshot):
    previous,data,roles=snapshot
    original=deepcopy(data)
    draft=onboarding.validated_availability(data,previous,clock.now().date(),roles=roles)
    assert draft['availability_known'] and draft['frequency_known'] is False
    assert draft['max_per_month'] is None and draft['recurring_windows']==WINDOWS
    assert draft['recurring_windows'][0]['end_time']=='10:00'
    assert draft['recurring_windows'][1]['start_time'] is None
    assert data==original

@pytest.mark.parametrize('value',[0,9,-1,1.5,True,'2'])
@pytest.mark.parametrize('known',[False,True])
def test_nonnull_invalid_frequency_is_still_rejected(clock,snapshot,value,known):
    previous,data,roles=snapshot
    data.update(max_per_month=value,frequency_known=known)
    with pytest.raises(ValueError):onboarding.validated_availability(data,previous,clock.now().date(),roles=roles)

def test_null_contradiction_preserves_prior_validated_frequency(clock,snapshot):
    previous,data,roles=snapshot
    previous.update(frequency_known=True,max_per_month=2)
    draft=onboarding.validated_availability(data,previous,clock.now().date(),roles=roles)
    assert draft['frequency_known'] and draft['max_per_month']==2


def test_nullable_frequency_does_not_relax_invalid_window(clock,snapshot):
    previous,data,roles=snapshot
    data['recurring_windows'][0]['role_ids']=[999]
    with pytest.raises(ValueError):onboarding.validated_availability(data,previous,clock.now().date(),roles=roles)

@pytest.mark.parametrize('bound',[False,True])
def test_verified_logged_extraction_uses_one_composition_and_no_parser(session,clock,provider,make_volunteer,snapshot,bound):
    previous,data,roles=snapshot
    person=make_volunteer('Alex Example',prefs={'onboarding_stage':'availability','signup_minimal_texts':True,
        'interested_roles':['Greeter','Usher','Child Care']})
    person.phone=PHONE
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}))
    incoming=m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',kind='inbound',
        body='Synthetic Sunday greeting window and Wednesday workshop coffee',created_at=clock.now())
    source_run=m.AgentRun(agent='onboarding',trigger='Profile availability',model='synthetic-parser',
        started_at=clock.now(),ended_at=clock.now(),outcome='needs_clarification')
    session.add_all([incoming,source_run]);session.flush()
    step=m.AgentStep(run_id=source_run.id,step_no=1,type='decision',created_at=clock.now(),
        result={'stage':'availability','extraction':data})
    session.add(step);session.flush()
    gate=SendGate(session,clock,provider,reply_to_message_id=incoming.id)
    gloo=AdaptiveGloo()
    original=gloo.create_response
    def composition_only(**kwargs):
        import json
        facts=json.loads(kwargs['input'])
        assert isinstance(facts,dict) and facts.get('recovery'), 'Must not make another parser call'
        return original(**kwargs)
    gloo.create_response=composition_only
    if bound:
        session.info['verified_onboarding_source']={'incoming_id':incoming.id,'step_id':step.id}
    result=onboarding.handle(session,clock,gate,person,incoming.body,gloo,recorded_step_id=step.id)
    assert person.preferences['onboarding_stage']=='availability'
    assert person.sms_opt_in
    if bound:
        assert result=='onboarding_clarify' and len(provider.sent)==1 and len(gloo.calls)==1
        assert provider.sent[0].body=='Thanks, that helps. What times can you help with Coffee on Wednesday, and how often would you like to serve each month?'
        draft=person.preferences['onboarding_availability_draft']
        assert draft['recurring_windows']==WINDOWS and draft['frequency_known'] is False
        assert 'Coffee' in person.preferences['interested_roles']
        assert 'max_per_month' not in person.preferences
        assert session.scalars(select(m.Message).where(m.Message.direction=='in')).all()==[incoming]
        assert step.result['extraction']['frequency_known'] is True
    else:
        assert result=='onboarding_review' and not provider.sent and not gloo.calls
        assert 'onboarding_availability_draft' not in person.preferences
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Qualification)) is None
