"""Scoped real-input contracts with offline model proposals, no native sends."""
import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.core import onboarding, outbound_conversation as conversation
from app.core.conversational_signup import digest, followup_binding, recover_recorded, resume_followup
from app.core.send_gate import SendGate
from app.core.signup_copy import ensure_exact_role_menu
from app.core.signup_recovery import validate_conversational_reply
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.test_sessions import TestSession as RecipientSession
from app.llm.gloo_client import GlooUnavailableError
from tests.test_mac_progress import progress_app

PHONE = '+15555550101'
BODY = 'Greeter first service twice monthly, Production second service the same days. Child Care for the women group monthly. Away in December.'


def proposal():
    return {'understood':True,'sensitive':False,'availability_known':True,'frequency_known':False,
        'weekdays':[6],'all_day':False,'preferred_services':['sun_1','sun_2'],'max_per_month':None,
        'available_dates':[], 'unavailable_dates':['2026-12-01','2026-12-31'],
        'recurring_windows':[
            {'weekday':6,'role_ids':[1],'role_label':'Greeter','any_role':False,'time_mode':'clock',
             'start_time':None,'end_time':None,'all_day':False,'event_context':None},
            {'weekday':6,'role_ids':[3],'role_label':'Production','any_role':False,'time_mode':'clock',
             'start_time':None,'end_time':None,'all_day':False,'event_context':None},
            {'weekday':None,'role_ids':[5],'role_label':'Child Care','any_role':False,'time_mode':'event',
             'start_time':None,'end_time':None,'all_day':False,
             'event_context':{'label':'women group','event_type_ids':[]}}],
        'role_frequency_caps':[{'role_id':1,'role_name':'Greeter','max_per_month':2},
            {'role_id':5,'role_name':'Child Care','max_per_month':1}],
        'pending_constraints':[{'kind':'same_day','description':'Production on the same days as greeting','role_ids':[1,3]}]}


class NaturalGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self, data=None):
        self.data = data or proposal()
        self.calls=[]
    def create_response(self,**kwargs):
        facts=json.loads(kwargs['input']);self.calls.append(facts)
        if facts.get('recovery'):
            recovery=facts['recovery']
            assert recovery['conversational']
            result={'stage':recovery['stage'],'missing':recovery['missing'],
                'acknowledgment':"I understand the role limits and that December is unavailable.",
                'question':"What day does the women's group meet, and should greeting and Production follow the church's first and second services?"}
            if recovery.get('complete'):
                result.update(acknowledgment='Your Sunday preferences are saved locally. Thank you!', question='')
        else:
            result=deepcopy(self.data)
        return SimpleNamespace(output_text=json.dumps(result),usage=None)


@pytest.fixture
def natural(session,clock,provider,make_volunteer):
    ensure_exact_role_menu(session)
    person=make_volunteer('Alex Example',prefs={'onboarding_stage':'availability','signup_minimal_texts':True,
        'interested_roles':['Greeter','Production','Child Care']})
    person.phone=PHONE
    selected=RecipientSession('a'*32,clock.now()-timedelta(minutes=1),clock.now()+timedelta(hours=1))
    session.info['mac_test_session']=selected
    provider.test_sessions={PHONE:selected}
    session.add(m.Policy(key='conversational_signup:'+PHONE,value={'value':True,'session_id':selected.id}))
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}))
    incoming=m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',kind='mac_test_in',
        purpose='test:'+selected.id,body=BODY,created_at=clock.now())
    session.add(incoming);session.flush()
    # The original broad availability prompt already has a durable reservation.
    meta,error=conversation.metadata(session,purpose='signup_reply',volunteer=person,phone=PHONE,
        now=clock.now(),supplied={'intake_fields':['availability']})
    assert not error
    session.add(m.Notification(key=meta['keys'][0],purpose='conversation_delivery',state='sent',
        message_id=999,body='',due_at=clock.now(),created_at=clock.now(),detail=meta))
    session.execute(text('CREATE TABLE admin_workspaces (id TEXT, owner_id TEXT, completed BOOLEAN, revision INTEGER, details TEXT)'))
    session.execute(text('INSERT INTO admin_workspaces VALUES (:id,:owner,1,2,:details)'),
        {'id':'workspace','owner':'owner','details':json.dumps({'service_times':'Sundays 9:00 AM and 11:00 AM','timezone':'America/Denver'})})
    session.flush()
    gate=SendGate(session,clock,provider,reply_to_message_id=incoming.id)
    return person,incoming,selected,gate


