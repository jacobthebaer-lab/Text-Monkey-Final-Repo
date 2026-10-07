"""Reviewed collection on isolated Mac queues, never native Messages delivery."""
from datetime import timedelta
from dataclasses import replace
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.clock import FakeClock
from app.config import Settings
from app.core import confirmations
from app.db import models as m
from app.main import create_app
from app.web import texty
from tests.conftest import NOW
from tests.session_fixtures import session_json
from tests.test_admin_setup import OWNER_A
from tests.test_planning_composition import CopyGloo
from tests.test_planning_workflows_api import requested, decision, mutate

PHONE = '+12025550139'
TOKEN = 'synthetic-collection-bridge-' + 'x' * 40
BRIDGE = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture(params=[True, False], ids=['global_exact', 'global_automatic'])
def native_collection(tmp_path, request):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/collection.db',
        sms_provider='mac_messages', mac_bridge_enabled=True, mac_bridge_token=TOKEN,
        mac_demo_phones=PHONE, mac_test_sessions=session_json([PHONE], NOW),
        admin_password='synthetic-collection-password', automation_enabled=False,
        competition_confirmation_required=request.param, demo_mode=False))
    app.state.clock = app.state.mac_delivery_clock = FakeClock(NOW)
    app.state.gloo = CopyGloo()
    app.dependency_overrides[texty.admin] = lambda: {
        'id': OWNER_A, 'email': 'synthetic-admin@example.test', 'email_confirmed_at': '2026-10-01'}
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.info['record_authorized'] = True
            session.add(m.Volunteer(name='Fictional Collection Volunteer', phone=PHONE,
                sms_opt_in=True, status='active', preferences={}, created_at=NOW))
            session.commit()
        parent = requested(client)
        assert decision(client, parent).status_code == 200
        staged = decision(client, parent, 'retry').json()
        assert len(staged['text_review_ids']) == 1 and staged['sent'] == 0
        assert client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages'] == []
        with app.state.session_factory() as session:
            exact = session.get(m.Approval, staged['text_review_ids'][0])
            review_id, digest, body = exact.id, exact.payload['content_hash'], exact.payload['body']
            assert exact.status == 'pending' and confirmations.valid(exact, NOW)
            assert exact.payload['phone'] == PHONE and exact.payload['transport'] == 'mac_messages'
        yield client, app, parent, review_id, digest, body


def approve(client, ident, digest):
    response = client.post(f'/api/proposals/{ident}/approve', json={'content_hash': digest})
    assert response.status_code == 200, response.text
    return response


def test_exact_review_claim_and_preflight_keep_original_collection_proof(native_collection):
    client, app, parent, ident, digest, body = native_collection
    assert approve(client, ident, digest).json()['delivery'] == 'queued_for_mac'
    batch = client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages']
    assert len(batch) == 1 and batch[0]['phone'] == PHONE and batch[0]['body'] == body
    item = batch[0]
    assert item['conversation_preflight_required']
    assert client.post(f"/mac/outbound/{item['id']}/verify", headers=BRIDGE,
        json={'token': item['token'], 'content_hash': digest}).status_code == 200
    assert client.post(f"/mac/outbound/{item['id']}/ack", headers=BRIDGE,
        json={'token': item['token'], 'outcome': 'submitted'}).status_code == 200
    assert client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages'] == []
    assert decision(client, parent, 'retry').json()['text_review_ids'] == []
    assert app.state.gloo.calls == 1
    with app.state.session_factory() as session:
        exact = session.get(m.Approval, ident)
        proof = session.get(m.Notification, f'conversation-message:{item["id"]}').detail
        assert proof == exact.payload['conversation']
        assert proof['binding']['parent_review_id'] == parent['id']
        assert proof['binding']['owner_id'] == OWNER_A and proof['binding']['recipient_phone'] == PHONE
        assert session.get(m.Message, item['id']).status == 'submitted'
        assert len(session.scalars(select(m.Message).where(m.Message.direction == 'out')).all()) == 1


@pytest.mark.parametrize('phase', ['approval', 'claim', 'preflight'])
@pytest.mark.parametrize('change', ['parent', 'child', 'scope', 'name', 'phone', 'stop',
    'care', 'availability', 'budget', 'timezone', 'gloo_proof', 'expired'])
