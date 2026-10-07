"""Offline committed-source/caller tests, no live model, API or text transport."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.clock import FakeClock
from app.config import Settings
from app.core import confirmations
from app.core.planning_center_blockout_sources import (
    CONTEXT, CommittedBlockoutReader, blockout_source_factory, enqueue_current_availability,
    queue_key, queue_saved_availability, receipt_key,
)
from app.db import models as m
from app.db.session import init_db, make_engine, make_session_factory
from app.integrations.planning_center import PCOBase, PCOConfig, PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_availability import _hash
from app.jobs import process_pco_blockouts
from app.llm.parser import ParsedMessage
from tests.test_signup_completion_ack import completion_case, receive
from tests.test_serving_requests import ExactGloo, incoming
from tests.test_signup_preference_review import review_app, choices
from tests.test_conversational_signup import natural

CONFIG = PCOConfig('synthetic', 'synthetic', '10', ('20',))
NOW = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
USER = {'id': '11111111-1111-4111-8111-111111111111', 'email': 'coordinator@example.test',
        'email_confirmed_at': NOW.isoformat()}


@pytest.fixture
def lane(tmp_path):
    engine = make_engine('sqlite:///' + str(tmp_path / 'source.sqlite'))
    init_db(engine)
    PCOBase.metadata.create_all(engine, tables=[PCOVolunteerPerson.__table__])
    key = tmp_path / 'key'; key.write_bytes(b'synthetic-test-signing-key-' * 2); key.chmod(0o600)
    settings = SimpleNamespace(**{**Settings(admin_email_allowlist=USER['email']).__dict__,
        'pco_blockout_write_enabled': True, 'pco_blockout_signing_key_path': str(key),
        'pco_blockout_acceptance_path': ''})
    factory = make_session_factory(engine)
    factory.configure(info={CONTEXT: (settings, CONFIG)})
    with factory() as session:
        person = m.Volunteer(name='Synthetic Avery', phone='+15550100001', status='active', sms_opt_in=True,
            is_coordinator=False, is_pastor=False, created_at=NOW,
            preferences={'consent_at': NOW.isoformat(), 'consent_source': 'synthetic_signup'})
        session.add(person); session.flush()
        session.add(PCOVolunteerPerson(organization_id='10', volunteer_id=person.id, person_id='70', created_at=NOW))
        session.add(m.Availability(volunteer_id=person.id, month='2026-12', available_dates=[],
            unavailable_dates=['2026-12-01', '2026-12-02'], parsed_at=NOW))
        session.commit()
        vid = person.id
    guarded = blockout_source_factory(engine)
    def read(session):
        return CommittedBlockoutReader(settings, CONFIG, volunteer_id=vid, clock=lambda: NOW)(session)
    def queue():
        with factory() as session:
            key = queue_saved_availability(session, vid)
            session.commit()
            return key
    yield SimpleNamespace(factory=factory, guarded=guarded, settings=settings, vid=vid,
        read=read, queue=queue, clock=FakeClock(NOW), engine=engine)
    engine.dispose()


def test_same_save_rollback_removes_intent_and_preserves_source(lane):
    with lane.factory() as session:
        row = session.scalar(select(m.Availability))
        row.unavailable_dates = ['2026-12-03']
        queue_saved_availability(session, lane.vid)
        session.rollback()
    with lane.guarded() as session:
        assert session.scalar(select(m.Availability)).unavailable_dates == ['2026-12-01', '2026-12-02']
        assert not session.get(m.Policy, queue_key('10', lane.vid))
        assert not session.get(m.Policy, receipt_key('10', lane.vid))
        with pytest.raises(PlanningCenterError, match='committed_receipt_required'):
            lane.read(session)


def test_committed_receipt_works_without_supabase_or_mac_receipt_tables(lane):
    key = lane.queue()
    with lane.guarded() as session:
        source = lane.read(session)
        assert source.value['unavailable_dates'] == ['2026-12-01', '2026-12-02']
        assert source.value['provenance']['revision'] == session.get(m.Policy, key).value['revision']
        assert source.value['dates_authoritative'] is True
        assert not session.new and not session.dirty
    with lane.factory() as session:
        person = session.get(m.Volunteer, lane.vid)
        person.preferences = {**person.preferences, 'serving_requests': [{'status': 'recorded_availability'}]}
        session.add(m.Message(phone=person.phone, volunteer_id=person.id, direction='out',
            body='Synthetic acknowledgment', kind='signup_reply', status='failed', created_at=NOW))
        session.commit()
    with lane.guarded() as session:
        assert lane.read(session).digest == source.digest


def test_repeat_save_dedup_and_empty_removal_preserve_unknown_journal(lane):
    key = lane.queue()
    journal_key = 'pco_bj:' + _hash(['10', lane.vid])
    with lane.factory() as session:
        queue = session.get(m.Policy, key)
        original = deepcopy(queue.value)
        queue.value = {**original, 'state': 'verified'}
        session.add(m.Policy(key=journal_key, value={'immutable_unknown': 'original-attempt'}))
        session.commit()
        assert queue_saved_availability(session, lane.vid) == key
        session.commit()
        assert session.get(m.Policy, key).value['state'] == 'verified'
        session.scalar(select(m.Availability)).unavailable_dates = []
        queue_saved_availability(session, lane.vid)
        session.commit()
        assert session.get(m.Policy, key).value['state'] == 'pending'
        assert session.get(m.Policy, key).value['revision'] != original['revision']
        assert session.get(m.Policy, journal_key).value == {'immutable_unknown': 'original-attempt'}
    with lane.guarded() as session:
        source = lane.read(session).value
        assert source['unavailable_dates'] == [] and source['dates_authoritative']


@pytest.mark.parametrize('fault', ['opt_out', 'inactive', 'consent', 'stop', 'dates', 'draft', 'mapping', 'receipt', 'queue'])
def test_current_source_or_consent_change_holds_before_native_effect(lane, fault):
    lane.queue()
    with lane.factory() as session:
        person = session.get(m.Volunteer, lane.vid)
        if fault == 'opt_out': person.sms_opt_in = False
        elif fault == 'inactive': person.status = 'inactive'
        elif fault == 'consent': person.preferences = {**person.preferences, 'consent_at': None}
        elif fault == 'stop': session.add(m.Policy(key='sms_opt_out:' + person.phone, value={'value': True}))
        elif fault == 'dates': session.scalar(select(m.Availability)).unavailable_dates = ['2026-12-03']
        elif fault == 'draft': person.preferences = {**person.preferences, 'onboarding_availability_draft': {}}
        elif fault == 'mapping': session.delete(session.scalar(select(PCOVolunteerPerson)))
        elif fault == 'receipt': session.get(m.Policy, receipt_key('10', lane.vid)).value = {'schema': 1}
        elif fault == 'queue': session.get(m.Policy, queue_key('10', lane.vid)).value = {'schema': 1}
        session.commit()
    with lane.guarded() as session:
        with pytest.raises(PlanningCenterError): lane.read(session)


@pytest.mark.parametrize('fault', ['dirty', 'flushed', 'raw_sql', 'bulk'])
def test_reader_rejects_uncommitted_source_even_after_flush(lane, fault):
    lane.queue()
    with lane.guarded() as session:
        if fault in {'dirty', 'flushed'}:
            session.get(m.Volunteer, lane.vid).name = 'Changed uncommitted name'
            if fault == 'flushed': session.flush()
        elif fault == 'raw_sql': session.execute(text('SELECT 1'))
        else: session.query(m.Volunteer).filter(m.Volunteer.id == lane.vid).update({'name': 'Changed by bulk'})
        with pytest.raises(PlanningCenterError, match='requires_clean_session'): lane.read(session)
        session.rollback()
        assert lane.read(session).value['volunteer_id'] == lane.vid
    with lane.factory() as session:
        with pytest.raises(PlanningCenterError, match='guarded_session_required'): lane.read(session)


def test_cached_reader_refreshes_latest_committed_revision(lane):
    lane.queue()
    with lane.guarded() as cached:
        cached.get(m.Availability, 1)
        cached.get(m.Policy, queue_key('10', lane.vid))
        with lane.factory() as writer:
            writer.scalar(select(m.Availability)).unavailable_dates = ['2026-12-03']
            queue_saved_availability(writer, lane.vid); writer.commit()
        assert lane.read(cached).value['unavailable_dates'] == ['2026-12-03']


def test_reviewed_availability_reparenting_rejects_before_any_source_or_queue_change(lane):
    key = lane.queue()
    with lane.factory() as session:
        queue = session.get(m.Policy, key)
        queue.value = {**queue.value, 'state': 'verified'}
        old_queue = deepcopy(queue.value)
        other = m.Volunteer(name='Synthetic Blake', phone='+15550100002', status='active', sms_opt_in=True,
            is_coordinator=False, is_pastor=False, created_at=NOW, preferences={})
        session.add(other); session.flush()
        row = session.scalar(select(m.Availability))
        before = confirmations.values(row)
        approval = m.Approval(kind='confirm_record', status='approved', requested_at=NOW,
            payload={'record': 'Availability', 'record_id': row.id, 'before': before,
                     'after': {**before, 'volunteer_id': other.id}})
        session.add(approval); session.commit()
        with pytest.raises(ValueError, match='Moving availability between volunteers is unsupported'):
            confirmations.apply_record(session, approval, NOW)
        session.commit()
        assert row.volunteer_id == lane.vid
        assert row.unavailable_dates == ['2026-12-01', '2026-12-02']
        assert session.get(m.Policy, key).value == old_queue
        assert not session.get(m.Policy, queue_key('10', other.id))


def test_missing_configuration_and_unfinished_signup_preserve_local_save(lane):
    with lane.factory() as session:
        session.info.pop(CONTEXT)
        assert queue_saved_availability(session, lane.vid) is None
        session.info[CONTEXT] = (lane.settings, PCOConfig())
        assert queue_saved_availability(session, lane.vid) is None
        session.info[CONTEXT] = (lane.settings, CONFIG)
        person = session.get(m.Volunteer, lane.vid)
        person.preferences = {**person.preferences, 'onboarding_availability_draft': {'unavailable_dates': ['2026-12-03']}}
        assert queue_saved_availability(session, lane.vid) is None
        session.commit()
        assert person.preferences['onboarding_availability_draft']['unavailable_dates'] == ['2026-12-03']
        assert not session.get(m.Policy, queue_key('10', lane.vid))


def test_policy_enable_creates_honest_authenticated_current_record_receipt(lane):
    with lane.factory() as session:
        with pytest.raises(PlanningCenterError):
            enqueue_current_availability(session, lane.settings, CONFIG, volunteer_id=lane.vid,
                user={**USER, 'email': 'foreign@example.test'}, clock=lambda: NOW)
        key = enqueue_current_availability(session, lane.settings, CONFIG, volunteer_id=lane.vid,
            user=USER, clock=lambda: NOW)
        session.commit()
        receipt = session.get(m.Policy, receipt_key('10', lane.vid)).value
        assert receipt['kind'] == 'coordinator_policy_enable' and receipt['actor_id'] == USER['id']
        assert session.get(m.Policy, key).value['state'] == 'pending'
    with lane.guarded() as session: assert lane.read(session).value['unavailable_dates']


def test_signup_save_enqueues_without_waiting_for_gloo_completion(session, clock, provider, completion_case):
    session.info[CONTEXT] = (Settings(), CONFIG)
    person, gloo, ctx = completion_case
    gloo.fail_reply = True
    result = receive(session, clock, provider, person, ctx)
    assert result.routed_to == 'onboarding_complete'
    assert person.preferences['onboarding_stage'] == 'complete'
    assert session.get(m.Policy, queue_key('10', person.id)).value['state'] == 'pending'
    assert not provider.sent
    assert len(session.scalars(select(m.Message).where(m.Message.direction == 'in')).all()) == 1


def test_monthly_collection_and_reviewed_date_removal_queue_final_facts(session, clock, make_volunteer, provider):
    from app.agents.planning_agent import record_availability
    session.info[CONTEXT] = (Settings(), CONFIG)
    person = make_volunteer()
    ctx = SimpleNamespace(session=session, clock=clock)
    outcome = record_availability(ctx, person, ParsedMessage(intent='availability'), 'not this month', month='2026-12')
    assert len(outcome['unavailable']) == 31
    row = session.scalar(select(m.Availability))
    before = confirmations.values(row)
    approval = m.Approval(kind='confirm_record', status='approved', requested_at=clock.now(),
        payload={'record': 'Availability', 'record_id': row.id, 'before': before,
                 'after': {**before, 'unavailable_dates': []}})
    session.add(approval); session.flush()
    old_revision = session.get(m.Policy, queue_key('10', person.id)).value['revision']
    confirmations.apply_record(session, approval, clock.now())
    session.commit()
    assert row.unavailable_dates == []
    assert session.get(m.Policy, queue_key('10', person.id)).value['revision'] != old_revision
    assert not provider.sent


def test_ordinary_month_absence_queues_once_with_one_existing_ack(session, clock, gate, provider, make_volunteer):
    from app.core.serving_requests import save_serving_request
    session.info[CONTEXT] = (Settings(), CONFIG)
    person = make_volunteer()
    body = "I'm away in December"
    message = incoming(session, clock, person, body)
    gloo = ExactGloo()
    args = (session, clock, gate, gloo, person, body, ParsedMessage(intent='availability', dates=['December']), message.id)
    assert save_serving_request(*args)
    revision = session.get(m.Policy, queue_key('10', person.id)).value['revision']
    assert not save_serving_request(*args)
    assert session.get(m.Policy, queue_key('10', person.id)).value['revision'] == revision
    assert len(provider.sent) == len(gloo.calls) == 1


def test_real_coordinator_signup_completion_queues_after_exact_review(session, clock, review_app):
    app, person, incoming_message, selected, group = review_app
    app.state.session_factory.configure(info={CONTEXT: (app.state.settings, CONFIG)})
    with TestClient(app) as client:
        card = client.get('/api/signup-preferences').json()['drafts'][0]
        assert not session.get(m.Policy, queue_key('10', person.id))
        response = client.post(f'/api/signup-preferences/{person.id}/review', json=choices(card, group))
        assert response.status_code == 200, response.text
        proposal = response.json()
        approved = client.post(f"/api/proposals/{proposal['approval_id']}/approve",
            json={'content_hash': proposal['content_hash']})
        assert approved.status_code == 200, approved.text
    session.expire_all()
    assert person.preferences['onboarding_stage'] == 'complete'
    assert session.get(m.Policy, queue_key('10', person.id)).value['state'] == 'pending'


def test_worker_restarts_use_committed_reader_without_replaying_texts(lane, monkeypatch):
    from app.integrations import planning_center_blockouts as executor
    lane.queue()
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def sync(factory, client, config, volunteer_id, **kwargs):
        with factory() as session:
            calls.append(kwargs['source_reader'](session).value)
        assert kwargs['acceptance'] is None
        return {'state': 'held', 'reason': 'synthetic_missing_acceptance'}
    monkeypatch.setattr(executor, 'sync_person_blockouts', sync)
    for _ in range(2):
        result = process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, client_factory=lambda _: Client())
        assert result['processed'][0]['state'] == 'held'
    assert calls[0] == calls[1]
    with lane.factory() as session:
        assert not session.scalars(select(m.Message)).all()
        assert session.get(m.Policy, queue_key('10', lane.vid)).value['state'] == 'pending'


def test_bounded_worker_rotation_prevents_held_person_starvation(lane, monkeypatch):
    from app.integrations import planning_center_blockouts as executor
    with lane.factory() as session:
        for vid in range(1, 5):
            session.add(m.Policy(key=queue_key('10', vid), value={'organization_id': '10', 'volunteer_id': vid, 'state': 'pending'}))
        session.commit()
    calls = []
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def sync(factory, client, config, volunteer_id, **kwargs):
        calls.append(volunteer_id)
        assert kwargs['enabled'] is False and kwargs['reconcile_only'] is True
        return {'state': 'unknown', 'reason': 'synthetic_old_attempt'}
    monkeypatch.setattr(executor, 'sync_person_blockouts', sync)
    lane.settings.pco_blockout_write_enabled = False
    for _ in range(4):
        process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, limit=1, client_factory=lambda _: Client())
    assert set(calls) == {1, 2, 3, 4}


def test_disabled_unconfigured_worker_has_no_native_or_text_work(lane):
    lane.settings.pco_blockout_write_enabled = False
    lane.settings.pco_blockout_signing_key_path = ''
    def forbidden(config): raise AssertionError('Disabled unconfigured worker opened a native client')
    assert process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, client_factory=forbidden)['state'] == 'disabled'


@pytest.mark.parametrize('stop_after_unknown', [False, True])
def test_real_executor_recovers_old_unknown_then_uses_latest_committed_save(lane, tmp_path, stop_after_unknown):
    import httpx
    from app.integrations.planning_center import PCOClient
    from app.integrations.planning_center_blockouts import (
        _key, _signed, issue_blockout_policy, sign_blockout_acceptance,
    )
    from tests.test_planning_center_blockouts import Native, KEY
    key_path = tmp_path / 'real-executor-key'; key_path.write_bytes(KEY); key_path.chmod(0o600)
    acceptance = sign_blockout_acceptance(CONFIG, timezone_name='America/Denver', evidence_hash='a' * 64,
        verified_at=NOW.isoformat(), signing_key=KEY)
    proof = tmp_path / 'acceptance.json'; proof.write_text(json.dumps(acceptance)); proof.chmod(0o600)
    lane.settings.pco_blockout_signing_key_path = str(key_path)
    lane.settings.pco_blockout_acceptance_path = str(proof)
    with lane.factory() as session:
        issue_blockout_policy(session, lane.settings, CONFIG, lane.vid, user=USER, clock=lambda: NOW,
            signing_key=KEY, enabled=True, acceptance=acceptance)
        enqueue_current_availability(session, lane.settings, CONFIG, volunteer_id=lane.vid, user=USER, clock=lambda: NOW)
        session.commit()
    native = Native(lane.factory, lane.vid)
    lost_readback = [False]
    def newer_source():
        with lane.factory() as session:
            session.scalar(select(m.Availability)).unavailable_dates = ['2026-12-03']
            if stop_after_unknown: session.get(m.Volunteer, lane.vid).sms_opt_in = False
            queue_saved_availability(session, lane.vid)
            session.commit()
        lost_readback[0] = True
    def fail_readback(path):
        if lost_readback[0]:
            raise httpx.ReadTimeout('Synthetic failed readback after successful POST')
    native.after_write = newer_source
    native.before_get = fail_readback
    # Exact POST response ID is available; only its follow-up readback fails.
    native.mode = 'success'
    def client(config): return PCOClient(config, transport=httpx.MockTransport(native.handle))
    result = process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, client_factory=client)
    assert result['processed'][0]['state'] == 'unknown'
    with lane.factory() as session:
        _, journal = _signed(session, _key('j', '10', lane.vid), KEY)
        old_key = journal['unknown']
        _, attempt = _signed(session, old_key, KEY)
        original_document = deepcopy(attempt['document'])
        assert original_document['source']['unavailable_dates'] == ['2026-12-01', '2026-12-02']
        assert original_document['source']['provenance']['revision'] != session.get(m.Policy, queue_key('10', lane.vid)).value['revision']
    native.after_write = None; native.before_get = None
    if stop_after_unknown: lane.settings.pco_blockout_write_enabled = False
    result = process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, client_factory=client)
    writes = [method for method, path in native.requests if method != 'GET']
    if stop_after_unknown:
        assert writes == ['POST']  # Original unknown recovery can only GET.
    else:
        assert result['processed'][0]['state'] == 'verified'
        assert writes == ['POST', 'POST', 'DELETE']
        assert len(native.rows) == 1 and native.rows[0]['attributes']['starts_at'].startswith('2026-12-03')
    with lane.factory() as session:
        _, old_attempt = _signed(session, old_key, KEY)
        assert old_attempt['document'] == original_document and old_attempt['state'] == 'verified'
        assert not session.scalars(select(m.Message)).all()


def test_uncorrelated_lost_post_never_adopts_foreign_match_after_source_advances(lane, tmp_path):
    import httpx
    from app.integrations.planning_center import PCOClient
    from app.integrations.planning_center_blockouts import _key, _signed, issue_blockout_policy, sign_blockout_acceptance
    from tests.test_planning_center_blockouts import Native, KEY, block
    key = tmp_path / 'uncorrelated-key'; key.write_bytes(KEY); key.chmod(0o600)
    acceptance = sign_blockout_acceptance(CONFIG, timezone_name='America/Denver', evidence_hash='a' * 64,
        verified_at=NOW.isoformat(), signing_key=KEY)
    proof = tmp_path / 'uncorrelated-proof.json'; proof.write_text(json.dumps(acceptance)); proof.chmod(0o600)
    lane.settings.pco_blockout_signing_key_path = str(key)
    lane.settings.pco_blockout_acceptance_path = str(proof)
    with lane.factory() as session:
        issue_blockout_policy(session, lane.settings, CONFIG, lane.vid, user=USER, clock=lambda: NOW,
            signing_key=KEY, enabled=True, acceptance=acceptance)
        enqueue_current_availability(session, lane.settings, CONFIG, volunteer_id=lane.vid, user=USER, clock=lambda: NOW)
        session.commit()
    native = Native(lane.factory, lane.vid); native.mode = 'timeout_before'
    def client(config): return PCOClient(config, transport=httpx.MockTransport(native.handle))
    first = process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, client_factory=client)
    assert first['processed'][0]['state'] == 'unknown' and not native.rows
    with lane.factory() as session:
        _, journal = _signed(session, _key('j', '10', lane.vid), KEY)
        old_key = journal['unknown']
        _, attempt = _signed(session, old_key, KEY)
        original = deepcopy(attempt['document'])
        # An independent coordinator creates identical content after our POST
        # timed out without changing anything. Content/newness proves no owner.
        native.rows.append(block('900', original['operation']['body']['data']['attributes']))
        session.scalar(select(m.Availability)).unavailable_dates = ['2026-12-03']
        queue_saved_availability(session, lane.vid); session.commit()
    native.mode = 'success'
    second = process_pco_blockouts(lane.factory, lane.settings, CONFIG, lane.clock, client_factory=client)
    assert second['processed'][0]['state'] == 'unknown'
    assert [method for method, path in native.requests if method != 'GET'] == ['POST']
    assert [row['id'] for row in native.rows] == ['900']
    with lane.factory() as session:
        _, journal = _signed(session, _key('j', '10', lane.vid), KEY)
        _, attempt = _signed(session, old_key, KEY)
        assert journal['unknown'] == old_key and journal['owned'] == []
        assert attempt['state'] == 'unknown' and attempt['document'] == original
        assert session.get(m.Policy, queue_key('10', lane.vid)).value['state'] == 'pending'
        assert not session.scalars(select(m.Message)).all()


def test_actual_policy_endpoint_validates_existing_source_and_enqueues_atomically(lane, tmp_path):
    from fastapi import FastAPI
    from app.integrations.planning_center_blockouts import sign_blockout_acceptance
    from app.web.planning_center_blockouts import router
    from app.web.texty import admin
    key = open(lane.settings.pco_blockout_signing_key_path, 'rb').read()
    acceptance = sign_blockout_acceptance(CONFIG, timezone_name='America/Denver', evidence_hash='a' * 64,
        verified_at=NOW.isoformat(), signing_key=key)
    proof = tmp_path / 'endpoint-proof.json'; proof.write_text(json.dumps(acceptance)); proof.chmod(0o600)
    lane.settings.pco_blockout_acceptance_path = str(proof)
    app = FastAPI(); app.state.settings = lane.settings; app.state.pco_config = CONFIG
    app.state.clock = lane.clock; app.state.session_factory = lane.factory
    app.include_router(router); app.dependency_overrides[admin] = lambda: USER
    with TestClient(app) as client:
        response = client.put(f'/api/planning-center/blockouts/{lane.vid}/policy', json={'enabled': True})
        assert response.status_code == 200, response.text
    with lane.guarded() as session:
        receipt = session.get(m.Policy, receipt_key('10', lane.vid))
        assert receipt.value['kind'] == 'coordinator_policy_enable'
        assert lane.read(session).value['unavailable_dates'] == ['2026-12-01', '2026-12-02']


@pytest.mark.parametrize('write_enabled', [False, True])
def test_combined_app_keeps_save_context_and_schedules_only_independent_worker(tmp_path, monkeypatch, write_enabled):
    from app.main import create_app
    schedulers, calls = [], []
    class Scheduler:
        def __init__(self): self.jobs = []; self.stopped = False; schedulers.append(self)
        def add_job(self, fn, trigger, **kwargs): self.jobs.append((fn, trigger, kwargs))
        def start(self): pass  # No real scheduler or transport threads.
        def shutdown(self, wait): self.stopped = True
    def worker(factory, settings, config, clock):
        with factory() as session:
            queue = session.scalar(select(m.Policy).where(m.Policy.key.startswith('pco_bq:')))
            assert queue.value['state'] == 'pending'
        calls.append(config.organization_id)
    monkeypatch.setenv('PCO_ORGANIZATION_ID', '10')
    monkeypatch.setenv('PCO_SERVICE_TYPE_IDS', '20')
    monkeypatch.setenv('PCO_APP_ID', 'synthetic')
    monkeypatch.setenv('PCO_SECRET', 'synthetic')
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler', Scheduler)
    monkeypatch.setattr('app.integrations.acceptance_workflow.start_service', lambda state: None)
    monkeypatch.setattr('app.integrations.acceptance_workflow.stop_service', lambda state: None)
    monkeypatch.setattr('app.jobs.process_pco_blockouts', worker)
    settings = Settings(database_url='sqlite:///' + str(tmp_path / 'startup.sqlite'),
        demo_mode=False, automation_enabled=False, pco_blockout_write_enabled=write_enabled)
    app = create_app(settings)
    with TestClient(app):
        with app.state.session_factory() as session:
            assert session.info[CONTEXT][0] is settings
            assert session.info[CONTEXT][1].organization_id == '10'
            person = m.Volunteer(name='Synthetic Casey', phone='+15550100003', status='active', sms_opt_in=True,
                is_coordinator=False, is_pastor=False, preferences={}, created_at=NOW)
            session.add(person); session.flush()
            session.add(m.Availability(volunteer_id=person.id, month='2026-12', available_dates=[],
                unavailable_dates=['2026-12-03'], parsed_at=NOW))
            assert queue_saved_availability(session, person.id)
            session.commit()
        if write_enabled:
            assert len(schedulers) == 1
            fn, trigger, kwargs = schedulers[0].jobs[0]
            assert len(schedulers[0].jobs) == 1 and trigger == 'interval'
            assert kwargs == {'seconds': 60, 'id': 'pco_blockout_tick', 'max_instances': 1, 'coalesce': True}
            fn()
            assert calls == ['10']
        else:
            assert not schedulers and not calls
        assert not app.state.provider.sent
    assert all(scheduler.stopped for scheduler in schedulers)
    app.state.engine.dispose()
