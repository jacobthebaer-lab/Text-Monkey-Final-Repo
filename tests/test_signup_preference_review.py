"""Offline website proposal/approval/capture, no model or native network."""
import hashlib
import json
from copy import deepcopy
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.core import onboarding, paired_planning, confirmations
from app.db import models as m
from app.db.session import make_session_factory
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.profile_models import ProfileBase, ProfileOutbox
from app.main import create_app
from app.web.texty import admin
from tests.test_conversational_signup import natural, PHONE
from tests.test_mac_natural_accuracy import TEXT, ReviewGloo, known_proposal


@pytest.fixture
def review_app(session,clock,provider,natural,tmp_path):
    person,incoming,selected,gate=natural
    incoming.body=TEXT
    person.preferences={**person.preferences,'signup_source':'sms','consent_at':clock.now().isoformat(),
        'consent_source':'actual synthetic name reply','keep_private':'untouched'}
    assert onboarding.handle(session,clock,gate,person,incoming.body,ReviewGloo(known_proposal()))=='onboarding_pending_review'
    group=m.EventType(name="Women's Ministry",title_patterns=['women']);session.add(group);session.flush()
    session.add(MacInboundReceipt(guid='synthetic-reviewed-source',fingerprint=hashlib.sha256(
        (PHONE+'\0SMS\0'+selected.id+'\0'+incoming.body).encode()).hexdigest(),
        result={'session_id':selected.id,'intent':'onboarding_pending_review'}))
    settings=Settings(database_url=f'sqlite:///{tmp_path}/web.db',demo_mode=True,automation_enabled=False,
        sms_provider='mac_messages',mac_bridge_enabled=True,mac_bridge_token='synthetic-review-'+'x'*40,
        admin_password='synthetic-admin-password',mac_demo_phones=PHONE,
        mac_test_sessions=json.dumps({PHONE:{'id':selected.id,'starts_at':selected.starts_at.isoformat(),
            'expires_at':selected.expires_at.isoformat()}}),profile_sync_enabled=True,profile_sync_phones=PHONE)
    app=create_app(settings)
    ProfileBase.metadata.create_all(session.bind)
    app.state.session_factory=make_session_factory(session.bind)
    app.state.clock=app.state.mac_delivery_clock=clock
    app.dependency_overrides[admin]=lambda:{'email':'reviewer@example.test'}
    session.commit()
    return app,person,incoming,selected,group


def choices(card,group):
    return {'source_hash':card['source_hash'],'event_mappings':[{'window_index':2,'event_type_id':group.id}],
        'window_ordinals':[{'window_index':2,'ordinals':[2]}],'absence_months':['2026-12']}


def test_real_website_card_proposal_approval_completes_only_profile_and_captures_fresh_outbox(session,clock,review_app):
    app,person,incoming,selected,group=review_app
    with TestClient(app) as client:
        state=client.get('/api/state');assert state.status_code==200,state.text
        cards=state.json()['signup_preference_drafts'];assert len(cards)==1
        assert cards[0]['group_windows']==[2]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(cards[0],group))
        assert response.status_code==200,response.text
        result=response.json();assert result['state']=='pending_exact_review'
        clock.advance(timedelta(seconds=2))
        repeated=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(cards[0],group))
        assert repeated.json()['approval_id']==result['approval_id']
        session.expire_all();assert person.preferences['onboarding_stage']=='availability'
        assert not session.scalar(select(ProfileOutbox))
        proposal=next(p for p in client.get('/api/state').json()['proposals'] if p['id']==str(result['approval_id']))
        assert proposal['confirmation_required'] and proposal['record_change']['after']['preferences']['onboarding_stage']=='complete'
        approved=client.post(f"/api/proposals/{result['approval_id']}/approve",json={'content_hash':result['content_hash']})
        assert approved.status_code==200,approved.text
        assert approved.json()['delivery']=='not_queued' and approved.json()['message_id'] is None
        assert not client.get('/api/state').json()['signup_preference_drafts']
    session.expire_all();prefs=person.preferences
    assert prefs['onboarding_stage']=='complete' and 'onboarding_availability_draft' not in prefs
    assert prefs['keep_private']=='untouched' and person.sms_opt_in and not person.is_coordinator and not person.is_pastor
    assert not session.scalar(select(m.Qualification)) and not session.scalar(select(m.Assignment))
    assert paired_planning.rule_problem(session,person) is None
    assert prefs['same_day_role_pairs']==[{'role_ids':[1,3]}]
    assert not any('month_ordinals' in w for w in prefs['recurring_windows'][:2])
    care=prefs['recurring_windows'][2]
    assert care['month_ordinals']==[2] and care['event_context']['event_type_ids']==[group.id]
    unavailable=session.scalar(select(m.Availability).where(m.Availability.month=='2026-12'))
    assert len(unavailable.unavailable_dates)==31
    outbox=session.scalar(select(ProfileOutbox));assert outbox.state=='pending'
    assert outbox.source_guid=='synthetic-reviewed-source' and outbox.payload['route']=='onboarding_complete'
    assert outbox.payload['profile']['preferences']['onboarding_stage']=='complete'
    assert 'availability_draft' not in outbox.payload['profile']
    assert outbox.payload['profile']['preferences']['same_day_role_pairs']==[{'role_names':['Greeter','Production']}]
    assert session.scalars(select(m.Message).where(m.Message.direction=='in')).all()==[incoming]


