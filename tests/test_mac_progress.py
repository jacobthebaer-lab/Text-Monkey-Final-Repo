"""Synthetic source-bound progress delivery and deferred network concurrency."""
import json
import threading
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.agents.fill_agent import FillContext
from app.core.notifications import flush_due
from app.db import models as m
from app.integrations import mac_progress
from app.integrations.mac_models import MacInboundReceipt
from app.llm.gloo_client import GlooUnavailableError
from app.main import create_app
from tests.session_fixtures import session_id, session_json
from tests.test_exact_signup_copy import ExactGloo

PHONE = '+12025550191'
TOKEN = 'synthetic-progress-' + 'x' * 40
HEADERS = {'Authorization': 'Bearer ' + TOKEN}
BODY = ('My availability is Sundays and Wednesdays all day, excluding October 18. '
        'Please keep that unavailable date in my preferences and use these days for future volunteer opportunities.')


class BlockingGloo(ExactGloo):
    def __init__(self):
        super().__init__()
        self.extracting = threading.Event()
        self.release = threading.Event()
        self.block_ack = False
        self.ack_started = threading.Event()
        self.fail_ack = False

    def create_response(self, **arguments):
        facts = json.loads(arguments['input'])
        if isinstance(facts, dict) and facts.get('approved_message') in {mac_progress.ACK_TEXT, mac_progress.SCHEDULE_ACK_TEXT}:
            self.ack_started.set()
            if self.fail_ack:
                raise GlooUnavailableError('Synthetic acknowledgment unavailable')
            if self.block_ack:
                assert self.release.wait(5)
        if isinstance(facts, dict) and facts.get('stage') == 'availability':
            self.extracting.set()
            assert self.release.wait(5)
        return super().create_response(**arguments)


@pytest.fixture
def progress_app(tmp_path, clock):
    application = create_app(Settings(database_url=f'sqlite:///{tmp_path}/progress.db',
        demo_mode=True, automation_enabled=False, sms_provider='mac_messages',
        mac_bridge_enabled=True, mac_bridge_token=TOKEN, admin_password='synthetic-admin-password',
        mac_demo_phones=PHONE, mac_test_sessions=session_json([PHONE], clock.now()),
        allow_text_signup=True, gloo_signup_replies=True))
    application.state.clock = application.state.mac_delivery_clock = clock
    application.state.gloo = BlockingGloo()
    with application.state.session_factory() as session:
        session.add_all([
            m.Volunteer(name='Synthetic Recipient', phone=PHONE, sms_opt_in=True, status='active',
                preferences={'signup_source': 'sms', 'signup_minimal_texts': True, 'onboarding_stage': 'availability',
                    'interested_roles': ['Greeter'], 'any_role': False}, created_at=clock.now()),
            m.Policy(key='full_text_onboarding', value={'value': True}),
            m.Policy(key='signup_exact_copy:' + PHONE, value={'value': True}),
        ])
        session.commit()
    return application


def incoming(guid='synthetic-complex-availability', body=BODY):
    return {'guid': guid, 'phone': PHONE, 'body': body, 'service': 'iMessage', 'session_id': session_id(PHONE)}


def post(client, path, data=None):
    return client.post(path, json=data or {}, headers=HEADERS)


def submit_ack(client, body=mac_progress.ACK_TEXT):
    batch = post(client, '/mac/outbound/pull').json()['messages']
    assert len(batch) == 1 and batch[0]['body'] == body
    ack = batch[0]
    assert post(client, f"/mac/outbound/{ack['id']}/verify", {'token': ack['token']}).status_code == 200
    assert post(client, f"/mac/outbound/{ack['id']}/ack", {'token': ack['token'], 'outcome': 'submitted'}).status_code == 200
    return ack


def wait_worker(application):
    thread = getattr(application.state, 'mac_progress_thread', None)
    if thread:
        thread.join(5)
        assert not thread.is_alive()


