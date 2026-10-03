"""Exceptional recovery stays grounded; all transports/models are synthetic."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.db import models as m
from app.core.onboarding import validated_availability
from app.core.signup_recovery import validate_reply
from app.core.signup_copy import WELCOME
from app.llm.gloo_client import GlooUnavailableError
from tests.test_concise_signup import PHONE,route
from tests.test_exact_signup_copy import ExactGloo,EXPECTED
from tests.test_mac_messages import mac_app

class AdaptiveGloo(ExactGloo):
    def create_response(self,**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,list):
            self.calls.append(facts)
            body=facts[-1]['body']
            valid=body in ('Alex','Example','Alex Example')
            first='Alex' if body!='Example' else ''
            last='Example' if body in ('Example','Alex Example') else ''
            return SimpleNamespace(output_text=json.dumps({'signup':valid,
                'identity_reply':valid,'first_name':first if valid else '',
                'last_name':last,'sensitive':False}))
        if isinstance(facts,dict) and facts.get('recovery'):
            self.calls.append(facts)
            recovery=facts['recovery']
            ack='Thanks, that helps.' if recovery['saved_answers'] else 'I can help with volunteer signup here.'
            return SimpleNamespace(output_text=json.dumps({'stage':recovery['stage'],
                'missing':recovery['missing'],'acknowledgment':ack,'question':facts['approved_message']}))
        if isinstance(facts,dict) and 'stage' in facts and facts['body'] in ('Who won the game?','Pizza recipe'):
            self.calls.append(facts)
            return SimpleNamespace(output_text='{"understood":false}')
        if isinstance(facts,dict) and facts.get('stage')=='availability' and facts['body']=='Twice a month':
            self.calls.append(facts)
            return SimpleNamespace(output_text=json.dumps({'understood':True,'frequency_known':True,'max_per_month':2}))
        if isinstance(facts,dict) and facts.get('stage')=='availability' and facts['body']=='Sundays and Wednesdays all day':
            self.calls.append(facts)
            return SimpleNamespace(output_text=json.dumps({'understood':True,'availability_known':True,
                'weekdays':[6,2],'all_day':True,'preferred_services':[]}))
        return super().create_response(**kwargs)

@pytest.fixture
def adaptive(session):
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}));session.flush()
    return AdaptiveGloo()

def start(session,clock,provider,adaptive,stage):
    route(session,clock,provider,'Hello',adaptive)
    if stage!='name':route(session,clock,provider,'Alex Example',adaptive)
    if stage=='availability':route(session,clock,provider,'Anything',adaptive)

@pytest.mark.parametrize('stage',['name','interests','availability'])
def test_no_progress_reply_never_repeats_requested_intake(session,clock,provider,adaptive,stage):
    start(session,clock,provider,adaptive,stage)
    count=len(provider.sent)
    for _ in range(2):
        result=route(session,clock,provider,'Who won the game?',adaptive)
        assert result.routed_to==('signup_intake_suppressed' if stage=='name' else 'onboarding_suppressed')
    assert len(provider.sent)==count
    assert not any(isinstance(f,dict) and f.get('recovery') for f in adaptive.calls)
    person=session.scalar(select(m.Volunteer))
    assert person is None if stage=='name' else person.preferences['onboarding_stage']==stage
    assert session.scalar(select(m.Qualification)) is None
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Notification).where(m.Notification.purpose=='conversation_suppression'))

def test_partial_name_asks_only_last_name_then_real_last_reply_advances(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'name')
    assert route(session,clock,provider,'Alex',adaptive).routed_to=='signup_name_needed'
    assert provider.sent[-1].body=="What's your last name?"
    assert session.scalar(select(m.Volunteer)) is None
    assert route(session,clock,provider,'Example',adaptive).routed_to=='onboarding_interests'
    person=session.scalar(select(m.Volunteer))
    assert person.name=='Alex Example' and person.sms_opt_in
    assert person.preferences['consent_source']=='sms_name_reply_to_exact_invitation'
    assert provider.sent[-1].body==EXPECTED[1]


def test_unrelated_two_words_are_not_a_name_even_if_parser_proposes_them(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'name')
    original=adaptive.create_response
    def wrong_name(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,list):return SimpleNamespace(output_text='{"signup":true,"identity_reply":false,"first_name":"Pizza","last_name":"Recipe"}')
        return original(**kwargs)
    adaptive.create_response=wrong_name
    count=len(provider.sent)
    assert route(session,clock,provider,'Pizza Recipe',adaptive).routed_to=='signup_intake_suppressed'
    assert len(provider.sent)==count
    assert session.scalar(select(m.Volunteer)) is None

def test_frequency_partial_saves_then_asks_days_only(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'availability')
    assert route(session,clock,provider,'Twice a month',adaptive).routed_to=='onboarding_clarify'
    person=session.scalar(select(m.Volunteer))
    draft=person.preferences['onboarding_availability_draft']
    assert draft['max_per_month']==2 and draft['frequency_known'] and not draft['availability_known']
    assert provider.sent[-1].body=='Which days or dates can you serve? You can also say "Flexible".'
    assert route(session,clock,provider,'Sundays and Wednesdays all day',adaptive).routed_to=='onboarding_complete'
    assert person.preferences['max_per_month']==2
    assert provider.sent[-1].body!=EXPECTED[3]
    assert person.preferences['onboarding_stage']=='complete'

@pytest.mark.parametrize('stage',['name','interests','availability'])
@pytest.mark.parametrize('failure',['unavailable','invalid'])
def test_recovery_failure_holds_without_template_or_advancement(session,clock,provider,adaptive,make_volunteer,stage,failure):
    if stage=='interests':
        # A durable profile may need its first essential question after a prior interruption.
        from app.core.signup_copy import ensure_exact_role_menu
        ensure_exact_role_menu(session)
        person=make_volunteer('Alex Example',prefs={'onboarding_stage':'interests'})
        person.phone=PHONE;session.flush()
    else:
        start(session,clock,provider,adaptive,stage)
    reply={'name':'Alex','interests':'Who won the game?','availability':'Twice a month'}[stage]
    original=adaptive.create_response
    def fail(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and facts.get('recovery'):
            if failure=='unavailable':raise GlooUnavailableError('Synthetic outage')
            return SimpleNamespace(output_text='{"stage":"completion","missing":[],"acknowledgment":"All set","question":"Reply YES"}')
        return original(**kwargs)
    adaptive.create_response=fail
    count=len(provider.sent)
    assert route(session,clock,provider,reply,adaptive).routed_to==('signup_identity_review' if stage=='name' else 'onboarding_review')
    assert len(provider.sent)==count
    person=session.scalar(select(m.Volunteer))
    assert person is None if stage=='name' else person.preferences['onboarding_stage']==stage
    assert session.scalar(select(m.Escalation).where(m.Escalation.category=='system_error'))
    adaptive.create_response=original
    retry=route(session,clock,provider,reply,adaptive)
    assert retry.routed_to==('signup_name_needed' if stage=='name' else 'onboarding_clarify')
    assert len(provider.sent)==count+1
    route(session,clock,provider,reply,adaptive)
    assert len(provider.sent)==count+1

@pytest.mark.parametrize('change',[{'stage':'completion'},{'missing':['frequency']},{'question':'Reply YES to finish.'},{'acknowledgment':'You are booked.'},{'acknowledgment':'What role?'},{'acknowledgment':'I saved your availability.'}])
def test_recovery_rejects_added_steps_claims_and_questions(change):
    recovery={'stage':'name','missing':['last_name']}
    data={'stage':'name','missing':['last_name'],'acknowledgment':'Thanks, that helps.',"question":"What's your last name?",**change}
    with pytest.raises(ValueError):validate_reply(json.dumps(data),recovery,"What's your last name?")

def test_repeated_off_base_stays_silent_and_stop_dominates(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'interests')
    route(session,clock,provider,'Who won the game?',adaptive)
    route(session,clock,provider,'Who won the game?',adaptive)
    count=len(provider.sent)
    assert route(session,clock,provider,'Who won the game?',adaptive).routed_to=='onboarding_suppressed'
    assert not any(isinstance(f,dict) and f.get('recovery') for f in adaptive.calls)
    assert len(provider.sent)==count
    assert route(session,clock,provider,'STOP',adaptive).routed_to=='stop'
    person=session.scalar(select(m.Volunteer));assert not person.sms_opt_in

WINDOWS=[
 {'weekday':6,'role_ids':[1],'role_label':'Greeter','any_role':False,'start_time':'08:00','end_time':'10:00','all_day':False,'event_context':None},
 {'weekday':2,'role_ids':[4],'role_label':'Coffee','any_role':False,'start_time':None,'end_time':None,'all_day':False,'event_context':{'label':'Weekly workshop','event_type_ids':[]}},
]

def test_role_windows_retained_frequency_only_recovery_and_new_interest_saved(session,clock,provider,adaptive,make_shift):
    start(session,clock,provider,adaptive,'availability')
    person=session.scalar(select(m.Volunteer))
    person.preferences={**person.preferences,'interested_roles':['Greeter','Usher','Child Care'],'any_role':False}
    original=adaptive.create_response
    def with_windows(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and facts.get('stage')=='availability' and facts['body']=='Greeter Sunday 8-10; Wednesday workshop coffee':
            adaptive.calls.append(facts)
            assert 'recurring_windows' in kwargs['instructions']
            assert 'event_types' in facts and facts['selected_roles']==['Greeter','Usher','Child Care']
            return SimpleNamespace(output_text=json.dumps({'understood':True,'availability_known':True,
                'frequency_known':False,'weekdays':[6,2],'all_day':False,'preferred_services':[],
                'max_per_month':None,'available_dates':[],'unavailable_dates':[],'recurring_windows':WINDOWS}))
        if isinstance(facts,dict) and facts.get('stage')=='availability' and facts['body']=='Coffee Wednesday 13:00-14:00':
            updated=[dict(window) for window in WINDOWS]
            updated[1]={**updated[1],'start_time':'13:00','end_time':'14:00'}
            return SimpleNamespace(output_text=json.dumps({'understood':True,'recurring_windows':updated}))
        return original(**kwargs)
    adaptive.create_response=with_windows
    assert route(session,clock,provider,'Greeter Sunday 8-10; Wednesday workshop coffee',adaptive).routed_to=='onboarding_clarify'
    assert person.preferences['onboarding_stage']=='availability'
    assert person.preferences['onboarding_availability_draft']['recurring_windows']==WINDOWS
    assert 'Coffee' in person.preferences['interested_roles']
    assert provider.sent[-1].body=='What times can you help with Coffee on Wednesday?'
    assert 'max_per_month' not in person.preferences
    count=len(provider.sent)
    assert route(session,clock,provider,'Twice a month',adaptive).routed_to=='onboarding_suppressed'
    assert len(provider.sent)==count
    assert person.preferences['onboarding_availability_draft']['recurring_windows']==WINDOWS
    assert provider.sent[-1].body=='What times can you help with Coffee on Wednesday?'
    assert route(session,clock,provider,'Coffee Wednesday 13:00-14:00',adaptive).routed_to=='onboarding_complete'
    assert person.preferences['recurring_windows'][0]==WINDOWS[0]
    assert person.preferences['recurring_windows'][1]['start_time']=='13:00'
    assert person.preferences['max_per_month']==2
    assert provider.sent[-1].body!=EXPECTED[3]
    assert person.preferences['onboarding_stage']=='complete'
    from datetime import timedelta
    from app.core.eligibility import check
    shift=make_shift('Greeter',starts=clock.now()+timedelta(days=3),minutes=60)
    from zoneinfo import ZoneInfo
    assert shift.event.starts_at.astimezone(ZoneInfo('America/Denver')).hour==10
    result=check(session,person,shift)
    assert not result and any('recurring availability' in reason for reason in result.reasons)
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Qualification)) is None

@pytest.mark.parametrize('stage',['name','interests','availability'])
def test_privacy_request_holds_without_signup_redirect(session,clock,provider,adaptive,stage):
    start(session,clock,provider,adaptive,stage)
    count=len(provider.sent)
    route(session,clock,provider,'Delete my data',adaptive)
    assert len(provider.sent)==count
    assert session.scalar(select(m.Escalation).where(m.Escalation.category=='privacy'))

@pytest.mark.parametrize('stage',['name','interests','availability'])
def test_sensitive_reply_retains_care_hold(session,clock,provider,adaptive,stage):
    start(session,clock,provider,adaptive,stage)
    count=len(provider.sent)
    assert route(session,clock,provider,'My father died',adaptive).routed_to=='escalated_sensitive'
    assert len(provider.sent)==count

@pytest.mark.parametrize('mode',['baseline','recovery'])
def test_em_dash_from_gloo_is_never_sent(session,clock,provider,adaptive,mode):
    if mode=='baseline':
        from app.core.signup_copy import compose_welcome
        adaptive.create_response=lambda **kwargs:SimpleNamespace(output_text=WELCOME+' — Hello')
        with pytest.raises(GlooUnavailableError):compose_welcome(session,clock,adaptive,PHONE)
        assert not provider.sent
    else:
        start(session,clock,provider,adaptive,'availability')
        original=adaptive.create_response
        def with_dash(**kwargs):
            facts=json.loads(kwargs['input'])
            if isinstance(facts,dict) and facts.get('recovery'):
                recovery=facts['recovery']
                return SimpleNamespace(output_text=json.dumps({'stage':recovery['stage'],
                    'missing':recovery['missing'],'question':facts['approved_message'],
                    'acknowledgment':'Thanks — that helps.'}))
            return original(**kwargs)
        adaptive.create_response=with_dash
        count=len(provider.sent)
        assert route(session,clock,provider,'Twice a month',adaptive).routed_to=='onboarding_review'
        assert len(provider.sent)==count

@pytest.mark.parametrize('reply',['NO',"I don't want to volunteer"])
def test_explicit_decline_does_not_receive_name_redirect(session,clock,provider,adaptive,reply):
    start(session,clock,provider,adaptive,'name')
    count=len(provider.sent)
    assert route(session,clock,provider,reply,adaptive).routed_to=='signup_declined'
    assert len(provider.sent)==count and session.scalar(select(m.Volunteer)) is None

def test_name_parts_from_another_session_cannot_complete_identity(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'name')
    route(session,clock,provider,'Alex',adaptive)
    from datetime import timedelta
    from app.integrations.test_sessions import TestSession as RecipientSession
    session.info['mac_test_session']=RecipientSession('f'*32,clock.now()-timedelta(minutes=1),clock.now()+timedelta(minutes=10))
    session.add(m.Message(direction='out',phone=PHONE,body=WELCOME,purpose='signup_reply',status='sent',kind='ai',
        provider_sid='MAC'+'f'*32+':new',created_at=clock.now()))
    session.flush()
    assert route(session,clock,provider,'Example',adaptive).routed_to=='signup_name_needed'
    assert session.scalar(select(m.Volunteer)) is None
    assert provider.sent[-1].body.endswith("What's your first name?")

def test_checked_windows_are_authoritative_without_guessing_legacy_service_enums(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'availability')
    from app.core.onboarding import availability_context
    person=session.scalar(select(m.Volunteer))
    previous=availability_context(session,person,clock.now().date())
    roles=session.scalars(select(m.Role)).all()
    draft=validated_availability({'recurring_windows':WINDOWS,'preferred_services':['sun_8-10','wed_workshop']},
        previous,clock.now().date(),roles=roles)
    assert draft['availability_known'] and draft['preferred_services']==[]
    assert draft['recurring_windows']==WINDOWS
    assert draft['frequency_known'] is False
    assert draft['max_per_month'] is None


def test_frequency_progress_asks_days_once_and_cap_correction_does_not_repeat(session,clock,provider,adaptive):
    start(session,clock,provider,adaptive,'availability')
    assert route(session,clock,provider,'Twice a month',adaptive).routed_to=='onboarding_clarify'
    assert provider.sent[-1].body=='Which days or dates can you serve? You can also say "Flexible".'
    count=len(provider.sent)
    assert route(session,clock,provider,'Twice a month',adaptive).routed_to=='onboarding_suppressed'
    original=adaptive.create_response
    def correction(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and facts.get('stage')=='availability' and facts['body']=='Three times a month':
            return SimpleNamespace(output_text=json.dumps({'understood':True,'frequency_known':True,'max_per_month':3}))
        return original(**kwargs)
    adaptive.create_response=correction
    assert route(session,clock,provider,'Three times a month',adaptive).routed_to=='onboarding_suppressed'
    assert len(provider.sent)==count
    person=session.scalar(select(m.Volunteer))
    assert person.preferences['onboarding_availability_draft']['max_per_month']==3
    assert route(session,clock,provider,'Sundays and Wednesdays all day',adaptive).routed_to=='onboarding_complete'
    assert person.preferences['max_per_month']==3 and len(provider.sent)==count


def test_partial_name_question_expires_if_actual_last_name_arrives_before_native_claim(mac_app):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from tests.test_mac_messages import PHONE as NATIVE_PHONE, incoming, post
    from tests.test_independent_text_database import submit_queued
    app=mac_app
    app.state.settings=replace(app.state.settings,allow_text_signup=True,gloo_signup_replies=True)
    app.state.gloo=AdaptiveGloo()
    with app.state.session_factory() as database:
        database.delete(database.scalar(select(m.Volunteer)))
        database.add(m.Policy(key='signup_exact_copy:'+NATIVE_PHONE,value={'value':True}));database.commit()
    with TestClient(app) as client:
        assert post(client,'/mac/inbound',incoming('progress-welcome','Hello')).json()['intent']=='signup_invitation'
        submit_queued(client,WELCOME)
        assert post(client,'/mac/inbound',incoming('progress-first','Alex')).json()['intent']=='signup_name_needed'
        item=post(client,'/mac/outbound/pull').json()['messages'][0]
        assert item['body']=="What's your last name?"
        assert post(client,'/mac/inbound',incoming('progress-last','Example')).json()['intent']=='onboarding_interests'
        assert post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']}).status_code==409
    with app.state.session_factory() as database:
        person=database.scalar(select(m.Volunteer))
        assert person.name=='Alex Example' and person.sms_opt_in and not person.is_coordinator and not person.is_pastor


@pytest.mark.parametrize('role_caps', [[], [{'role_id':1,'role_name':'Greeter','max_per_month':2}]])
def test_complete_role_windows_never_demand_optional_global_frequency(session,clock,provider,adaptive,role_caps):
    start(session,clock,provider,adaptive,'availability')
    windows=[{**window,'start_time':'13:00','end_time':'14:00','event_context':None}
             if window['weekday']==2 else dict(window) for window in WINDOWS]
    original=adaptive.create_response
    def complete(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and facts.get('stage')=='availability':
            return SimpleNamespace(output_text=json.dumps({'understood':True,'availability_known':True,
                'frequency_known':False,'weekdays':[6,2],'all_day':False,'recurring_windows':windows,
                'role_frequency_caps':role_caps,'preferred_services':[]}))
        return original(**kwargs)
    adaptive.create_response=complete
    count=len(provider.sent)
    assert route(session,clock,provider,'Sunday Greeter 8-10, Wednesday Coffee 13-14',adaptive).routed_to=='onboarding_complete'
    person=session.scalar(select(m.Volunteer))
    assert len(provider.sent)==count and person.preferences['onboarding_stage']=='complete'
    assert person.preferences['availability_frequency_known'] is False and 'max_per_month' not in person.preferences
    assert person.preferences.get('role_frequency_caps',[])==role_caps
    assert person.preferences['recurring_windows']==windows
    assert session.scalar(select(m.Assignment)) is None and session.scalar(select(m.Qualification)) is None
