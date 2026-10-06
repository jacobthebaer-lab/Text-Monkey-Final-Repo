"""Submitted question/reply context, prior clock proof and internal mapping."""
import hashlib,json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core import onboarding,conversational_signup as natural
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim,MacInboundReceipt
from tests.test_conversational_signup import natural as natural_fixture,PHONE,proposal


def clocks():
    data=proposal();data['weekdays']=[6,2];data['preferred_services']=[]
    for w,hours in zip(data['recurring_windows'],[('09:00','10:15'),('11:00','12:15'),('18:00','20:00')]):
        w.update(start_time=hours[0],end_time=hours[1],time_mode='clock')
    child=data['recurring_windows'][-1];child.update(weekday=2,month_ordinals=[2])
    return data


@pytest.fixture
def confirmed(session,clock,provider,natural_fixture):
    person,first,selected,gate=natural_fixture
    first.body='Sunday greeting twice monthly. Child Care second Wednesday 6 to 8 PM for the women group.'
    first.created_at=clock.now()-timedelta(seconds=40)
    run=m.AgentRun(agent='onboarding',trigger='Profile availability',started_at=clock.now()-timedelta(seconds=39),
        ended_at=clock.now()-timedelta(seconds=37),outcome='partial_saved')
    session.add(run);session.flush()
    result={'stage':'availability','extraction':clocks()}
    step=m.AgentStep(run_id=run.id,step_no=1,type='decision',result=result,created_at=clock.now()-timedelta(seconds=38))
    session.add(step);session.flush()
    session.add(m.Notification(key='onboarding-turn:'+str(first.id),purpose='onboarding_turn',state='pending',
        volunteer_id=person.id,message_id=first.id,body='',created_at=first.created_at,due_at=first.created_at,
        detail={'session_id':selected.id,'phone':PHONE,'body_hash':hashlib.sha256(first.body.encode()).hexdigest(),
            'step_id':step.id,'step_hash':natural.digest(result)}))
    fingerprint=hashlib.sha256((PHONE+'\0SMS\0'+selected.id+'\0'+first.body).encode()).hexdigest()
    session.add(MacInboundReceipt(guid='original-synthetic-window',fingerprint=fingerprint,result={'session_id':selected.id}))
    question=m.Message(phone=PHONE,volunteer_id=person.id,direction='out',kind='ai',purpose='signup_reply',
        status='submitted',provider_sid=selected.outbound_prefix+'original-question',created_at=clock.now()-timedelta(seconds=20),
        body='Which two Sundays should I use, and is the second Wednesday 6 to 8 PM correct for women group Child Care?')
    session.add(question);session.flush()
    session.add(MacDeliveryClaim(message_id=question.id,token='synthetic-native-token'+'x'*32))
    incoming=m.Message(phone=PHONE,volunteer_id=person.id,direction='in',kind='mac_test_in',purpose='test:'+selected.id,
        status='received',body='First and third and yes that is correct',created_at=clock.now())
    session.add(incoming);session.flush();gate.reply_to_message_id=incoming.id
    draft=clocks();draft.pop('understood');draft.pop('sensitive')
    draft['recurring_windows'][-1].update(time_mode='event',start_time=None,end_time=None)
    draft['pending_constraints'].append({'kind':'event_mapping','description':'women group','role_ids':[5]})
    person.preferences={**person.preferences,'onboarding_availability_draft':draft}
    return person,incoming,selected,gate,question,step,first