def test_ack_is_queued_before_extraction_and_replays_update_once(progress_app):
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming())
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['progress_state'] == 'waiting_ack'
        assert not progress_app.state.gloo.extracting.is_set()
        assert post(client, '/mac/inbound', incoming()).json()['duplicate']
        assert post(client, '/mac/inbound', incoming(body=BODY + ' Changed')).status_code == 409
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        assert not progress_app.state.gloo.extracting.is_set()
        ack = submit_ack(client)
        post(client, '/mac/progress/tick')
        assert progress_app.state.gloo.extracting.wait(2)
        # Gloo has no live write lock: another application transaction commits.
        with progress_app.state.session_factory() as session:
            session.add(m.Policy(key='synthetic-concurrent-write', value={'value': True}))
            session.commit()
        progress_app.state.gloo.release.set(); wait_worker(progress_app)
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            flush_due(FillContext(session,progress_app.state.clock,progress_app.state.provider,progress_app.state.gloo))
            session.commit()
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification, accepted.json()['progress_key'])
            assert job.state == 'done', job.detail
            person = session.scalar(select(m.Volunteer))
            assert person.preferences['onboarding_stage'] == 'complete'
            assert person.preferences['availability_weekdays'] == [6, 2]
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'in')).all()) == 1
            outgoing=session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
            assert len(outgoing)==2 and sum(row.body==mac_progress.ACK_TEXT for row in outgoing)==1
            completion=session.get(m.Notification,f'ordinary-reply:{job.message_id}')
            assert completion.detail['signup_completion'] and completion.message_id!=ack['id']
            assert session.get(m.Message, ack['id']).status == 'submitted'
            assert session.scalar(select(m.Assignment)) is None
        assert len(progress_app.state.gloo.calls) == 3  # Immediate ACK, extraction, completion.


@pytest.mark.parametrize('change', ['stop', 'profile', 'session', 'new_input', 'input_content', 'care'])
def test_work_invalidated_during_gloo_never_overwrites_current_profile(progress_app, clock, change):
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming()).json()
        submit_ack(client); post(client, '/mac/progress/tick')
        assert progress_app.state.gloo.extracting.wait(2)
        with progress_app.state.session_factory() as session:
            person = session.scalar(select(m.Volunteer))
            if change == 'profile': person.preferences = {**person.preferences, 'operator_note': 'keep this newer preference'}
            if change == 'input_content':
                session.get(m.Message, session.get(m.Notification, accepted['progress_key']).message_id).body = 'Changed original stored input'
            if change == 'care':
                session.add(m.Escalation(category='sensitive', severity='normal', summary='Synthetic care hold',
                    related_ids={'phone': PHONE}, status='open', created_at=clock.now()))
            if change == 'new_input':
                session.add(m.Message(direction='in', volunteer_id=person.id, phone=PHONE, body='A newer actual reply',
                    kind='mac_test_in', purpose='test:' + session_id(PHONE), status='received', created_at=clock.now()))
            session.commit()
        if change == 'stop':
            stopped = post(client, '/mac/inbound', incoming(guid='synthetic-stop-during-extraction', body='STOP'))
            assert stopped.status_code == 200 and stopped.json()['intent'] == 'stop'
        if change == 'session': clock.advance(timedelta(hours=2))
        progress_app.state.gloo.release.set(); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification, accepted['progress_key'])
            assert job.state == 'superseded'
            person = session.scalar(select(m.Volunteer))
            assert person.preferences['onboarding_stage'] == 'availability'
            assert 'availability_weekdays' not in person.preferences
            if change == 'profile': assert person.preferences['operator_note'] == 'keep this newer preference'


def test_concurrent_accept_and_restart_drain_share_one_ack(progress_app):
    progress_app.state.gloo.block_ack = True
    result = {}
    with TestClient(progress_app) as client:
        thread = threading.Thread(target=lambda: result.update(post(client, '/mac/inbound', incoming()).json()))
        thread.start()
        assert progress_app.state.gloo.ack_started.wait(2)
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        duplicate = post(client, '/mac/inbound', incoming()).json()
        assert duplicate['duplicate'] and duplicate['progress_state'] == 'ack_pending'
        progress_app.state.gloo.release.set(); thread.join(5)
        assert not thread.is_alive() and result['progress_state'] == 'waiting_ack'
        assert sum(call.get('approved_message') == mac_progress.ACK_TEXT for call in progress_app.state.gloo.calls) == 1
        with progress_app.state.session_factory() as session:
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'out')).all()) == 1


