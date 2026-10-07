"""Composition must follow durable accepted scope, never stale startup phones."""
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.integrations import mac_roster as r
from app.core.signup_responder import compose_signup_reply
from app.llm.gloo_client import GlooUnavailableError,GlooClient
from tests.test_mac_roster_enrollment import roster,ADDED
import pytest


def composer(f):
    f.app.state.clock=f.app.state.mac_delivery_clock
    calls=[]
    def compose(**kwargs):
        calls.append(json.loads(kwargs['input']))
        return SimpleNamespace(output_text='Hi Example! Which volunteer roles would you like to help with?')
    return GlooClient(f.app.state.settings,client=SimpleNamespace(responses=SimpleNamespace(create=compose))),calls


@pytest.mark.parametrize('action',['single','bulk'])
def test_enrolled_website_profile_welcome_uses_accepted_scope_with_stale_gloo_settings(roster,action):
    f=roster;w=f.worker();w.once()
    assert ADDED not in f.app.state.settings.mac_demo_phones
    gloo,calls=composer(f);f.app.state.gloo=gloo
    if action=='single':
        result=f.backend.post(f'/api/volunteers/{f.person_id}/text-setup')
    else:
        from uuid import uuid4
        result=f.backend.post('/api/welcome-batches',json={'request_id':str(uuid4()),'volunteer_ids':[f.person_id]})
    assert result.status_code==200,result.text
    receipt=result.json() if action=='single' else result.json()['results'][0]
    assert receipt['delivery']=='queued_for_mac'
    assert len(calls)==1 and calls[0]['sender']['volunteer_id']==f.person_id
    with f.app.state.session_factory() as session:
        row=session.get(m.Message,receipt['message_id'])
        assert row.phone==ADDED and row.provider_sid.startswith(w.test_sessions[ADDED].outbound_prefix)
        assert row.body=='Hi Example! Which volunteer roles would you like to help with?'
        assert row.status=='queued'


def test_enrolled_composition_reads_only_exact_current_session_history(roster):
    f=roster;w=f.worker();w.once();gloo,calls=composer(f)
    selected=w.test_sessions[ADDED]
    with f.app.state.session_factory() as session:
        session.info['mac_test_session']=selected
        person=session.get(m.Volunteer,f.person_id)
        for purpose,body in [('test:'+selected.id,'Current synthetic input'),('test:'+'f'*32,'Unrelated prior session input')]:
            session.add(m.Message(phone=ADDED,volunteer_id=person.id,body=body,direction='in',kind='mac_test_in',
                purpose=purpose,status='received',created_at=f.app.state.clock.now()))
        session.flush()
        assert compose_signup_reply(session,f.app.state.clock,gloo,'Which roles?',volunteer=person,
            signup_conversation=True,require_gloo=True)
    assert calls[0]['recent_messages']==[{'direction':'in','body':'Current synthetic input'}]


@pytest.mark.parametrize('defect',['wrong_recipient','wrong_id','wrong_prefix','expired','unknown_phone','tampered_scope','bad_signature','malformed_scope','not_started','unaccepted_scope'])
def test_invalid_current_authority_is_held_before_history_or_gloo(roster,defect):
    from dataclasses import replace
    from datetime import timedelta
    from sqlalchemy import event
    f=roster;w=f.worker();w.once();gloo,calls=composer(f)
    with f.app.state.session_factory() as session:
        person=session.get(m.Volunteer,f.person_id);selected=w.test_sessions[ADDED]
        if defect=='wrong_recipient': selected=w.test_sessions[f.config['phones'][0]]
        elif defect=='wrong_id': selected=replace(selected,id='f'*32)
        elif defect=='wrong_prefix': selected=SimpleNamespace(spec=selected.spec,outbound_prefix='GV'+'f'*32+':')
        elif defect=='expired': selected=replace(selected,expires_at=f.app.state.clock.now()-timedelta(seconds=1))
        elif defect=='unknown_phone': person.phone='+13035559999';session.flush()
        elif defect=='tampered_scope':
            saved=session.get(m.Policy,r.SCOPE);value=json.loads(json.dumps(saved.value))
            value['journal']['sessions'][ADDED]['id']='f'*32;saved.value=value;session.flush()
        elif defect in {'bad_signature','malformed_scope'}:
            saved=session.get(m.Policy,r.SCOPE);value=json.loads(json.dumps(saved.value))
            if defect=='bad_signature': value['journal']['signature']='f'*64
            else: value['journal']=[]
            saved.value=value;session.flush()
        elif defect=='not_started':
            f.app.state.clock=SimpleNamespace(now=lambda:selected.starts_at-timedelta(seconds=1))
        elif defect=='unaccepted_scope':
            session.delete(session.get(m.Policy,r.SCOPE));session.flush()
        session.info['mac_test_session']=selected
        statements=[]
        def capture(connection,cursor,statement,*args): statements.append(statement)
        engine=session.get_bind();event.listen(engine,'before_cursor_execute',capture)
        try:
            with pytest.raises(GlooUnavailableError):
                compose_signup_reply(session,f.app.state.clock,gloo,'Which roles?',volunteer=person,
                    signup_conversation=True,require_gloo=True)
        finally:event.remove(engine,'before_cursor_execute',capture)
        assert calls==[]
        assert not any('FROM messages' in statement for statement in statements)


