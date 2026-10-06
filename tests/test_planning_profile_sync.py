"""Approved planning restrictions survive mirrors with independent catalog IDs."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.core import confirmations, paired_planning, profile_sync as sync
from app.db import models as m
from tests.test_profile_sync import PHONE, settings, stores

PATTERN = {'weekday_ordinals': [{'weekday': 6, 'ordinals': [4, 2]}],
           'annual_unavailable_months': [12]}


def source_pair(stores, clock):
    local, person, _ = stores
    for identifier, name in ((11, 'Greeter'), (27, 'Production')):
        local.add(m.Role(id=identifier, name=name, ministry='Synthetic',
            required_qualifications=['sound_training'] if name == 'Production' else [],
            criticality='standard', fill_policy='auto'))
    local.flush()
    approval = paired_planning.stage_rules(local, clock.now(), person,
        pairs=[{'role_ids': [27, 11]}])
    # Synthetic reviewed record fixture, never an operator/remote action.
    person.preferences = deepcopy(approval.payload['after']['preferences'])
    approval.status = 'approved'
    approval.payload = {**approval.payload, 'applied_record_id': person.id}
    paired_planning.record_rule_receipt(local, approval)
    local.commit()
    assert paired_planning.rule_problem(local, person) is None
    return approval


def capture(stores, settings, clock, *, before=None, guid='synthetic-planning-input'):
    local, person, _ = stores
    row = sync.capture(local, settings, phone=PHONE, guid=guid, route='availability',
        before=before, effective_at=clock.now())
    local.commit()
    return row


def test_calendar_and_approved_role_pairs_map_cloud_ids_preserving_privileges(stores, settings, clock):
    local, person, factory = stores
    source_pair(stores, clock)
    person.preferences = {**person.preferences, 'calendar_patterns': PATTERN}
    with factory() as cloud:
        cloud.add_all([m.Role(id=70, name='Cloud Greeter', ministry='Cloud', required_qualifications=[],
            criticality='standard', fill_policy='auto'),
            m.Role(id=90, name='Production', ministry='Cloud', required_qualifications=['sound_training'],
            criticality='standard', fill_policy='auto'),
            m.Volunteer(id=900, name='Existing Synthetic', phone=PHONE, sms_opt_in=True, status='active',
                is_coordinator=True, is_pastor=True, preferences={'unrelated': 'preserve'}, created_at=clock.now())])
        cloud.flush()
        cloud.add(m.Qualification(volunteer_id=900, type='background_check', status='verified'))
        cloud.commit()
    settings = replace(settings, profile_sync_role_map=json.dumps({'Greeter': 'Cloud Greeter'}))
    row = capture(stores, settings, clock)
    assert row.payload['profile']['preferences']['same_day_role_pairs'] == [{'role_names': ['Greeter', 'Production']}]
    assert row.payload['profile']['preferences']['calendar_patterns']['weekday_ordinals'][0]['ordinals'] == [2, 4]
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced' and row.cloud_id == 900
    with factory() as cloud:
        saved = cloud.get(m.Volunteer, 900)
        assert saved.preferences['same_day_role_pairs'] == [{'role_ids': [70, 90]}]
        assert saved.preferences['calendar_patterns']['annual_unavailable_months'] == [12]
        assert saved.is_coordinator and saved.is_pastor and saved.preferences['unrelated'] == 'preserve'
        assert [(q.type, q.status) for q in saved.qualifications] == [('background_check', 'verified')]
        assert not cloud.scalars(select(m.Assignment)).all()
        # A mirror is not a transfer of the source database's review authority.
        assert cloud.get(m.Policy, 'planning-rules:900') is None


@pytest.mark.parametrize('defect', ['missing', 'rejected', 'hash', 'role_changed'])
def test_nonempty_pairs_require_current_source_review(stores, clock, defect):
    local, person, _ = stores
    approval = source_pair(stores, clock)
    if defect == 'missing': local.delete(local.get(m.Policy, 'planning-rules:' + str(person.id)))
    if defect == 'rejected': approval.status = 'rejected'
    if defect == 'hash': approval.payload = {**approval.payload, 'content_hash': 'altered'}
    if defect == 'role_changed': local.get(m.Role, 27).required_qualifications = ['new_requirement']
    local.flush()
    assert sync.safe_snapshot(local, PHONE) == {'_held': 'profile_validation_requires_review'}


@pytest.mark.parametrize('key,value', [
    ('calendar_patterns', None),
    ('calendar_patterns', {'weekday_ordinals': [], 'annual_unavailable_months': [True]}),
    ('calendar_patterns', {'weekday_ordinals': [{'weekday': 6, 'ordinals': [2, 2]}], 'annual_unavailable_months': []}),
    ('calendar_patterns', {'weekday_ordinals': [], 'annual_unavailable_months': [], 'invented': True}),
    ('same_day_role_pairs', None),
    ('same_day_role_pairs', [{'role_ids': [11, 999]}]),
    ('same_day_role_pairs', [{'role_ids': [11, 11]}]),
    ('same_day_role_pairs', [{'role_ids': [True, 27]}]),
])
def test_unknown_or_malformed_rules_do_not_partially_publish(stores, settings, clock, key, value):
    local, person, factory = stores
    source_pair(stores, clock)
    person.preferences = {**person.preferences, key: value}
    row = capture(stores, settings, clock)
    assert row.state == 'held' and row.payload['profile'] is None
    sync.publish_pending(local, factory, settings, retry_held=True)
    assert row.state == 'held'
    with factory() as cloud:
        assert not cloud.scalars(select(m.Volunteer)).all()


@pytest.mark.parametrize('mapping', ['missing', 'collapse'])
def test_unresolved_or_collapsed_cloud_roles_hold_entire_transaction(stores, settings, clock, mapping):
    local, person, factory = stores
    source_pair(stores, clock)
    person.preferences = {**person.preferences, 'calendar_patterns': PATTERN}
    with factory() as cloud:
        cloud.add(m.Role(id=90, name='Production', ministry='Cloud', required_qualifications=[],
            criticality='standard', fill_policy='auto'))
        cloud.commit()
    if mapping == 'collapse':
        settings = replace(settings, profile_sync_role_map=json.dumps({'Greeter': 'Production'}))
    row = capture(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held'
    with factory() as cloud:
        assert not cloud.scalars(select(m.Volunteer)).all()
        assert not cloud.scalars(select(m.Qualification)).all()


def test_explicit_clear_and_removal_preserve_null_monthly_limit(stores, settings, clock):
    local, person, factory = stores
    source_pair(stores, clock)
    person.preferences = {**person.preferences, 'calendar_patterns': PATTERN}
    with factory() as cloud:
        cloud.add_all([m.Role(id=70, name='Greeter', ministry='Cloud', required_qualifications=[],
            criticality='standard', fill_policy='auto'), m.Role(id=90, name='Production', ministry='Cloud',
            required_qualifications=[], criticality='standard', fill_policy='auto')])
        cloud.commit()
    row = capture(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    before = sync.snapshot(local, PHONE)
    person.preferences = {**person.preferences, 'calendar_patterns': {'weekday_ordinals': [], 'annual_unavailable_months': []},
        'same_day_role_pairs': [], 'max_per_month': None}
    clock.advance(timedelta(seconds=1))
    cleared = capture(stores, settings, clock, before=before, guid='synthetic-clear')
    assert 'max_per_month' in cleared.payload['preference_keys']
    sync.publish_pending(local, factory, settings)
    assert cleared.state == 'synced'
    with factory() as cloud:
        saved = cloud.get(m.Volunteer, row.cloud_id)
        assert saved.preferences['calendar_patterns'] == {'weekday_ordinals': [], 'annual_unavailable_months': []}
        assert saved.preferences['same_day_role_pairs'] == []
        assert 'max_per_month' in saved.preferences and saved.preferences['max_per_month'] is None
    before = sync.snapshot(local, PHONE)
    person.preferences = {key: value for key, value in person.preferences.items()
        if key not in {'calendar_patterns', 'same_day_role_pairs'}}
    clock.advance(timedelta(seconds=1))
    removed = capture(stores, settings, clock, before=before, guid='synthetic-remove')
    sync.publish_pending(local, factory, settings)
    assert removed.state == 'synced'
    with factory() as cloud:
        saved = cloud.get(m.Volunteer, row.cloud_id)
        assert 'calendar_patterns' not in saved.preferences and 'same_day_role_pairs' not in saved.preferences
        assert saved.preferences['max_per_month'] is None


def test_one_time_december_exclusion_does_not_create_annual_absence(stores, settings, clock):
    local, person, factory = stores
    local.add(m.Availability(volunteer_id=person.id, month='2026-12', available_dates=[],
        unavailable_dates=['2026-12-01', '2026-12-31']))
    row = capture(stores, settings, clock)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'synced'
    with factory() as cloud:
        assert 'calendar_patterns' not in cloud.get(m.Volunteer, row.cloud_id).preferences
        assert cloud.scalar(select(m.Availability)).unavailable_dates == ['2026-12-01', '2026-12-31']


def test_revoked_pair_review_after_capture_prevents_full_publication(stores, settings, clock):
    local, person, factory = stores
    approval = source_pair(stores, clock)
    row = capture(stores, settings, clock)
    approval.status = 'rejected'
    local.commit()
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'newer_local_profile'
    with factory() as cloud:
        assert not cloud.scalars(select(m.Volunteer)).all()
