"""Pre-send route failures use disposable SQL metadata, fake HTTP and no native sends."""
import json
import sqlite3
import subprocess
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.integrations import mac_messages as connector
from app.integrations.mac_models import MacDeliveryClaim
from tests.test_mac_messages import mac_app, config, ReaderFixture, PHONE, post, queue_essential_intake
from tests.test_mac_worker_recovery import item


class RouteReader(ReaderFixture):
    def __init__(self, count):
        self.phones = (PHONE,)
        self.receiving_number = '+15555550900'
        self.services = ('iMessage',)
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE chat (ROWID INTEGER, guid TEXT, service_name TEXT, last_addressed_handle TEXT);
            CREATE TABLE handle (ROWID INTEGER, id TEXT);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        ''')
        self.connection.execute('INSERT INTO handle VALUES (1, ?)', (PHONE,))
        for index in range(count):
            self.connection.execute('INSERT INTO chat VALUES (?, ?, ?, ?)',
                (index+1, f'synthetic-direct-{index}', 'iMessage', self.receiving_number))
            self.connection.execute('INSERT INTO chat_handle_join VALUES (?, 1)', (index+1,))

    def outgoing_chat(self, phone):
        return connector.MessagesReader.outgoing_chat(self, phone)


def native_worker(tmp_path, server, reader, monkeypatch, send):
    monkeypatch.setattr(connector, 'send_native', send)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: pytest.fail('No native process allowed'))
    return connector.MacWorker(config(tmp_path), live=True, reader=reader, sender=send,
        client=httpx.Client(transport=httpx.MockTransport(server)))


@pytest.mark.parametrize('routes', [0, 2])
@pytest.mark.parametrize('lost', [None, 'before_commit', 'after_commit'])
def test_missing_or_ambiguous_route_never_attempts_and_retries_only_hold_token(tmp_path, monkeypatch, routes, lost):
    calls, committed = [], []
    failure = [lost]
    def server(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith('/pull'):
            return httpx.Response(200, json={'messages': [item()]})
        assert request.url.path == '/mac/outbound/7/route-hold'
        assert json.loads(request.content) == {'token': item()['token']}
        mode = failure.pop() if failure else None
        if mode == 'before_commit': raise httpx.ReadError('Synthetic pre-commit loss')
        committed.append(item()['token'])
        if mode == 'after_commit': raise httpx.ReadError('Synthetic post-commit loss')
        return httpx.Response(200, json={'message_id': 7, 'status': 'blocked_native_route', 'native_attempted': False})
    send = lambda *args: pytest.fail('A route-only failure cannot invoke native sender')
    first = native_worker(tmp_path, server, RouteReader(routes), monkeypatch, send)
    if lost:
        with pytest.raises(httpx.ReadError): first.once()
        assert first.state['dispatches']['7']['outcome'] == 'route_hold_pending'
        assert connector.checkpoint_diagnostic(config(tmp_path))['receipts_pending_ack'] == 1
        # A route becoming available after restart does not change this claim's no-send result.
        resumed = native_worker(tmp_path, server, RouteReader(1), monkeypatch, send)
        resumed.once()
    else:
        first.once()
        resumed = native_worker(tmp_path, server, RouteReader(1), monkeypatch, send)
        resumed.once()
    assert resumed.state['dispatches']['7'] == {
        'token': item()['token'], 'outcome': 'blocked', 'reason': 'native_route_unavailable'}
    assert not resumed.active_path.exists()
    assert not any(path.endswith('/ack') for path, _ in calls)
    assert committed and set(committed) == {item()['token']}


def test_crash_after_route_hold_intent_resumes_without_route_lookup_or_sender(tmp_path, monkeypatch):
    class Crash(BaseException): pass
    crashing = [True]
    def server(request):
        if request.url.path.endswith('/pull'): return httpx.Response(200, json={'messages': [item()]})
        if crashing.pop() if crashing else False: raise Crash()
        return httpx.Response(200, json={'message_id': 7, 'status': 'blocked_native_route', 'native_attempted': False})
    send = lambda *args: pytest.fail('No native send')
    first = native_worker(tmp_path, server, RouteReader(0), monkeypatch, send)
    with pytest.raises(Crash): first.once()
    reader = RouteReader(1)
    reader.outgoing_chat = lambda *args: pytest.fail('Pending hold acknowledgment must not resolve or send again')
    resumed = native_worker(tmp_path, server, reader, monkeypatch, send)
    resumed.once()
    assert resumed.state['dispatches']['7']['outcome'] == 'blocked'


def test_exact_route_is_resolved_before_attempt_and_passed_to_native_sender(tmp_path, monkeypatch):
    sent = []
    holder = []
    def server(request):
        if request.url.path.endswith('/pull'): return httpx.Response(200, json={'messages': [item()]})
        assert request.url.path.endswith('/ack')
        return httpx.Response(200, json={'status': 'submitted'})
    def send(phone, body, chat_guid):
        assert holder[0].state['dispatches']['7']['outcome'] == 'attempting'
        sent.append((phone, body, chat_guid)); return 'submitted'
    worker = native_worker(tmp_path, server, RouteReader(1), monkeypatch, send); holder.append(worker)
    worker.once()
    assert sent == [(PHONE, item()['body'], 'synthetic-direct-0')]
    assert worker.state['dispatches']['7']['outcome'] == 'submitted'


def test_real_attempt_timeout_remains_uncertain_even_if_route_later_disappears(tmp_path, monkeypatch):
    attempts, acknowledgments = [], []
    failed = [True]
    def server(request):
        if request.url.path.endswith('/pull'): return httpx.Response(200, json={'messages': [item()]})
        assert request.url.path.endswith('/ack')
        acknowledgments.append(json.loads(request.content))
        if failed.pop() if failed else False: raise httpx.ReadError('Synthetic lost uncertain ack')
        return httpx.Response(200, json={'status': 'uncertain'})
    def send(*args):
        attempts.append(args); raise subprocess.TimeoutExpired('synthetic', 30)
    first = native_worker(tmp_path, server, RouteReader(1), monkeypatch, send)
    with pytest.raises(httpx.ReadError): first.once()
    assert first.state['dispatches']['7']['outcome'] == 'uncertain'
    resumed = native_worker(tmp_path, server, RouteReader(0), monkeypatch, lambda *args: pytest.fail('Never resend uncertainty'))
    resumed.once()
    assert len(attempts) == 1
    assert acknowledgments == [{'token': item()['token'], 'outcome': 'uncertain'}]*2


def test_crash_after_backend_hold_ack_before_local_receipt_save_replays_only_ack(tmp_path, monkeypatch):
    class Crash(BaseException): pass
    holds = []
    def server(request):
        if request.url.path.endswith('/pull'): return httpx.Response(200, json={'messages': [item()]})
        assert request.url.path.endswith('/route-hold')
        holds.append(json.loads(request.content))
        return httpx.Response(200, json={'message_id': 7, 'status': 'blocked_native_route', 'native_attempted': False})
    send = lambda *args: pytest.fail('No native attempt')
    first = native_worker(tmp_path, server, RouteReader(0), monkeypatch, send)
    original = first.save
    def crashing_save():
        if first.state['dispatches'].get('7', {}).get('outcome') == 'blocked': raise Crash()
        original()
    first.save = crashing_save
    with pytest.raises(Crash): first.once()
    assert json.loads(first.state_path.read_text())['dispatches']['7']['outcome'] == 'route_hold_pending'
    resumed = native_worker(tmp_path, server, RouteReader(1), monkeypatch, send)
    resumed.once()
    assert holds == [{'token': item()['token']}]*2
    assert resumed.state['dispatches']['7']['outcome'] == 'blocked'


def test_route_read_database_error_is_not_claimed_as_a_route_hold_or_native_attempt(tmp_path, monkeypatch):
    calls = []
    def server(request):
        calls.append(request.url.path)
        assert request.url.path.endswith('/pull')
        return httpx.Response(200, json={'messages': [item()]})
    reader = RouteReader(1); reader.connection.close()
    worker = native_worker(tmp_path, server, reader, monkeypatch, lambda *args: pytest.fail('No native attempt'))
    with pytest.raises(sqlite3.ProgrammingError): worker.once()
    assert worker.state['dispatches'] == {}
    assert worker.active_path.exists()
    assert calls == ['/mac/outbound/pull']


def test_backend_route_hold_is_authenticated_token_bound_and_idempotent(mac_app):
    with TestClient(mac_app) as client:
        ident = queue_essential_intake(mac_app)
        claimed = post(client, '/mac/outbound/pull').json()['messages'][0]
        path = f'/mac/outbound/{ident}/route-hold'
        assert client.post(path, json={'token': claimed['token']}).status_code == 401
        assert post(client, path, {'token': 'z'*64}).status_code == 409
        for _ in range(2):
            response = post(client, path, {'token': claimed['token']})
            assert response.status_code == 200
            assert response.json() == {'message_id': ident, 'status': 'blocked_native_route', 'native_attempted': False}
        assert post(client, f'/mac/outbound/{ident}/ack', {'token': claimed['token'], 'outcome': 'submitted'}).status_code == 409
    with mac_app.state.session_factory() as session:
        row = session.get(m.Message, ident)
        assert row.status == 'blocked_native_route'
        marker = session.get(m.Notification, f'mac-native-route:{ident}')
        assert marker.detail['native_attempted'] is False
        assert len(session.scalars(select(m.Notification).where(m.Notification.purpose == 'native_route_hold')).all()) == 1
        assert session.get(MacDeliveryClaim, ident).token == claimed['token']


@pytest.mark.parametrize('status', ['submitted', 'uncertain', 'queued', 'blocked_policy'])
def test_route_hold_never_reclassifies_attempted_or_unrelated_claim_status(mac_app, status):
    with TestClient(mac_app) as client:
        ident = queue_essential_intake(mac_app)
        claimed = post(client, '/mac/outbound/pull').json()['messages'][0]
        with mac_app.state.session_factory() as session:
            session.get(m.Message, ident).status = status; session.commit()
        assert post(client, f'/mac/outbound/{ident}/route-hold', {'token': claimed['token']}).status_code == 409
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message, ident).status == status
        assert session.get(m.Notification, f'mac-native-route:{ident}') is None


@pytest.mark.parametrize('fault', ['missing', 'attempted', 'token', 'body'])
def test_route_hold_replay_requires_unchanged_durable_no_attempt_proof(mac_app, fault):
    with TestClient(mac_app) as client:
        ident = queue_essential_intake(mac_app)
        claimed = post(client, '/mac/outbound/pull').json()['messages'][0]
        path = f'/mac/outbound/{ident}/route-hold'; data = {'token': claimed['token']}
        assert post(client, path, data).status_code == 200
        with mac_app.state.session_factory() as session:
            marker = session.get(m.Notification, f'mac-native-route:{ident}')
            if fault == 'missing': session.delete(marker)
            elif fault == 'attempted': marker.detail = {**marker.detail, 'native_attempted': True}
            elif fault == 'token': marker.detail = {**marker.detail, 'token_hash': '0'*64}
            else: session.get(m.Message, ident).body = 'Different unreviewed body'
            session.commit()
        assert post(client, path, data).status_code == 409
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message, ident).status == 'blocked_native_route'


def test_route_hold_closes_unsent_offer_and_holds_fill_internally_once(mac_app):
    from app.core import offer_windows as offers
    with TestClient(mac_app) as client:
        ident = queue_essential_intake(mac_app)
        claimed = post(client, '/mac/outbound/pull').json()['messages'][0]
        with mac_app.state.session_factory() as session:
            now = mac_app.state.mac_delivery_clock.now()
            person = session.scalar(select(m.Volunteer))
            role = m.Role(name='Synthetic welcome team', ministry='Synthetic team', criticality='standard', fill_policy='auto')
            event = m.Event(title='Synthetic event', starts_at=now+timedelta(hours=6), ends_at=now+timedelta(hours=7))
            session.add_all([role, event]); session.flush()
            shift = m.Shift(event_id=event.id, role_id=role.id)
            session.add(shift); session.flush()
            fill = m.FillRequest(shift_id=shift.id, urgency='normal', state='in_progress', created_at=now,
                                 next_action_at=now+timedelta(hours=1))
            session.add(fill); session.flush()
            outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=person.id, tranche=1, message_id=ident)
            session.add(outreach); session.flush()
            meta = offers.prepare(session, outreach, 'Synthetic invitation', now)
            meta.state = 'offer_claimed'
            session.get(m.Message, ident).purpose = 'outreach'
            ids = (outreach.id, fill.id); session.commit()
        for _ in range(2):
            assert post(client, f'/mac/outbound/{ident}/route-hold', {'token': claimed['token']}).status_code == 200
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach, ids[0]); fill = session.get(m.FillRequest, ids[1])
        assert outreach.response == 'blocked' and offers.metadata(session, outreach).state == 'offer_blocked'
        assert fill.state == 'escalated' and fill.next_action_at is None
        assert len(session.scalars(select(m.Escalation)).all()) == 1
        assert len(session.scalars(select(m.Message)).all()) == 1
        assert session.scalar(select(m.Assignment)) is None
        from app.core.send_gate import SendGate, UNSENT_STATUSES
        gate = SendGate(session, mac_app.state.mac_delivery_clock, mac_app.state.provider)
        assert gate._asks_this_month(person.id, now) == 0
        assert session.scalar(select(m.Message.id).where(
            m.Message.volunteer_id == person.id, m.Message.direction == 'out',
            m.Message.purpose.in_({'outreach', 'availability_ask'}),
            m.Message.status.not_in(UNSENT_STATUSES),
            m.Message.created_at > now-timedelta(hours=24))) is None
