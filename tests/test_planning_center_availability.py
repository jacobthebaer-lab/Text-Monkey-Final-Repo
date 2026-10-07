"""Synthetic Services fixtures; no credentials, people, phones or live calls."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Availability, Assignment, EventType, Message, Qualification
from app.integrations.planning_center import PCOBase, PCOClient, PCOConfig, PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_availability import (
    FrozenSnapshot, MembershipBinding, OwnedResource, PCOAvailabilityIntent,
    PCOAvailabilityPreview, PreviewPolicy, build_preview, capture_source,
    enqueue_preview, read_remote, resource_hash, verify_current,
)

CONFIG = PCOConfig('synthetic', 'synthetic', '10', ('20',))
PROVENANCE = {'source_id': 'test-source', 'receipt_id': 'synthetic-receipt', 'revision': '1'}
NOW = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)


def rel(kind, identifier):
    return {'data': {'type': kind, 'id': identifier}}


def block(identifier='90', start='2026-12-01T07:00:00Z', end='2027-01-01T07:00:00Z'):
    return {'type': 'Blockout', 'id': identifier, 'attributes': {
        'starts_at': start, 'ends_at': end, 'reason': 'Text Monkey unavailable dates',
        'repeat_frequency': 'no_repeat', 'share': False, 'updated_at': '2026-10-01T16:00:00Z'},
        'relationships': {'person': rel('Person', '70'), 'organization': rel('Organization', '10')}}


class API:
    """Strict HTTP route/method fixture independently shaped from official docs."""
    def __init__(self, bindings):
        self.bindings = bindings
        self.blocks = []
        self.generated = {}
        self.preferences = {b.membership_id: 'Every week' for b in bindings}
        self.person_override = None
        self.organization = '10'
        self.requests = []
        self.team_relationships = {'service_type': rel('ServiceType', '20')}

    def membership(self, binding):
        return {'type': 'PersonTeamPositionAssignment', 'id': binding.membership_id,
            'attributes': {'schedule_preference': self.preferences[binding.membership_id],
                'preferred_weeks': [], 'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-10-01T16:00:00Z'},
            'relationships': {'person': rel('Person', self.person_override or '70'),
                'team_position': rel('TeamPosition', binding.position_id), 'time_preference_options': {'data': []}}}

    def handle(self, request):
        self.requests.append((request.method, request.url.path, request.headers.get('X-PCO-API-Version')))
        assert request.method == 'GET', 'Preview must never issue a remote mutation'
        assert request.headers['X-PCO-API-Version'] == '2018-11-01'
        path = request.url.path
        if path == '/services/v2':
            data = {'type': 'Organization', 'id': self.organization}
        elif path == '/services/v2/people/70':
            data = {'type': 'Person', 'id': '70', 'attributes': {'site_administrator': False}}
        elif path == '/services/v2/people/70/blockouts':
            data = deepcopy(self.blocks)
        elif path.endswith('/blockout_dates'):
            data = deepcopy(self.generated.get(path.split('/')[-2], []))
        else:
            data = None
            for b in self.bindings:
                base = f'/services/v2/service_types/20/team_positions/{b.position_id}'
                if path == f'/services/v2/teams/{b.team_id}':
                    data = {'type': 'Team', 'id': b.team_id, 'attributes': {},
                        'relationships': deepcopy(self.team_relationships)}
                elif path == base:
                    data = {'type': 'TeamPosition', 'id': b.position_id, 'attributes': {'name': b.position_name},
                        'relationships': {'team': rel('Team', b.team_id)}}
                elif path == base + '/person_team_position_assignments/' + b.membership_id:
                    data = self.membership(b)
            assert data is not None, path
        return httpx.Response(200, json={'data': data, 'links': {}})


@pytest.fixture
def setup(session, make_shift, make_volunteer):
    PCOBase.metadata.create_all(session.get_bind())
    greeter = make_shift(role_name='Greeter').role
    coffee = make_shift(role_name='Coffee').role
    group = EventType(name='Synthetic Wednesday Group')
    session.add(group); session.flush()
    prefs = {'onboarding_availability_draft': {
        'availability_known': True, 'frequency_known': True, 'max_per_month': None,
        'weekdays': [6, 2], 'preferred_services': [], 'all_day': False, 'available_dates': [],
        'unavailable_dates': [f'2026-12-{d:02}' for d in range(1, 32)],
        'role_frequency_caps': [{'role_id': greeter.id, 'role_name': 'Greeter', 'max_per_month': 2}],
        'recurring_windows': [
            {'weekday': 6, 'role_ids': [greeter.id], 'role_label': 'Greeter', 'any_role': False,
             'start_time': '08:00', 'end_time': '10:00', 'all_day': False, 'event_context': None},
            {'weekday': 2, 'role_ids': [coffee.id], 'role_label': 'Coffee', 'any_role': False,
             'start_time': None, 'end_time': None, 'all_day': False, 'time_mode': 'event',
             'event_context': {'label': 'Synthetic Wednesday Group', 'event_type_ids': [group.id]}}]}}
    volunteer = make_volunteer(prefs=deepcopy(prefs), quals=[('synthetic_training', 'pending', None)])
    session.add(PCOVolunteerPerson(organization_id='10', volunteer_id=volunteer.id, person_id='70', created_at=NOW))
    session.flush()
    bindings = [MembershipBinding(greeter.id, 'Greeter', '20', '30', '40', 'Greeter', '80'),
                MembershipBinding(coffee.id, 'Coffee', '20', '31', '41', 'Coffee', '81')]
    api = API(bindings)
    def capture(revision='1'):
        return capture_source(session, CONFIG, volunteer_id=volunteer.id,
            provenance={**PROVENANCE, 'revision': revision}, tz='America/Denver', now=NOW)
    def remote(source):
        with PCOClient(CONFIG, transport=httpx.MockTransport(api.handle)) as client:
            return read_remote(client, CONFIG, source, bindings)
    return volunteer, bindings, api, capture, remote


def owned(kind, key, row):
    return OwnedResource('10', '70', kind, key, row['id'], resource_hash(row))


def reviewed(remote):
    return PreviewPolicy(remote.digest, 'a' * 64, True, True, 'inclusive_local_end_second')


def operations(preview, kind):
    return [x for x in preview.value['operations'] if x['kind'] == kind]


def test_preview_december_and_greeter_only_with_unsupported_windows_held(setup, session):
    volunteer, bindings, api, capture, read = setup
    source = capture(); remote = read(source)
    owner = owned('membership_frequency', 'role:' + str(bindings[0].role_id), api.membership(bindings[0]))
    preview = build_preview(source, remote, owned=[owner], policy=reviewed(remote))
    dates = operations(preview, 'blockout')
    assert len(dates) == 1 and dates[0]['method'] == 'POST' and dates[0]['state'] == 'preview'
    attrs = dates[0]['body']['data']['attributes']
    assert attrs['starts_at'] == '2026-12-01T07:00:00Z' and attrs['ends_at'] == '2027-01-01T06:59:59Z'
    assert set(attrs) == {'starts_at', 'ends_at', 'reason', 'share', 'repeat_frequency'}
    assert attrs['share'] is False and attrs['repeat_frequency'] == 'no_repeat'
    frequency = operations(preview, 'membership_frequency')
    assert len(frequency) == 1 and '/40/' in frequency[0]['path']
    assert frequency[0]['body']['data']['attributes'] == {'schedule_preference': 'Twice a month'}
    assert all('/41/' not in op['path'] for op in preview.value['operations'])
    assert len(preview.value['holds']) == 2  # Both clock windows and event-follow context remain local.
    assert source.value['global_max_per_month'] is None
    assert not session.scalars(select(Assignment)).all() and not session.scalars(select(Message)).all()
    assert session.scalars(select(Qualification)).all()[0].status == 'pending'
    assert not any(method != 'GET' for method, _, _ in api.requests)
    assert volunteer.preferences['onboarding_availability_draft']['max_per_month'] is None


@pytest.mark.parametrize('relationships', [
    {'service_type': rel('ServiceType', '20')},
    {'service_type': {'data': None}, 'service_types': {'data': [{'type': 'ServiceType', 'id': '20'}]}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '20'}]}},
    {'service_type': rel('ServiceType', '20'), 'service_types': {'data': [{'type': 'ServiceType', 'id': '20'}]}},
    {'service_type': {'data': None}, 'service_types': {'data': [
        {'type': 'ServiceType', 'id': '21'}, {'type': 'ServiceType', 'id': '20'}]}},
])
def test_documented_team_scopes_still_read_exact_position_and_membership(setup, relationships):
    _, bindings, api, capture, read = setup
    api.team_relationships = relationships
    native = read(capture())
    assert len(native.value['memberships']) == len(bindings)
    for binding in bindings:
        path = f'/services/v2/service_types/20/team_positions/{binding.position_id}'
        assert ('GET', path, '2018-11-01') in api.requests
        assert ('GET', path + '/person_team_position_assignments/' + binding.membership_id,
                '2018-11-01') in api.requests
    assert all(method == 'GET' for method, _, _ in api.requests)


@pytest.mark.parametrize('relationships', [
    {}, None,
    {'service_type': {'data': None}},
    {'service_type': rel('ServiceType', '21')},
    {'service_type': {'data': {'type': 'Team', 'id': '20'}}},
    {'service_type': {}},
    {'service_types': None},
    {'service_types': {'data': None}},
    {'service_types': {'data': []}},
    {'service_types': {'data': {'type': 'ServiceType', 'id': '20'}}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '21'}]}},
    {'service_types': {'data': [{'type': 'Team', 'id': '20'}]}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': 20}]}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': 'invalid'}]}},
    {'service_types': {'data': [None]}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '20'},
                               {'type': 'ServiceType', 'id': '20'}]}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '20'},
                               {'type': 'ServiceType', 'id': 'invalid'}]}},
    {'service_type': rel('ServiceType', '21'),
     'service_types': {'data': [{'type': 'ServiceType', 'id': '20'}]}},
    {'service_type': rel('ServiceType', '20'),
     'service_types': {'data': [{'type': 'ServiceType', 'id': '21'}]}},
    {'service_type': rel('ServiceType', '20'), 'service_types': {'data': None}},
])
def test_missing_malformed_duplicate_or_contradictory_team_scope_is_held(setup, relationships):
    _, _, api, capture, read = setup
    api.team_relationships = relationships
    with pytest.raises(PlanningCenterError, match='availability_team_binding_changed'):
        read(capture())
    assert not any('/team_positions/' in path for _, path, _ in api.requests)


def test_plural_team_scope_does_not_expand_configured_service_allowlist(setup):
    _, bindings, api, capture, _ = setup
    api.team_relationships = {'service_types': {'data': [{'type': 'ServiceType', 'id': '21'}]}}
    outside = MembershipBinding(bindings[0].role_id, 'Greeter', '21', '30', '40', 'Greeter', '80')
    with PCOClient(CONFIG, transport=httpx.MockTransport(api.handle)) as client:
        with pytest.raises(PlanningCenterError, match='availability_membership_binding_invalid'):
            read_remote(client, CONFIG, capture(), [outside])
    assert not any('/teams/' in path for _, path, _ in api.requests)


def test_no_policy_proof_can_claim_silent_blockout(setup):
    _, _, _, capture, read = setup
    source = capture(); preview = build_preview(source, read(source))
    operation = operations(preview, 'blockout')[0]
    assert operation['state'] == 'held'
    assert 'notification_policy_not_verified' in operation['holds']
    assert 'blockout_date_contract_not_verified' in operation['holds']


def test_policy_evidence_cannot_survive_a_different_remote_snapshot(setup):
    _, _, api, capture, read = setup
    source = capture(); first = read(source); policy = reviewed(first)
    api.blocks = [block('99', '2026-11-01T06:00:00Z', '2026-11-02T07:00:00Z')]
    preview = build_preview(source, read(source), policy=policy)
    assert 'notification_policy_not_verified' in operations(preview, 'blockout')[0]['holds']


def test_dedup_unowned_exclusion_preserves_reason_and_never_adopts(setup):
    _, _, api, capture, read = setup
    api.blocks = [block()]; api.blocks[0]['attributes']['reason'] = 'User-owned private reason'
    original = deepcopy(api.blocks)
    source = capture(); preview = build_preview(source, read(source))
    assert operations(preview, 'blockout')[0]['state'] == 'noop'
    assert preview.value['ownership'] == [] and api.blocks == original


def test_union_of_existing_generated_recurrences_prevents_duplicate_blockout(setup):
    _, _, api, capture, read = setup
    row = block(); row['attributes']['repeat_frequency'] = 'every_1'; api.blocks = [row]
    api.generated['90'] = [{'type': 'BlockoutDate', 'id': '100', 'attributes': {
        'starts_at_utc': '2026-12-01T07:00:00Z', 'ends_at_utc': '2027-01-01T07:00:00Z'}}]
    source = capture(); preview = build_preview(source, read(source))
    assert operations(preview, 'blockout')[0]['state'] == 'noop'


def test_unresolved_recurrence_is_a_hold_not_a_duplicate_create(setup):
    _, _, api, capture, read = setup
    row = block(); row['attributes']['repeat_frequency'] = 'every_1'; api.blocks = [row]
    source = capture(); preview = build_preview(source, read(source), policy=reviewed(read(source)))
    op = operations(preview, 'blockout')[0]
    assert op['state'] == 'held' and 'recurring_blockout_coverage_unresolved' in op['holds']


def test_owned_upsert_and_removal_require_exact_native_baseline(setup):
    volunteer, _, api, capture, read = setup
    row = block(); api.blocks = [row]; owner = owned('blockout', 'date:2026-12-01', row)
    prefs = deepcopy(volunteer.preferences); prefs['onboarding_availability_draft']['unavailable_dates'] = ['2026-12-01']
    volunteer.preferences = prefs
    source = capture('2'); remote = read(source)
    preview = build_preview(source, remote, owned=[owner], policy=reviewed(remote))
    assert operations(preview, 'blockout')[0]['method'] == 'PATCH'
    assert operations(preview, 'blockout')[0]['body']['data']['attributes']['ends_at'] == '2026-12-02T06:59:59Z'
    api.blocks[0]['attributes']['reason'] = 'Changed by an administrator'
    remote = read(source); edited = build_preview(source, remote, owned=[owner], policy=reviewed(remote))
    assert operations(edited, 'blockout')[0]['state'] == 'conflict'
    prefs['onboarding_availability_draft']['unavailable_dates'] = []; volunteer.preferences = deepcopy(prefs)
    source = capture('3'); remote = read(source)
    removal = build_preview(source, remote, owned=[owner], policy=reviewed(remote))
    assert operations(removal, 'blockout')[0]['method'] == 'DELETE'
    assert operations(removal, 'blockout')[0]['state'] == 'conflict'
    api.blocks = [block()]  # Restore the exact owned baseline, not the edited shared object.
    remote = read(source); removal = build_preview(source, remote, owned=[owner], policy=reviewed(remote))
    assert operations(removal, 'blockout')[0]['state'] == 'preview'


def test_absent_availability_is_not_permission_to_delete_owned_blockouts(setup):
    volunteer, _, api, capture, read = setup
    volunteer.preferences = {}; api.blocks = [block()]
    source = capture(); remote = read(source)
    preview = build_preview(source, remote, owned=[owned('blockout', 'date:2026-12-01', api.blocks[0])], policy=reviewed(remote))
    assert not operations(preview, 'blockout')
    assert any(h['reason'] == 'absence_is_not_an_authoritative_clear' for h in preview.value['holds'])


@pytest.mark.parametrize('edited', [False, True])
def test_unowned_or_user_edited_membership_is_preserved(setup, edited):
    _, bindings, api, capture, read = setup
    source = capture(); remote = read(source)
    owners = []
    if edited:
        owners = [owned('membership_frequency', 'role:' + str(bindings[0].role_id), api.membership(bindings[0]))]
        api.preferences['80'] = 'Once a month'; remote = read(source)
    preview = build_preview(source, remote, owned=owners, policy=reviewed(remote))
    operation = operations(preview, 'membership_frequency')[0]
    assert operation['state'] == ('conflict' if edited else 'held')


def test_equal_unowned_frequency_is_noop_and_removed_cap_cannot_delete_membership(setup):
    volunteer, bindings, api, capture, read = setup
    api.preferences['80'] = 'Twice a month'
    source = capture(); remote = read(source)
    assert operations(build_preview(source, remote), 'membership_frequency')[0]['state'] == 'noop'
    owner = owned('membership_frequency', 'role:' + str(bindings[0].role_id), api.membership(bindings[0]))
    prefs = deepcopy(volunteer.preferences); prefs['onboarding_availability_draft']['role_frequency_caps'] = []
    volunteer.preferences = prefs; source = capture('2'); remote = read(source)
    preview = build_preview(source, remote, owned=[owner])
    assert not operations(preview, 'membership_frequency')
    assert any(h['reason'] == 'removed_cap_requires_reviewed_restore' for h in preview.value['holds'])


def test_unsupported_frequency_and_global_cap_never_broaden_other_roles(setup):
    volunteer, _, _, capture, read = setup
    prefs = deepcopy(volunteer.preferences); draft = prefs['onboarding_availability_draft']
    draft['role_frequency_caps'][0]['max_per_month'] = 4; draft['max_per_month'] = 2
    volunteer.preferences = prefs; source = capture(); preview = build_preview(source, read(source))
    assert not operations(preview, 'membership_frequency')
    reasons = {h['reason'] for h in preview.value['holds']}
    assert {'native_frequency_not_exactly_representable', 'no_supported_global_frequency_write'} <= reasons


def test_local_date_bounds_honor_dst_and_do_not_invent_time_zone_api_field(setup):
    volunteer, _, _, capture, read = setup
    prefs = deepcopy(volunteer.preferences); prefs['onboarding_availability_draft']['unavailable_dates'] = ['2026-11-01']
    volunteer.preferences = prefs; source = capture(); remote = read(source)
    attrs = operations(build_preview(source, remote), 'blockout')[0]['body']['data']['attributes']
    assert attrs['starts_at'] == '2026-11-01T06:00:00Z' and attrs['ends_at'] == '2026-11-02T06:59:59Z'
    assert 'time_zone' not in attrs and 'all_day' not in attrs


@pytest.mark.parametrize('dates, start, end', [
    (['2026-10-18'], '2026-10-18T06:00:00Z', '2026-10-19T05:59:59Z'),
    (['2026-10-18', '2026-10-19'], '2026-10-18T06:00:00Z', '2026-10-20T05:59:59Z'),
    (['2026-11-01'], '2026-11-01T06:00:00Z', '2026-11-02T06:59:59Z'),
    (['2027-03-14'], '2027-03-14T07:00:00Z', '2027-03-15T05:59:59Z'),
])
def test_finite_blockout_ends_on_last_second_of_final_local_date(setup, dates, start, end):
    volunteer, _, _, capture, read = setup
    prefs = deepcopy(volunteer.preferences)
    prefs['onboarding_availability_draft']['unavailable_dates'] = dates
    volunteer.preferences = prefs
    source = capture()
    operations_ = operations(build_preview(source, read(source)), 'blockout')
    assert len(operations_) == 1
    attrs = operations_[0]['body']['data']['attributes']
    assert (attrs['starts_at'], attrs['ends_at']) == (start, end)
    assert datetime.fromisoformat(end).astimezone(ZoneInfo('America/Denver')).strftime('%Y-%m-%d %H:%M:%S') == dates[-1] + ' 23:59:59'


def test_finite_date_preview_native_cache_keeps_adjacent_day_available(setup, session):
    from app.integrations.planning_center_sync import refresh_mapped_availability, native_availability_problem
    volunteer, _, api, capture, read = setup
    prefs = deepcopy(volunteer.preferences)
    prefs['onboarding_availability_draft']['unavailable_dates'] = ['2026-10-18', '2026-10-20']
    volunteer.preferences = prefs
    source = capture()
    attrs = [op['body']['data']['attributes'] for op in operations(build_preview(source, read(source)), 'blockout')]
    assert len(attrs) == 2
    api.blocks = [block(str(90 + index), value['starts_at'], value['ends_at']) for index, value in enumerate(attrs)]
    api.generated = {row['id']: [{'type': 'BlockoutDate', 'attributes': {
        'starts_at_utc': row['attributes']['starts_at'], 'ends_at_utc': row['attributes']['ends_at'], 'time_zone': 'America/Denver'}}] for row in api.blocks}
    with PCOClient(CONFIG, transport=httpx.MockTransport(api.handle)) as client:
        assert refresh_mapped_availability(session, client, CONFIG, NOW)['refreshed'] == 1
    for day in (18, 20):
        start = datetime(2026, 10, day, 23, 59, 59, tzinfo=ZoneInfo('America/Denver'))
        assert native_availability_problem(session, volunteer, SimpleNamespace(
            starts_at=start, ends_at=start + timedelta(seconds=1))) == 'Unavailable in Planning Center for this interval'
    for day in (18, 19, 20, 21):
        start = datetime(2026, 10, day, tzinfo=ZoneInfo('America/Denver'))
        shift = SimpleNamespace(starts_at=start, ends_at=start + timedelta(hours=1))
        expected = 'Unavailable in Planning Center for this interval' if day in (18, 20) else None
        assert native_availability_problem(session, volunteer, shift) == expected


def test_source_immutable_and_changed_receipt_or_native_state_rejects_preview(setup):
    _, _, api, capture, read = setup
    source = capture(); remote = read(source); preview = build_preview(source, remote)
    detached = source.value; detached['unavailable_dates'].clear()
    assert len(source.value['unavailable_dates']) == 31
    assert 'phone' not in source.value and 'name' not in source.value and 'raw_reply' not in source.document
    with pytest.raises(FrozenInstanceError): source.document = '{}'
    with pytest.raises(PlanningCenterError, match='source_or_remote_changed'):
        verify_current(preview, capture('2'), remote)
    api.preferences['80'] = 'Once a month'
    with pytest.raises(PlanningCenterError, match='source_or_remote_changed'):
        verify_current(preview, source, read(source))
    value = preview.value; value['operations'][0]['body']['data']['attributes']['share'] = True
    with pytest.raises(PlanningCenterError, match='operations_changed'):
        verify_current(FrozenSnapshot.capture(value), source, remote)


def test_scope_and_membership_person_mismatches_fail_before_outbox(setup):
    _, _, api, capture, read = setup
    source = capture(); api.organization = '11'
    with pytest.raises(PlanningCenterError, match='organization_mismatch'): read(source)
    api.organization = '10'; api.person_override = '71'
    with pytest.raises(PlanningCenterError, match='membership_person'): read(source)


def test_missing_mapping_and_provenance_are_held(setup, session):
    volunteer, _, _, _, _ = setup
    with pytest.raises(PlanningCenterError, match='provenance_required'):
        capture_source(session, CONFIG, volunteer_id=volunteer.id, provenance={}, tz='America/Denver', now=NOW)
    with pytest.raises(PlanningCenterError, match='person_mapping_required'):
        capture_source(session, CONFIG, volunteer_id=999, provenance=PROVENANCE, tz='America/Denver', now=NOW)


def test_outbox_replay_dedup_source_supersession_and_unknown_cross_range_barrier(setup, session):
    volunteer, _, _, capture, read = setup
    source = capture(); remote = read(source); preview = build_preview(source, remote)
    first = enqueue_preview(session, preview, source=source, remote=remote, now=NOW)
    assert enqueue_preview(session, preview, source=source, remote=remote, now=NOW).key == first.key
    assert len(session.scalars(select(PCOAvailabilityPreview)).all()) == 1
    original = session.scalars(select(PCOAvailabilityIntent)).all()
    assert len(original) == 2 and all(row.state == 'held' for row in original)
    second_source = capture('2'); second_preview = build_preview(second_source, remote)
    enqueue_preview(session, second_preview, source=second_source, remote=remote, now=NOW)
    assert all(row.state == 'superseded' for row in original)
    active = session.scalars(select(PCOAvailabilityIntent).where(PCOAvailabilityIntent.state == 'held')).all()
    active[0].state = 'unknown'; session.flush()
    prefs = deepcopy(volunteer.preferences); prefs['onboarding_availability_draft']['unavailable_dates'].pop(0)
    volunteer.preferences = prefs; third_source = capture('3'); third_preview = build_preview(third_source, remote)
    third = enqueue_preview(session, third_preview, source=third_source, remote=remote, now=NOW)
    rows = session.scalars(select(PCOAvailabilityIntent).where(PCOAvailabilityIntent.preview_key == third.key)).all()
    assert all(row.state == 'held' and 'previous_outcome_requires_reconciliation' in json.loads(row.document)['holds'] for row in rows)
    assert len(session.scalars(select(PCOAvailabilityIntent).where(PCOAvailabilityIntent.state == 'unknown')).all()) == 1
    assert not session.scalars(select(Assignment)).all() and not session.scalars(select(Message)).all()


def test_preview_outbox_participates_in_caller_transaction(setup, session):
    _, _, _, capture, read = setup
    source = capture(); remote = read(source); preview = build_preview(source, remote)
    session.commit()
    enqueue_preview(session, preview, source=source, remote=remote, now=NOW)
    session.rollback()
    assert not session.scalars(select(PCOAvailabilityIntent)).all()
    assert not session.scalars(select(PCOAvailabilityPreview)).all()


@pytest.mark.parametrize('dates', [['2026-12-32'], ['2026-1-1'], ['2026-09-30']])
def test_malformed_or_expired_exclusions_cannot_make_a_preview(setup, dates):
    volunteer, _, _, capture, _ = setup
    prefs = deepcopy(volunteer.preferences)
    prefs['onboarding_availability_draft']['unavailable_dates'] = dates
    volunteer.preferences = prefs
    with pytest.raises(PlanningCenterError, match='invalid_exclusion_dates'):
        capture()


def test_invalid_timezone_and_provenance_use_explicit_hold_errors(setup, session):
    volunteer, _, _, _, _ = setup
    with pytest.raises(PlanningCenterError, match='timezone_invalid'):
        capture_source(session, CONFIG, volunteer_id=volunteer.id,
            provenance=PROVENANCE, tz='Synthetic/NoSuchZone', now=NOW)
    with pytest.raises(PlanningCenterError, match='provenance_required'):
        capture_source(session, CONFIG, volunteer_id=volunteer.id,
            provenance=None, tz='America/Denver', now=NOW)


def test_duplicate_paginated_blockouts_cannot_be_collapsed_silently(setup):
    _, _, api, capture, read = setup
    api.blocks = [block(), block()]
    with pytest.raises(PlanningCenterError, match='duplicate_remote_blockout'):
        read(capture())


def test_same_native_membership_cannot_bind_two_local_roles(setup):
    _, bindings, api, capture, _ = setup
    first = bindings[0]
    duplicate = MembershipBinding(bindings[1].role_id, 'Coffee', first.service_type_id,
        first.team_id, first.position_id, first.position_name, first.membership_id)
    with PCOClient(CONFIG, transport=httpx.MockTransport(api.handle)) as client:
        with pytest.raises(PlanningCenterError, match='membership_binding_invalid'):
            read_remote(client, CONFIG, capture(), [first, duplicate])


def test_detached_remote_snapshot_must_preserve_all_membership_identities(setup):
    _, _, _, capture, read = setup
    source = capture(); native = read(source).value
    # Even a role with no requested frequency cannot carry the wrong person.
    native['memberships'][1]['resource']['relationships']['person'] = rel('Person', '71')
    with pytest.raises(PlanningCenterError, match='preview_membership_scope_mismatch'):
        build_preview(source, FrozenSnapshot.capture(native))
    native['memberships'][1]['resource']['relationships']['person'] = rel('Person', '70')
    native['memberships'][0]['resource']['id'] = '999'
    with pytest.raises(PlanningCenterError, match='preview_membership_scope_mismatch'):
        build_preview(source, FrozenSnapshot.capture(native))


@pytest.mark.parametrize('day', ['2026-11-01', '2027-03-14'])
def test_native_finite_day_end_normalizes_to_exclusive_midnight_across_dst(setup, session, day):
    from app.integrations.planning_center_sync import refresh_mapped_availability, native_availability_problem
    volunteer, _, api, capture, read = setup
    prefs = deepcopy(volunteer.preferences)
    prefs['onboarding_availability_draft']['unavailable_dates'] = [day]
    volunteer.preferences = prefs
    source = capture()
    attrs = operations(build_preview(source, read(source)), 'blockout')[0]['body']['data']['attributes']
    api.blocks = [block('90', attrs['starts_at'], attrs['ends_at'])]
    api.generated = {'90': [{'type': 'BlockoutDate', 'attributes': {
        'starts_at_utc': attrs['starts_at'], 'ends_at_utc': attrs['ends_at'], 'time_zone': 'America/Denver'}}]}
    with PCOClient(CONFIG, transport=httpx.MockTransport(api.handle)) as client:
        assert refresh_mapped_availability(session, client, CONFIG, NOW)['refreshed'] == 1
    end = datetime.fromisoformat(attrs['ends_at'])
    assert native_availability_problem(session, volunteer, SimpleNamespace(
        starts_at=end, ends_at=end + timedelta(seconds=1))) == 'Unavailable in Planning Center for this interval'
    assert native_availability_problem(session, volunteer, SimpleNamespace(
        starts_at=end + timedelta(seconds=1), ends_at=end + timedelta(hours=1))) is None


@pytest.mark.parametrize('frequency, end, zone', [
    ('no_repeat', '2026-10-18T17:00:00Z', 'America/Denver'),
    ('weekly', '2026-10-19T05:59:59Z', 'America/Denver'),
    ('no_repeat', '2026-10-19T05:59:59Z', None),
])
def test_native_timed_recurring_or_unknown_zone_end_is_not_broadened(setup, session, frequency, end, zone):
    from app.integrations.planning_center_sync import refresh_mapped_availability, native_availability_problem
    volunteer, _, api, _, _ = setup
    api.blocks = [block('90', '2026-10-18T06:00:00Z', end)]
    api.blocks[0]['attributes']['repeat_frequency'] = frequency
    api.generated = {'90': [{'type': 'BlockoutDate', 'attributes': {
        'starts_at_utc': '2026-10-18T06:00:00Z', 'ends_at_utc': end, 'time_zone': zone}}]}
    with PCOClient(CONFIG, transport=httpx.MockTransport(api.handle)) as client:
        assert refresh_mapped_availability(session, client, CONFIG, NOW)['refreshed'] == 1
    start = datetime.fromisoformat(end)
    assert native_availability_problem(session, volunteer, SimpleNamespace(
        starts_at=start, ends_at=start + timedelta(seconds=1))) is None


def test_old_exclusive_midnight_contract_stays_held_and_old_preview_cannot_be_reused(setup):
    _, _, _, capture, read = setup
    source = capture(); remote = read(source)
    policy = PreviewPolicy(remote.digest, 'a' * 64, True, True, 'exclusive_local_midnight')
    preview = build_preview(source, remote, policy=policy)
    operation = operations(preview, 'blockout')[0]
    assert operation['state'] == 'held' and 'blockout_date_contract_not_verified' in operation['holds']
    old = preview.value
    old['operations'][0]['body']['data']['attributes']['ends_at'] = '2027-01-01T07:00:00Z'
    with pytest.raises(PlanningCenterError, match='availability_preview_operations_changed'):
        verify_current(FrozenSnapshot.capture(old), source, remote)
