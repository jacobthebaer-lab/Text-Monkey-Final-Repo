"""Synthetic scope, Gloo and Google connector only, no external delivery."""
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.integrations import acceptance_workflow as flow
from app.llm.gloo_client import GlooUnavailableError
from tests.test_google_voice_demo import demo
from tests.test_google_voice import PHONE, EMAIL, NUMBER

URL = '/api/acceptance-event'


@pytest.fixture
def scoped(demo, tmp_path, monkeypatch):
    path = tmp_path / 'scope.json'
    path.write_text(json.dumps({'phone':PHONE,'admin_email':EMAIL,'sender_email':EMAIL,
                               'sender_number':NUMBER,'timer_available':False}))
    path.chmod(0o600)
    monkeypatch.setenv(flow.ENV,str(path))
    with demo.state.session_factory() as session:
        session.info['record_authorized'] = True
        session.add(m.Role(name='Greeter',ministry='Welcome',required_qualifications=[],criticality='standard',fill_policy='auto'))
        session.commit()
    return demo, path


def post(app, action, data):
    return TestClient(app).post(URL+'/'+action,json=data)


def approve(app, result):
    a = next(a for a in result['reviews'] if a['status']=='pending')
    response = post(app,'approve',{'review_id':a['id'],'content_hash':a['content_hash']})
    assert response.status_code==200, response.text
    return response.json()


def create_event(app):
    start = app.state.clock.now() + timedelta(days=1)
    response = post(app,'event',{'title':'Demo: Synthetic event','zone':'America/Denver','role_id':1,
        'starts_at':start.isoformat(),'ends_at':(start+timedelta(hours=1)).isoformat()})
    assert response.status_code==200, response.text
    return approve(app,approve(app,response.json()))


def assigned(app):
    result = create_event(app)
    response = post(app,'assignment',{'event_id':result['event']['id']})
    assert response.status_code==200, response.text
    return approve(app,response.json())


def prepared(app):
    result = assigned(app)
    response = post(app,'prepare',{'event_id':result['event']['id'],'assignment_id':result['assignment_id']})
    assert response.status_code==200, response.text
    assert response.json()['reviews'][-1]['kind']=='confirm_text'
    return approve(app,response.json())


def message_data(result):
    return {'event_id':result['event']['id'],'assignment_id':result['assignment_id'],
            'message_id':result['message']['id'],'body_hash':result['message']['body_hash']}


def test_scope_absent_private_file_actor_and_single_recipient(scoped, monkeypatch):
    app,path = scoped
    monkeypatch.delenv(flow.ENV)
    assert TestClient(app).get(URL).status_code==404
    monkeypatch.setenv(flow.ENV,str(path))
    path.chmod(0o644)
    assert TestClient(app).get(URL).status_code==409
    path.chmod(0o600)
    link = path.with_name('link.json');link.symlink_to(path)
    monkeypatch.setenv(flow.ENV,str(link))
    assert TestClient(app).get(URL).status_code==409
    monkeypatch.setenv(flow.ENV,str(path))
    with pytest.raises(Exception) as error: flow.private_scope(app.state,'other@example.test')
    assert error.value.status_code==403
    from tests.session_fixtures import session_json
    app.state.settings = replace(app.state.settings, google_voice_demo_phones=PHONE+',+15555550303',
        google_voice_test_sessions=session_json([PHONE,'+15555550303'],app.state.clock.now()))
    assert TestClient(app).get(URL).status_code==409


def test_reviewed_event_assignment_exact_gloo_reminder_and_dedupe(scoped):
    app,_ = scoped
    result = prepared(app)
    assert result['assignment_id'] and result['message']['status']=='queued'
    assert "Hey Synthetic, Text Monkey here. You're signed up to greet tomorrow at 10am." in result['message']['body']
    assert '\u2014' not in result['message']['body']
    assert not result['timer']['enabled'] and not result['delivery_verified']
    calls = len(app.state.gloo.calls)
    response = post(app,'prepare',{'event_id':result['event']['id'],'assignment_id':result['assignment_id']})
    assert response.status_code==200 and len(app.state.gloo.calls)==calls
    assert app.state.google_voice_connector.calls==[]
    with app.state.session_factory() as session:
        assert len(list(session.scalars(select(m.Event))))==1
        assert len(list(session.scalars(select(m.Shift))))==1
        assert len(list(session.scalars(select(m.Assignment))))==1
        assert len(list(session.scalars(select(m.Message).where(m.Message.purpose=='reminder'))))==1