def test_actual_partial_preferences_survive_and_natural_followup_is_not_first_question_duplicate(session,clock,provider,natural):
    person,incoming,selected,gate=natural;gloo=NaturalGloo()
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_clarify'
    draft=person.preferences['onboarding_availability_draft']
    assert draft['unavailable_dates']==['2026-12-01','2026-12-31']
    assert [c['max_per_month'] for c in draft['role_frequency_caps']]==[2,1]
    assert draft['preferred_services']==[] and draft['max_per_month'] is None
    assert draft['recurring_windows'][0]['start_time'] is None
    assert {p['kind'] for p in draft['pending_constraints']}=={'unresolved_window','same_day'}
    assert gloo.calls[0]['verified_church_context']['service_times']=='Sundays 9:00 AM and 11:00 AM'
    assert len(gloo.calls)==2 and len(provider.sent)==1
    assert provider.sent[0].body.startswith('I understand') and 'women' in provider.sent[0].body
    assert person.preferences['onboarding_stage']=='availability'
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Qualification))
    assert session.scalars(select(m.Message).where(m.Message.direction=='in')).all()==[incoming]
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_suppressed'
    assert len(gloo.calls)==2 and len(provider.sent)==1


@pytest.mark.parametrize('defect',['newer','body','draft','audit','stop','session','disabled','privacy','care','expired'])
def test_queued_natural_followup_revalidates_immutable_source_and_current_scope(session,clock,provider,natural,defect):
    person,incoming,selected,gate=natural
    onboarding.handle(session,clock,gate,person,incoming.body,NaturalGloo())
    proof={'incoming_id':incoming.id,'turn_key':'onboarding-turn:'+str(incoming.id)}
    assert followup_binding(session,person,proof,clock.now())
    if defect=='newer':
        session.add(m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',kind='mac_test_in',
            purpose=incoming.purpose,body='A correction',created_at=clock.now()));session.flush()
    if defect=='body':incoming.body='Changed source'
    if defect=='draft':person.preferences={**person.preferences,'onboarding_availability_draft':{}}
    if defect=='audit':
        row=session.get(m.Notification,proof['turn_key']);step=session.get(m.AgentStep,row.detail['step_id'])
        step.result={'stage':'availability','extraction':{}}
    if defect=='stop':session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
    if defect=='session':session.info['mac_test_session']=RecipientSession('b'*32,selected.starts_at,selected.expires_at)
    if defect=='disabled':session.get(m.Policy,'conversational_signup:'+PHONE).value={'value':False,'session_id':selected.id}
    if defect in {'privacy','care'}:
        session.add(m.Escalation(category='privacy' if defect=='privacy' else 'sensitive',severity='normal',summary='Hold',
            related_ids={'volunteer_id':person.id},status='open',created_at=clock.now()))
    if defect=='expired':clock.advance(timedelta(hours=2))
    session.flush()
    assert followup_binding(session,person,proof,clock.now()) is None


@pytest.mark.parametrize('bad',['PCO updated your record.','You are all set.','You are qualified.','Your shift is booked.','Thanks — got it.','Reply YES.'])
def test_natural_wording_does_not_create_operational_claims(bad):
    with pytest.raises(ValueError):validate_conversational_reply(json.dumps({'stage':'availability','missing':['availability'],
        'acknowledgment':bad,'question':'What time is the group?'}),{'stage':'availability','missing':['availability']})