def test_collection_authority_is_rechecked_at_every_delivery_boundary(native_collection, phase, change):
    client, app, parent, ident, digest, _ = native_collection
    item = None
    if phase != 'approval':
        approve(client, ident, digest)
    if phase == 'preflight':
        batch = client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages']
        assert len(batch) == 1
        item = batch[0]
    def update(session):
        person = session.scalar(select(m.Volunteer))
        p = session.get(m.Approval, parent['id'])
        child = session.get(m.Approval, p.payload['collection_id'])
        if change == 'parent': p.status = 'rejected'
        if change == 'child': child.status = 'rejected'
        if change == 'scope': child.payload = {**child.payload, 'recipient_ids': [999]}
        if change == 'name': person.name = 'Changed Fictional Name'
        if change == 'phone': person.phone = '+12025550138'
        if change == 'stop': session.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
        if change == 'care': session.add(m.Escalation(category='sensitive', severity='normal',
            summary='Fictional phone care hold', related_ids={'phone': PHONE}, status='open', created_at=NOW))
        if change == 'availability': session.add(m.Availability(volunteer_id=person.id,
            month='2026-11', available_dates=['2026-11-01']))
        if change == 'budget':
            session.add(m.Policy(key='monthly_ask_budget_per_volunteer', value={'value': 1}))
            session.add(m.Message(direction='out', phone=PHONE, volunteer_id=person.id,
                purpose='availability_ask', kind='ai', status='submitted', body='Another synthetic reviewed ask', created_at=NOW))
        if change == 'timezone': session.add(m.Policy(key='church_timezone', value={'value': 'America/New_York'}))
        if change == 'gloo_proof':
            proof=session.scalar(select(m.Notification).where(m.Notification.purpose=='availability_composition'))
            proof.state='revoked'
    mutate(app, update)
    if change == 'expired': app.state.clock.advance(timedelta(hours=3))
    if phase == 'approval':
        if change == 'expired':
            response=client.post(f'/api/proposals/{ident}/approve', json={'content_hash': digest})
            assert response.status_code in {404, 409}, response.text
        else:
            approve(client, ident, digest)
        with app.state.session_factory() as session:
            exact = session.get(m.Approval, ident)
            assert not exact.payload.get('message_id')
            if change == 'expired': assert not confirmations.valid(exact, app.state.clock.now())
            else: assert exact.status == 'expired'
    elif phase == 'claim':
        assert client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages'] == []
    else:
        response = client.post(f"/mac/outbound/{item['id']}/verify", headers=BRIDGE,
            json={'token': item['token'], 'content_hash': digest})
        assert response.status_code == 409, response.text
    assert app.state.gloo.calls == 1
    assert client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages'] == []


@pytest.fixture
def ongoing_collection(native_collection):
    """Sign an explicit ongoing synthetic scope through the production contract."""
    from app.integrations.mac_ongoing import authorize
    from app.sms.mac_provider import MacMessagesProvider
    client, app, *_ = native_collection
    selected = app.state.provider.test_sessions[PHONE]
    services = sorted(app.state.provider.services)
    config = {'phones': [PHONE], 'test_sessions': {PHONE: selected.spec()}, 'token': TOKEN,
        'backend_url': 'https://synthetic.example.test', 'receiving_number': '+12025550137',
        'services': services, 'input_mode': 'natural'}
    checkpoint = json.dumps({**config, 'after': 0}).encode()
    configured = authorize(config, checkpoint, phone=PHONE, guid='synthetic-ongoing-guid', row_id=1,
        body_hash=hashlib.sha256(b'Synthetic ongoing approval').hexdigest(), received_at=NOW.isoformat(),
        actor='Synthetic operator, isolated ongoing timing test', operator_confirmed=True, now=NOW)
    settings = replace(app.state.settings, mac_test_sessions=json.dumps(configured['test_sessions']),
        mac_ongoing_authorization=json.dumps(configured['ongoing_authorization']))
    app.state.settings = settings
    app.state.provider = MacMessagesProvider(settings)
    return native_collection


