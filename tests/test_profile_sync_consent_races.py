"""Exercise actual Mac STOP and committed concurrent source edits in isolated stores."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app.clock import FakeClock
from app.config import Settings
from app.core import profile_sync as sync
from app.db import models as m
from app.db.session import make_engine, make_session_factory
from app.integrations.profile_models import ProfileBase, ProfileOutbox
from app.main import create_app
from tests.session_fixtures import session_id, session_json

PHONE = '+12025550147'
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize('existing_cloud', [True, False])
@pytest.mark.parametrize('invalid', ['serving_partner', 'availability_draft', 'missing_role'])
def test_actual_mac_stop_publishes_only_consent_and_preserves_unfinished_profile(tmp_path, existing_cloud, invalid):
    token = 'synthetic-token-' + 'x' * 32
    settings = Settings(database_url='sqlite:///' + str(tmp_path / 'local.sqlite'), demo_mode=True,
        automation_enabled=False, sms_provider='mac_messages', mac_bridge_enabled=True,
        mac_bridge_token=token, admin_password='synthetic-password', mac_demo_phones=PHONE,
        mac_test_sessions=session_json([PHONE], NOW), profile_sync_enabled=True, profile_sync_phones=PHONE)
    app = create_app(settings)
    app.state.clock = app.state.mac_delivery_clock = FakeClock(NOW)
    class NoGloo:
        def __getattr__(self, name):
            raise AssertionError('STOP must not require Gloo')
    app.state.gloo = NoGloo()
    engine = make_engine('sqlite:///' + str(tmp_path / 'cloud.sqlite'))
    m.Base.metadata.create_all(engine)
    cloud_factory = make_session_factory(engine)
    with cloud_factory() as cloud:
        if existing_cloud:
            cloud.add(m.Volunteer(name='Cloud identity', phone=PHONE, status='active', sms_opt_in=True,
                is_coordinator=True, preferences={'max_per_month': 3}, created_at=NOW))
        cloud.commit()
    with app.state.session_factory() as local:
        partner = m.Volunteer(name='Local partner', phone='+12025550148', status='active',
                              sms_opt_in=True, preferences={}, created_at=NOW)
        local.add(partner)
        local.flush()
        prefs = {'onboarding_stage': 'complete', 'max_per_month': 2}
        person = m.Volunteer(name='Local identity', phone=PHONE, status='active', sms_opt_in=True,
                             preferences=prefs, created_at=NOW)
        local.add(person)
        local.flush()
        pending = sync.capture(local, settings, phone=PHONE, guid='unfinished-profile',
            route='onboarding_complete', before=None, effective_at=NOW - timedelta(seconds=1))
        pending_key, pending_payload = pending.key, dict(pending.payload)
        prefs = {**prefs, **{
            'serving_partner': {'serves_with_volunteer_id': partner.id},
            'availability_draft': {'onboarding_availability_draft': {'weekdays': ['invalid']}},
            'missing_role': {'interested_roles': ['Missing role']},
        }[invalid]}
        person.preferences = prefs
        local.commit()
        assert sync.safe_snapshot(local, PHONE).get('_held')
    request = {'guid': 'actual-stop', 'phone': PHONE, 'body': 'STOP', 'service': 'iMessage',
               'session_id': session_id(PHONE)}
    with TestClient(app) as client:
        first = client.post('/mac/inbound', headers={'Authorization': 'Bearer ' + token}, json=request)
        duplicate = client.post('/mac/inbound', headers={'Authorization': 'Bearer ' + token}, json=request)
        assert first.status_code == duplicate.status_code == 200
        assert first.json()['profile_sync'] == 'pending' and duplicate.json()['duplicate']
    with app.state.session_factory() as local:
        row = local.scalar(select(ProfileOutbox).where(ProfileOutbox.source_guid == 'actual-stop'))
        assert row.payload['changed'] == ['sms_opt_in']
        assert row.payload['preference_keys'] == row.payload['availability_months'] == []
        result = sync.publish_pending(local, cloud_factory, settings, identity_only=True)
        assert result[0]['key'] == row.key  # Withdrawal precedes older unfinished work.
        assert row.state == ('synced' if existing_cloud else 'held')
        if not existing_cloud:
            assert row.detail == 'stop_cloud_profile_missing'
        assert local.get(ProfileOutbox, pending_key).payload == pending_payload
        person = local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.preferences == prefs and person.sms_opt_in is False
        assert local.get(m.Policy, 'sms_opt_out:' + PHONE)
        assert len(local.scalars(select(ProfileOutbox)).all()) == 2
        if existing_cloud:
            row.state = 'pending'  # Lost ACK after successful cloud transaction.
            local.commit()
            assert sync.publish_pending(local, cloud_factory, settings)[0]['detail'] == 'already_applied'
    with cloud_factory() as cloud:
        people = cloud.scalars(select(m.Volunteer)).all()
        assert len(people) == int(existing_cloud)
        if existing_cloud:
            person = people[0]
            assert not person.sms_opt_in and person.name == 'Cloud identity'
            assert person.status == 'active' and person.is_coordinator
            assert person.preferences['max_per_month'] == 3
        assert cloud.scalars(select(m.Message)).all() == []
    engine.dispose()


def test_batch_reloads_concurrent_stop_and_constraints_before_next_publication(tmp_path, monkeypatch):
    engine = make_engine('sqlite:///' + str(tmp_path / 'local.sqlite'))
    m.Base.metadata.create_all(engine)
    ProfileBase.metadata.create_all(engine)
    cloud_engine = make_engine('sqlite:///' + str(tmp_path / 'cloud.sqlite'))
    m.Base.metadata.create_all(cloud_engine)
    factory, cloud = make_session_factory(engine), make_session_factory(cloud_engine)
    settings = Settings(profile_sync_enabled=True, profile_sync_phones=PHONE)
    with factory() as local:
        local.add(m.Volunteer(name='Synthetic participant', phone=PHONE, status='active', sms_opt_in=True,
            created_at=NOW, preferences={'onboarding_stage': 'complete'}))
        local.flush()
        for i in range(2):
            sync.capture(local, settings, phone=PHONE, guid=f'profile-{i}', route='onboarding_complete',
                         before=None, effective_at=NOW + timedelta(seconds=i))
        local.commit()
    writes, apply = [], sync._apply
    def recording(*args, **kwargs):
        writes.append(args[1].source_guid)
        return apply(*args, **kwargs)
    monkeypatch.setattr(sync, '_apply', recording)
    stopped = False
    with factory() as publisher:
        def concurrent_stop(session):
            nonlocal stopped
            if stopped:
                return
            stopped = True
            with factory() as incoming:
                person = incoming.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
                person.sms_opt_in = False
                person.preferences = {'serves_with_volunteer_id': 99999}
                incoming.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
                incoming.flush()
                sync.capture(incoming, settings, phone=PHONE, guid='concurrent-stop', route='stop',
                    before=None, consent_only=True, effective_at=NOW + timedelta(seconds=2))
                incoming.commit()
        event.listen(publisher, 'after_commit', concurrent_stop)
        results = sync.publish_pending(publisher, cloud, settings, limit=2)
        assert writes == ['profile-0']
        assert results[1]['detail'] == 'newer_local_opt_out'
        assert all(result['attempts'] == 1 for result in results)
        assert sync.publish_pending(publisher, cloud, settings)[0]['state'] == 'synced'
    with cloud() as check:
        assert check.scalar(select(m.Volunteer)).sms_opt_in is False
    engine.dispose()
    cloud_engine.dispose()