def test_restart_resumes_actual_input_without_resending_ack(progress_app):
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming()).json()
        submit_ack(client)
    replacement = create_app(replace(progress_app.state.settings))
    replacement.state.clock = replacement.state.mac_delivery_clock = progress_app.state.clock
    replacement.state.gloo = ExactGloo()
    with TestClient(replacement) as client:
        assert post(client, '/mac/inbound', incoming()).json()['duplicate']
        post(client, '/mac/progress/tick'); wait_worker(replacement)
        with replacement.state.session_factory() as session:
            flush_due(FillContext(session,replacement.state.clock,replacement.state.provider,replacement.state.gloo))
            session.commit()
        with replacement.state.session_factory() as session:
            assert session.get(m.Notification, accepted['progress_key']).state == 'done'
            outgoing=session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
            assert len(outgoing)==2 and sum(row.body==mac_progress.ACK_TEXT for row in outgoing)==1
            job=session.get(m.Notification,accepted['progress_key'])
            completion=session.get(m.Notification,f'ordinary-reply:{job.message_id}')
            assert completion.detail['signup_completion'] and completion.message_id!=job.detail['ack_message_id']
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'in')).all()) == 1
        assert len(replacement.state.gloo.calls) == 2  # Extraction and completion, no repeated ACK.


def test_gloo_ack_failure_is_held_without_fallback_or_extraction(progress_app):
    progress_app.state.gloo.fail_ack = True
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming()).json()
        assert accepted['progress_state'] == 'held'
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        assert post(client, '/mac/outbound/pull').json()['messages'] == []
        assert not progress_app.state.gloo.extracting.is_set()
        with progress_app.state.session_factory() as session:
            assert session.get(m.Notification, accepted['progress_key']).state == 'held'
            assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None


def test_replay_preserves_actual_function_output_and_usage():
    actual = SimpleNamespace(settings=Settings())
    response = SimpleNamespace(output_text='', status='completed', usage=SimpleNamespace(input_tokens=9, output_tokens=7),
        output=[SimpleNamespace(type='function_call', name='choose_preferences', arguments='{"weekday":2}', call_id='synthetic-call')])
    arguments = {'model': 'synthetic', 'input': 'synthetic'}
    try:
        mac_progress._ReplayGloo(actual, {}).create_response(**arguments)
    except mac_progress._NetworkBoundary as pending:
        key = pending.key
    replay = mac_progress._ReplayGloo(actual, {key: mac_progress._json_response(response)}).create_response(**arguments)
    assert replay.output[0].arguments == response.output[0].arguments
    assert replay.output[0].call_id == response.output[0].call_id
    assert replay.usage.input_tokens == 9 and replay.status == 'completed'


def test_quick_ack_uses_one_bounded_gloo_attempt():
    import httpx
    import openai
    from app.llm.gloo_client import GlooClient
    attempts, options = [], []
    class Client:
        def with_options(self, **kwargs):
            options.append(kwargs)
            return self
        @property
        def responses(self): return self
        def create(self, **kwargs):
            attempts.append(kwargs)
            raise openai.RateLimitError('synthetic rate limited', body={},
                response=httpx.Response(429, request=httpx.Request('POST', 'https://synthetic.example.test')))
    actual = GlooClient(Settings(), client=Client(), sleeper=lambda delay: pytest.fail('Quick acknowledgment must not retry'))
    with pytest.raises(GlooUnavailableError):
        mac_progress._fast_gloo(actual).create_response(model='synthetic', input='synthetic')
    assert len(attempts) == 1
    assert options == [{'timeout': 8.0, 'max_retries': 0}]


def test_worker_dispatches_ack_before_next_input_and_processing(tmp_path):
    import httpx
    from app.integrations.mac_messages import MacWorker
    from tests.test_mac_messages import config, PHONE as WORKER_PHONE
    ordered, sent = [], []
    first = {'row_id': 43, 'guid': 'synthetic-first', 'phone': WORKER_PHONE, 'body': 'Synthetic availability',
             'session_id': session_id(WORKER_PHONE), 'service': 'iMessage'}
    second = {**first, 'row_id': 44, 'guid': 'synthetic-second', 'body': 'Synthetic next input'}
    class Reader:
        def watermark(self): return 42
        def new_messages(self, after): return [item for item in (first, second) if item['row_id'] > after]
    item = {'id': 100, 'token': 'c' * 64, 'phone': WORKER_PHONE, 'session_id': session_id(WORKER_PHONE),
            'body': mac_progress.ACK_TEXT, 'conversation_preflight_required': True}
    def server(request):
        path = request.url.path
        data = json.loads(request.content)
        ordered.append((path, data.get('guid')))
        if path == '/mac/inbound': return httpx.Response(200, json={'progress_key': 'synthetic-job'})
        if path == '/mac/outbound/pull': return httpx.Response(200, json={'messages': [] if sent else [item]})
        if path.endswith('/verify'): return httpx.Response(200, json={'verified': True, 'phone': WORKER_PHONE, 'body': mac_progress.ACK_TEXT})
        return httpx.Response(200, json={})
    worker = MacWorker(config(tmp_path), live=True, reader=Reader(),
        client=httpx.Client(transport=httpx.MockTransport(server)),
        sender=lambda phone, body: sent.append(body) or 'submitted')
    worker.once()
    first_ack = ordered.index(('/mac/outbound/100/ack', None))
    first_work = ordered.index(('/mac/progress/tick', None))
    second_ingress = ordered.index(('/mac/inbound', 'synthetic-second'))
    assert first_ack < first_work < second_ingress
    assert sent == [mac_progress.ACK_TEXT]