def queued_initial(case):
    client, app, _, ident, digest, _ = case
    approve(client, ident, digest)
    item = client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages'][0]
    assert client.post(f"/mac/outbound/{item['id']}/verify", headers=BRIDGE,
        json={'token': item['token'], 'content_hash': digest}).status_code == 200
    return item


def stage_reminder(case):
    from app.agents.fill_agent import FillContext
    from app.agents.planning_agent import collect
    _, app, parent, *_ = case
    with app.state.session_factory() as session:
        session.info[confirmations.MODE_KEY] = True
        session.info['confirmation_now'] = app.state.clock.now()
        child = session.get(m.Approval, session.get(m.Approval, parent['id']).payload['collection_id'])
        result = collect(FillContext(session, app.state.clock, app.state.provider, app.state.gloo), child, reminder=True)
        session.commit()
        return result


def submitted_initial(case, *, stop=False):
    client, app, *_ = case
    item = queued_initial(case)
    app.state.clock.advance(timedelta(minutes=30))
    if stop:
        from tests.session_fixtures import session_id
        response=client.post('/mac/inbound', headers=BRIDGE, json={'phone':PHONE, 'body':'STOP',
            'guid':'synthetic-stop-before-ack', 'session_id':session_id(PHONE)})
        assert response.status_code == 200
    response=client.post(f"/mac/outbound/{item['id']}/ack", headers=BRIDGE,
        json={'token':item['token'], 'outcome':'submitted'})
    assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        proof=session.get(m.Notification, f"mac-submission:{item['id']}")
        assert proof.created_at == NOW+timedelta(minutes=30)
        assert proof.detail['clock_basis']=='server_ack_observed' and proof.state=='submitted'
    return item


def test_reminder_waits_three_days_from_delayed_submission_ack_not_queue(ongoing_collection):
    client, app, *_ = ongoing_collection
    item=submitted_initial(ongoing_collection)
    due=NOW+timedelta(days=3,minutes=30)
    app.state.clock.set_time(NOW+timedelta(days=3))
    assert stage_reminder(ongoing_collection)=={'sent':[],'reviews':[]}
    app.state.clock.set_time(due-timedelta(microseconds=1))
    assert stage_reminder(ongoing_collection)=={'sent':[],'reviews':[]}
    assert app.state.gloo.calls==1
    app.state.clock.set_time(due)
    result=stage_reminder(ongoing_collection)
    assert result['sent']==[] and len(result['reviews'])==1 and app.state.gloo.calls==2
    assert stage_reminder(ongoing_collection)==result and app.state.gloo.calls==2
    assert client.post('/mac/outbound/pull', headers=BRIDGE, json={}).json()['messages']==[]
    with app.state.session_factory() as session:
        exact=session.get(m.Approval,result['reviews'][0])
        assert exact.status=='pending' and exact.payload['conversation']['binding']['reminder'] is True
        first=session.get(m.Message,item['id'])
        assert first.created_at==NOW and first.status=='submitted'


@pytest.mark.parametrize('phase',['approval','claim','preflight'])
def test_reminder_elapsed_time_is_rechecked_at_each_delivery_boundary(ongoing_collection,phase):
    from app.core import reminders, outbound_conversation
    client, app, *_ = ongoing_collection
    submitted_initial(ongoing_collection)
    due=NOW+timedelta(days=3,minutes=30)
    app.state.clock.set_time(due)
    ident=stage_reminder(ongoing_collection)['reviews'][0]
    with app.state.session_factory() as session:
        digest=session.get(m.Approval,ident).payload['content_hash']
    if phase!='approval': approve(client,ident,digest)
    item=None
    if phase=='preflight':
        item=client.post('/mac/outbound/pull',headers=BRIDGE,json={}).json()['messages'][0]
        assert client.post(f"/mac/outbound/{item['id']}/verify",headers=BRIDGE,
            json={'token':item['token'],'content_hash':digest}).status_code==200
    app.state.clock.set_time(due-timedelta(microseconds=1))
    with app.state.session_factory() as session:
        exact=session.get(m.Approval,ident)
        issue=reminders.delivery_problem(session,exact,app.state.clock.now())
        assert 'three-day wait' in issue
        if item:
            message=session.get(m.Message,item['id'])
            assert outbound_conversation.queued_problem(session,message,app.state.clock.now(),exact)
    if phase=='approval':
        approve(client,ident,digest)
        with app.state.session_factory() as session:
            exact=session.get(m.Approval,ident)
            assert exact.status=='expired' and not exact.payload.get('message_id')
    elif phase=='claim':
        assert client.post('/mac/outbound/pull',headers=BRIDGE,json={}).json()['messages']==[]
    else:
        response=client.post(f"/mac/outbound/{item['id']}/verify",headers=BRIDGE,
            json={'token':item['token'],'content_hash':digest})
        assert response.status_code==409