def test_recorded_native_input_recovery_repairs_with_gloo_without_new_message_or_receipt(session,clock,provider,natural):
    person,incoming,selected,gate=natural
    old=proposal();old.pop('pending_constraints')
    run=m.AgentRun(agent='onboarding',trigger='Profile availability',model='synthetic',started_at=clock.now(),
        ended_at=clock.now(),outcome='needs_clarification')
    session.add(run);session.flush()
    step=m.AgentStep(run_id=run.id,step_no=1,type='decision',created_at=clock.now(),
        result={'stage':'availability','extraction':old})
    guid='synthetic-original-guid'
    fingerprint=hashlib.sha256((PHONE+'\0SMS\0'+selected.id+'\0'+BODY).encode()).hexdigest()
    receipt=MacInboundReceipt(guid=guid,fingerprint=fingerprint,result={'intent':'onboarding_suppressed','session_id':selected.id})
    session.add_all([step,receipt]);session.flush()
    original=deepcopy(step.result)
    repaired=proposal();repaired.update(unavailable_dates=[],role_frequency_caps=[])
    gloo=NaturalGloo(repaired)
    args={'receipt_guid':guid,'step_id':step.id,'extraction_hash':digest(old)}
    assert recover_recorded(session,clock,gate,person,gloo,**args)=='onboarding_clarify'
    assert gloo.calls[0]['repair_evidence']['step_id']==step.id
    assert person.preferences['onboarding_availability_draft']['unavailable_dates']==['2026-12-01','2026-12-31']
    assert len(person.preferences['onboarding_availability_draft']['role_frequency_caps'])==2
    assert step.result==original and receipt.result=={'intent':'onboarding_suppressed','session_id':selected.id}
    assert session.scalars(select(m.Message).where(m.Message.direction=='in')).all()==[incoming]
    assert recover_recorded(session,clock,gate,person,gloo,**args)=='onboarding_suppressed'
    assert len(provider.sent)==1 and len(gloo.calls)==2


def test_wrong_recorded_hash_cannot_generate_recovery(session,clock,natural):
    person,incoming,_,gate=natural
    with pytest.raises(GlooUnavailableError):
        recover_recorded(session,clock,gate,person,NaturalGloo(),receipt_guid='missing',step_id=999,
            extraction_hash='not-original')


def test_malformed_interest_reply_gets_source_bound_natural_question(session,clock,provider,natural):
    person,incoming,_,gate=natural
    person.preferences={'onboarding_stage':'interests'}
    incoming.body='A role not in the list'
    gloo=NaturalGloo({'understood':False})
    original=gloo.create_response
    def response(**kwargs):
        facts=json.loads(kwargs['input'])
        if facts.get('recovery'):
            gloo.calls.append(facts)
            return SimpleNamespace(output_text=json.dumps({'stage':'interests','missing':['interests'],
                'acknowledgment':'I can help you find an available volunteer role.',
                'question':'Would you like Greeter, Usher, Production, Coffee or Child Care?'}),usage=None)
        return original(**kwargs)
    gloo.create_response=response
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_clarify'
    assert len(provider.sent)==1 and len(gloo.calls)==2
    assert 'interested_roles' not in person.preferences


def test_gloo_failure_can_explicitly_resume_unsent_composition_without_reinterpreting_source(session,clock,provider,natural):
    person,incoming,_,gate=natural
    failing=NaturalGloo()
    original=failing.create_response
    def fail_composition(**kwargs):
        if json.loads(kwargs['input']).get('recovery'):
            raise GlooUnavailableError('Offline')
        return original(**kwargs)
    failing.create_response=fail_composition
    assert onboarding.handle(session,clock,gate,person,incoming.body,failing)=='onboarding_review'
    key='onboarding-turn:'+str(incoming.id);turn=session.get(m.Notification,key)
    audit=deepcopy(turn.detail);draft=deepcopy(person.preferences['onboarding_availability_draft'])
    assert not provider.sent
    composer=NaturalGloo()
    assert resume_followup(session,clock,gate,person,composer,turn_key=key,step_hash=audit['step_hash'])=='onboarding_clarify'
    assert len(composer.calls)==1 and composer.calls[0].get('recovery')
    assert person.preferences['onboarding_availability_draft']==draft and turn.detail==audit
    assert resume_followup(session,clock,gate,person,composer,turn_key=key,step_hash=audit['step_hash'])=='onboarding_suppressed'
    assert len(provider.sent)==1 and len(composer.calls)==1