@pytest.mark.parametrize('guard',['qualification','availability','monthly','consent'])
def test_assignment_hard_guards(scoped,guard):
    app,_ = scoped
    result = create_event(app)
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        v = session.scalar(select(m.Volunteer))
        if guard=='qualification':session.get(m.Role,1).required_qualifications=['background_check']
        if guard=='availability':v.preferences={'availability_weekdays':[6]}
        if guard=='monthly':v.preferences={'max_per_month':0}
        if guard=='consent':v.sms_opt_in=False
        session.commit()
    assert post(app,'assignment',{'event_id':result['event']['id']}).status_code==409
    with app.state.session_factory() as session: assert session.scalar(select(m.Assignment)) is None


def test_assignment_review_rechecks_concurrent_capacity(scoped):
    app,_ = scoped
    result = create_event(app)
    response = post(app,'assignment',{'event_id':result['event']['id']}).json()
    a = response['reviews'][-1]
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        session.scalar(select(m.Volunteer)).preferences={'max_per_month':0}
        session.commit()
    assert post(app,'approve',{'review_id':a['id'],'content_hash':a['content_hash']}).status_code==409


def test_gloo_fail_closed(scoped):
    app,_ = scoped
    result = assigned(app)
    def unavailable(**kwargs): raise GlooUnavailableError('Synthetic unavailable')
    app.state.gloo.create_response=unavailable
    response=post(app,'prepare',{'event_id':result['event']['id'],'assignment_id':result['assignment_id']})
    assert response.status_code==200 and response.json()['reminder_state']=='gloo_unavailable'
    assert response.json()['message'] is None


@pytest.mark.parametrize('guard',['hash','source','stop','uncertain','paused','replay'])
def test_dispatch_guards_and_exact_scope(scoped,guard):
    app,_ = scoped
    result = prepared(app)
    data = message_data(result)
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        if guard=='hash':data['body_hash']='a'*64
        if guard=='source':session.get(m.Event,result['event']['id']).title='Demo: Changed'
        if guard=='stop':session.scalar(select(m.Volunteer)).sms_opt_in=False
        if guard=='uncertain':session.add(m.Message(phone=PHONE,direction='out',body='Prior unknown',kind='ai',purpose='signup_reply',provider_sid='GV-synthetic-unknown',status='uncertain',created_at=app.state.clock.now()))
        if guard=='paused':session.get(m.Policy,'google_voice:paused').value={'value':True}
        if guard=='replay':session.get(m.Message,data['message_id']).status='submitted'
        session.commit()
    assert post(app,'dispatch',data).status_code==409
    assert app.state.google_voice_connector.calls==[]


def test_fresh_intake_submission_is_once_not_phone_delivery(scoped):
    app,_ = scoped
    result=prepared(app)
    response=post(app,'dispatch',message_data(result))
    assert response.status_code==200,response.text
    assert response.json()['message']['status']=='submitted'
    assert response.json()['delivery_verified'] is False
    assert len(app.state.google_voice_connector.calls)==1
    assert post(app,'dispatch',message_data(result)).status_code==409
    assert len(app.state.google_voice_connector.calls)==1


def test_timer_default_off_and_no_fake_clock(scoped,monkeypatch):
    app,path=scoped
    result=prepared(app)
    timer={**message_data(result),'enabled':True,'due_at':app.state.clock.now().isoformat()}
    assert post(app,'timer',timer).status_code==409
    config=json.loads(path.read_text());config['timer_available']=True;path.write_text(json.dumps(config))
    assert post(app,'timer',timer).status_code==409
    flow.tick(app.state)
    assert app.state.google_voice_connector.calls==[]


def real_test_clock(app):
    from app.clock import RealClock
    class DeterministicRealClock(RealClock):
        def __init__(self):self.value=app.state.clock.now()
        def now(self):return self.value
    clock=DeterministicRealClock()
    app.state.clock=app.state.google_voice_clock=app.state.mac_delivery_clock=clock
    return clock


