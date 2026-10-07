"""Clock skew uses an isolated backend, durable fake worker and no native send."""
import json
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core import confirmations
from app.db import models as m
from app.integrations import mac_messages as connector, mac_roster
from tests.test_demo_acceptance_review import acceptance_app, BRIDGE_TOKEN, pull
from tests.test_literal_reminder_mac_acceptance import reminder_mac, book, tick
from tests.test_mac_messages import ReaderFixture


def queue_short_review(client, app, clock):
    book(app)
    _, reviews = tick(app)
    ident = reviews[0][0]
    with app.state.session_factory() as session:
        review = session.get(m.Approval, ident)
        review.payload = {**review.payload, 'expires_at': (clock.now()+timedelta(minutes=1)).isoformat()}
        review.payload = {**review.payload, 'content_hash': confirmations.digest(review.payload)}
        digest = review.payload['content_hash']
        session.commit()
    assert client.post(f'/api/proposals/{ident}/approve', json={'content_hash':digest}).status_code == 200
    return digest


def post(client, path, data):
    return client.post(path, json=data, headers={'Authorization':'Bearer '+BRIDGE_TOKEN})


@pytest.mark.parametrize('lost', [None, 'before_commit', 'after_commit'])
def test_skew_hold_reconciles_both_ledgers_across_restart(reminder_mac, tmp_path, monkeypatch, lost):
    client, app, _, clock = reminder_mac
    queue_short_review(client, app, clock)
    class Ahead(datetime):
        @classmethod
        def now(cls, tz=None): return clock.now()+timedelta(minutes=2)
    monkeypatch.setattr(connector, 'datetime', Ahead)
    cfg = {'backend_url':'https://fixture.example.test', 'token':BRIDGE_TOKEN,
           'phones':list(app.state.provider.phones), 'receiving_number':'+15555550200',
           'test_sessions':json.loads(app.state.settings.mac_test_sessions), 'state_path':str(tmp_path/'checkpoint.json')}
    failure, hold_requests, paths = [lost], [], []
    def server(request):
        paths.append(request.url.path)
        mode = failure.pop() if request.url.path.endswith('/review-hold') and failure else None
        if request.url.path.endswith('/review-hold'):
            hold_requests.append(json.loads(request.content))
        if mode == 'before_commit': raise httpx.ReadError('Synthetic loss before hold commit')
        response = client.post(request.url.path, json=json.loads(request.content), headers=dict(request.headers))
        if mode == 'after_commit':
            assert response.status_code == 200
            raise httpx.ReadError('Synthetic loss after hold commit')
        return httpx.Response(response.status_code, json=response.json())
    def build():
        return connector.MacWorker(cfg, live=True, client=httpx.Client(transport=httpx.MockTransport(server)),
            reader=ReaderFixture(), sender=lambda *_: pytest.fail('Expired review cannot send'))
    first = build()
    if lost:
        with pytest.raises(httpx.ReadError): first.once()
        assert list(first.state['dispatches'].values())[0]['outcome'] == 'review_hold_pending'
        assert connector.checkpoint_diagnostic(cfg, now=Ahead.now())['receipts_pending_ack'] == 1
    else:
        first.once()
    resumed = build(); resumed.once()
    assert not resumed.active_path.exists()
    assert list(resumed.state['dispatches'].values())[0]['outcome'] == 'blocked'
    assert all(request == hold_requests[0] for request in hold_requests)
    assert not any(path.endswith('/ack') for path in paths)
    assert sum(path.endswith('/verify') for path in paths) == 1
    with app.state.session_factory() as session:
        row = session.scalar(select(m.Message))
        assert row.status == 'blocked_review_expired'
        assert not mac_roster.unsettled(session)
        assert session.get(m.Notification, f'mac-review-hold:{row.id}').detail['native_attempted'] is False
        assert post(client, f'/mac/outbound/{row.id}/ack',
                    {'token':list(resumed.state['dispatches'].values())[0]['token'], 'outcome':'submitted'}).status_code == 409


@pytest.mark.parametrize('defect', ['token', 'body', 'hash', 'session', 'missing_preflight', 'too_early',
                                   'submitted', 'uncertain', 'attempting'])
def test_review_hold_cannot_downgrade_attempts_or_changed_proof(reminder_mac, defect):
    client, app, _, clock = reminder_mac
    digest = queue_short_review(client, app, clock)
    claim = pull(client).json()['messages'][0]
    ident = claim['id']
    data = {'token':claim['token'], 'content_hash':digest,
            'observed_at':(clock.now()+timedelta(minutes=2)).isoformat()}
    if defect != 'missing_preflight':
        assert post(client, f'/mac/outbound/{ident}/verify', {'token':claim['token'], 'content_hash':digest}).status_code == 200
    if defect == 'token': data['token'] = 'z'*64
    if defect == 'hash': data['content_hash'] = '0'*64
    if defect == 'too_early': data['observed_at'] = clock.now().isoformat()
    if defect == 'session':
        current = app.state.provider.test_sessions[claim['phone']]
        app.state.provider.test_sessions[claim['phone']] = replace(current, id='f'*32)
    with app.state.session_factory() as session:
        row = session.get(m.Message, ident)
        if defect == 'body': row.body += ' Changed.'
        if defect in {'submitted', 'uncertain', 'attempting'}: row.status = defect
        before = row.status
        session.commit()
    assert post(client, f'/mac/outbound/{ident}/review-hold', data).status_code == 409
    with app.state.session_factory() as session:
        assert session.get(m.Message, ident).status == before
        assert session.get(m.Notification, f'mac-review-hold:{ident}') is None


def test_stop_before_noattempt_receipt_keeps_optout(reminder_mac):
    client, app, _, clock = reminder_mac
    digest = queue_short_review(client, app, clock)
    claim = pull(client).json()['messages'][0]
    ident = claim['id']
    assert post(client, f'/mac/outbound/{ident}/verify', {'token':claim['token'], 'content_hash':digest}).status_code == 200
    with app.state.session_factory() as session:
        row = session.get(m.Message, ident)
        confirmations.suppress_phone(session, row.phone)
        session.add(m.Policy(key='sms_opt_out:'+row.phone, value={'value':True}))
        session.commit()
    data = {'token':claim['token'], 'content_hash':digest,
            'observed_at':(clock.now()+timedelta(minutes=2)).isoformat()}
    for _ in range(2):
        result = post(client, f'/mac/outbound/{ident}/review-hold', data)
        assert result.status_code == 200 and result.json()['status'] == 'blocked_opt_out'
    assert post(client, f'/mac/outbound/{ident}/ack', {'token':claim['token'], 'outcome':'submitted'}).status_code == 409
    with app.state.session_factory() as session:
        assert not mac_roster.unsettled(session)
        assert session.get(m.Policy, 'sms_opt_out:'+claim['phone']).value['value'] is True
