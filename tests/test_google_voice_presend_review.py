"""Offline exact pre-send rejection recovery. No Gloo/native requests."""
from copy import deepcopy
from datetime import timedelta
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.core import confirmations
from app.core.cloud_composition import reviewed_composition
from app.db import models as m
from app.integrations.google_voice_demo import sender_fingerprint, scope_fingerprint
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
from tests.test_google_voice_quiet_review import held, allow, stage
from tests.test_google_voice_signup import signup, enable, inbound, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo, register


@pytest.fixture
def rejected(held):
    app, *_ = held
    allow(held)
    result = stage(held).json()
    client = TestClient(app)
    assert client.post(f"/api/proposals/{result['approval_id']}/approve",json={'content_hash':result['content_hash']}).status_code == 200
    state = app.state
    with state.session_factory() as session:
        approval = session.get(m.Approval,result['approval_id'])
        row = session.get(m.Message,approval.payload['message_id'])
        row.status = 'rejected'
        session.add(GoogleVoiceDeliveryClaim(message_id=row.id,idempotency_key=row.provider_sid,created_at=state.clock.now()))
        session.commit()
        record = {'id':row.id,'approval_id':approval.id,'hash':result['content_hash'],
            'body':row.body,'key':row.provider_sid,'payload':deepcopy(approval.payload)}
    observations = []
    def observe(request):
        observations.append(request)
        return {'status':'unsubmitted','proof':{'submission_key_hash':hashlib.sha256(record['key'].encode()).hexdigest(),
            'body_hash':hashlib.sha256(record['body'].encode()).hexdigest(),'session_id':state.provider.test_sessions[PHONE].id,
            'sender_fingerprint':sender_fingerprint(state.settings),'scope_fingerprint':scope_fingerprint(state.provider.test_sessions),
            'reason_code':'recipient_choice_wait_unavailable','ledger_absent':True,'original_key_disabled':True,
            'native_submission_attempted':False,'observed_at':state.clock.now().isoformat()}}
    state.google_voice_connector.observe_presend_absence = observe
    return app,record,observations,observe


def recover(rejected,**changes):
    app,record,*_ = rejected
    payload = {'content_hash':record['hash'],'reason_code':'recipient_choice_wait_unavailable',
        'prepare_receipt_sha256':'f'*64,'confirmed':True,**changes}
    return TestClient(app).post(f"/api/cloud-texting/demo/presend-review/{record['id']}",json=payload)


def test_original_terminal_claim_retained_and_one_successor_requires_fresh_human_review(rejected):
    app,record,observations,_ = rejected
    state = app.state
    models,native = len(state.gloo.calls),len(state.google_voice_connector.calls)
    from app.integrations.google_voice_presend_review import original
    with state.session_factory() as session:
        row,claim,approval,selected = original(session,state,record['id'],record['hash'])
        session.info['mac_test_session'] = selected
        assert confirmations.delivery_problem(session,state.provider,approval,state.clock.now(),row) is None
        from app.integrations.google_voice_demo import demo_text_problem
        from app.core import outbound_conversation
        assert demo_text_problem(session,state.provider,row.phone,row.body,row.purpose,state.clock.now(),reply_id=approval.payload.get('reply_to_message_id')) is None
        assert outbound_conversation.problem(session,purpose=row.purpose,volunteer=session.get(m.Volunteer,row.volunteer_id),phone=row.phone,
            body=row.body,now=state.clock.now(),meta=approval.payload.get('conversation',{}),approval=approval,message=row) is None
    response = recover(rejected)
    assert response.status_code == 200,response.text
    new_id = response.json()['approval_id']
    assert recover(rejected).json() == {**response.json(),'already_staged':True}
    assert len(observations) == 1
    with state.session_factory() as session:
        old = session.get(m.Approval,record['approval_id'])
        row = session.get(m.Message,record['id'])
        successor = session.get(m.Approval,new_id)
        assert old.status == 'approved' and old.payload == record['payload']
        assert row.status == 'rejected' and row.provider_sid == record['key'] and row.body == record['body']
        assert session.get(GoogleVoiceDeliveryClaim,row.id).idempotency_key == record['key']
        assert successor.status == 'pending' and 'message_id' not in successor.payload
        assert successor.payload == {k:v for k,v in old.payload.items() if k != 'message_id'}
        assert reviewed_composition(session,successor,state.provider.test_sessions[PHONE])
        from app.integrations.google_voice_signup import authorize_pending, refresh_unsent_signup_replies
        authorize_pending(session,state);refresh_unsent_signup_replies(session,state)
        assert successor.status == 'pending'
        session.commit()
    assert TestClient(app).post(f'/api/proposals/{new_id}/approve',json={'content_hash':record['hash']}).status_code == 200
    with state.session_factory() as session:
        successor = session.get(m.Approval,new_id)
        assert successor.status == 'approved'
        fresh = session.get(m.Message,successor.payload['message_id'])
        assert fresh.id != record['id'] and fresh.provider_sid != record['key'] and fresh.status == 'queued'
        assert fresh.body == record['body']
        assert session.get(m.Message,record['id']).status == 'rejected'
        assert session.get(GoogleVoiceDeliveryClaim,fresh.id) is None
    assert len(state.gloo.calls) == models and len(state.google_voice_connector.calls) == native