@pytest.mark.parametrize('defect',['missing','uncertain','wrong_token','no_preflight'])
def test_unknown_submission_time_never_authorizes_a_reminder(ongoing_collection,defect):
    client,app,*_=ongoing_collection
    item=queued_initial(ongoing_collection)
    app.state.clock.advance(timedelta(minutes=30))
    if defect=='missing':
        # A legacy submitted row has no historical ACK clock. Do not invent one.
        mutate(app,lambda session:setattr(session.get(m.Message,item['id']),'status','submitted'))
        assert client.post(f"/mac/outbound/{item['id']}/ack",headers=BRIDGE,
            json={'token':item['token'],'outcome':'submitted'}).status_code==200
    elif defect=='uncertain':
        assert client.post(f"/mac/outbound/{item['id']}/ack",headers=BRIDGE,
            json={'token':item['token'],'outcome':'uncertain'}).status_code==200
        app.state.clock.advance(timedelta(minutes=20))
        assert client.post(f"/mac/outbound/{item['id']}/ack",headers=BRIDGE,
            json={'token':item['token'],'outcome':'submitted'}).status_code==409
    elif defect=='wrong_token':
        assert client.post(f"/mac/outbound/{item['id']}/ack",headers=BRIDGE,
            json={'token':'z'*64,'outcome':'submitted'}).status_code==409
    else:
        mutate(app,lambda session:session.delete(session.get(m.Notification,f"mac-preflight:{item['id']}")))
        assert client.post(f"/mac/outbound/{item['id']}/ack",headers=BRIDGE,
            json={'token':item['token'],'outcome':'submitted'}).status_code==200
    app.state.clock.set_time(NOW+timedelta(days=4))
    assert stage_reminder(ongoing_collection)=={'sent':[],'reviews':[]} and app.state.gloo.calls==1
    with app.state.session_factory() as session:
        assert session.get(m.Notification,f"mac-submission:{item['id']}") is None


@pytest.mark.parametrize('stop',[False,True])
def test_duplicate_and_stop_race_acks_keep_truthful_original_observation(ongoing_collection,stop):
    client,app,*_=ongoing_collection
    item=submitted_initial(ongoing_collection,stop=stop)
    app.state.clock.advance(timedelta(days=3))
    assert client.post(f"/mac/outbound/{item['id']}/ack",headers=BRIDGE,
        json={'token':item['token'],'outcome':'submitted'}).status_code==200
    with app.state.session_factory() as session:
        proof=session.get(m.Notification,f"mac-submission:{item['id']}")
        assert proof.created_at==NOW+timedelta(minutes=30)
        assert len(session.scalars(select(m.Notification).where(m.Notification.key==proof.key)).all())==1
        person=session.scalar(select(m.Volunteer))
        assert person.sms_opt_in is not stop
    if stop:
        assert stage_reminder(ongoing_collection)=={'sent':[],'reviews':[]} and app.state.gloo.calls==1


@pytest.mark.parametrize('change',['token_hash','body_hash','phone','provider_sid','clock_basis','future_time'])
def test_submission_timing_receipt_must_match_the_original_native_claim(ongoing_collection,change):
    client,app,*_=ongoing_collection
    item=submitted_initial(ongoing_collection)
    def update(session):
        receipt=session.get(m.Notification,f"mac-submission:{item['id']}")
        if change=='future_time': receipt.created_at=NOW+timedelta(days=5)
        else: receipt.detail={**receipt.detail,change:'changed'}
    mutate(app,update)
    app.state.clock.set_time(NOW+timedelta(days=4))
    assert stage_reminder(ongoing_collection)=={'sent':[],'reviews':[]} and app.state.gloo.calls==1