def test_enrolled_recorded_inbound_continues_to_gloo_followup_in_same_session(roster):
    from app.core.onboarding import handle
    from app.core.send_gate import SendGate
    f=roster;w=f.worker();w.once();gloo,calls=composer(f)
    def create(**kwargs):
        facts=json.loads(kwargs['input']);calls.append(facts)
        text=(json.dumps({'understood':True,'sensitive':False,'role_ids':[100],'any_role':False})
              if facts.get('stage')=='interests' else 'Which days can you serve, and how often each month?')
        return SimpleNamespace(output_text=text)
    gloo._client.responses.create=create;f.app.state.gloo=gloo
    welcome=f.backend.post(f'/api/volunteers/{f.person_id}/text-setup');assert welcome.status_code==200
    selected=w.test_sessions[ADDED]
    with f.app.state.session_factory() as session:
        session.info['mac_test_session']=selected;session.info['record_authorized']=True
        person=session.get(m.Volunteer,f.person_id)
        session.add(m.Role(id=100,name='Greeter',ministry='Welcome',required_qualifications=[],criticality='standard',fill_policy='auto'))
        session.get(m.Message,welcome.json()['message_id']).status='submitted' # fabricated transport receipt only
        incoming=m.Message(phone=ADDED,volunteer_id=person.id,direction='in',kind='mac_test_in',purpose='test:'+selected.id,
            status='received',body='I can help as a greeter.',created_at=f.app.state.clock.now())
        session.add(incoming);session.flush()
        gate=SendGate(session,f.app.state.clock,f.app.state.provider);gate.reply_to_message_id=incoming.id
        assert handle(session,f.app.state.clock,gate,person,incoming.body,gloo)=='onboarding_availability'
        assert person.preferences['interested_roles']==['Greeter']
        assert person.preferences['onboarding_stage']=='availability'
        followup=session.scalars(select(m.Message).where(m.Message.phone==ADDED,m.Message.direction=='out').order_by(m.Message.id)).all()[-1]
        assert followup.status=='queued' and followup.provider_sid.startswith(selected.outbound_prefix)
        assert followup.body=='Which days can you serve, and how often each month?'
    assert len(calls)==3 and calls[1]['body']=='I can help as a greeter.'
    assert calls[2]['recent_messages'][-1]['body']=='I can help as a greeter.'
    assert ADDED not in gloo.settings.mac_demo_phones



def test_prepared_uncommitted_canonical_scope_cannot_compose_for_candidate(roster):
    import httpx
    from dataclasses import replace
    from app.integrations.test_sessions import parse_sessions
    f=roster;w=f.worker();f.fault['lose']='/mac/roster/prepare'
    with pytest.raises(httpx.ReadError):w.once()
    staged=json.loads(w.enrollment_path.read_text())['config']
    gloo,calls=composer(f)
    gloo.settings=replace(gloo.settings,mac_demo_phones=','.join(staged['phones']),
        mac_test_sessions=json.dumps(staged['test_sessions']),mac_ongoing_authorization=json.dumps(staged['ongoing_authorization']))
    selected=parse_sessions(staged['test_sessions'],set(staged['phones']),allow_ongoing=True)[ADDED]
    with f.app.state.session_factory() as session:
        session.info['mac_test_session']=selected;person=session.get(m.Volunteer,f.person_id)
        with pytest.raises(GlooUnavailableError):
            compose_signup_reply(session,f.app.state.clock,gloo,'Which roles?',volunteer=person,
                signup_conversation=True,require_gloo=True)
    assert calls==[] and ADDED not in f.app.state.provider.phones


def test_provider_failure_for_enrolled_welcome_remains_held_without_fallback(roster):
    f=roster;w=f.worker();w.once();gloo,calls=composer(f)
    def failed(**kwargs):
        calls.append(kwargs);raise GlooUnavailableError('Synthetic provider outage')
    gloo._client.responses.create=failed;f.app.state.gloo=gloo
    result=f.backend.post(f'/api/volunteers/{f.person_id}/text-setup')
    assert result.status_code==503 and len(calls)==1
    with f.app.state.session_factory() as session:
        assert session.scalar(select(m.Message).where(m.Message.phone==ADDED)) is None
        assert session.scalar(select(m.Approval)) is None
        assert session.get(m.Notification,'volunteer-welcome:'+str(f.person_id)) is None
        assert session.get(m.Volunteer,f.person_id).preferences=={}


def test_original_bounded_session_expiry_still_blocks_before_history_and_gloo(session,clock):
    from datetime import timedelta
    from sqlalchemy import event
    from app.config import Settings
    from app.sms.mac_provider import MacMessagesProvider
    from tests.session_fixtures import session_json
    from tests.test_mac_ongoing import TOKEN
    calls=[];phone=ADDED
    settings=Settings(sms_provider='mac_messages',mac_bridge_enabled=True,mac_bridge_token=TOKEN,
        admin_password='synthetic-password-123',mac_demo_phones=phone,mac_test_sessions=session_json([phone],clock.now()))
    gloo=GlooClient(settings,client=SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs:calls.append(kwargs))))
    selected=MacMessagesProvider(settings).test_sessions[phone];session.info['mac_test_session']=selected
    clock.advance(timedelta(hours=3));statements=[]
    def capture(connection,cursor,statement,*args):statements.append(statement)
    engine=session.get_bind();event.listen(engine,'before_cursor_execute',capture)
    try:
        with pytest.raises(GlooUnavailableError):
            compose_signup_reply(session,clock,gloo,'Which roles?',phone=phone,signup_conversation=True,require_gloo=True)
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert calls==[] and not any('FROM messages' in statement for statement in statements)
