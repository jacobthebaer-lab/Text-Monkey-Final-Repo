"""Expired texting permission never prevents exact internal profile review."""
from copy import deepcopy
from datetime import timedelta
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core import conversational_signup as natural_source, signup_preference_review as review
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt,MacDeliveryClaim
from app.integrations.profile_models import ProfileOutbox
from tests.test_signup_preference_review import review_app, choices, natural, PHONE
from tests.test_mac_followup_recovery import rejected


def test_expired_texting_epoch_retains_usable_internal_review_and_fresh_ttl(session,clock,review_app):
    app,person,incoming,selected,group=review_app
    app.state.settings=replace(app.state.settings,competition_confirmation_required=True)
    clock.advance(timedelta(hours=3))
    assert not selected.active(clock.now())
    assert natural_source.source(session,person,incoming.id,clock.now()) is None
    before_messages=list(session.scalars(select(m.Message.id)))
    with TestClient(app) as client:
        card=client.get('/api/state').json()['signup_preference_drafts'][0]
        staged=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(card,group))
        assert staged.status_code==200,staged.text
        result=staged.json();approval=session.get(m.Approval,result['approval_id'])
        assert approval.requested_at==clock.now()
        assert __import__('datetime').datetime.fromisoformat(approval.payload['expires_at'])==clock.now()+timedelta(hours=2)
        assert approval.payload[review.KEY]['source']['original_session']['expires_at']==selected.expires_at.isoformat()
        assert any(p['id']==str(approval.id) for p in client.get('/api/state').json()['proposals'])
        approved=client.post(f'/api/proposals/{approval.id}/approve',json={'content_hash':result['content_hash']})
        assert approved.status_code==200,approved.text
        assert approved.json()['message_id'] is None and approved.json()['delivery']=='not_queued'
        pull=client.post('/mac/outbound/pull',headers={'Authorization':'Bearer '+app.state.settings.mac_bridge_token})
        assert pull.status_code==200,pull.text
        assert pull.json()['messages']==[]
    session.expire_all()
    assert person.preferences['onboarding_stage']=='complete'
    assert list(session.scalars(select(m.Message.id)))==before_messages
    assert not session.scalar(select(MacDeliveryClaim))
    outbox=session.scalar(select(ProfileOutbox));assert outbox.state=='pending'
    assert outbox.source_guid=='synthetic-reviewed-source' and outbox.payload['route']=='onboarding_complete'
    assert not selected.active(clock.now()) and natural_source.source(session,person,incoming.id,clock.now()) is None


@pytest.mark.parametrize('change',['stop','new_input','receipt','step','original_bounds','privacy','revoked_policy','qualification'])
def test_expired_internal_review_holds_changed_current_or_original_evidence(session,clock,review_app,change):
    app,person,incoming,selected,group=review_app
    clock.advance(timedelta(hours=3))
    with TestClient(app) as client:
        card=client.get('/api/signup-preferences').json()['drafts'][0]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(card,group))
        assert response.status_code==200,response.text
        result=response.json()
        if change=='stop':session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
        if change=='new_input':session.add(m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',
            kind='mac_test_in',purpose='test:'+('e'*32),body='A new restriction in another session.',created_at=clock.now()))
        if change=='receipt':session.get(MacInboundReceipt,'synthetic-reviewed-source').fingerprint='0'*64
        if change=='step':
            turn=session.get(m.Notification,'onboarding-turn:'+str(incoming.id))
            step=session.get(m.AgentStep,turn.detail['step_id']);step.result={**step.result,'altered':True}
        if change=='original_bounds':incoming.created_at=selected.expires_at
        if change=='privacy':session.add(m.Escalation(category='privacy',severity='normal',summary='Synthetic privacy request',
            related_ids={'phone':PHONE,'volunteer_id':person.id},status='open',created_at=clock.now()))
        if change=='revoked_policy':session.get(m.Policy,'conversational_signup:'+PHONE).value={'value':False,'session_id':selected.id}
        if change=='qualification':session.add(m.Qualification(volunteer_id=person.id,type='sound_training',status='verified',verified_by='Synthetic reviewer'))
        session.commit()
        response=client.post(f"/api/proposals/{result['approval_id']}/approve",json={'content_hash':result['content_hash']})
        assert response.status_code==409,response.text
    session.expire_all();assert person.preferences['onboarding_stage']=='availability'
    assert not session.scalar(select(ProfileOutbox)) and not session.scalar(select(MacDeliveryClaim))