@pytest.mark.parametrize('status', ['uncertain', 'blocked_opt_out', 'blocked_sensitive', 'blocked_style', 'blocked_transport', 'rejected'])
def test_terminal_ack_failures_hold_work_instead_of_waiting_forever(progress_app, status):
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming()).json()
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification, accepted['progress_key'])
            session.get(m.Message, job.detail['ack_message_id']).status = status
            session.commit()
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification, accepted['progress_key'])
            assert job.state == 'held'
            assert session.get(MacInboundReceipt, incoming()['guid']).result['progress_state'] == 'held'
        assert not progress_app.state.gloo.extracting.is_set()


SCHEDULE_BODY = "I can't make it October 18th, but could you show me some additional service dates?"


def schedule_bookings(application):
    with application.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        person.preferences = {**person.preferences, 'onboarding_stage': 'complete', 'max_per_month': 2}
        role = m.Role(name='Greeter', ministry='Welcome', required_qualifications=[], criticality='normal', fill_policy='auto')
        session.add(role)
        for days in (10, 17):
            event = m.Event(title='Sunday Service', starts_at=application.state.clock.now() + timedelta(days=days),
                ends_at=application.state.clock.now() + timedelta(days=days, hours=1), status='scheduled')
            shift = m.Shift(event=event, role=role, slot_index=0)
            session.add(shift)
            session.flush()
            session.add(m.Assignment(shift_id=shift.id, volunteer_id=person.id, status='approved', source='admin',
                created_at=application.state.clock.now(), updated_at=application.state.clock.now()))
        session.commit()


def test_schedule_ack_does_not_wait_for_slow_work_and_resume_uses_original_input(progress_app, monkeypatch, record_property):
    from app.core import inbound
    schedule_bookings(progress_app)
    started, release = threading.Event(), threading.Event()
    original = progress_app.state.gloo.create_response
    def slow(**arguments):
        facts = json.loads(arguments['input'])
        if facts.get('stage') == 'schedule_processing':
            started.set()
            assert release.wait(5)
        return original(**arguments)
    progress_app.state.gloo.create_response = slow
    seen = []
    def resume(session, clock, provider, phone, body, parser, ctx, allow_signup, *, existing_message):
        job = session.get(m.Notification, session.info['mac_progress_resume'])
        assert existing_message.id == job.message_id and body == SCHEDULE_BODY
        assert session.get(m.Message, job.detail['ack_message_id']).status == 'submitted'
        ctx.gloo.create_response(input=json.dumps({'stage': 'schedule_processing'}))
        seen.append(existing_message.id)
        return inbound.InboundResult('fill_agent', notes=['finished_schedule_work'])
    monkeypatch.setattr(inbound, 'handle_inbound', resume)
    # The timeout is a synthetic concurrency measurement, not a delivery claim.
    with TestClient(progress_app) as client:
        before = time.monotonic()
        accepted = post(client, '/mac/inbound', incoming('synthetic-schedule', SCHEDULE_BODY)).json()
        ingress_seconds = time.monotonic() - before
        record_property('synthetic_schedule_ingress_seconds', ingress_seconds)
        assert ingress_seconds < 1, ingress_seconds
        assert accepted['intent'] == 'schedule_processing' and accepted['progress_state'] == 'waiting_ack'
        assert not started.is_set()
        assert post(client, '/mac/inbound', incoming('synthetic-schedule', SCHEDULE_BODY)).json()['duplicate']
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        assert not started.is_set()
        submit_ack(client, mac_progress.SCHEDULE_ACK_TEXT)
        post(client, '/mac/progress/tick')
        assert started.wait(2)
        with progress_app.state.session_factory() as session:
            session.add(m.Policy(key='write-while-schedule-gloo-waits', value={'value': True}))
            session.commit()
        release.set(); wait_worker(progress_app)
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification, accepted['progress_key'])
            assert job.state == 'done' and job.detail['workflow'] == 'schedule'
            assert session.get(MacInboundReceipt, 'synthetic-schedule').result['notes'] == ['finished_schedule_work']
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'in')).all()) == 1
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'out')).all()) == 1
            assert seen == [job.message_id]


