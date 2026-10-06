"""Fast acknowledgment uses real transport settings, offline Gloo and HTTP only."""
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.integrations.mac_ongoing import authorize
from app.sms.mac_provider import MacMessagesProvider
from tests.test_mac_progress import progress_app,PHONE,TOKEN,incoming,post,submit_ack,wait_worker


def ongoing(application,now):
    # Keep the existing original ID and explicitly authorize one exact unread reply.
    old=application.state.provider.test_sessions[PHONE]
    spec={'id':old.id,'starts_at':(now-timedelta(hours=3)).isoformat(),
        'expires_at':(now-timedelta(hours=1)).isoformat()}
    config={'backend_url':'http://localhost:61887','phones':[PHONE],'receiving_number':'+15555550200',
        'services':['iMessage'],'input_mode':'natural','test_sessions':{PHONE:spec},'token':TOKEN}
    state={'phones':[PHONE],'services':['iMessage'],'receiving_number':config['receiving_number'],
        'input_mode':'natural','test_sessions':config['test_sessions'],'after':10,'dispatches':{}}
    result=authorize(config,json.dumps(state).encode(),phone=PHONE,guid='ongoing-short-input',row_id=12,
        body_hash=hashlib.sha256(b'First and third Sundays').hexdigest(),received_at=(now-timedelta(minutes=5)).isoformat(),
        actor='Explicit operator keep this exact Mac demo live until stopped',operator_confirmed=True,now=now)
    application.state.settings=replace(application.state.settings,
        mac_test_sessions=json.dumps(result['test_sessions']),mac_ongoing_authorization=json.dumps(result['ongoing_authorization']))
    application.state.provider=MacMessagesProvider(application.state.settings)
    # Critical regression: real live Gloo carries application settings, not mock defaults.
    application.state.gloo.settings=application.state.settings
    actual_fake=application.state.gloo.create_response
    def compose(**arguments):
        facts=json.loads(arguments['input'])
        if isinstance(facts,dict) and facts.get('recovery'):
            application.state.gloo.calls.append(facts)
            recovery=facts['recovery']
            return SimpleNamespace(output_text=json.dumps({'stage':recovery['stage'],'missing':recovery['missing'],
                'acknowledgment':('Your Sunday preferences are saved locally. Thank you!' if recovery.get('complete') else 'I understand your Sunday preference. It is pending coordinator review.' if recovery.get('needs_coordinator') else 'I understand your Sunday preference.'),
                'question':'' if recovery.get('complete') or recovery.get('needs_coordinator') else 'What time can you serve on Sundays?'}))
        response=actual_fake(**arguments)
        if isinstance(facts,dict) and facts.get('stage')=='availability':
            data=json.loads(response.output_text);data['sensitive']=False
            return SimpleNamespace(output_text=json.dumps(data))
        return response
    application.state.gloo.create_response=compose
    with application.state.session_factory() as session:
        session.add(m.Policy(key='conversational_signup:'+PHONE,value={'value':True,'session_id':old.id}))
        session.commit()
    return result


def test_ongoing_short_input_ack_claim_verify_submit_then_background_gloo(progress_app,clock):
    ongoing(progress_app,clock.now())
    data=incoming(guid='ongoing-short-input',body='First and third Sundays')
    with TestClient(progress_app) as client:
        accepted=post(client,'/mac/inbound',data)
        assert accepted.status_code==200,accepted.text
        result=accepted.json()
        assert result['progress_state']=='waiting_ack',result
        assert progress_app.state.gloo.ack_started.is_set()
        assert not progress_app.state.gloo.extracting.is_set()
        assert post(client,'/mac/inbound',data).json()['duplicate']
        ack=submit_ack(client)  # Real HTTP pull -> verify -> submitted receipt.
        assert not progress_app.state.gloo.extracting.is_set()
        post(client,'/mac/progress/tick')
        assert progress_app.state.gloo.extracting.wait(2)
        progress_app.state.gloo.release.set();wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            job=session.get(m.Notification,result['progress_key'])
            assert job.state=='done',json.dumps(job.detail,sort_keys=True)
            assert session.get(m.Message,ack['id']).status=='submitted'
            assert len(session.scalars(select(m.Message).where(m.Message.direction=='in')).all())==1
            assert session.scalar(select(m.Assignment)) is None
        assert len([call for call in progress_app.state.gloo.calls if call.get('approved_message')==ack['body']])==1


def test_ongoing_ack_rejects_changed_signed_scope_before_gloo(progress_app,clock):
    ongoing(progress_app,clock.now())
    with progress_app.state.session_factory() as session:
        from app.core.signup_responder import compose_signup_reply
        from app.llm.gloo_client import GlooUnavailableError
        person=session.scalar(select(m.Volunteer))
        progress_app.state.gloo.settings=replace(progress_app.state.settings,mac_ongoing_authorization='{}')
        with pytest.raises(ValueError):
            compose_signup_reply(session,clock,progress_app.state.gloo,"Thanks, I'm working through your preferences now.",
                volunteer=person,phone=PHONE,require_gloo=True,exact_copy=True)
        assert not progress_app.state.gloo.ack_started.is_set()
        assert session.scalar(select(m.Message).where(m.Message.direction=='out')) is None
