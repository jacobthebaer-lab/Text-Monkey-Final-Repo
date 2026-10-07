"""Reviewed collection on isolated Mac queues, never native Messages delivery."""
from datetime import timedelta
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