@pytest.mark.parametrize('change', ['booking', 'profile', 'stop', 'session', 'new_input', 'input_content', 'care', 'privacy'])
def test_schedule_work_rechecks_original_source_before_resume(progress_app, clock, monkeypatch, change):
    from app.core import inbound
    schedule_bookings(progress_app)
    monkeypatch.setattr(inbound, 'handle_inbound', lambda *args, **kwargs: pytest.fail('Changed work must not resume'))
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming('synthetic-schedule-changed', SCHEDULE_BODY)).json()
        submit_ack(client, mac_progress.SCHEDULE_ACK_TEXT)
        with progress_app.state.session_factory() as session:
            person = session.scalar(select(m.Volunteer))
            if change == 'booking': session.scalar(select(m.Assignment)).status = 'cancelled'
            if change == 'profile': person.preferences = {**person.preferences, 'max_per_month': 1}
            if change == 'stop': person.sms_opt_in = False
            if change == 'input_content': session.get(m.Message, session.get(m.Notification, accepted['progress_key']).message_id).body = 'Changed stored input'
            if change in {'care', 'privacy'}: session.add(m.Escalation(category='privacy' if change == 'privacy' else 'sensitive', severity='normal', summary='Human hold', related_ids={'phone': PHONE}, status='open', created_at=clock.now()))
            if change == 'new_input': session.add(m.Message(direction='in', volunteer_id=person.id, phone=PHONE, body='Newer actual message', kind='mac_test_in', purpose='test:' + session_id(PHONE), status='received', created_at=clock.now()))
            session.commit()
        if change == 'session': clock.advance(timedelta(hours=2))
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            assert session.get(m.Notification, accepted['progress_key']).state == 'superseded'


@pytest.mark.parametrize('body', ['STOP', 'delete my account', 'I cannot make it because of surgery', 'What if I cannot make October 18?', "Don't cancel my shift", 'Thanks!', 'Any other opportunities?'])
def test_schedule_ack_does_not_admit_controls_sensitive_or_noninstructions(progress_app, body):
    schedule_bookings(progress_app)
    with progress_app.state.session_factory() as session:
        data = SimpleNamespace(body=body, phone=PHONE, session_id=session_id(PHONE))
        assert mac_progress.workflow(session, progress_app.state, data) is None


def test_schedule_ack_gloo_failure_retries_bounded_then_holds_without_fallback(progress_app, clock):
    schedule_bookings(progress_app)
    progress_app.state.gloo.fail_ack = True
    with TestClient(progress_app) as client:
        accepted = post(client, '/mac/inbound', incoming('synthetic-schedule-outage', SCHEDULE_BODY)).json()
        assert accepted['progress_state'] == 'ack_pending'
        for attempt in (2, 3):
            post(client, '/mac/progress/tick'); wait_worker(progress_app)
            with progress_app.state.session_factory() as session:
                assert session.get(m.Notification, accepted['progress_key']).detail['retry_attempts']['ack'] == attempt - 1
            clock.advance(timedelta(seconds=30))
            post(client, '/mac/progress/tick'); wait_worker(progress_app)
        post(client, '/mac/progress/tick'); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification, accepted['progress_key'])
            assert job.state == 'held' and job.detail['retry_attempts']['ack'] == 3
            assert len(session.scalars(select(m.Escalation).where(m.Escalation.category == 'mac_progress')).all()) == 1
            assert 'unavailable' in job.detail['reason']
            assert all(a.status == 'approved' for a in session.scalars(select(m.Assignment)))
            assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None
