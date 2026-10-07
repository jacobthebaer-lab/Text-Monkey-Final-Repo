"""Synthetic durable outbox + strict fake Services transport, no live calls."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.models import Availability, Assignment, Message
from app.integrations.planning_center import PCOBase, PCOClient, PCOConfig, PCOVolunteerPerson
from app.integrations.planning_center_availability import (
    MembershipBinding, OwnedResource, PCOAvailabilityIntent, PreviewPolicy, _hash,
    build_preview, capture_source, enqueue_preview, read_remote, resource_hash,
)
from app.integrations.planning_center_frequency_executor import (
    FrequencyReview, PCOFrequencyAttempt, PCOFrequencyClaim, PCOFrequencyOwnership,
    execute_frequency_intent,
)

NOW = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
CONFIG = PCOConfig('synthetic', 'synthetic', '10', ('20',))


def rel(kind, identifier):
    return {'data': {'type': kind, 'id': identifier}}


class Native:
    def __init__(self, bindings, factory):
        self.bindings, self.factory = bindings, factory
        self.rows = {b.membership_id: {'type': 'PersonTeamPositionAssignment', 'id': b.membership_id,
            'attributes': {'schedule_preference': 'Every week', 'preferred_weeks': [1],
                'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-10-01T16:00:00Z'},
            'relationships': {'person': rel('Person', '70'),
                'team_position': rel('TeamPosition', b.position_id),
                'time_preference_options': {'data': []}}} for b in bindings}
        self.requests = []
        self.patch_mode = 'success'
        self.before_patch = None
        self.after_patch = None
        self.fail_readback = False
        self.person_override = None

    def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        assert request.headers['X-PCO-API-Version'] == '2018-11-01'
        if request.method == 'PATCH':
            assert path.endswith('/40/person_team_position_assignments/80')
            assert json.loads(request.content) == {'data': {'type': 'PersonTeamPositionAssignment',
                'attributes': {'schedule_preference': 'Twice a month'}}}
            with self.factory() as session:
                claims = session.scalars(select(PCOFrequencyAttempt)).all()
                assert len(claims) == 1 and claims[0].verified_at is None
                assert session.get(PCOAvailabilityIntent, claims[0].intent_key).state == 'unknown'
                assert session.get(PCOFrequencyClaim, '10:70').intent_key == claims[0].intent_key
            if self.before_patch:
                self.before_patch()
            if self.patch_mode == 'timeout_before':
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            self.rows['80']['attributes']['schedule_preference'] = 'Twice a month'
            self.rows['80']['attributes']['updated_at'] = '2026-10-01T16:01:00Z'
            if self.after_patch:
                self.after_patch()
            if self.patch_mode == 'timeout_after':
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            data = deepcopy(self.rows['80'])
            if self.patch_mode == 'wrong_response':
                data['id'] = '999'
            return httpx.Response(200, json={'data': data})
        assert request.method == 'GET', 'Executor must never POST/DELETE or touch messages/events'
        if path == '/services/v2':
            data = {'type': 'Organization', 'id': '10'}
        elif path == '/services/v2/people/70':
            data = {'type': 'Person', 'id': '70', 'attributes': {'site_administrator': False}}
        elif path == '/services/v2/people/70/blockouts':
            data = []
        else:
            data = None
            for b in self.bindings:
                base = f'/services/v2/service_types/20/team_positions/{b.position_id}'
                if path == f'/services/v2/teams/{b.team_id}':
                    data = {'type': 'Team', 'id': b.team_id, 'attributes': {},
                        'relationships': {'service_type': rel('ServiceType', '20')}}
                elif path == base:
                    data = {'type': 'TeamPosition', 'id': b.position_id,
                        'attributes': {'name': b.position_name}, 'relationships': {'team': rel('Team', b.team_id)}}
                elif path == base + '/person_team_position_assignments/' + b.membership_id:
                    if self.fail_readback and any(m == 'PATCH' for m, _ in self.requests):
                        return httpx.Response(503, json={})
                    data = deepcopy(self.rows[b.membership_id])
                    if self.person_override:
                        data['relationships']['person'] = rel('Person', self.person_override)
            assert data is not None, path
        return httpx.Response(200, json={'data': data, 'links': {}})


@pytest.fixture
def lane(session, make_shift, make_volunteer):
    PCOBase.metadata.create_all(session.get_bind())
    greeter, coffee = make_shift(role_name='Greeter').role, make_shift(role_name='Coffee').role
    volunteer = make_volunteer(prefs={'role_frequency_caps': [
        {'role_id': greeter.id, 'role_name': 'Greeter', 'max_per_month': 2}], 'max_per_month': None})
    session.add(PCOVolunteerPerson(organization_id='10', volunteer_id=volunteer.id,
                                 person_id='70', created_at=NOW))
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    bindings = [MembershipBinding(greeter.id, 'Greeter', '20', '30', '40', 'Greeter', '80'),
                MembershipBinding(coffee.id, 'Coffee', '20', '31', '41', 'Coffee', '81')]
    native = Native(bindings, factory)
    client = PCOClient(CONFIG, transport=httpx.MockTransport(native.handle))
    revision, clock = ['1'], [NOW]
    def source_reader(s):
        return capture_source(s, CONFIG, volunteer_id=volunteer.id,
            provenance={'source_id': 'synthetic-source', 'receipt_id': 'synthetic-receipt', 'revision': revision[0]},
            tz='America/Denver', now=clock[0])
    with factory() as s:
        source = source_reader(s)
        remote = read_remote(client, CONFIG, source, bindings)
        ownership = OwnedResource('10', '70', 'membership_frequency', 'role:' + str(greeter.id),
                                  '80', resource_hash(native.rows['80']))
        preview = build_preview(source, remote, owned=[ownership],
            policy=PreviewPolicy(remote.digest, 'a' * 64, False, True, ''))
        record = enqueue_preview(s, preview, source=source, remote=remote, now=NOW)
        s.commit()
        intent = s.scalar(select(PCOAvailabilityIntent).where(PCOAvailabilityIntent.preview_key == record.key))
        key = intent.key
    op = preview.value['operations'][0]
    review = FrequencyReview(key, preview.digest, source.digest, remote.digest, _hash(op), '10', '70',
                            'b' * 64, 'a' * 64, NOW, NOW + timedelta(minutes=10))
    def run(**kwargs):
        return execute_frequency_intent(factory, client, CONFIG, key,
            source_reader=source_reader, clock=lambda: clock[0], review=kwargs.pop('review', review),
            **kwargs)
    native.requests.clear()
    yield {'run': run, 'native': native, 'factory': factory, 'key': key, 'review': review,
           'revision': revision, 'clock': clock, 'volunteer': volunteer.id, 'source_reader': source_reader,
           'client': client, 'preview': preview}
    client.http.close()


def patches(lane):
    return [p for p in lane['native'].requests if p[0] == 'PATCH']


def test_disabled_performs_no_reads_writes_or_claims(lane):
    assert lane['run']().state == 'disabled'
    assert not lane['native'].requests
    with lane['factory']() as s:
        assert not s.scalars(select(PCOFrequencyAttempt)).all()


def test_reviewed_existing_membership_patch_readback_and_replay(lane):
    coffee = deepcopy(lane['native'].rows['81'])
    result = lane['run'](enabled=True)
    assert result.state == 'verified' and result.ownership.remote_id == '80'
    assert lane['native'].rows['81'] == coffee and len(patches(lane)) == 1
    with lane['factory']() as s:
        assert s.get(PCOAvailabilityIntent, lane['key']).state == 'verified'
        assert s.get(PCOFrequencyAttempt, lane['key']).readback
        assert json.loads(s.scalars(select(PCOFrequencyOwnership)).one().document)['snapshot_hash'] == resource_hash(lane['native'].rows['80'])
        assert s.get(PCOFrequencyClaim, '10:70').intent_key == ''
        assert not s.scalars(select(Message)).all() and not s.scalars(select(Assignment)).all()
    lane['clock'][0] += timedelta(hours=1)
    assert lane['run'](enabled=True).state == 'verified'  # Receipt expiry never causes re-execution.
    assert len(patches(lane)) == 1


@pytest.mark.parametrize('change', ['missing', 'expired', 'future', 'wrong_person', 'wrong_operation', 'wrong_silence'])
def test_release_must_be_exact_current_and_silent(lane, change):
    review = lane['review']
    review = {'missing': None, 'expired': replace(review, issued_at=NOW-timedelta(minutes=11), expires_at=NOW-timedelta(minutes=1)),
        'future': replace(review, issued_at=NOW+timedelta(minutes=1), expires_at=NOW+timedelta(minutes=11)),
        'wrong_person': replace(review, person_id='71'), 'wrong_operation': replace(review, operation_hash='c'*64),
        'wrong_silence': replace(review, silence_evidence_hash='c'*64)}[change]
    assert lane['run'](enabled=True, review=review).state == 'held'
    assert not patches(lane)


@pytest.mark.parametrize('change', ['receipt', 'native', 'mapping', 'intent_document', 'ownership'])
def test_local_native_and_persisted_changes_hold_without_patch(lane, change):
    if change == 'receipt': lane['revision'][0] = '2'
    elif change == 'native': lane['native'].rows['80']['attributes']['preferred_weeks'] = [2]
    else:
        with lane['factory']() as s:
            if change == 'mapping': s.scalars(select(PCOVolunteerPerson)).one().person_id = '71'
            elif change == 'intent_document': s.get(PCOAvailabilityIntent, lane['key']).document = '{}'
            else:
                s.add(PCOFrequencyOwnership(key='10:70:role:1', intent_key='old',
                    document=json.dumps({'snapshot_hash': 'c'*64}), verified_at=NOW))
            s.commit()
    assert lane['run'](enabled=True).state == 'held'
    assert not patches(lane)


@pytest.mark.parametrize('mode', ['timeout_before', 'timeout_after', 'wrong_response'])
def test_uncertain_outcome_is_never_repatched(lane, mode):
    lane['native'].patch_mode = mode
    assert lane['run'](enabled=True).state == 'unknown'
    assert len(patches(lane)) == 1
    lane['clock'][0] += timedelta(hours=1)
    reconciled = lane['run'](enabled=True, reconcile_only=True)
    assert reconciled.state == ('unknown' if mode == 'timeout_before' else 'verified')
    assert len(patches(lane)) == 1


def test_readback_failure_can_reconcile_with_get_only(lane):
    lane['native'].fail_readback = True
    assert lane['run'](enabled=True).state == 'unknown'
    lane['native'].fail_readback = False
    assert lane['run'](enabled=True, reconcile_only=True).state == 'verified'
    assert len(patches(lane)) == 1


@pytest.mark.parametrize('change', ['other_role', 'preferred_weeks', 'wrong_person', 'source'])
def test_readback_detects_unowned_field_identity_or_source_changes(lane, change):
    def mutate():
        if change == 'other_role': lane['native'].rows['81']['attributes']['schedule_preference'] = 'Once a month'
        elif change == 'preferred_weeks': lane['native'].rows['80']['attributes']['preferred_weeks'] = [2]
        elif change == 'wrong_person': lane['native'].person_override = '71'
        else: lane['revision'][0] = '2'
    lane['native'].after_patch = mutate
    assert lane['run'](enabled=True).state == 'unknown'
    assert lane['run'](enabled=True, reconcile_only=True).state == 'unknown'
    assert len(patches(lane)) == 1
    with lane['factory']() as s:
        assert not s.scalars(select(PCOFrequencyOwnership)).all()
        assert s.get(PCOFrequencyClaim, '10:70').intent_key == lane['key']


def test_other_unknown_outcome_and_claim_barriers(lane):
    with lane['factory']() as s:
        current = s.get(PCOAvailabilityIntent, lane['key'])
        s.add(PCOAvailabilityIntent(key='f'*64, preview_key=current.preview_key, organization_id='10',
            person_id='70', resource_key='blockout:date:2026-12-01', source_hash=current.source_hash,
            state='unknown', document='{}', created_at=NOW))
        s.commit()
    assert lane['run'](enabled=True).reason == 'person_outcome_requires_reconciliation'
    assert not patches(lane)


def test_nested_worker_cannot_patch_claimed_intent_twice(lane):
    observed = []
    lane['native'].before_patch = lambda: observed.append(lane['run'](enabled=True).state)
    assert lane['run'](enabled=True).state == 'verified'
    assert observed == ['unknown'] and len(patches(lane)) == 1


def test_concurrent_reconciler_can_finish_readback_before_original_worker(lane):
    observed = []
    lane['native'].after_patch = lambda: observed.append(lane['run'](enabled=True, reconcile_only=True).state)
    assert lane['run'](enabled=True).state == 'verified'
    assert observed == ['verified'] and len(patches(lane)) == 1


def test_reconciliation_only_does_not_start_a_new_write(lane):
    assert lane['run'](enabled=True, reconcile_only=True).state == 'held'
    assert not lane['native'].requests


def test_blockout_remains_held_even_with_a_synthetic_review(lane):
    with lane['factory']() as s:
        s.add(Availability(volunteer_id=lane['volunteer'], month='2026-12',
            available_dates=[], unavailable_dates=['2026-12-01']))
        s.commit()
        source = lane['source_reader'](s)
        before = read_remote(lane['client'], CONFIG, source, [
            MembershipBinding(**m['binding']) for m in lane['preview'].value['remote']['memberships']])
        preview = build_preview(source, before,
            policy=PreviewPolicy(before.digest, 'a'*64, True, True, 'inclusive_local_end_second'))
        record = enqueue_preview(s, preview, source=source, remote=before, now=NOW)
        s.commit()
        intent = s.scalar(select(PCOAvailabilityIntent).where(
            PCOAvailabilityIntent.preview_key == record.key,
            PCOAvailabilityIntent.resource_key.like('blockout:%')))
        op = next(op for op in preview.value['operations'] if op['kind'] == 'blockout')
        review = replace(lane['review'], intent_key=intent.key, preview_hash=preview.digest,
            source_hash=source.digest, remote_hash=before.digest, operation_hash=_hash(op))
        key = intent.key
    lane['native'].requests.clear()
    result = execute_frequency_intent(lane['factory'], lane['client'], CONFIG, key,
        source_reader=lane['source_reader'], clock=lambda: NOW, review=review, enabled=True)
    assert result.state == 'held' and not lane['native'].requests


def test_source_change_during_preflight_is_checked_again_under_claim_lock(lane):
    original = lane['source_reader']
    calls = []
    def changed_reader(s):
        calls.append(1)
        if len(calls) == 2: lane['revision'][0] = '2'
        return original(s)
    result = execute_frequency_intent(lane['factory'], lane['client'], CONFIG, lane['key'],
        source_reader=changed_reader, clock=lambda: NOW, review=lane['review'], enabled=True)
    assert result.state == 'held' and not patches(lane)
    with lane['factory']() as s:
        assert not s.scalars(select(PCOFrequencyAttempt)).all()


def test_source_change_after_durable_claim_suppresses_http_without_releasing_claim(lane):
    original = lane['source_reader']
    calls = []
    def changed_reader(s):
        calls.append(1)
        if len(calls) == 3: lane['revision'][0] = '2'
        return original(s)
    result = execute_frequency_intent(lane['factory'], lane['client'], CONFIG, lane['key'],
        source_reader=changed_reader, clock=lambda: NOW, review=lane['review'], enabled=True)
    assert result.state == 'unknown' and not patches(lane)
    with lane['factory']() as s:
        assert s.get(PCOAvailabilityIntent, lane['key']).state == 'unknown'


def test_expiry_after_claim_prevents_http_and_cannot_automatically_retry(lane):
    calls = []
    def expires():
        calls.append(1)
        return NOW if len(calls) < 3 else NOW + timedelta(minutes=11)
    result = execute_frequency_intent(lane['factory'], lane['client'], CONFIG, lane['key'],
        source_reader=lane['source_reader'], clock=expires, review=lane['review'], enabled=True)
    assert result.state == 'unknown' and not patches(lane)
    assert lane['run'](enabled=True).state == 'unknown' and not patches(lane)


@pytest.mark.parametrize('change', ['claim', 'attempt', 'approval'])
def test_unknown_reconciliation_preserves_claim_provenance(lane, change):
    lane['native'].patch_mode = 'timeout_after'
    assert lane['run'](enabled=True).state == 'unknown'
    review = lane['review']
    if change == 'approval': review = replace(review, receipt_hash='c'*64)
    else:
        with lane['factory']() as s:
            if change == 'claim': s.get(PCOFrequencyClaim, '10:70').intent_key = 'c'*64
            else: s.get(PCOFrequencyAttempt, lane['key']).document = '{}'
            s.commit()
    assert lane['run'](enabled=True, reconcile_only=True, review=review).state == 'unknown'
    assert len(patches(lane)) == 1


def test_verified_replay_checks_persisted_readback_and_ownership(lane):
    assert lane['run'](enabled=True).state == 'verified'
    with lane['factory']() as s:
        owner = s.scalars(select(PCOFrequencyOwnership)).one()
        payload = json.loads(owner.document); payload['remote_id'] = '999'
        owner.document = json.dumps(payload); s.commit()
    assert lane['run'](enabled=True).state == 'held'
    assert len(patches(lane)) == 1