def test_deferred_natural_completion_caches_composition_despite_real_time_advancing(progress_app,clock):
    from fastapi.testclient import TestClient
    from app.integrations import mac_progress
    from tests.test_mac_progress import incoming,post,submit_ack,wait_worker,PHONE as MAC_PHONE
    application=progress_app
    class AdvancingGloo(NaturalGloo):
        settings=application.state.settings
        def create_response(self,**kwargs):
            clock.advance(timedelta(seconds=1))
            facts=json.loads(kwargs['input'])
            if facts.get('approved_message')==mac_progress.ACK_TEXT:
                self.calls.append(facts)
                return SimpleNamespace(output_text=mac_progress.ACK_TEXT,usage=None)
            return super().create_response(**kwargs)
    application.state.gloo=AdvancingGloo({'understood':True,'availability_known':True,'frequency_known':True,
        'weekdays':[6],'all_day':True,'preferred_services':[],'max_per_month':2,
        'available_dates':[],'unavailable_dates':[],'pending_constraints':[]})
    with application.state.session_factory() as db:
        selected=application.state.provider.test_sessions[MAC_PHONE]
        db.add(m.Policy(key='conversational_signup:'+MAC_PHONE,value={'value':True,'session_id':selected.id}))
        db.commit()
    with TestClient(application) as client:
        accepted=post(client,'/mac/inbound',incoming())
        assert accepted.status_code==200 and accepted.json()['progress_state']=='waiting_ack'
        submit_ack(client)
        post(client,'/mac/progress/tick');wait_worker(application)
        with application.state.session_factory() as db:
            job=db.get(m.Notification,accepted.json()['progress_key'])
            assert job.state=='done',job.detail
            assert job.detail['route']=='onboarding_complete'
            outgoing=db.scalars(select(m.Message).where(m.Message.direction=='out')).all()
            assert len(outgoing)==2 and 'saved locally' in outgoing[-1].body
        assert len(application.state.gloo.calls)==3


def test_invalid_role_proposal_never_promotes_qualification_or_valid_window(session,clock,provider,natural):
    person,incoming,_,gate=natural;data=proposal()
    data['recurring_windows'][0]['role_ids']=[999]
    assert onboarding.handle(session,clock,gate,person,incoming.body,NaturalGloo(data))=='onboarding_clarify'
    draft=person.preferences['onboarding_availability_draft']
    assert all(999 not in w['role_ids'] for w in draft['recurring_windows'])
    assert not session.scalar(select(m.Qualification))


def test_short_final_availability_has_one_truthful_source_bound_completion(session,clock,provider,natural):
    person,incoming,_,gate=natural
    incoming.body='Sundays all day, twice per month'
    data={'understood':True,'availability_known':True,'frequency_known':True,'weekdays':[6],
        'all_day':True,'preferred_services':[],'max_per_month':2,'available_dates':[],
        'unavailable_dates':[],'pending_constraints':[]}
    gloo=NaturalGloo(data)
    # Opt-in is independent from the legacy exact-question policy.
    session.delete(session.get(m.Policy,'signup_exact_copy:'+PHONE));session.flush()
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_complete'
    assert person.preferences['onboarding_stage']=='complete'
    assert provider.sent[0].body=='Your Sunday preferences are saved locally. Thank you!'
    proof={'incoming_id':incoming.id,'turn_key':'onboarding-turn:'+str(incoming.id)}
    assert followup_binding(session,person,proof,clock.now())
    assert len(gloo.calls)==2 and len(provider.sent)==1
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo) is None
    assert len(provider.sent)==1 and len(gloo.calls)==2
