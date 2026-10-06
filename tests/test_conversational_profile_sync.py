"""Unresolved conversational evidence stays local while identity remains publishable."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.core import profile_sync as sync
from app.core.conversational_signup import partial_availability
from app.core.signup_copy import ensure_exact_role_menu
from app.db import models as m
from tests.test_conversational_signup import proposal
from tests.test_profile_sync import PHONE, settings, stores


def unfinished(stores, clock):
    local, person, _ = stores
    ensure_exact_role_menu(local)
    draft = partial_availability(proposal(), {}, clock.now().date(),
        local.scalars(select(m.Role)).all(), local.scalars(select(m.EventType)).all(),
        actual_body='Production on the same days as greeting')
    person.preferences = {**person.preferences, 'onboarding_stage': 'availability',
        'interested_roles': ['Greeter', 'Production', 'Child Care'],
        'onboarding_availability_draft': draft}
    local.commit()
    return draft


def test_actual_partial_draft_capture_preserves_unresolved_proposals_without_promoting_them(stores, settings, clock):
    local, person, _ = stores
    draft = unfinished(stores, clock)
    original = deepcopy(draft)
    row = sync.capture(local, settings, phone=PHONE, guid='actual-partial-input',
        route='onboarding_clarify', before=None, effective_at=clock.now())
    local.commit()
    assert row.state == 'pending' and row.payload['profile'] is not None
    snapshot = row.payload['profile']
    assert snapshot['availability_draft']['pending_constraints'] == original['pending_constraints']
    assert snapshot['availability_draft']['unavailable_dates'] == ['2026-12-01', '2026-12-31']
    assert [cap['max_per_month'] for cap in snapshot['availability_draft']['role_frequency_caps']] == [2, 1]
    assert 'pending_constraints' not in snapshot['preferences']
    assert 'recurring_windows' not in snapshot['preferences']
    assert snapshot['availability'] == [] and person.preferences['onboarding_availability_draft'] == original
    assert local.scalar(select(m.Qualification)) is None


@pytest.mark.parametrize('route', ['onboarding_clarify', 'onboarding_pending_review'])
def test_identity_publish_ignores_raw_pending_and_full_publish_stays_held(stores, settings, clock, route):
    local, person, factory = stores
    unfinished(stores, clock)
    row = sync.capture(local, settings, phone=PHONE, guid='actual-partial-input',
        route=route, before=None, effective_at=clock.now())
    local.commit()
    sync.publish_pending(local, factory, settings, identity_only=True)
    assert row.state == 'pending' and row.detail == 'identity_synced_preferences_pending'
    cloud_id = row.cloud_id
    with factory() as cloud:
        saved = cloud.get(m.Volunteer, cloud_id)
        assert saved.name == person.name and saved.sms_opt_in and saved.status == 'inactive'
        assert set(saved.preferences) == sync.IDENTITY_KEYS | {sync.MARKER}
        assert not cloud.scalars(select(m.Availability)).all()
        assert not cloud.scalars(select(m.Qualification)).all()
        original = deepcopy(saved.preferences)
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'incomplete_availability_draft'
    with factory() as cloud:
        assert cloud.get(m.Volunteer, cloud_id).preferences == original
        assert len(cloud.scalars(select(m.Volunteer)).all()) == 1


def test_pending_review_with_unvalidated_source_evidence_records_hold_without_publishing(stores, settings, clock):
    local, person, factory = stores
    draft = unfinished(stores, clock)
    person.preferences = {**person.preferences, 'onboarding_availability_draft': {
        **draft, 'source_window_evidence': [{'text': 'unmapped local source restriction'}]}}
    local.commit()
    original = deepcopy(person.preferences)
    row = sync.capture(local, settings, phone=PHONE, guid='actual-review-input',
        route='onboarding_pending_review', before=None, effective_at=clock.now())
    local.commit()
    assert row is not None and row.state == 'held'
    assert row.detail == 'profile_validation_requires_review'
    assert row.payload['profile'] is None
    sync.publish_pending(local, factory, settings)
    assert person.preferences == original
    with factory() as cloud:
        assert not cloud.scalars(select(m.Volunteer)).all()


def test_empty_pending_list_is_valid_but_draft_still_does_not_authorize_full_publish(stores, settings, clock):
    local, person, factory = stores
    draft = unfinished(stores, clock)
    person.preferences = {**person.preferences,
        'onboarding_availability_draft': {**draft, 'pending_constraints': []}}
    row = sync.capture(local, settings, phone=PHONE, guid='empty-pending-input',
        route='onboarding_clarify', before=None, effective_at=clock.now())
    local.commit()
    assert row.payload['profile']['availability_draft']['pending_constraints'] == []
    sync.publish_pending(local, factory, settings)
    assert row.state == 'held' and row.detail == 'incomplete_availability_draft'
    with factory() as cloud:
        assert not cloud.scalars(select(m.Volunteer)).all()


@pytest.mark.parametrize('pending', [
    None,
    [{'kind': 'unknown', 'description': 'not supported'}],
    [{'kind': 'unresolved_window', 'proposal': 'not a window object'}],
    [{'kind': 'validation', 'reason': 'x' * 501}],
    [{'kind': 'same_day', 'description': 'same days', 'role_ids': [999]}],
    [{'kind': 'same_day', 'description': 'same days', 'role_ids': [True]}],
    [{'kind': 'same_day', 'description': 'same days', 'role_ids': [], 'extra': 'authority'}],
    [{'kind': 'validation', 'reason': 'unfinished'}] * 81,
    [{'kind': 'unresolved_window', 'proposal': {'raw': 'x' * 65536}}],
    [{'kind': 'unresolved_window', 'proposal': {'raw': float('nan')}}],
])
def test_malformed_or_unbounded_pending_evidence_is_held(stores, pending, clock):
    local, person, _ = stores
    draft = unfinished(stores, clock)
    person.preferences = {**person.preferences,
        'onboarding_availability_draft': {**draft, 'pending_constraints': pending}}
    assert sync.safe_snapshot(local, PHONE) == {'_held': 'profile_validation_requires_review'}


def test_pending_proposal_is_not_validated_as_a_real_role_or_time(stores, clock):
    local, person, _ = stores
    draft = unfinished(stores, clock)
    raw = {'role_ids': [999], 'start_time': '25:99', 'event_context': {'invented': 'group'}}
    person.preferences = {**person.preferences, 'onboarding_availability_draft': {
        **draft, 'pending_constraints': [{'kind': 'unresolved_window', 'proposal': raw},
            {'kind': 'validation', 'reason': 'Time needs clarification'}]}}
    snapshot = sync.snapshot(local, PHONE)
    assert snapshot['availability_draft']['pending_constraints'][0]['proposal'] == raw
    assert not snapshot['availability'] and 'recurring_windows' not in snapshot['preferences']
