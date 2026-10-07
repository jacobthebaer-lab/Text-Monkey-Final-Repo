"""Independent mock stores prove sender scope, durable retries and privilege safety."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.core import profile_sync as sync
from app.db import models as m
from app.db.session import make_engine
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.profile_models import ProfileBase, ProfileOutbox
from app.main import create_app
from app.web.texty import admin
from tests.session_fixtures import session_id, session_json

PHONE = '+12025550147'
OTHER = '+12025550148'


@pytest.fixture
def settings():
    return Settings(profile_sync_enabled=True, profile_sync_phones=PHONE)


@pytest.fixture
def stores(session, clock):
    ProfileBase.metadata.create_all(session.get_bind())
    engine = make_engine('sqlite://')
    m.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    local = m.Volunteer(name='Jordan Demo', phone=PHONE, sms_opt_in=True, status='active',
                        preferences={'signup_source': 'sms', 'consent_pending': False,
                                     'consent_at': clock.now().isoformat(), 'consent_source': 'sms_reply',
                                     'onboarding_stage': 'complete'}, created_at=clock.now())
    session.add(local)
    session.commit()
    yield session, local, factory
    engine.dispose()


def queue(stores, settings, clock, *, guid='actual-receipt', before=None):
    local, volunteer, cloud = stores
    row = sync.capture(local, settings, phone=PHONE, guid=guid, route='onboarding_complete',
                       before=before, effective_at=clock.now())
    local.commit()
    return row


def test_capture_is_atomic_duplicate_safe_and_disabled_by_default(stores, settings, clock):
    local, volunteer, cloud = stores
    before = sync.snapshot(local, PHONE)
    volunteer.name = 'Jordan Changed'
    row = sync.capture(local, settings, phone=PHONE, guid='real', route='signup_complete',
                       before=before, effective_at=clock.now())
    local.rollback()
    assert local.get(ProfileOutbox, row.key) is None
    assert local.get(m.Volunteer, volunteer.id).name == 'Jordan Demo'
    first = queue(stores, settings, clock)
    assert queue(stores, settings, clock).key == first.key
    assert len(local.scalars(select(ProfileOutbox)).all()) == 1
    assert sync.capture(local, Settings(), phone=PHONE, guid='disabled', route='stop',
                        before=None, effective_at=clock.now()) is None
    assert sync.capture(local, replace(settings, profile_sync_phones=OTHER), phone=PHONE,
                        guid='outside', route='stop', before=None, effective_at=clock.now()) is None
    assert sync.capture(local, settings, phone=PHONE, guid='care', route='escalated_sensitive',
                        before=None, effective_at=clock.now()) is None


def test_cloud_outage_keeps_payload_and_retry_allocates_cloud_id(stores, settings, clock):
    local, volunteer, factory = stores
    row = queue(stores, settings, clock)
    saved = dict(row.payload)
    def unavailable():
        raise RuntimeError('PRIVATE_DSN_SENTINEL')
    sync.publish_pending(local, unavailable, settings)
    assert row.state == 'failed' and row.payload == saved and 'PRIVATE' not in row.detail
    with factory() as cloud:
        cloud.add(m.Volunteer(id=500, name='Other Demo', phone=OTHER, sms_opt_in=False,
                              status='inactive', preferences={}, created_at=clock.now()))
        cloud.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced' and row.cloud_id != volunteer.id and row.attempts == 2
    # Simulate lost local acknowledgment after the successful cloud transaction.
    row.state = 'pending'
    local.commit()
    sync.publish_pending(local, factory, settings)
    assert row.detail == 'already_applied'
    with factory() as cloud:
        assert len(cloud.scalars(select(m.Volunteer).where(m.Volunteer.phone == PHONE)).all()) == 1


def test_existing_cloud_privileges_qualifications_and_unrelated_preferences_survive(stores, settings, clock):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'is_admin': True, 'admin_text_owner': 'local-admin'}
    local.commit()
    with factory() as cloud:
        cloud.add(m.Volunteer(id=900, name='Previous Name', phone=PHONE, sms_opt_in=True,
                              status='active', is_coordinator=True, is_pastor=True,
                              preferences={'paused_roles': ['Greeter'], 'admin_text_owner': 'cloud-owner'},
                              created_at=clock.now()))
        cloud.add(m.Qualification(volunteer_id=900, type='background_check', status='verified'))
        cloud.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    with factory() as cloud:
        mirrored = cloud.get(m.Volunteer, 900)
        assert mirrored.name == 'Jordan Demo' and mirrored.is_coordinator and mirrored.is_pastor
        assert mirrored.preferences['admin_text_owner'] == 'cloud-owner'
        assert mirrored.preferences['paused_roles'] == ['Greeter'] and 'is_admin' not in mirrored.preferences
        assert cloud.scalar(select(m.Qualification)).status == 'verified'
        assert row.cloud_id == 900


def test_only_changed_preferences_and_availability_months_are_merged(stores, settings, clock):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'max_per_month': 2}
    local.commit()
    before = sync.snapshot(local, PHONE)
    volunteer.preferences = {**volunteer.preferences, 'availability_weekdays': [2, 6]}
    local.commit()
    with factory() as cloud:
        cloud.add(m.Volunteer(name='Cloud Name', phone=PHONE, sms_opt_in=True, status='active',
                              preferences={'max_per_month': 4}, created_at=clock.now()))
        cloud.commit()
    queue(stores, settings, clock, before=before)
    sync.publish_pending(local, factory, settings)
    with factory() as cloud:
        mirrored = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert mirrored.name == 'Cloud Name' and mirrored.preferences['max_per_month'] == 4
        assert mirrored.preferences['availability_weekdays'] == [2, 6]


@pytest.mark.parametrize('mapped', [False, True])
def test_role_names_are_verified_or_held_never_copied_as_numeric_ids(stores, settings, clock, mapped):
    local, volunteer, factory = stores
    local.add(m.Role(id=5, name='Local Greeter', ministry='Local', required_qualifications=[], criticality='standard', fill_policy='auto'))
    volunteer.preferences = {**volunteer.preferences, 'interested_roles': ['Local Greeter']}
    local.commit()
    with factory() as cloud:
        cloud.add(m.Role(id=70, name='Cloud Greeter', ministry='Cloud', required_qualifications=[], criticality='standard', fill_policy='auto'))
        cloud.commit()
    if mapped:
        settings = replace(settings, profile_sync_role_map=json.dumps({'Local Greeter': 'Cloud Greeter'}))
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    assert row.state == ('synced' if mapped else 'held')
    with factory() as cloud:
        mirrored = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        if mapped:
            assert mirrored.preferences['interested_roles'] == ['Cloud Greeter']
        else:
            assert mirrored is None


@pytest.mark.parametrize('suppressed', [False, True])
def test_cloud_opt_out_is_never_reenabled(stores, settings, clock, suppressed):
    local, volunteer, factory = stores
    with factory() as cloud:
        cloud.add(m.Volunteer(name='Cloud User', phone=PHONE, sms_opt_in=bool(suppressed), status='active',
                              preferences={}, created_at=clock.now()))
        if suppressed:
            cloud.add(m.Policy(key='sms_opt_out:' + PHONE, value={'at': clock.now().isoformat()}))
        cloud.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'cloud_opt_out_requires_review'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)).name == 'Cloud User'


def test_publisher_owned_pending_signup_can_advance_but_later_cloud_stop_holds(stores, settings, clock):
    local, volunteer, factory = stores
    volunteer.sms_opt_in = False
    volunteer.status = 'inactive'
    volunteer.preferences = {'signup_source': 'sms', 'consent_pending': True}
    local.commit()
    queue(stores, settings, clock, guid='name')
    sync.publish_pending(local, factory, settings)
    before = sync.snapshot(local, PHONE)
    volunteer.sms_opt_in = True
    volunteer.status = 'active'
    volunteer.preferences = {**volunteer.preferences, 'consent_pending': False, 'consent_at': clock.now().isoformat()}
    local.commit()
    row = queue(stores, settings, clock, guid='yes', before=before)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced'
    with factory() as cloud:
        current = cloud.scalar(select(m.Volunteer))
        current.sms_opt_in = False
        cloud.commit()
    # A later availability answer cannot undo the cloud opt-out.
    row = queue(stores, settings, clock, guid='later')
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'cloud_opt_out_requires_review'


def test_availability_uses_cloud_identity_and_retries_do_not_duplicate(stores, settings, clock):
    local, volunteer, factory = stores
    local.add(m.Availability(volunteer_id=volunteer.id, month='2026-10', available_dates=['2026-10-04'],
                             unavailable_dates=['2026-10-11'], raw_reply='private sender text', parsed_at=clock.now()))
    local.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    row.state = 'pending'
    local.commit()
    sync.publish_pending(local, factory, settings)
    with factory() as cloud:
        availability = cloud.scalars(select(m.Availability)).all()
        assert len(availability) == 1 and availability[0].volunteer_id == row.cloud_id
        assert availability[0].unavailable_dates == ['2026-10-11'] and availability[0].raw_reply is None


def test_catch_up_requires_actual_matching_receipt_and_approved_sender_history(stores, settings, clock):
    local, volunteer, factory = stores
    body = 'Jordan Demo'
    sid = 'fictional-session'
    fingerprint = hashlib.sha256((PHONE + '\0iMessage\0' + sid + '\0' + body).encode()).hexdigest()
    local.add(m.Message(phone=PHONE, body=body, direction='in', kind='mac_test_in', purpose='test:' + sid,
                        status='received', created_at=clock.now()))
    local.add(MacInboundReceipt(guid='recorded', fingerprint=fingerprint,
                               result={'intent': 'signup_consent_pending', 'session_id': sid}))
    local.commit()
    with pytest.raises(sync.ProfileHeld):
        sync.catch_up(local, settings, phone=PHONE, guid='invented')
    with pytest.raises(sync.ProfileHeld):
        sync.catch_up(local, settings, phone=OTHER, guid='recorded')
    row = sync.catch_up(local, settings, phone=PHONE, guid='recorded')
    local.commit()
    assert row.payload['catch_up'] is True and len(local.scalars(select(m.Message)).all()) == 1
    assert sync.catch_up(local, settings, phone=PHONE, guid='recorded').key == row.key


def test_scope_removed_after_capture_blocks_publication(stores, settings, clock):
    local, volunteer, factory = stores
    row = queue(stores, settings, clock)
    assert sync.publish_pending(local, factory, replace(settings, profile_sync_phones=OTHER)) == []
    assert row.state == 'pending'


def test_wrong_cloud_target_is_refused_before_connection(settings, monkeypatch):
    monkeypatch.setattr(sync, 'create_engine', lambda *args, **kwargs: pytest.fail('Must not connect'))
    with pytest.raises(sync.ProfileHeld, match='cloud_target_mismatch'):
        sync.cloud_connection(replace(settings, profile_sync_project_ref='abcdefghijklmnopqrst',
                              profile_sync_database_url='postgresql://backend.wrong:secret@aws-0-ca-central-1.pooler.supabase.com/postgres'))


def test_newer_cloud_consent_and_profile_edits_are_held(stores, settings, clock):
    local, volunteer, factory = stores
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    with factory() as cloud:
        current = cloud.scalar(select(m.Volunteer))
        current.name = 'Newer Cloud Name'
        cloud.commit()
    before = sync.snapshot(local, PHONE)
    volunteer.name = 'Sender Changed Name'
    local.commit()
    clock.advance(timedelta(minutes=1))
    row = queue(stores, settings, clock, guid='rename', before=before)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'cloud_profile_changed'
    with factory() as cloud:
        current = cloud.scalar(select(m.Volunteer))
        current.preferences = {**current.preferences, 'consent_at': (clock.now() + timedelta(hours=1)).isoformat()}
        cloud.commit()
    row = queue(stores, settings, clock, guid='newer-consent')
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'newer_cloud_consent'


def test_unresolved_local_reference_is_durably_held_without_interrupting_profile_save(stores, settings, clock):
    local, volunteer, factory = stores
    before = sync.safe_snapshot(local, PHONE)
    volunteer.preferences = {**volunteer.preferences, 'serves_with_volunteer_id': 777}
    row = queue(stores, settings, clock, before=before)
    assert row.state == 'held' and row.detail == 'profile_validation_requires_review'
    assert local.get(m.Volunteer, volunteer.id).preferences['serves_with_volunteer_id'] == 777


def test_catch_up_wrong_fingerprint_and_missing_consent_are_refused(stores, settings, clock):
    local, volunteer, factory = stores
    local.add(MacInboundReceipt(guid='wrong', fingerprint='incorrect', result={'intent':'signup_complete','session_id':'session'}))
    local.add(m.Message(phone=PHONE, body='Jordan Demo', direction='in', kind='mac_test_in', purpose='test:session',
                        status='received', created_at=clock.now()))
    local.commit()
    with pytest.raises(sync.ProfileHeld):
        sync.catch_up(local, settings, phone=PHONE, guid='wrong')
    receipt = local.get(MacInboundReceipt, 'wrong')
    receipt.fingerprint = hashlib.sha256((PHONE + '\0iMessage\0session\0Jordan Demo').encode()).hexdigest()
    volunteer.preferences = {'signup_source':'sms'}
    local.commit()
    with pytest.raises(sync.ProfileHeld, match='recorded_consent_required'):
        sync.catch_up(local, settings, phone=PHONE, guid='wrong')


def test_normal_opt_out_mirrors_even_without_name_or_privilege_changes(stores, settings, clock):
    local, volunteer, factory = stores
    queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    before = sync.snapshot(local, PHONE)
    volunteer.sms_opt_in = False
    row = sync.capture(local, settings, phone=PHONE, guid='stop', route='stop',
                       before=before, effective_at=clock.now())
    local.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)).sms_opt_in is False


def test_real_inbound_queues_in_transaction_and_status_is_protected(clock, monkeypatch):
    credential = 'synthetic-credential-' + 'x' * 32
    settings = Settings(database_url='sqlite://', demo_mode=True, automation_enabled=False,
                        sms_provider='mac_messages', mac_bridge_enabled=True,
                        mac_bridge_token=credential, admin_password='synthetic-admin-password',
                        mac_demo_phones=PHONE, mac_test_sessions=session_json([PHONE], clock.now()),
                        profile_sync_enabled=True, profile_sync_phones=PHONE)
    app = create_app(settings)
    app.state.clock = app.state.mac_delivery_clock = clock
    def fake_inbound(session, *args, **kwargs):
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        volunteer.preferences = {**volunteer.preferences, 'availability_weekdays': [2, 6]}
        from app.core.inbound import InboundResult
        return InboundResult(routed_to='onboarding_complete')
    monkeypatch.setattr('app.web.mac_messages.handle_inbound', fake_inbound)
    with app.state.session_factory() as local:
        local.add(m.Volunteer(name='Jordan Demo', phone=PHONE, sms_opt_in=True, status='active',
                              preferences={'signup_source': 'sms'}, created_at=clock.now()))
        local.commit()
    with TestClient(app) as client:
        assert client.get('/api/profile-sync').status_code == 503
        app.dependency_overrides[admin] = lambda: {'id': 'synthetic-admin'}
        data = {'guid': 'accepted', 'phone': PHONE, 'body': 'Sundays and Wednesdays',
                'service': 'iMessage', 'session_id': session_id(PHONE)}
        result = client.post('/mac/inbound', json=data, headers={'Authorization': 'Bearer ' + credential})
        assert result.status_code == 200 and result.json()['profile_sync'] == 'pending'
        assert client.post('/mac/inbound', json=data, headers={'Authorization': 'Bearer ' + credential}).json()['duplicate']
        records = client.get('/api/profile-sync').json()['records']
        assert len(records) == 1 and records[0]['state'] == 'pending'
        assert PHONE not in json.dumps(records) and 'Sundays' not in json.dumps(records)


def test_identity_only_retains_unresolved_preferences_then_verified_full_retry(stores, settings, clock):
    local, volunteer, factory = stores
    local.add(m.Role(id=5, name='Local Greeter', ministry='Hospitality', required_qualifications=[], criticality='standard', fill_policy='auto'))
    volunteer.preferences = {**volunteer.preferences, 'interested_roles': ['Local Greeter']}
    local.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings, identity_only=True)
    assert row.state == 'pending' and row.detail == 'identity_synced_preferences_pending'
    assert row.payload['profile']['preferences']['interested_roles'] == ['Local Greeter']
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer))
        assert person.sms_opt_in and person.status == 'inactive'
        assert person.preferences['consent_source'] == 'sms_reply'
        assert 'interested_roles' not in person.preferences
        assert not person.is_coordinator and not person.is_pastor
        assert cloud.scalar(select(m.Qualification)) is None
    assert sync.publish_pending(local, factory, settings, identity_only=True) == []
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'unresolved_cloud_role'
    with factory() as cloud:
        cloud.add(m.Role(id=71, name='Cloud Greeter', ministry='Hospitality', required_qualifications=[], criticality='standard', fill_policy='auto'))
        cloud.commit()
    mapped = replace(settings, profile_sync_role_map=json.dumps({'Local Greeter':'Cloud Greeter'}))
    sync.publish_pending(local, factory, mapped, retry_held=True)
    assert row.state == 'synced'
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer))
        assert person.status == 'active' and person.preferences['interested_roles'] == ['Cloud Greeter']
        assert len(cloud.scalars(select(m.Volunteer)).all()) == 1


def test_identity_partial_never_overrides_later_cloud_opt_out(stores, settings, clock):
    local, volunteer, factory = stores
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings, identity_only=True)
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer))
        person.sms_opt_in = False
        cloud.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'cloud_opt_out_requires_review'


def test_recurring_windows_preserve_restrictions_with_cloud_role_ids(stores, settings, clock):
    local, volunteer, factory = stores
    local.add(m.Role(id=11, name='Greeter', ministry='Hospitality', required_qualifications=[], criticality='standard', fill_policy='auto'))
    window = {'weekday':6, 'role_ids':[11], 'role_label':'Greeter', 'any_role':False,
              'start_time':'08:00','end_time':'10:00','all_day':False,'event_context':None}
    volunteer.preferences = {**volunteer.preferences, 'recurring_windows':[window]}
    local.commit()
    row = queue(stores, settings, clock)
    assert 'role_ids' not in row.payload['profile']['preferences']['recurring_windows'][0]
    with factory() as cloud:
        cloud.add(m.Role(id=71, name='Greeter', ministry='Hospitality', required_qualifications=[], criticality='standard', fill_policy='auto'))
        cloud.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)).preferences['recurring_windows'] == [{**window,'role_ids':[71]}]


def test_same_receipt_corrected_snapshot_retains_revision_provenance(stores, settings, clock):
    local, volunteer, factory = stores
    first = queue(stores, settings, clock, guid='original-receipt')
    before = sync.snapshot(local, PHONE)
    volunteer.preferences = {**volunteer.preferences, 'availability_weekdays':[6]}
    local.commit()
    corrected = queue(stores, settings, clock, guid='original-receipt', before=before)
    assert first.key != corrected.key and first.source_guid == corrected.source_guid
    assert first.source_id == corrected.source_id
    assert queue(stores, settings, clock, guid='original-receipt').key == corrected.key


def test_pending_old_opt_in_cannot_publish_after_actual_local_stop(stores, settings, clock):
    local, volunteer, factory = stores
    row = queue(stores, settings, clock)
    volunteer.sms_opt_in = False
    local.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'newer_local_opt_out'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)) is None


def test_cli_watch_drains_independently_and_rechecks_removed_scope(tmp_path, settings, clock, monkeypatch, capsys):
    from tools import publish_profiles as tool
    from app.db.session import make_session_factory
    source = tmp_path/'local.db'
    engine = make_engine('sqlite:///'+str(source))
    m.Base.metadata.create_all(engine); ProfileBase.metadata.create_all(engine)
    cloud_engine = make_engine('sqlite://')
    m.Base.metadata.create_all(cloud_engine)
    factory = make_session_factory(cloud_engine)
    with make_session_factory(engine)() as local:
        volunteer = m.Volunteer(name='Jordan Demo', phone=PHONE, sms_opt_in=True, status='active',
                               preferences={'signup_source':'sms'},created_at=clock.now())
        local.add(volunteer);local.commit()
        sync.capture(local,settings,phone=PHONE,guid='first',route='signup_complete',before=None,effective_at=clock.now())
        local.commit()
        local.add(m.Volunteer(name='Second Demo', phone=OTHER, sms_opt_in=True, status='active',
                              preferences={'signup_source':'sms'},created_at=clock.now()));local.commit()
        sync.capture(local,replace(settings,profile_sync_phones=PHONE+','+OTHER),phone=OTHER,guid='second',route='signup_complete',before=None,effective_at=clock.now()+timedelta(seconds=1))
        local.commit()
    scope=tmp_path/'scope.json';scope.write_text(json.dumps({'phones':[PHONE,OTHER],'project_ref':'a'*20}))
    private=tmp_path/'target.env';private.write_text('DATABASE_URL=SECRET_DSN_SENTINEL')
    monkeypatch.setattr(sync,'cloud_connection',lambda settings:(cloud_engine,factory))
    monkeypatch.setattr(tool.time,'sleep',lambda seconds:scope.write_text(json.dumps({'phones':[PHONE],'project_ref':'a'*20})))
    assert tool.main(['--source-db','sqlite:///'+str(source),'--scope-file',str(scope),
        '--target-env-file',str(private),'--publish','--watch','--cycles','2']) == 0
    output=capsys.readouterr().out
    assert PHONE not in output and 'SECRET' not in output
    with make_session_factory(engine)() as local:
        assert sorted(local.scalars(select(ProfileOutbox.state)).all()) == ['pending','synced']
    engine.dispose()


def test_partial_draft_preserves_unknown_windows_in_identity_pending_payload(stores, settings, clock):
    local, volunteer, factory = stores
    local.add(m.Role(id=4,name='Coffee',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'))
    window={'weekday':2,'role_ids':[4],'role_label':'Coffee','any_role':False,
            'start_time':None,'end_time':None,'all_day':False,
            'event_context':{'label':'Weekly workshop','event_type_ids':[]}}
    volunteer.preferences={**volunteer.preferences,'onboarding_availability_draft':{'frequency_known':False,'max_per_month':None,'recurring_windows':[window]}}
    local.commit()
    row=queue(stores,settings,clock)
    preserved=row.payload['profile']['availability_draft']['recurring_windows'][0]
    assert preserved['role_names']==['Coffee'] and preserved['start_time'] is None
    assert preserved['event_context']=={'label':'Weekly workshop','event_type_names':[]}
    sync.publish_pending(local,factory,settings,identity_only=True)
    assert row.state=='pending' and row.detail=='identity_synced_preferences_pending'
    sync.publish_pending(local,factory,settings)
    assert row.state=='held' and row.detail=='incomplete_availability_draft'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)).status=='inactive'


def test_event_context_translates_stable_event_names_or_holds(stores,settings,clock):
    local,volunteer,factory=stores
    local.add(m.EventType(id=3,name='Workshop',title_patterns=[]))
    window={'weekday':2,'role_ids':[],'role_label':None,'any_role':True,
            'start_time':'13:00','end_time':'14:00','all_day':False,
            'event_context':{'label':'Workshop','event_type_ids':[3]}}
    volunteer.preferences={**volunteer.preferences,'recurring_windows':[window]}
    local.commit();row=queue(stores,settings,clock)
    sync.publish_pending(local,factory,settings)
    assert row.state=='held' and row.detail=='unresolved_cloud_event_context'
    with factory() as cloud:
        cloud.add(m.EventType(id=77,name='Workshop',title_patterns=[]));cloud.commit()
    sync.publish_pending(local,factory,settings,retry_held=True)
    assert row.state=='synced'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)).preferences['recurring_windows']==[
            {**window,'event_context':{'label':'Workshop','event_type_ids':[77]}}]


def test_latest_revision_carries_unfinished_changes_without_reverting_newer_profile(stores, settings, clock):
    local, volunteer, factory=stores
    local.add(m.Role(id=11,name='Greeter',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'))
    before=sync.snapshot(local,PHONE)
    volunteer.preferences={**volunteer.preferences,'interested_roles':['Greeter']};local.commit()
    older=queue(stores,settings,clock,guid='role-answer',before=before)
    before=sync.snapshot(local,PHONE)
    volunteer.name='Jordan Updated';local.commit()
    latest=queue(stores,settings,clock,guid='name-answer',before=before)
    assert 'interested_roles' in latest.payload['preference_keys']
    with factory() as cloud:
        cloud.add(m.Volunteer(name='Previous',phone=PHONE,sms_opt_in=True,status='active',preferences={},created_at=clock.now()))
        cloud.add(m.Role(id=71,name='Greeter',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'));cloud.commit()
    sync.publish_pending(local,factory,settings,limit=20)
    assert older.state=='held' and older.detail=='newer_local_profile'
    assert latest.state=='synced'
    with factory() as cloud:
        person=cloud.scalar(select(m.Volunteer))
        assert person.name=='Jordan Updated' and person.preferences['interested_roles']==['Greeter']


@pytest.mark.parametrize('cloud_edit', [False, True])
def test_preference_removal_preserves_newer_manual_cloud_value(stores, settings, clock, cloud_edit):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'max_per_month': 2}
    local.commit()
    queue(stores, settings, clock, guid='frequency-two')
    sync.publish_pending(local, factory, settings)
    before = sync.snapshot(local, PHONE)
    volunteer.preferences = {key:value for key,value in volunteer.preferences.items() if key != 'max_per_month'}
    local.commit()
    row = queue(stores, settings, clock, guid='frequency-removed', before=before)
    assert row.payload['preference_removals'] == ['max_per_month']
    if cloud_edit:
        with factory() as cloud:
            person = cloud.scalar(select(m.Volunteer))
            person.preferences = {**person.preferences, 'max_per_month':4}
            cloud.commit()
    sync.publish_pending(local, factory, settings)
    with factory() as cloud:
        preferences = cloud.scalar(select(m.Volunteer)).preferences
        if cloud_edit:
            assert row.state == 'held' and row.detail == 'cloud_preferences_changed'
            assert preferences['max_per_month'] == 4
        else:
            assert row.state == 'synced' and 'max_per_month' not in preferences


def event_role_profile(stores, clock, *, global_null=False, time_mode='event'):
    """Fictional catalogs deliberately allocate different local/cloud IDs."""
    from datetime import date
    local, volunteer, factory = stores
    local.add_all([
        m.Role(id=11,name='Greeter',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'),
        m.Role(id=12,name='Coffee',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'),
        m.EventType(id=3,name="Men's Group",title_patterns=[])])
    clock_window={'weekday':6,'role_ids':[11],'role_label':'Greeter','any_role':False,
                  'start_time':'08:00','end_time':'10:00','all_day':False,'event_context':None}
    group_window={'weekday':2,'role_ids':[12],'role_label':'Coffee','any_role':False,
                  'start_time':None,'end_time':None,'all_day':False,'time_mode':time_mode,
                  'event_context':{'label':"men's group",'event_type_ids':[3]}}
    volunteer.preferences={**volunteer.preferences,'interested_roles':['Greeter','Coffee'],
        'recurring_windows':[clock_window,group_window],
        'role_frequency_caps':[{'role_id':11,'role_name':'Greeter','max_per_month':2}]}
    if global_null:
        volunteer.preferences={**volunteer.preferences,'max_per_month':None}
    exclusions=[date(2026,12,day).isoformat() for day in range(1,32)]
    local.add(m.Availability(volunteer_id=volunteer.id,month='2026-12',available_dates=[],
                             unavailable_dates=exclusions,raw_reply='synthetic private text',parsed_at=clock.now()))
    local.commit()
    with factory() as cloud:
        cloud.add_all([
            m.Role(id=71,name='greeter',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'),
            m.Role(id=72,name='coffee',ministry='Hospitality',required_qualifications=[],criticality='standard',fill_policy='auto'),
            m.EventType(id=76,name="Men's Group",title_patterns=[])])
        cloud.commit()
    return clock_window,group_window,exclusions


@pytest.mark.parametrize('global_null', [False, True])
def test_event_follow_role_only_cap_and_december_exclusions_publish_exactly(stores,settings,clock,global_null):
    from app.core.recurring_availability import global_frequency_limit
    local,volunteer,factory=stores
    clock_window,group_window,exclusions=event_role_profile(stores,clock,global_null=global_null)
    settings=replace(settings,profile_sync_role_map=json.dumps({'Greeter':'greeter','Coffee':'coffee'}))
    row=queue(stores,settings,clock)
    preferences=row.payload['profile']['preferences']
    assert preferences['role_frequency_caps']==[{'role_name':'Greeter','max_per_month':2}]
    assert 'role_id' not in json.dumps(row.payload)
    assert ('max_per_month' in preferences)==global_null
    sync.publish_pending(local,factory,settings)
    assert row.state=='synced'
    with factory() as cloud:
        person=cloud.scalar(select(m.Volunteer))
        prefs=person.preferences
        assert prefs['recurring_windows']==[
            {**clock_window,'role_ids':[71]},
            {**group_window,'role_ids':[72],'event_context':{'label':"men's group",'event_type_ids':[76]}}]
        assert prefs['role_frequency_caps']==[{'role_id':71,'role_name':'greeter','max_per_month':2}]
        assert global_frequency_limit(prefs) is None
        assert ('max_per_month' in prefs)==global_null
        availability=cloud.scalar(select(m.Availability))
        assert availability.volunteer_id==person.id
        assert availability.available_dates==[] and availability.unavailable_dates==exclusions
        assert availability.raw_reply is None
    row.state='pending';local.commit()
    sync.publish_pending(local,factory,settings)
    assert row.detail=='already_applied'
    with factory() as cloud:
        assert len(cloud.scalars(select(m.Availability)).all())==1


@pytest.mark.parametrize('missing', ['role','group','clock_hours'])
def test_event_schema_never_broadens_unknown_clock_or_unmapped_catalog(stores,settings,clock,missing):
    local,volunteer,factory=stores
    event_role_profile(stores,clock,time_mode='clock' if missing=='clock_hours' else 'event')
    settings=replace(settings,profile_sync_role_map=json.dumps({'Greeter':'greeter','Coffee':'coffee'}))
    if missing in {'role','group'}:
        with factory() as cloud:
            cloud.delete(cloud.get(m.Role,71) if missing=='role' else cloud.get(m.EventType,76));cloud.commit()
    row=queue(stores,settings,clock)
    sync.publish_pending(local,factory,settings)
    assert row.state=='held'
    assert row.detail=={'role':'unresolved_cloud_role','group':'unresolved_cloud_event_context',
                        'clock_hours':'unresolved_window_time'}[missing]
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)) is None


@pytest.mark.parametrize('bad_cap',[
    {'role_id':11,'role_name':'Coffee','max_per_month':2},
    {'role_id':True,'role_name':'Greeter','max_per_month':2},
    {'role_id':11,'role_name':'Greeter','max_per_month':9},
    {'role_id':999,'role_name':'Greeter','max_per_month':2}])
def test_invalid_role_frequency_is_held_without_losing_local_transaction(stores,settings,clock,bad_cap):
    local,volunteer,factory=stores
    event_role_profile(stores,clock)
    volunteer.preferences={**volunteer.preferences,'role_frequency_caps':[bad_cap]};local.commit()
    row=queue(stores,settings,clock)
    assert row.state=='held' and row.payload['profile'] is None
    assert local.get(m.Volunteer,volunteer.id).preferences['role_frequency_caps']==[bad_cap]
    assert sync.publish_pending(local,factory,settings)==[]


def test_partial_draft_retains_event_mode_and_role_caps_as_identity_pending(stores,settings,clock):
    local,volunteer,factory=stores
    clock_window,group_window,exclusions=event_role_profile(stores,clock)
    volunteer.preferences={**volunteer.preferences,'onboarding_stage':'availability',
        'onboarding_availability_draft':{'availability_known':True,'frequency_known':True,
             'max_per_month':None,'recurring_windows':[clock_window,group_window],
             'role_frequency_caps':[{'role_id':11,'role_name':'Greeter','max_per_month':2}],
             'unavailable_dates':exclusions}}
    local.commit();row=queue(stores,settings,clock)
    draft=row.payload['profile']['availability_draft']
    assert draft['role_frequency_caps']==[{'role_name':'Greeter','max_per_month':2}]
    assert draft['recurring_windows'][1]['time_mode']=='event'
    assert draft['recurring_windows'][1]['event_context']['event_type_names']==["Men's Group"]
    assert draft['max_per_month'] is None and draft['unavailable_dates']==exclusions
    sync.publish_pending(local,factory,settings,identity_only=True)
    assert row.state=='pending' and row.detail=='identity_synced_preferences_pending'
    with factory() as cloud:
        prefs=cloud.scalar(select(m.Volunteer)).preferences
        assert 'role_frequency_caps' not in prefs and 'max_per_month' not in prefs
    sync.publish_pending(local,factory,settings)
    assert row.state=='held' and row.detail=='incomplete_availability_draft'


def test_role_cap_translation_collision_is_held(stores,settings,clock):
    local,volunteer,factory=stores
    event_role_profile(stores,clock)
    volunteer.preferences={**volunteer.preferences,'role_frequency_caps':[
        {'role_id':11,'role_name':'Greeter','max_per_month':2},
        {'role_id':12,'role_name':'Coffee','max_per_month':4}]};local.commit()
    settings=replace(settings,profile_sync_role_map=json.dumps({'Greeter':'greeter','Coffee':'greeter'}))
    row=queue(stores,settings,clock)
    sync.publish_pending(local,factory,settings)
    assert row.state=='held' and row.detail=='ambiguous_cloud_role_frequency'
    with factory() as cloud:assert cloud.scalar(select(m.Volunteer)) is None


def test_validated_correction_after_old_schema_hold_captures_new_revision(stores,settings,clock):
    local,volunteer,factory=stores
    event_role_profile(stores,clock)
    invalid={**volunteer.preferences,'role_frequency_caps':[{'role_id':11,'role_name':'Wrong','max_per_month':2}]}
    volunteer.preferences=invalid;local.commit()
    held=queue(stores,settings,clock,guid='original-actual-receipt')
    before=sync.safe_snapshot(local,PHONE)
    assert held.state=='held' and before.get('_held')
    volunteer.preferences={**volunteer.preferences,'role_frequency_caps':[{'role_id':11,'role_name':'Greeter','max_per_month':2}]}
    local.commit()
    corrected=queue(stores,settings,clock,guid='original-actual-receipt',before=before)
    assert corrected.key!=held.key and corrected.source_guid==held.source_guid
    assert corrected.state=='pending'
    assert corrected.payload['profile']['preferences']['role_frequency_caps']==[{'role_name':'Greeter','max_per_month':2}]


@pytest.mark.parametrize('value', [True, False])
def test_explicit_any_role_is_mirrored_without_assumed_interests_or_privileges(stores, settings, clock, value):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'any_role': value, 'interested_roles': []}
    local.commit()
    with factory() as cloud:
        cloud.add(m.Volunteer(id=900, name='Existing User', phone=PHONE, sms_opt_in=True,
            status='active', is_coordinator=True, preferences={'any_role': not value,
                'paused_roles': ['Greeter'], 'admin_text_owner': 'cloud-owner'}, created_at=clock.now()))
        cloud.add(m.Qualification(volunteer_id=900, type='background_check', status='verified'))
        cloud.commit()
    row = queue(stores, settings, clock)
    assert row.payload['profile']['preferences']['any_role'] is value
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced' and row.cloud_id == 900
    with factory() as cloud:
        mirrored = cloud.get(m.Volunteer, 900)
        assert mirrored.preferences['any_role'] is value
        assert mirrored.preferences['interested_roles'] == []
        assert mirrored.preferences['paused_roles'] == ['Greeter']
        assert mirrored.preferences['admin_text_owner'] == 'cloud-owner' and mirrored.is_coordinator
        assert cloud.scalars(select(m.Role)).all() == []
        assert [(q.type, q.status) for q in cloud.scalars(select(m.Qualification))] == [('background_check', 'verified')]


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_invalid_any_role_is_held_before_remote_profile_write(stores, settings, clock, value):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'any_role': value}
    local.commit()
    with pytest.raises(sync.ProfileHeld, match='invalid_any_role'):
        sync.snapshot(local, PHONE)
    row = queue(stores, settings, clock)
    assert row.state == 'held' and row.payload['profile'] is None
    sync.publish_pending(local, factory, settings, retry_held=True)
    assert row.state == 'held'
    with factory() as cloud:
        assert cloud.scalars(select(m.Volunteer)).all() == []


def test_missing_any_role_preserves_existing_remote_value(stores, settings, clock):
    local, volunteer, factory = stores
    assert 'any_role' not in volunteer.preferences
    with factory() as cloud:
        cloud.add(m.Volunteer(name='Existing User', phone=PHONE, sms_opt_in=True, status='active',
            preferences={'any_role': True}, created_at=clock.now()))
        cloud.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer)).preferences['any_role'] is True


def test_any_role_does_not_escape_identity_only_publication(stores, settings, clock):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'any_role': True, 'interested_roles': [],
        'onboarding_stage': 'availability'}
    local.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings, identity_when_incomplete=True)
    assert row.state == 'pending' and row.detail == 'identity_synced_preferences_pending'
    with factory() as cloud:
        mirrored = cloud.scalar(select(m.Volunteer))
        assert 'any_role' not in mirrored.preferences and 'interested_roles' not in mirrored.preferences
        assert mirrored.status == 'inactive'


def test_apply_rejects_invalid_stored_any_role(stores, settings, clock):
    local, volunteer, factory = stores
    row = queue(stores, settings, clock)
    profile = {**row.payload['profile'], 'preferences': {**row.payload['profile']['preferences'], 'any_role': 'true'}}
    row.payload = {**row.payload, 'profile': profile}
    with factory() as cloud:
        with pytest.raises(sync.ProfileHeld, match='invalid_any_role'):
            sync._apply(cloud, row, {})
        assert cloud.scalars(select(m.Volunteer)).all() == []


@pytest.mark.parametrize('identity_when_incomplete', [False, True])
def test_pending_scheduling_constraint_cannot_publish_an_eligible_cloud_profile(
    stores, settings, clock, identity_when_incomplete
):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'pending_constraints': [
        {'kind':'validation', 'reason':'Fictional paired-date restriction awaits coordinator review.'}
    ]}
    local.commit()
    row = queue(stores, settings, clock)
    sync.publish_pending(local, factory, settings, identity_when_incomplete=identity_when_incomplete)
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        if identity_when_incomplete:
            assert person is not None and person.status == 'inactive'
            assert row.state == 'pending' and row.detail == 'identity_synced_preferences_pending'
            assert 'pending_constraints' not in person.preferences
        else:
            assert person is None
            assert row.state == 'held' and row.detail == 'incomplete_scheduling_constraints'


@pytest.mark.parametrize('identity_when_incomplete', [False, True])
def test_resolved_constraints_can_complete_the_held_profile_without_losing_identity_fields(
    stores, settings, clock, identity_when_incomplete
):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'pending_constraints': [
        {'kind':'validation', 'reason':'Fictional restriction awaiting review.'}
    ]}
    local.commit()
    queue(stores, settings, clock, guid='pending-constraint')
    sync.publish_pending(local, factory, settings, identity_when_incomplete=identity_when_incomplete)
    before = sync.snapshot(local, PHONE)
    volunteer.preferences = {**volunteer.preferences, 'pending_constraints': []}
    local.commit()
    row = queue(stores, settings, clock, guid='resolved-constraint', before=before)
    sync.publish_pending(local, factory, settings, identity_when_incomplete=identity_when_incomplete)
    assert row.state == 'synced'
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.name == volunteer.name and person.status == 'active' and person.sms_opt_in
        assert 'pending_constraints' not in person.preferences
        assert cloud.scalar(select(m.Qualification)) is None
        assert cloud.scalar(select(m.Message)) is None


def test_pending_constraints_do_not_block_actual_stop_withdrawal(stores, settings, clock):
    local, volunteer, factory = stores
    volunteer.preferences = {**volunteer.preferences, 'pending_constraints': [
        {'kind':'validation', 'reason':'Fictional unresolved restriction.'}
    ]}
    volunteer.sms_opt_in = False
    local.commit()
    row = sync.capture(local, settings, phone=PHONE, guid='actual-stop', route='stop',
                       before=None, effective_at=clock.now())
    local.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced'
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person is not None and not person.sms_opt_in and person.status == 'inactive'
        assert 'pending_constraints' not in person.preferences
        assert cloud.scalar(select(m.Message)) is None
