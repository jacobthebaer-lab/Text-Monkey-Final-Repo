"""STOP and native receipts race against disposable SQL and fake senders only."""
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.integrations import mac_messages as connector
from tests.test_mac_messages import (
    mac_app, post, incoming, queue_essential_intake, config, ReaderFixture, PHONE,
)
from tests.session_fixtures import session_specs


@pytest.mark.parametrize('outcome', ['submitted', 'uncertain'])
def test_verified_receipt_survives_stop_without_restoring_consent(mac_app, outcome):
    ident = queue_essential_intake(mac_app)
    with TestClient(mac_app) as client:
        claim = post(client, '/mac/outbound/pull').json()['messages'][0]
        assert post(client, f'/mac/outbound/{ident}/verify', {'token': claim['token']}).status_code == 200
        assert post(client, '/mac/inbound', incoming('receipt-race-stop', 'STOP')).status_code == 200
        for _ in range(2):
            response = post(client, f'/mac/outbound/{ident}/ack', {'token': claim['token'], 'outcome': outcome})
            assert response.status_code == 200
            assert response.json()['status'] == outcome
        opposite = 'uncertain' if outcome == 'submitted' else 'submitted'
        assert post(client, f'/mac/outbound/{ident}/ack', {'token': claim['token'], 'outcome': opposite}).status_code == 409
        assert post(client, f'/mac/outbound/{ident}/verify', {'token': claim['token']}).status_code == 409
        assert post(client, '/mac/outbound/pull').json()['messages'] == []
    with mac_app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        assert not person.sms_opt_in
        assert session.get(m.Policy, 'sms_opt_out:' + PHONE).value['value'] is True
        assert session.get(m.Message, ident).status == outcome
        gate = SendGate(session, mac_app.state.clock, mac_app.state.provider)
        assert gate.send(body='What roles would you like?', purpose='signup_reply', volunteer=person,
                         kind='ai', conversation={'intake_fields':['interests']}).status == SendStatus.BLOCKED_OPT_OUT


@pytest.mark.parametrize('defect', ['no_preflight', 'token', 'body', 'proof', 'route_hold'])
def test_stop_does_not_authorize_forged_or_known_unsent_receipt(mac_app, defect):
    ident = queue_essential_intake(mac_app)
    with TestClient(mac_app) as client:
        claim = post(client, '/mac/outbound/pull').json()['messages'][0]
        if defect != 'no_preflight':
            assert post(client, f'/mac/outbound/{ident}/verify', {'token': claim['token']}).status_code == 200
        if defect == 'route_hold':
            assert post(client, f'/mac/outbound/{ident}/route-hold', {'token': claim['token']}).status_code == 200
        assert post(client, '/mac/inbound', incoming('invalid-receipt-stop', 'STOP')).status_code == 200
        with mac_app.state.session_factory() as session:
            if defect == 'body':
                session.get(m.Message, ident).body += ' Changed.'
            if defect == 'proof':
                proof = session.get(m.Notification, f'mac-preflight:{ident}')
                if proof:
                    proof.detail = {**proof.detail, 'token_hash':'0'*64}
            session.commit()
        token = 'z'*64 if defect == 'token' else claim['token']
        assert post(client, f'/mac/outbound/{ident}/ack', {'token': token, 'outcome':'submitted'}).status_code == 409
        assert post(client, f'/mac/outbound/{ident}/verify', {'token': claim['token']}).status_code == 409


@pytest.mark.parametrize('sender_result', ['submitted', 'uncertain', 'failed'])
@pytest.mark.parametrize('lost_response', [False, True])
def test_worker_stop_race_ack_recovery_never_resends(mac_app, tmp_path, monkeypatch, sender_result, lost_response):
    now = mac_app.state.clock.now()
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return now
    monkeypatch.setattr(connector, 'datetime', Frozen)
    ident = queue_essential_intake(mac_app)
    cfg = config(tmp_path)
    cfg['test_sessions'] = session_specs(cfg['phones'], now)
    attempts, paths = [], []
    lose = [lost_response]
    with TestClient(mac_app) as client:
        def server(request):
            paths.append(request.url.path)
            response = client.post(request.url.path, json=json.loads(request.content), headers=dict(request.headers))
            if request.url.path.endswith('/ack') and lose and lose.pop():
                assert response.status_code == 200
                raise httpx.ReadError('Synthetic lost committed acknowledgment')
            return httpx.Response(response.status_code, json=response.json())
        def sender(phone, body):
            attempts.append(ident)
            assert post(client, '/mac/inbound', incoming('worker-receipt-stop', 'STOP')).status_code == 200
            return sender_result
        def build():
            return connector.MacWorker(cfg, live=True, client=httpx.Client(transport=httpx.MockTransport(server)),
                reader=ReaderFixture(), sender=sender)
        first = build()
        if lost_response:
            with pytest.raises(httpx.ReadError): first.once()
        else:
            first.once()
        resumed = build()
        resumed.once()
        assert not resumed.active_path.exists()
        assert attempts == [ident]
        assert paths.count(f'/mac/outbound/{ident}/ack') == (2 if lost_response else 1)
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message, ident).status == ('submitted' if sender_result == 'submitted' else 'uncertain')
        assert not session.scalar(select(m.Volunteer)).sms_opt_in


@pytest.mark.parametrize('outcome', ['submitted', 'uncertain'])
def test_late_offer_receipt_does_not_reopen_stop_closed_invitation(mac_app, clock, outcome):
    from app.core import offer_windows as offers
    from app.web.mac_messages import native_preflight_binding
    from tests.test_offer_transport import queue_offer, record_claim
    outreach_id, fill_id = queue_offer(mac_app, clock)
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach, outreach_id)
        message, claim = record_claim(session, outreach, clock)
        assert offers.dispatch(session, outreach, message, clock.now()) is None
        # Historical verified receipt fixture; does not enable current outreach.
        session.add(m.Notification(key=f'mac-preflight:{message.id}', purpose='native_preflight',
            state='verified', message_id=message.id, volunteer_id=message.volunteer_id,
            created_at=clock.now(), due_at=clock.now(), detail=native_preflight_binding(message, claim)))
        ident, token = message.id, claim.token
        session.commit()
    with TestClient(mac_app) as client:
        assert post(client, '/mac/inbound', incoming('offer-receipt-stop', 'STOP')).status_code == 200
        assert post(client, f'/mac/outbound/{ident}/ack', {'token':token, 'outcome':outcome}).status_code == 200
        assert post(client, '/mac/outbound/pull').json()['messages'] == []
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach, outreach_id)
        assert outreach.response == 'blocked'
        assert offers.metadata(session, outreach).state == 'offer_blocked'
        assert session.get(m.Message, ident).status == outcome
        assert session.scalar(select(m.Assignment)) is None
        assert not session.scalar(select(m.Volunteer)).sms_opt_in