def test_single_unavailable_date_cannot_be_presented_as_sender_whole_month_absence(session,clock,review_app):
    app,person,incoming,selected,group=review_app
    draft=deepcopy(person.preferences['onboarding_availability_draft'])
    draft['unavailable_dates']=['2026-12-05']
    draft['pending_constraints']=[item for item in draft['pending_constraints']
        if item.get('proposal',{}).get('calendar_restriction')!='December off']
    person.preferences={**person.preferences,'onboarding_availability_draft':draft}
    turn=session.get(m.Notification,'onboarding-turn:'+str(incoming.id));turn.detail={**turn.detail,'draft_hash':natural_source.digest(draft)}
    record=session.get(m.Notification,turn.detail['coordinator_review_key']);record.detail={**record.detail,'draft_hash':turn.detail['draft_hash']}
    escalation=session.get(m.Escalation,record.detail['escalation_id']);escalation.related_ids={**escalation.related_ids,'draft_hash':turn.detail['draft_hash']}
    session.commit()
    with TestClient(app) as client:
        card=client.get('/api/signup-preferences').json()['drafts'][0]
        assert card['unavailable_months']==[]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(card,group))
        assert response.status_code==409 and 'month-off' in response.text,response.text
    assert not session.scalar(select(m.Approval))


def test_expired_review_of_real_recovery_chain_retains_original_never_claimed_guard(session,clock,provider,rejected):
    from app.core.mac_followup_recovery import recover_blocked_followup
    from tests.test_mac_natural_accuracy import ReviewGloo,known_proposal
    person,incoming,selected,gate,blocked,original,args=rejected
    assert recover_blocked_followup(session,clock,gate,person,ReviewGloo(known_proposal()),**args)=='onboarding_pending_review'
    group=m.EventType(name="Women's Ministry",title_patterns=['women']);session.add(group);session.flush()
    clock.advance(timedelta(hours=3))
    card=review.card(session,person,clock.now())
    approval=review.stage(session,person,choices(card,group),clock.now())
    assert review.review_problem(session,approval,clock.now()) is None
    assert not selected.active(clock.now())
    assert natural_source.followup_binding(session,person,{'incoming_id':incoming.id,'turn_key':'onboarding-turn-recovery:'+str(incoming.id)},clock.now()) is None
    session.add(MacDeliveryClaim(message_id=blocked.id,token='x'*64));session.flush()
    assert review.review_problem(session,approval,clock.now()) is not None


@pytest.mark.parametrize('defect',['sensitive','understood_number','sensitive_missing','unfinished',
    'ended_before_step','started_after_step','started_before_input','future_completion'])
def test_semantically_invalid_or_unfinished_current_gloo_run_has_no_durable_read_authority(session,clock,review_app,defect):
    from app.core.signup_review_authority import binding
    app,person,incoming,selected,group=review_app
    turn=session.get(m.Notification,'onboarding-turn:'+str(incoming.id));step=session.get(m.AgentStep,turn.detail['step_id'])
    if defect in {'sensitive','understood_number','sensitive_missing'}:
        extraction={**step.result['extraction']}
        if defect=='sensitive':extraction['sensitive']=True
        if defect=='understood_number':extraction['understood']=1
        if defect=='sensitive_missing':extraction.pop('sensitive')
        step.result={**step.result,'extraction':extraction}
        turn.detail={**turn.detail,'step_hash':natural_source.digest(step.result)}
        record=session.get(m.Notification,turn.detail['coordinator_review_key'])
        record.detail={**record.detail,'step_hash':turn.detail['step_hash']}
    if defect=='unfinished':step.run.ended_at=None
    if defect=='ended_before_step':step.run.ended_at=step.created_at-timedelta(microseconds=1)
    if defect=='started_after_step':step.run.started_at=step.created_at+timedelta(microseconds=1)
    if defect=='started_before_input':step.run.started_at=incoming.created_at-timedelta(microseconds=1)
    if defect=='future_completion':step.run.ended_at=clock.now()+timedelta(days=1)
    session.flush()
    clock.advance(timedelta(hours=3))
    assert binding(session,person,{'incoming_id':incoming.id,'turn_key':turn.key},clock.now()) is None
