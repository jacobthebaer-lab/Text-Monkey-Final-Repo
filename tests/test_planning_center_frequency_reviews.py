"""Committed synthetic Mac/Gloo evidence, local authentication fakes only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.core import planning_center_frequency_reviews as reviews, profile_sync
from app.core.planning_center_committed_source import CommittedAvailabilityReader, committed_source_factory
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.profile_models import ProfileBase, ProfileOutbox
from app.integrations.planning_center import PCOBase, PCOConfig, PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_availability import (
    FrozenSnapshot, OwnedResource, PCOAvailabilityIntent, PreviewPolicy, build_preview, enqueue_preview, resource_hash,
)
from app.integrations.planning_center_frequency_executor import FrequencyReview
from app.integrations.planning_center_review_models import ReviewBase, PCOFrequencyReviewReceipt
from app.web.planning_center_reviews import router

PHONE = '+12025550147'
USER = {'id': '00000000-0000-4000-8000-000000000001', 'email': 'coordinator@example.test',
        'email_confirmed_at': '2026-01-01T00:00:00Z'}
CONFIG = PCOConfig('synthetic', 'synthetic', '10', ('20',))
KEY = b'synthetic-private-signing-key-00000001'


def rel(kind, identifier): return {'data': {'type': kind, 'id': identifier}}


@pytest.fixture
def lane(tmp_path):
    now = datetime.now(timezone.utc)
    engine = create_engine('sqlite:///' + str(tmp_path / 'synthetic.db'))
    for metadata in (m.Base.metadata, PCOBase.metadata, ProfileBase.metadata, ReviewBase.metadata):
        metadata.create_all(engine)
    factory = committed_source_factory(engine)
    settings = Settings(sms_provider='mac_messages', profile_sync_enabled=True, profile_sync_phones=PHONE,
        mac_demo_phones=PHONE, admin_email_allowlist=USER['email'],
        supabase_url='https://synthetic.supabase.test', supabase_publishable_key='synthetic-public-key')
    with factory() as s:
        role = m.Role(name='Greeter', ministry='Synthetic', required_qualifications=[], criticality='standard', fill_policy='auto')
        s.add(role); s.flush()
        volunteer = m.Volunteer(name='Synthetic Person', phone=PHONE, sms_opt_in=True, status='active',
            preferences={'signup_source': 'sms', 'consent_at': now.isoformat(), 'consent_source': 'sms_reply',
                'max_per_month': None, 'role_frequency_caps': [
                    {'role_id': role.id, 'role_name': role.name, 'max_per_month': 2}]}, created_at=now)
        s.add(volunteer); s.flush()
        body, guid, sid = 'Synthetic availability answer', 'synthetic-guid', 'synthetic-session'
        message = m.Message(phone=PHONE, volunteer_id=volunteer.id, body=body, direction='in', kind='mac_test_in',
            purpose='test:' + sid, status='received', created_at=now)
        s.add(message)
        s.add(MacInboundReceipt(guid=guid, fingerprint=hashlib.sha256((PHONE+'\0iMessage\0'+sid+'\0'+body).encode()).hexdigest(),
            result={'intent': 'onboarding_availability', 'session_id': sid}))
        s.add(PCOVolunteerPerson(organization_id='10', volunteer_id=volunteer.id, person_id='70', created_at=now))
        s.flush()
        row = profile_sync.capture(s, settings, phone=PHONE, guid=guid, route='onboarding_availability',
                                   before=None, effective_at=now)
        vid, profile_key, source_id = volunteer.id, row.key, row.source_id
        s.commit()
    clock = [now]
    reader = CommittedAvailabilityReader(settings, CONFIG, volunteer_id=vid, profile_key=profile_key,
                                        source_id=source_id, clock=lambda: clock[0])
    binding = {'role_id': role.id, 'role_name': 'Greeter', 'service_type_id': '20', 'team_id': '30',
               'position_id': '40', 'position_name': 'Greeter', 'membership_id': '80'}
    member = {'type': 'PersonTeamPositionAssignment', 'id': '80', 'attributes': {
        'schedule_preference': 'Every week', 'preferred_weeks': [], 'created_at': now.isoformat(), 'updated_at': now.isoformat()},
        'relationships': {'person': rel('Person', '70'), 'team_position': rel('TeamPosition', '40'),
                          'time_preference_options': {'data': []}}}
    remote = FrozenSnapshot.capture({'organization_id': '10', 'person_id': '70', 'blockouts': [],
        'blockout_dates': {}, 'memberships': [{'binding': binding, 'resource': member}]})
    with factory() as s:
        source = reader(s)
        preview = build_preview(source, remote, owned=[OwnedResource('10', '70', 'membership_frequency',
            'role:' + str(role.id), '80', resource_hash(member))], policy=PreviewPolicy())
        record = enqueue_preview(s, preview, source=source, remote=remote, now=now)
        s.commit()
        intent_key = s.scalar(select(PCOAvailabilityIntent).where(PCOAvailabilityIntent.preview_key == record.key)).key
    def proposal(s):
        return reviews.review_proposal(s, settings, CONFIG, intent_key=intent_key, user=USER, clock=lambda: clock[0])
    def issue(s, **changes):
        p = proposal(s)
        return reviews.issue_review_receipt(s, settings, CONFIG, intent_key=intent_key,
            user=changes.get('user', USER), expected=changes.get('expected', {k:p[k] for k in
                ('preview_hash', 'source_hash', 'remote_hash', 'operation_hash')}),
            clock=lambda: clock[0], signing_key=changes.get('signing_key', KEY))
    def verify(s, receipt_id, **changes):
        return reviews.verify_review_receipt(s, settings, changes.get('config', CONFIG), receipt_id=receipt_id,
            user=changes.get('user', USER), clock=lambda: clock[0], signing_key=changes.get('signing_key', KEY))
    yield dict(engine=engine, factory=factory, settings=settings, reader=reader, volunteer_id=vid,
        profile_key=profile_key, source_id=source_id, proposal=proposal, issue=issue, verify=verify,
        clock=clock, intent_key=intent_key)
    engine.dispose()


def test_source_is_committed_locked_read_only_and_reuses_exact_receipt(lane):
    with lane['factory']() as s:
        volunteer = s.get(m.Volunteer, lane['volunteer_id'])
        saved = deepcopy(volunteer.preferences)
        source = lane['reader'](s)
        assert source.value['provenance'] == {'source_id': lane['source_id'],
            'receipt_id': 'synthetic-guid', 'revision': lane['profile_key']}
        assert source.value['role_frequency_caps'][0]['max_per_month'] == 2
        assert 'phone' not in source.value and 'Synthetic availability answer' not in source.document
        assert volunteer.preferences == saved and not s.new and not s.dirty


@pytest.mark.parametrize('change', ['fingerprint', 'message_sender', 'source_id', 'snapshot', 'profile_changed',
    'missing_receipt', 'held_profile', 'wrong_route', 'mapping', 'newer_revision', 'opt_out'])
def test_foreign_stale_changed_or_forged_source_evidence_rejected(lane, change):
    with lane['factory']() as s:
        row = s.get(ProfileOutbox, lane['profile_key'])
        if change == 'fingerprint': s.get(MacInboundReceipt, 'synthetic-guid').fingerprint = 'a'*64
        elif change == 'message_sender': s.scalars(select(m.Message)).one().volunteer_id = None
        elif change == 'source_id': row.source_id = '00000000-0000-4000-8000-000000000002'
        elif change == 'snapshot': row.payload = {**row.payload, 'profile': {'fabricated': True}}
        elif change == 'profile_changed': s.get(m.Volunteer, lane['volunteer_id']).preferences = {'max_per_month': 8}
        elif change == 'missing_receipt': s.delete(s.get(MacInboundReceipt, 'synthetic-guid'))
        elif change == 'held_profile': row.state = 'held'
        elif change == 'wrong_route': row.payload = {**row.payload, 'route': 'signup_complete'}
        elif change == 'mapping': s.scalars(select(PCOVolunteerPerson)).one().person_id = '71'
        elif change == 'opt_out': s.get(m.Volunteer, lane['volunteer_id']).sms_opt_in = False
        else:
            s.add(ProfileOutbox(key='c'*64, source_id=row.source_id, source_guid='newer', phone=PHONE,
                payload={}, state='held', detail='', attempts=0, created_at=row.created_at+timedelta(seconds=1)))
        s.commit()
    with lane['factory']() as s:
        # Mapping changes alter immutable preview source even when source is legitimate.
        with pytest.raises(PlanningCenterError): lane['proposal'](s)


@pytest.mark.parametrize('flushed', [False, True])
def test_uncommitted_source_mutation_never_becomes_evidence(lane, flushed):
    with lane['factory']() as s:
        s.get(m.Volunteer, lane['volunteer_id']).name = 'Uncommitted change'
        if flushed: s.flush()
        with pytest.raises(PlanningCenterError, match='clean_session'): lane['reader'](s)
        s.rollback()
        assert lane['reader'](s)


@pytest.mark.parametrize('path', ['orm_execute', 'core_execute', 'text_execute', 'query_update',
    'scalar_returning', 'scalars_returning', 'bulk_update_mappings', 'bulk_save_objects'])
def test_uncommitted_bulk_matching_message_and_receipt_cannot_forge_source(lane, path):
    body = 'Different uncommitted source evidence'
    fingerprint = hashlib.sha256((PHONE+'\0iMessage\0synthetic-session\0'+body).encode()).hexdigest()
    with lane['factory']() as s:
        if path == 'orm_execute':
            s.execute(update(m.Message).where(m.Message.phone == PHONE).values(body=body))
            s.execute(update(MacInboundReceipt).where(MacInboundReceipt.guid == 'synthetic-guid').values(fingerprint=fingerprint))
        elif path == 'core_execute':
            s.execute(m.Message.__table__.update().where(m.Message.phone == PHONE).values(body=body))
            s.execute(MacInboundReceipt.__table__.update().values(fingerprint=fingerprint))
        elif path == 'text_execute':
            s.execute(text('UPDATE messages SET body=:body WHERE phone=:phone'), {'body': body, 'phone': PHONE})
            s.execute(text('UPDATE mac_inbound_receipts SET fingerprint=:fingerprint WHERE guid=:guid'),
                      {'fingerprint': fingerprint, 'guid': 'synthetic-guid'})
        elif path == 'query_update':
            s.query(m.Message).filter(m.Message.phone == PHONE).update({'body': body}, synchronize_session=False)
            s.query(MacInboundReceipt).update({'fingerprint': fingerprint}, synchronize_session=False)
        elif path in {'scalar_returning', 'scalars_returning'}:
            method = s.scalar if path == 'scalar_returning' else s.scalars
            method(update(m.Message).values(body=body).returning(m.Message.id))
            method(update(MacInboundReceipt).values(fingerprint=fingerprint).returning(MacInboundReceipt.guid))
        elif path == 'bulk_update_mappings':
            message = s.scalars(select(m.Message)).one()
            s.bulk_update_mappings(m.Message, [{'id': message.id, 'body': body}])
            s.bulk_update_mappings(MacInboundReceipt, [{'guid': 'synthetic-guid', 'fingerprint': fingerprint}])
        else:
            message = s.scalars(select(m.Message)).one()
            receipt = s.get(MacInboundReceipt, 'synthetic-guid')
            s.expunge(message); s.expunge(receipt)
            message.body, receipt.fingerprint = body, fingerprint
            s.bulk_save_objects([message, receipt])
        s.flush()
        assert not s.new and not s.dirty and not s.deleted
        with pytest.raises(PlanningCenterError, match='clean_session'): lane['reader'](s)
        s.rollback()
        assert lane['reader'](s)  # Rollback restores the legitimate committed evidence.


def test_uncommitted_legacy_bulk_insert_is_not_committed_evidence(lane):
    with lane['factory']() as s:
        s.bulk_insert_mappings(m.Message, [{'phone': PHONE, 'volunteer_id': lane['volunteer_id'],
            'body': 'Uncommitted unrelated source row', 'direction': 'in', 'kind': 'mac_test_in',
            'purpose': 'test:uncommitted', 'status': 'received', 'created_at': lane['clock'][0]}])
        assert not s.new and not s.dirty
        with pytest.raises(PlanningCenterError, match='clean_session'): lane['reader'](s)


def test_pure_core_reads_work_but_even_textual_reads_taint_the_contract(lane):
    with lane['factory']() as s:
        assert s.scalar(select(m.Message.body)) == 'Synthetic availability answer'
        assert lane['reader'](s)
        s.execute(text('SELECT 1'))
        with pytest.raises(PlanningCenterError, match='clean_session'): lane['reader'](s)
        s.rollback()
        assert lane['reader'](s)


def test_committing_a_direct_noop_write_resets_the_uncommitted_marker(lane):
    with lane['factory']() as s:
        s.execute(update(m.Message).values(body='Synthetic availability answer'))
        with pytest.raises(PlanningCenterError, match='clean_session'): lane['reader'](s)
        s.commit()
        assert lane['reader'](s)


def test_dedicated_guarded_session_and_current_recipient_scope_required(lane):
    from sqlalchemy.orm import Session
    with Session(lane['engine']) as s:
        with pytest.raises(PlanningCenterError, match='guarded_session'): lane['reader'](s)
    for settings in (replace(lane['settings'], mac_demo_phones=''), replace(lane['settings'], profile_sync_enabled=False)):
        reader = CommittedAvailabilityReader(settings, CONFIG, volunteer_id=lane['volunteer_id'],
            profile_key=lane['profile_key'], source_id=lane['source_id'], clock=lambda: lane['clock'][0])
        with lane['factory']() as s:
            with pytest.raises(PlanningCenterError): reader(s)


def test_source_reservation_blocks_a_separate_sqlite_writer(lane):
    with lane['factory']() as reader_session:
        assert lane['reader'](reader_session)
        with lane['engine'].connect() as other:
            other.exec_driver_sql('PRAGMA busy_timeout=20')
            with pytest.raises(OperationalError, match='locked'):
                other.exec_driver_sql("UPDATE volunteers SET name='Concurrent synthetic edit' WHERE id=?",
                                      (lane['volunteer_id'],))
        reader_session.rollback()


def test_non_utc_aware_clock_is_normalized_for_persisted_receipt(lane):
    from zoneinfo import ZoneInfo
    lane['clock'][0] = lane['clock'][0].astimezone(ZoneInfo('America/Denver'))
    with lane['factory']() as s:
        receipt = lane['issue'](s); s.commit()
    with lane['factory']() as s:
        assert lane['verify'](s, receipt['receipt_id'])['state'] == 'reviewed_held'


def test_authenticated_signed_durable_review_is_held_and_transaction_owned(lane):
    with lane['factory']() as s:
        receipt = lane['issue'](s)
        assert receipt['state'] == 'reviewed_held' and receipt['execution_enabled'] is False
        assert set(reviews.RELEASE_HOLDS) <= set(receipt['release_holds'])
        s.rollback()
    with lane['factory']() as s:
        assert s.get(PCOFrequencyReviewReceipt, receipt['receipt_id']) is None
        receipt = lane['issue'](s); s.commit()
    with lane['factory']() as s:
        assert lane['verify'](s, receipt['receipt_id'])['receipt_hash'] == receipt['receipt_hash']
        with pytest.raises(PlanningCenterError, match='live_release_evidence_unavailable'):
            reviews.authorize_frequency_execution(s, lane['settings'], CONFIG, receipt_id=receipt['receipt_id'],
                user=USER, clock=lambda: lane['clock'][0], signing_key=KEY)


@pytest.mark.parametrize('change', ['signature', 'document', 'actor', 'foreign_org', 'expired', 'revoked', 'key_rotation', 'source'])
def test_receipts_reject_forgery_staleness_revocation_and_foreign_bindings(lane, change):
    with lane['factory']() as s:
        receipt = lane['issue'](s); s.commit()
    kwargs = {}
    if change == 'actor': kwargs['user'] = {**USER, 'id': '00000000-0000-4000-8000-000000000003'}
    elif change == 'foreign_org': kwargs['config'] = replace(CONFIG, organization_id='11')
    elif change == 'expired': lane['clock'][0] += timedelta(minutes=10)
    elif change == 'key_rotation': kwargs['signing_key'] = b'another-synthetic-key-that-is-long-enough'
    else:
        with lane['factory']() as s:
            row = s.get(PCOFrequencyReviewReceipt, receipt['receipt_id'])
            if change == 'signature': row.signature = 'a'*64
            elif change == 'document': row.document = row.document.replace('reviewed_held', 'approved')
            elif change == 'revoked': row.state = 'revoked'
            else: s.get(m.Volunteer, lane['volunteer_id']).sms_opt_in = False
            s.commit()
    with lane['factory']() as s:
        with pytest.raises(PlanningCenterError): lane['verify'](s, receipt['receipt_id'], **kwargs)


def test_dataclass_hash_or_unverified_actor_cannot_authorize_receipt(lane):
    with lane['factory']() as s:
        for user in ({**USER, 'email_confirmed_at': None}, {**USER, 'email': 'foreign@example.test'}):
            with pytest.raises(PlanningCenterError, match='verified_coordinator'): lane['issue'](s, user=user)
        with pytest.raises(PlanningCenterError, match='signing_key'): lane['issue'](s, signing_key=None)
        with pytest.raises(PlanningCenterError, match='stale_or_changed'): lane['issue'](s, expected={'preview_hash':'a'*64})
        with pytest.raises(PlanningCenterError): lane['verify'](s, 'a'*64)
        forged = FrequencyReview('a'*64, 'a'*64, 'a'*64, 'a'*64, 'a'*64, '10', '70',
                                'a'*64, 'a'*64, lane['clock'][0], lane['clock'][0]+timedelta(minutes=10))
        # The authority consumes a durable receipt ID, never this dataclass.
        with pytest.raises(PlanningCenterError, match='durable_receipt_id_required'):
            lane['verify'](s, forged)
        assert not s.scalars(select(PCOFrequencyReviewReceipt)).all()


def test_protected_router_uses_existing_supabase_auth_and_never_executes(lane, monkeypatch):
    app = FastAPI(); app.include_router(router)
    app.state.engine, app.state.settings, app.state.pco_config = lane['engine'], lane['settings'], CONFIG
    app.state.pco_review_signing_key = KEY
    requests = []
    class AuthClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, headers):
            requests.append(url)
            assert url == lane['settings'].supabase_url + '/auth/v1/user'
            return httpx.Response(200, json=USER)
    monkeypatch.setattr('app.web.texty.httpx.AsyncClient', lambda **kwargs: AuthClient())
    path = '/api/planning-center/frequency-reviews/' + lane['intent_key']
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        headers = {'Authorization': 'Bearer synthetic-session'}
        p = client.get(path, headers=headers)
        assert p.status_code == 200 and p.json()['execution_enabled'] is False
        exact = {k:p.json()[k] for k in ('preview_hash','source_hash','remote_hash','operation_hash')}
        assert client.post(path, headers=headers, json={**exact, 'actor': USER}).status_code == 422
        response = client.post(path, headers=headers, json=exact)
        assert response.status_code == 200 and response.json()['state'] == 'reviewed_held'
        assert client.post(path+'/execute', headers=headers, json={}).status_code == 404
    assert requests and all('/auth/v1/user' in url for url in requests)
    with lane['factory']() as s:
        assert len(s.scalars(select(PCOFrequencyReviewReceipt)).all()) == 1
        assert len(s.scalars(select(m.Message)).all()) == 1 and not s.scalars(select(m.Assignment)).all()


def test_unconfigured_signing_key_holds_optional_route(lane, monkeypatch):
    app = FastAPI(); app.include_router(router)
    app.state.engine, app.state.settings, app.state.pco_config = lane['engine'], lane['settings'], CONFIG
    from app.web.texty import admin
    app.dependency_overrides[admin] = lambda: USER  # Auth dependency tested independently above.
    with TestClient(app) as client:
        path = '/api/planning-center/frequency-reviews/' + lane['intent_key']
        proposal = client.get(path).json()
        exact = {k:proposal[k] for k in ('preview_hash','source_hash','remote_hash','operation_hash')}
        assert client.post(path, json=exact).status_code == 409
    with lane['factory']() as s:
        assert not s.scalars(select(PCOFrequencyReviewReceipt)).all()
