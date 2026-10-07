from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.cloud_registration import initialize, scan
from app.db import models as m
from app.db.session import make_engine


@pytest.fixture
def stores():
    engines = [make_engine('sqlite://') for _ in range(2)]
    for engine in engines:
        m.Base.metadata.create_all(engine)
    factories = [sessionmaker(bind=engine, expire_on_commit=False) for engine in engines]
    with factories[0]() as local:
        local.add(person('+12025550101', 'Existing Fixture'))
        local.commit()
        journal = initialize(local, source='synthetic-store', project_ref='synthetic-project')
        yield local, factories[1], journal
    for engine in engines:
        engine.dispose()


def person(phone, name='New Volunteer', **kwargs):
    return m.Volunteer(phone=phone, name=name, sms_opt_in=True, status='active',
                       created_at=datetime.now(timezone.utc), preferences={}, **kwargs)


def run(stores, factory=None, **kwargs):
    local, cloud, journal = stores
    return scan(local, factory or cloud, journal, source='synthetic-store',
                project_ref='synthetic-project', role_map={}, **kwargs)


def test_baseline_is_excluded_and_new_user_is_durable_without_privileges(stores):
    local, cloud, journal = stores
    assert run(stores) == []
    local.add(person('+12025550102', is_coordinator=True, is_pastor=True))
    local.commit()
    assert run(stores)[0]['state'] == 'profile_pending'
    with cloud() as session:
        saved = session.scalar(select(m.Volunteer))
        assert saved.name == 'New Volunteer' and saved.sms_opt_in
        assert saved.status == 'inactive'
        assert not saved.is_coordinator and not saved.is_pastor
        assert not session.scalars(select(m.Message)).all()
        assert not session.scalars(select(m.Assignment)).all()
    assert run(stores) == []
    with cloud() as session:
        assert len(session.scalars(select(m.Volunteer)).all()) == 1


def test_completion_saves_validated_preferences_after_identity_registration(stores):
    local, cloud, journal = stores
    v = person('+12025550102')
    local.add(v)
    local.commit()
    run(stores)
    v.preferences = {'onboarding_stage': 'complete', 'max_per_month': 2,
                     'availability_weekdays': [6], 'private_raw_reply': 'Never copy this'}
    local.commit()
    assert run(stores)[0]['state'] == 'saved'
    with cloud() as session:
        saved = session.scalar(select(m.Volunteer))
        assert saved.status == 'active' and saved.preferences['max_per_month'] == 2
        assert saved.preferences['availability_weekdays'] == [6]
        assert 'private_raw_reply' not in saved.preferences


def test_existing_cloud_profile_and_opt_out_are_preserved(stores):
    local, cloud, journal = stores
    local.add(person('+12025550102'))
    local.commit()
    with cloud() as session:
        v = person('+12025550102', name='Cloud Name', is_coordinator=True)
        v.sms_opt_in = False
        v.preferences = {'notes': 'Cloud-owned note'}
        session.add(v)
        session.commit()
    assert run(stores)[0]['state'] == 'existing_preserved'
    with cloud() as session:
        v = session.scalar(select(m.Volunteer))
        assert v.name == 'Cloud Name' and not v.sms_opt_in and v.is_coordinator
        assert v.preferences == {'notes': 'Cloud-owned note'}


def test_outage_retries_without_losing_new_user_or_leaking_error(stores):
    local, cloud, journal = stores
    local.add(person('+12025550102'))
    local.commit()
    unavailable = Mock(side_effect=RuntimeError('PRIVATE_CREDENTIAL'))
    assert run(stores, unavailable) == [{'state': 'retry', 'detail': 'cloud_save_failed'}]
    assert 'PRIVATE' not in str(journal)
    assert run(stores)[0]['state'] == 'profile_pending'


def test_bad_identity_is_held_and_wrong_database_binding_fails(stores):
    local, cloud, journal = stores
    local.add(person('not-a-number'))
    local.commit()
    assert run(stores) == [{'state': 'held', 'detail': 'invalid_identity'}]
    journal['source'] = 'wrong-store'
    with pytest.raises(ValueError, match='registration_binding_mismatch'):
        run(stores)


def test_unmapped_role_still_registers_identity_and_retries_full_profile(stores):
    local, cloud, journal = stores
    local.add(m.Role(name='Local Only', ministry='Welcome', required_qualifications=[],
                     criticality='standard', fill_policy='auto'))
    v = person('+12025550102')
    v.preferences = {'onboarding_stage': 'complete', 'interested_roles': ['Local Only']}
    local.add(v)
    local.commit()
    assert run(stores)[0]['state'] == 'profile_pending'
    with cloud() as session:
        v = session.scalar(select(m.Volunteer))
        assert v.status == 'inactive' and 'interested_roles' not in v.preferences
        session.add(m.Role(name='Local Only', ministry='Welcome', required_qualifications=[],
                         criticality='standard', fill_policy='auto'))
        session.commit()
    assert run(stores)[0]['state'] == 'saved'


def test_replay_after_lost_journal_write_does_not_duplicate(stores):
    local, cloud, journal = stores
    local.add(person('+12025550102'))
    local.commit()
    assert run(stores)[0]['state'] == 'profile_pending'
    journal['records'] = {}
    assert run(stores)[0]['state'] == 'profile_pending'
    with cloud() as session:
        assert len(session.scalars(select(m.Volunteer)).all()) == 1


def test_local_withdrawal_updates_owned_cloud_consent(stores):
    local, cloud, journal = stores
    v = person('+12025550102')
    v.preferences = {'onboarding_stage': 'complete'}
    local.add(v)
    local.commit()
    assert run(stores)[0]['state'] == 'saved'
    v.sms_opt_in = False
    local.commit()
    assert run(stores)[0]['state'] == 'saved'
    with cloud() as session:
        assert session.scalar(select(m.Volunteer)).sms_opt_in is False
