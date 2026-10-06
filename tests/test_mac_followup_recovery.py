"""No native calls: only a proven never-claimed Mac rejection may have a successor."""
import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import onboarding, outbound_conversation
from app.core.conversational_signup import digest, followup_binding
from app.core.mac_followup_recovery import recover_blocked_followup
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt, MacDeliveryClaim
from app.integrations.mac_progress import profile_hash
from app.llm.gloo_client import GlooUnavailableError
from tests.test_conversational_signup import natural, NaturalGloo, PHONE
from tests.test_mac_natural_accuracy import TEXT, ReviewGloo, known_proposal


@pytest.fixture
def rejected(session,clock,provider,natural):
    person,incoming,selected,gate=natural
    incoming.body=TEXT
    earlier=known_proposal();earlier.pop('understood');earlier.pop('sensitive')
    person.preferences={**person.preferences,'onboarding_availability_draft':deepcopy(earlier)}
    assert onboarding.handle(session,clock,gate,person,incoming.body,NaturalGloo({'understood':False}))=='onboarding_clarify'
    original=session.get(m.Notification,'onboarding-turn:'+str(incoming.id))
    blocked=session.scalar(select(m.Message).where(m.Message.direction=='out'))
    # Portable fixture of the actual pre-native mismatch, not an attempted send.
    blocked.status='blocked_policy';blocked.provider_sid=selected.outbound_prefix+'f'*24
    receipt=session.get(m.Notification,'conversation-message:'+str(blocked.id))
    for key in receipt.detail['keys']:session.get(m.Notification,key).state='queued'
    person.preferences={**person.preferences,'onboarding_availability_draft':deepcopy(earlier)}
    reason='Conversational followup needs its current sender and validated draft'
    outbound_conversation.record_suppression(session,PHONE,'signup_reply',blocked.body,clock.now(),reason)
    suppression=session.scalar(select(m.Notification).where(m.Notification.purpose=='conversation_suppression',
        m.Notification.detail['reason'].as_string()==reason))
    guid='synthetic-recovery-input'
    fingerprint=hashlib.sha256((PHONE+'\0SMS\0'+selected.id+'\0'+incoming.body).encode()).hexdigest()
    session.add(MacInboundReceipt(guid=guid,fingerprint=fingerprint,
        result={'session_id':selected.id,'intent':'onboarding_clarify'}));session.flush()
    args={'blocked_id':blocked.id,'receipt_guid':guid,'suppression_key':suppression.key,
        'original_turn_hash':digest(original.detail),'step_hash':original.detail['step_hash'],
        'blocked_body_hash':hashlib.sha256(blocked.body.encode()).hexdigest(),
        'expected_profile_hash':profile_hash(person)}
    return person,incoming,selected,gate,blocked,original,args


def test_proven_pre_native_rejection_gets_one_fresh_gloo_successor_with_immutable_donor(session,clock,provider,rejected):
    person,incoming,selected,gate,blocked,original,args=rejected
    original_detail=deepcopy(original.detail);oldbody=blocked.body;oldsid=blocked.provider_sid
    native_receipt=deepcopy(session.get(MacInboundReceipt,args['receipt_guid']).result)
    gloo=ReviewGloo(known_proposal())
    assert recover_blocked_followup(session,clock,gate,person,gloo,**args)=='onboarding_pending_review'
    successor=session.scalar(select(m.Message).where(m.Message.direction=='out',m.Message.id!=blocked.id))
    assert successor and '?' not in successor.body
    assert len(gloo.calls)==2
    assert original.detail==original_detail and blocked.body==oldbody
    assert blocked.status=='blocked_policy' and blocked.provider_sid==oldsid
    assert session.get(MacInboundReceipt,args['receipt_guid']).result==native_receipt
    assert not session.scalar(select(MacDeliveryClaim))
    assert session.scalars(select(m.Message).where(m.Message.direction=='in')).all()==[incoming]
    turn=session.get(m.Notification,'onboarding-turn-recovery:'+str(incoming.id))
    assert turn.detail['recovery_key']=='mac-followup-recovery:'+str(incoming.id)
    assert recover_blocked_followup(session,clock,gate,person,gloo,**args)=='onboarding_suppressed'
    assert len(gloo.calls)==2
    assert len(session.scalars(select(m.Message).where(m.Message.direction=='out')).all())==2
    session.commit()
    with Session(session.bind) as fresh:
        fresh.info['mac_test_session']=selected
        assert followup_binding(fresh,fresh.get(m.Volunteer,person.id),
            {'incoming_id':incoming.id,'turn_key':turn.key},clock.now())