def test_gloo_binds_current_reply_to_actual_prompt_and_preserves_known_hours(session,clock,provider,confirmed):
    person,incoming,selected,gate,question,step,first=confirmed
    calls=[]
    def interpret(**kwargs):
        facts=json.loads(kwargs['input']);calls.append(facts)
        if facts.get('recovery'):
            recovery=facts['recovery']
            assert recovery['missing']==[] and recovery['needs_coordinator'] is True
            assert recovery['interpretation_context']['reply_to_submitted_prompt']['message_id']==question.id
            return SimpleNamespace(output_text=json.dumps({'stage':'availability','missing':[],
                'acknowledgment':'I understand your first and third Sundays and second Wednesday hours. Coordinator mapping is pending review.',
                'question':''}))
        context=facts['interpretation_context'];prompt=context['reply_to_submitted_prompt']
        assert prompt['body']==question.body and prompt['incoming_message_id']==incoming.id
        assert any(e['incoming_id']==first.id and e['window']['start_time']=='18:00'
            and e['window']['end_time']=='20:00' for e in context['validated_prior_clock_evidence'])
        data=clocks()
        for w in data['recurring_windows'][:2]:w['month_ordinals']=[1,3]
        data['pending_constraints'].append({'kind':'event_mapping','description':'women group','role_ids':[5]})
        return SimpleNamespace(output_text=json.dumps(data))
    gloo=SimpleNamespace(create_response=interpret,settings=SimpleNamespace(parser_model='synthetic-only'))
    # Use established synthetic model settings while retaining actual deterministic bindings.
    from app.config import Settings
    gloo.settings=Settings(gloo_signup_replies=True)
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_pending_review'
    draft=person.preferences['onboarding_availability_draft']
    assert draft['recurring_windows'][-1]['start_time']=='18:00'
    assert draft['recurring_windows'][-1]['month_ordinals']==[2]
    assert draft['recurring_windows'][0]['month_ordinals']==[1,3]
    assert provider.sent[-1].body.endswith('pending review.') and '?' not in provider.sent[-1].body
    assert len(calls)==2
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_suppressed'
    assert len(calls)==2 and len(provider.sent)==1
    assert step.result['extraction']==clocks()
    assert session.scalar(select(m.Assignment)) is None and session.scalar(select(m.Qualification)) is None
    assert person.preferences['onboarding_stage']=='availability'


@pytest.mark.parametrize('defect',['queued','uncertain','blocked','wrong_phone','wrong_session','missing_claim','future'])
def test_unsubmitted_or_wrong_scope_question_never_becomes_confirmation_context(session,clock,confirmed,defect):
    person,incoming,selected,gate,question,step,first=confirmed
    if defect in {'queued','uncertain','blocked'}:question.status=defect
    if defect=='wrong_phone':question.phone='+15555550999'
    if defect=='wrong_session':question.provider_sid='MAC'+'b'*32+':different'
    if defect=='missing_claim':session.delete(session.get(MacDeliveryClaim,question.id))
    if defect=='future':question.created_at=clock.now()+timedelta(seconds=1)
    session.flush()
    context=natural.interpretation_context(session,person,incoming.id,clock.now())
    assert context['reply_to_submitted_prompt'] is None


def test_event_catalog_mapping_is_internal_but_unknown_group_day_is_a_question():
    draft=clocks();draft['pending_constraints'].append({'kind':'event_mapping','description':'women group','role_ids':[5]})
    assert natural.missing_facts(draft,concise=True)==[]
    draft['recurring_windows'][-1].update(time_mode='event',start_time=None,end_time=None)
    assert natural.missing_facts(draft,concise=True)==[]
    draft['recurring_windows'].pop()
    draft['pending_constraints'].append({'kind':'unresolved_window','proposal':{'weekday':None,'role_ids':[5]}})
    assert 'window_schedule' in natural.missing_facts(draft,concise=True)



def test_answer_after_an_intervening_received_reply_cannot_reuse_old_question(session,clock,confirmed):
    person,incoming,selected,gate,question,step,first=confirmed
    newer=m.Message(phone=PHONE,volunteer_id=person.id,direction='in',kind='mac_test_in',purpose='test:'+selected.id,
        status='received',body='Yes',created_at=clock.now())
    session.add(newer);session.flush()
    assert natural.interpretation_context(session,person,newer.id,clock.now())['reply_to_submitted_prompt'] is None


@pytest.mark.parametrize('defect',['step','body','receipt','unfinished','stop'])
def test_bad_prior_clock_proof_is_not_supplied_as_a_confirmed_fact(session,clock,confirmed,defect):
    person,incoming,selected,gate,question,step,first=confirmed
    if defect=='step':step.result={'stage':'availability','extraction':{}}
    if defect=='body':first.body='Changed historical sender text'
    if defect=='receipt':session.delete(session.get(MacInboundReceipt,'original-synthetic-window'))
    if defect=='unfinished':step.run.ended_at=None
    if defect=='stop':session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
    session.flush()
    context=natural.interpretation_context(session,person,incoming.id,clock.now())
    assert not context.get('validated_prior_clock_evidence')