@pytest.mark.parametrize('change',['input','new_input','draft','role','group','qualification','stop','session','review_record'])
def test_actual_review_rejects_stale_source_catalog_consent_and_clearances(session,clock,review_app,change):
    app,person,incoming,selected,group=review_app
    with TestClient(app) as client:
        card=client.get('/api/signup-preferences').json()['drafts'][0]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(card,group));assert response.status_code==200,response.text
        result=response.json()
        if change=='input':incoming.body='Changed sender input'
        if change=='new_input':session.add(m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',
            kind=incoming.kind,purpose=incoming.purpose,body='A new restriction',created_at=clock.now()))
        if change=='draft':person.preferences={**person.preferences,'onboarding_availability_draft':{}}
        if change=='role':session.get(m.Role,3).required_qualifications=['new_training']
        if change=='group':group.name='Other group'
        if change=='qualification':session.add(m.Qualification(volunteer_id=person.id,type='sound_training',status='verified',verified_by='Synthetic coordinator'))
        if change=='stop':session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
        if change=='session':
            from app.integrations.test_sessions import TestSession
            app.state.provider.test_sessions[PHONE]=TestSession('b'*32,selected.starts_at,selected.expires_at)
        if change=='review_record':
            row=session.get(m.Notification,'onboarding-coordinator:'+str(incoming.id))
            session.get(m.Escalation,row.detail['escalation_id']).status='resolved'
        session.commit()
        approved=client.post(f"/api/proposals/{result['approval_id']}/approve",json={'content_hash':result['content_hash']})
        assert approved.status_code==409,approved.text
    session.expire_all();assert person.preferences['onboarding_stage']=='availability'
    assert not session.scalar(select(ProfileOutbox)) and not session.scalar(select(m.Assignment))
    assert session.get(m.Approval,result['approval_id']).status=='pending'


@pytest.mark.parametrize('change',['fake_fixed_sundays','wrong_occurrence','wrong_group_role','extra_grant','missing_group','missing_absence'])
def test_completion_does_not_invent_sender_restrictions_or_skip_pending_facts(session,clock,review_app,change):
    app,person,_,_,group=review_app
    with TestClient(app) as client:
        card=client.get('/api/signup-preferences').json()['drafts'][0];data=choices(card,group)
        if change=='fake_fixed_sundays':data['window_ordinals'].append({'window_index':0,'ordinals':[1,3]})
        if change=='wrong_occurrence':data['window_ordinals'][0]['ordinals']=[3]
        if change=='wrong_group_role':data['event_mappings'][0]['window_index']=0
        if change=='extra_grant':data['is_coordinator']=True
        if change=='missing_group':data['event_mappings']=[]
        if change=='missing_absence':data['absence_months']=[]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=data)
        assert response.status_code in (409,422),response.text
    assert not session.scalar(select(m.Approval))