def test_real_time_armed_job_due_restart_and_at_most_once(scoped,monkeypatch):
    app,path=scoped
    result=prepared(app)
    clock=real_test_clock(app)
    config=json.loads(path.read_text());config['timer_available']=True;path.write_text(json.dumps(config))
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler.start',lambda _s:None)
    data={**message_data(result),'enabled':True,'due_at':(clock.now()+timedelta(minutes=1)).isoformat()}
    assert post(app,'timer',data).status_code==200
    flow.stop_service(app.state);flow.start_service(app.state)  # restore durable exact scope
    assert app.state.acceptance_scheduler is not None
    flow.tick(app.state)
    assert app.state.google_voice_connector.calls==[]
    clock.value+=timedelta(minutes=1)
    flow.tick(app.state)
    assert len(app.state.google_voice_connector.calls)==1
    flow.stop_service(app.state);flow.start_service(app.state);flow.tick(app.state)
    assert len(app.state.google_voice_connector.calls)==1
    result=TestClient(app).get(URL).json()
    assert result['timer']['state']=='submitted' and not result['timer']['enabled']
    with app.state.session_factory() as session:
        assert session.get(m.Assignment,result['assignment_id']).status=='approved'  # silence preserves assignment


@pytest.mark.parametrize('guard',['stop','cancellation','quiet','expiry','unknown','wrong_scope'])
def test_timer_rechecks_due_source_consent_time_and_delivery(scoped,monkeypatch,guard):
    app,path=scoped
    result=prepared(app)
    clock=real_test_clock(app)
    config=json.loads(path.read_text());config['timer_available']=True;path.write_text(json.dumps(config))
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler.start',lambda _s:None)
    data={**message_data(result),'enabled':True,'due_at':(clock.now()+timedelta(minutes=1)).isoformat()}
    assert post(app,'timer',data).status_code==200
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        if guard=='cancellation':session.get(m.Assignment,result['assignment_id']).status='cancelled'
        if guard=='expiry':session.get(m.Approval,result['reviews'][-1]['id']).payload={**session.get(m.Approval,result['reviews'][-1]['id']).payload,'expires_at':clock.now().isoformat()}
        if guard=='unknown':session.get(m.Message,result['message']['id']).status='uncertain'
        if guard=='quiet':session.add(m.Policy(key='quiet_hours',value={'value':{'start':'09:00','end':'11:00'}}))
        session.commit()
    if guard=='stop':
        from tests.test_google_voice import incoming
        app.state.google_voice_connector.messages=[incoming(app,'STOP')]
        app.state.google_voice_connector.cursor=1
    if guard=='wrong_scope':
        config['phone']='+15555550303';path.write_text(json.dumps(config))
    clock.value+=timedelta(minutes=1)
    flow.tick(app.state)
    flow.stop_service(app.state)
    assert app.state.google_voice_connector.calls==[]
    if guard=='stop':
        with app.state.session_factory() as session:
            assert session.scalar(select(m.Volunteer)).sms_opt_in is False


def test_wrong_event_and_unreviewed_message_cannot_arm_or_dispatch(scoped):
    app,_=scoped
    result=prepared(app)
    data=message_data(result);data['event_id']+=1
    assert post(app,'dispatch',data).status_code==409
    data=message_data(result);data['message_id']=True
    assert post(app,'dispatch',data).status_code==409
    assert app.state.google_voice_connector.calls==[]


def test_fresh_cancellation_routes_through_existing_gloo_and_holds_reminder(scoped):
    from types import SimpleNamespace
    from tests.test_google_voice import incoming
    app,_=scoped
    result=prepared(app)
    original=app.state.gloo.create_response
    def cancellation_gloo(**kwargs):
        if kwargs['input'] == "I can't come tomorrow":
            app.state.gloo.calls.append(kwargs)
            return SimpleNamespace(output_text=json.dumps({'intent':'cancel','confidence':1.0,'sensitive':False}),usage=None)
        return original(**kwargs)
    app.state.gloo.create_response=cancellation_gloo
    app.state.google_voice_connector.messages=[incoming(app,"I can't come tomorrow")]
    app.state.google_voice_connector.cursor=1
    response=post(app,'dispatch',message_data(result))
    assert response.status_code==409,response.text
    with app.state.session_factory() as session:
        assert session.get(m.Assignment,result['assignment_id']).status=='cancelled'
    assert app.state.google_voice_connector.calls==[]


def test_timer_rejects_due_after_queue_age_cutoff(scoped,monkeypatch):
    app,path=scoped
    result=prepared(app);clock=real_test_clock(app)
    config=json.loads(path.read_text());config['timer_available']=True;path.write_text(json.dumps(config))
    data={**message_data(result),'enabled':True,'due_at':(clock.now()+timedelta(minutes=20)).isoformat()}
    assert post(app,'timer',data).status_code==409
    assert not TestClient(app).get(URL).json()['timer']['enabled']