@pytest.mark.parametrize('change',['unknown','submitted','no_claim','wrong_hash','STOP','expired','Gloo','sender','ledger','scope','wrong_key','reason','confirmation'])
def test_missing_or_changed_presend_proof_cannot_stage(rejected,change):
    app,record,observations,observe = rejected
    state = app.state
    with state.session_factory() as session:
        row = session.get(m.Message,record['id'])
        if change == 'unknown':row.status = 'uncertain'
        if change == 'submitted':row.status = 'submitted'
        if change == 'no_claim':session.delete(session.get(GoogleVoiceDeliveryClaim,row.id))
        if change == 'STOP':session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
        if change == 'expired':state.clock.advance(timedelta(hours=3))
        session.commit()
    if change == 'Gloo':
        from dataclasses import replace
        state.settings = replace(state.settings,gloo_api_key='')
    if change in {'sender','ledger','scope','wrong_key'}:
        field = {'sender':'sender_fingerprint','ledger':'ledger_absent','scope':'scope_fingerprint','wrong_key':'submission_key_hash'}[change]
        def altered(request):
            response = observe(request);response['proof'][field] = False if field == 'ledger_absent' else '0'*64
            return response
        state.google_voice_connector.observe_presend_absence = altered
    changes = {'content_hash':'0'*64} if change == 'wrong_hash' else {'reason_code':'browser_unavailable'} if change == 'reason' else {'confirmed':False} if change == 'confirmation' else {}
    assert recover(rejected,**changes).status_code in {400,409}
    with state.session_factory() as session:
        assert session.get(m.Policy,f"google-presend-review:{record['approval_id']}") is None


def test_donor_change_after_staging_blocks_new_review(rejected):
    app,record,*_ = rejected
    response = recover(rejected);assert response.status_code == 200,response.text
    successor_id = response.json()['approval_id']
    with app.state.session_factory() as session:
        old = session.get(m.Message,record['id']);old.body += ' Changed'
        session.commit()
    TestClient(app).post(f'/api/proposals/{successor_id}/approve',json={'content_hash':record['hash']})
    with app.state.session_factory() as session:
        assert session.get(m.Approval,successor_id).status == 'expired'
        assert session.scalar(select(m.Message.id).where(m.Message.id != record['id'],m.Message.status == 'queued')) is None


def test_rejected_claim_and_original_reservation_changes_hold_at_native_preflight(rejected):
    app,record,*_ = rejected
    response = recover(rejected);assert response.status_code == 200,response.text
    successor_id = response.json()['approval_id']
    assert TestClient(app).post(f'/api/proposals/{successor_id}/approve',json={'content_hash':record['hash']}).status_code == 200
    with app.state.session_factory() as session:
        approval = session.get(m.Approval,successor_id)
        row = session.get(m.Message,approval.payload['message_id'])
        session.info['mac_test_session'] = app.state.provider.test_sessions[PHONE]
        assert reviewed_composition(session,approval,app.state.provider.test_sessions[PHONE])
        from app.integrations.google_voice_runtime import _delivery_problem
        assert _delivery_problem(session,app.state,row,app.state.clock.now()) is None
        original = session.get(m.Message,record['id']);original.status = 'uncertain'
        assert _delivery_problem(session,app.state,row,app.state.clock.now()) is not None


@pytest.mark.parametrize('change',['delete_audit','audit_actor','audit_action','audit_id','via','decision_actor','decision_time'])
def test_original_donor_human_decision_remains_required_after_staging(rejected,change):
    app,record,*_ = rejected
    response = recover(rejected);assert response.status_code == 200,response.text
    successor_id = response.json()['approval_id']
    with app.state.session_factory() as session:
        donor = session.get(m.Approval,record['approval_id'])
        audit = session.get(m.Notification,f'review:{donor.id}:approve')
        if change == 'delete_audit':session.delete(audit)
        if change == 'audit_actor':audit.detail = {**audit.detail,'actor':'different@example.test'}
        if change == 'audit_action':audit.detail = {**audit.detail,'action':'reject'}
        if change == 'audit_id':audit.detail = {**audit.detail,'approval_id':999}
        if change == 'via':donor.via = 'automatic'
        if change == 'decision_actor':donor.decided_by = 'different@example.test'
        if change == 'decision_time':donor.decided_at += timedelta(seconds=1)
        session.commit()
    TestClient(app).post(f'/api/proposals/{successor_id}/approve',json={'content_hash':record['hash']})
    with app.state.session_factory() as session:
        assert session.get(m.Approval,successor_id).status == 'expired'
        assert session.get(m.Approval,successor_id).payload.get('message_id') is None