def test_unknown_sender_schedule_remains_targeted_clarification_not_coordinator_completion(session,clock,review_app):
    app,person,incoming,_,group=review_app
    # A current turn remains source-bound, but its draft explicitly needs facts.
    draft=deepcopy(person.preferences['onboarding_availability_draft'])
    draft['pending_constraints'].append({'kind':'validation','reason':'Unknown fixed weeks'})
    person.preferences={**person.preferences,'onboarding_availability_draft':draft}
    row=session.get(m.Notification,'onboarding-turn:'+str(incoming.id))
    row.detail={**row.detail,'draft_hash':__import__('app.core.conversational_signup',fromlist=['digest']).digest(draft)}
    review=session.get(m.Notification,row.detail['coordinator_review_key'])
    review.detail={**review.detail,'draft_hash':row.detail['draft_hash']}
    escalation=session.get(m.Escalation,review.detail['escalation_id'])
    escalation.related_ids={**escalation.related_ids,'draft_hash':row.detail['draft_hash']};session.commit()
    with TestClient(app) as client:
        card=client.get('/api/signup-preferences').json()['drafts'][0]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(card,group))
        assert response.status_code==409 and 'missing' in response.text


def test_misunderstood_current_interpretation_cannot_promote_prior_saved_facts(session,clock,review_app):
    app,person,incoming,_,group=review_app
    from app.core.conversational_signup import digest
    with TestClient(app) as client:
        card=client.get('/api/signup-preferences').json()['drafts'][0]
        turn=session.get(m.Notification,'onboarding-turn:'+str(incoming.id));step=session.get(m.AgentStep,turn.detail['step_id'])
        step.result={**step.result,'extraction':{**step.result['extraction'],'understood':False}}
        turn.detail={**turn.detail,'step_hash':digest(step.result)}
        record=session.get(m.Notification,turn.detail['coordinator_review_key'])
        record.detail={**record.detail,'step_hash':turn.detail['step_hash']};session.commit()
        assert client.get('/api/signup-preferences').json()['drafts']==[]
        response=client.post(f'/api/signup-preferences/{person.id}/review',json=choices(card,group))
        assert response.status_code==409,response.text
    assert not session.scalar(select(m.Approval))


def test_event_mapping_preserves_audited_prior_clock_limits_and_rejects_changed_history(session,clock,review_app,natural):
    app,person,previous,selected,group=review_app
    from app.core.signup_preference_review import card,stage,review_problem
    gate=natural[3]
    current=m.Message(phone=PHONE,volunteer_id=person.id,direction='in',status='received',kind=previous.kind,
        purpose=previous.purpose,body='A fixed serving schedule sounds good.',created_at=clock.now())
    session.add(current);session.flush();gate.reply_to_message_id=current.id
    data=known_proposal()
    data['recurring_windows'][2].update(time_mode='event',start_time=None,end_time=None,
        event_context={'label':"women's ministry",'event_type_ids':[]},month_ordinals=[2])
    onboarding.handle(session,clock,gate,person,current.body,ReviewGloo(data))
    session.add(MacInboundReceipt(guid='synthetic-later-source',fingerprint=hashlib.sha256(
        (PHONE+'\0SMS\0'+selected.id+'\0'+current.body).encode()).hexdigest(),result={'session_id':selected.id}))
    session.flush()
    projected=card(session,person,clock.now())
    assert projected['windows'][2]['start_time']=='18:00' and projected['windows'][2]['end_time']=='20:00'
    approval=stage(session,person,choices(projected,group),clock.now())
    window=approval.payload['after']['preferences']['recurring_windows'][2]
    assert window['time_mode']=='clock' and window['event_context']['event_type_ids']==[group.id]
    assert window['month_ordinals']==[2] and window['start_time']=='18:00' and window['end_time']=='20:00'
    assert review_problem(session,approval,clock.now()) is None
    previous.body='Changed original clock restriction'
    assert review_problem(session,approval,clock.now()) is not None