@pytest.mark.parametrize('defect',['claim','sent','uncertain','body','audit','receipt','source','stop','session',
    'disabled','newer','profile','reason','reservation','metadata','other_uncertain'])
def test_original_scope_or_any_possible_native_attempt_holds_before_gloo(session,clock,provider,rejected,defect):
    person,incoming,selected,gate,blocked,original,args=rejected
    if defect=='claim':session.add(MacDeliveryClaim(message_id=blocked.id,token='x'*64))
    if defect=='sent':blocked.status='sent'
    if defect=='uncertain':blocked.status='uncertain'
    if defect=='body':blocked.body+='Changed'
    if defect=='audit':
        step=session.get(m.AgentStep,original.detail['step_id']);step.result={**step.result,'tampered':True}
    if defect=='receipt':session.get(MacInboundReceipt,args['receipt_guid']).fingerprint='0'*64
    if defect=='source':incoming.body='Different original input'
    if defect=='stop':session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
    if defect=='session':session.info.pop('mac_test_session')
    if defect=='disabled':session.get(m.Policy,'conversational_signup:'+PHONE).value={'value':False,'session_id':selected.id}
    if defect=='newer':session.add(m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',
        kind=incoming.kind,purpose=incoming.purpose,body='New input',created_at=clock.now()))
    if defect=='profile':person.preferences={**person.preferences,'new_fact':'changed'}
    if defect=='reason':session.get(m.Notification,args['suppression_key']).detail={'purpose':'signup_reply','reason':'Other hold'}
    recorded=session.get(m.Notification,'conversation-message:'+str(blocked.id))
    if defect=='reservation':session.get(m.Notification,recorded.detail['keys'][0]).state='sent'
    if defect=='metadata':recorded.detail={**recorded.detail,'extra':'changed'}
    if defect=='other_uncertain':session.add(m.Message(phone=PHONE,volunteer_id=person.id,direction='out',status='uncertain',
        kind='template',purpose='signup_reply',provider_sid=selected.outbound_prefix+'u'*24,body='Other submission',created_at=clock.now()))
    session.flush();gloo=ReviewGloo(known_proposal())
    with pytest.raises(GlooUnavailableError):recover_blocked_followup(session,clock,gate,person,gloo,**args)
    assert gloo.calls==[]
    assert session.get(m.Notification,'mac-followup-recovery:'+str(incoming.id)) is None


def test_successor_native_preflight_revalidates_original_never_claimed_proof(session,clock,provider,rejected):
    person,incoming,_,gate,blocked,_,args=rejected
    recover_blocked_followup(session,clock,gate,person,ReviewGloo(known_proposal()),**args)
    successor=session.scalar(select(m.Message).where(m.Message.direction=='out',m.Message.id!=blocked.id))
    assert outbound_conversation.queued_problem(session,successor,clock.now()) is None
    session.add(MacDeliveryClaim(message_id=blocked.id,token='x'*64));session.flush()
    assert outbound_conversation.queued_problem(session,successor,clock.now())


def test_composition_outage_continues_one_turn_without_second_interpretation(session,clock,provider,rejected):
    person,incoming,_,gate,blocked,_,args=rejected
    gloo=ReviewGloo(known_proposal());original=gloo.create_response
    def unavailable(**kwargs):
        if json.loads(kwargs['input']).get('recovery'):raise GlooUnavailableError('Offline fixture')
        return original(**kwargs)
    gloo.create_response=unavailable
    assert recover_blocked_followup(session,clock,gate,person,gloo,**args)=='onboarding_review'
    assert session.get(m.Notification,'onboarding-turn-recovery:'+str(incoming.id))
    recovered=ReviewGloo(known_proposal())
    assert recover_blocked_followup(session,clock,gate,person,recovered,**args)=='onboarding_pending_review'
    assert len(recovered.calls)==1 and recovered.calls[0].get('recovery')
    assert len(session.scalars(select(m.Message).where(m.Message.direction=='out')).all())==2
