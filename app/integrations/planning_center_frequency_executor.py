"""Opt-in, one-intent membership frequency executor. No runtime registration.

Call only after independent release review. Blockouts/events/permissions are
never executable here. A claimed write is never retried, even after a timeout.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import re

from sqlalchemy import String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Availability, EventType, Role, Volunteer
from app.integrations.planning_center import PCOBase, PCOVolunteerPerson, PlanningCenterError, relation
from app.integrations.planning_center_availability import (
    FrozenSnapshot, MembershipBinding, MONTHLY, OwnedResource, PCOAvailabilityIntent,
    PCOAvailabilityPreview, _hash, _json, read_remote, resource_hash, verify_current,
)


@dataclass(frozen=True)
class FrequencyReview:
    """Caller-authenticated review receipt, bound to one immutable operation."""
    intent_key: str
    preview_hash: str
    source_hash: str
    remote_hash: str
    operation_hash: str
    organization_id: str
    person_id: str
    receipt_hash: str
    silence_evidence_hash: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class FrequencyResult:
    state: str
    reason: str = ''
    ownership: OwnedResource | None = None


class PCOFrequencyClaim(PCOBase):
    """Person mutex: unknown writes retain the claim until reconciled."""
    __tablename__ = 'pco_frequency_claims'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    intent_key: Mapped[str] = mapped_column(String(64), default='')


class PCOFrequencyAttempt(PCOBase):
    """One immutable pre-HTTP claim per intent; no automatic retry counter."""
    __tablename__ = 'pco_frequency_attempts'
    intent_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    document: Mapped[str] = mapped_column(Text)
    document_hash: Mapped[str] = mapped_column(String(64))
    claimed_at: Mapped[datetime]
    verified_at: Mapped[datetime | None]
    readback: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(100), default='outcome_unknown')


class PCOFrequencyOwnership(PCOBase):
    """Verified native baseline for a future explicitly reviewed preview."""
    __tablename__ = 'pco_frequency_ownership'
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    intent_key: Mapped[str] = mapped_column(String(64))
    document: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime]


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PlanningCenterError('frequency_clock_requires_timezone')
    return now


def _load(session, config, key):
    intent = session.get(PCOAvailabilityIntent, key)
    record = session.get(PCOAvailabilityPreview, intent.preview_key) if intent else None
    if not intent or not record:
        raise PlanningCenterError('frequency_preview_or_intent_missing')
    preview = FrozenSnapshot(record.document)
    data = preview.value
    source, remote = FrozenSnapshot.capture(data['source']), FrozenSnapshot.capture(data['remote'])
    verify_current(preview, source, remote)
    if (preview.digest != record.key or record.source_hash != source.digest or
            record.remote_hash != remote.digest or intent.source_hash != source.digest or
            (record.organization_id, record.person_id) != (data['organization_id'], data['person_id']) or
            (intent.organization_id, intent.person_id) != (record.organization_id, record.person_id) or
            config.organization_id != record.organization_id):
        raise PlanningCenterError('frequency_persisted_scope_changed')
    matches = [op for op in data['operations'] if _hash([preview.digest, op]) == key]
    if len(matches) != 1 or intent.document != _json(matches[0]):
        raise PlanningCenterError('frequency_intent_document_changed')
    op = matches[0]
    if intent.resource_key != op['kind'] + ':' + op['logical_key']:
        raise PlanningCenterError('frequency_intent_resource_changed')
    return intent, preview, source, remote, op


def _supported(config, preview, op):
    if op['kind'] != 'membership_frequency' or op['method'] != 'PATCH' or op['state'] != 'preview' or op['holds']:
        raise PlanningCenterError('frequency_operation_not_released')
    data = preview.value
    members = [m for m in data['remote']['memberships']
               if 'role:' + str(m['binding']['role_id']) == op['logical_key']]
    if len(members) != 1:
        raise PlanningCenterError('frequency_membership_ambiguous')
    binding, row = members[0]['binding'], members[0]['resource']
    if (binding['service_type_id'] not in config.service_type_ids or
            any(not re.fullmatch('[1-9][0-9]*', str(binding[field]))
                for field in ('service_type_id', 'team_id', 'position_id', 'membership_id'))):
        raise PlanningCenterError('frequency_membership_outside_scope')
    path = (f"/services/v2/service_types/{binding['service_type_id']}/team_positions/"
            f"{binding['position_id']}/person_team_position_assignments/{binding['membership_id']}")
    caps = [c for c in data['source']['role_frequency_caps'] if c['role_id'] == binding['role_id']]
    if len(caps) != 1 or caps[0]['role_name'] != binding['role_name'] or caps[0]['max_per_month'] not in MONTHLY:
        raise PlanningCenterError('frequency_role_preference_unsupported')
    body = {'data': {'type': 'PersonTeamPositionAssignment',
                     'attributes': {'schedule_preference': MONTHLY[caps[0]['max_per_month']]}}}
    owners = [o for o in data['ownership'] if o['kind'] == 'membership_frequency' and
              o['logical_key'] == op['logical_key'] and o['remote_id'] == row['id'] and
              o['snapshot_hash'] == resource_hash(row)]
    if op['path'] != path or op['body'] != body or len(owners) != 1 or op['expected_remote_hash'] != resource_hash(row):
        raise PlanningCenterError('frequency_owned_baseline_required')
    return binding, row


def _review(review, key, preview, op, now, *, write):
    data = preview.value
    if (not isinstance(review, FrequencyReview) or
            (review.intent_key, review.preview_hash, review.source_hash, review.remote_hash,
             review.operation_hash, review.organization_id, review.person_id) !=
            (key, preview.digest, data['source_hash'], data['remote_hash'], _hash(op),
             data['organization_id'], data['person_id']) or
            not re.fullmatch('[a-f0-9]{64}', review.receipt_hash) or
            not re.fullmatch('[a-f0-9]{64}', review.silence_evidence_hash) or
            review.silence_evidence_hash != data['policy']['evidence_hash'] or
            not data['policy']['membership_silence_verified'] or
            data['policy']['remote_hash'] != data['remote_hash']):
        raise PlanningCenterError('frequency_exact_review_required')
    _aware(review.issued_at); _aware(review.expires_at)
    if not timedelta(0) < review.expires_at - review.issued_at <= timedelta(minutes=15):
        raise PlanningCenterError('frequency_review_lifetime_invalid')
    if write and not review.issued_at <= now < review.expires_at:
        raise PlanningCenterError('frequency_review_expired_or_future')


def _review_hash(review):
    value = asdict(review)
    for key in ('issued_at', 'expires_at'):
        value[key] = value[key].isoformat()
    return _hash(value)


def _lock(session, data):
    # Serializes local claims; no database lock is held across network I/O.
    if session.get_bind().dialect.name == 'sqlite':
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    mutex_key = data['organization_id'] + ':' + data['person_id']
    mutex = session.scalar(select(PCOFrequencyClaim).where(PCOFrequencyClaim.key == mutex_key).with_for_update())
    if mutex is None:
        mutex = PCOFrequencyClaim(key=mutex_key, intent_key='')
        session.add(mutex); session.flush()  # Unique key rejects concurrent first claims.
    session.scalar(select(Volunteer).where(Volunteer.id == data['source']['volunteer_id']).with_for_update())
    session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == data['organization_id'],
        PCOVolunteerPerson.volunteer_id == data['source']['volunteer_id']).with_for_update())
    session.scalars(select(Availability).where(
        Availability.volunteer_id == data['source']['volunteer_id']).with_for_update()).all()
    session.scalars(select(Role).with_for_update()).all()
    session.scalars(select(EventType).with_for_update()).all()
    return mutex


def _bindings(preview):
    return [MembershipBinding(**m['binding']) for m in preview.value['remote']['memberships']]


def _converged(before, after, binding, desired):
    expected = deepcopy(before.value)
    rows = [m['resource'] for m in after.value['memberships'] if m['binding'] == binding]
    if len(rows) != 1:
        return False
    row = rows[0]
    original = next(m['resource'] for m in expected['memberships'] if m['binding'] == binding)
    original['attributes']['schedule_preference'] = desired
    # Only the target revision timestamp may differ in addition to preference.
    if 'updated_at' in original['attributes'] and 'updated_at' in row['attributes']:
        original['attributes']['updated_at'] = row['attributes']['updated_at']
    return expected == after.value


def _attempt_matches(attempt, key, preview, op, review):
    return (attempt is not None and attempt.intent_key == key and
        attempt.document_hash == _hash(json.loads(attempt.document)) and
        json.loads(attempt.document) == {'preview_hash': preview.digest, 'operation_hash': _hash(op),
                                         'review_hash': _review_hash(review)})


def _verified_result(session, config, intent, preview, before, op, binding, review):
    owned = session.get(PCOFrequencyOwnership,
        config.organization_id + ':' + intent.person_id + ':' + op['logical_key'])
    attempt = session.get(PCOFrequencyAttempt, intent.key)
    if (not owned or owned.intent_key != intent.key or
            not _attempt_matches(attempt, intent.key, preview, op, review) or
            not attempt.verified_at or not attempt.readback):
        return FrequencyResult('held', 'verified_ownership_missing_or_superseded')
    readback = FrozenSnapshot(attempt.readback)
    target = next(m['resource'] for m in readback.value['memberships'] if m['binding'] == binding)
    expected_owner = OwnedResource(config.organization_id, intent.person_id, 'membership_frequency',
        op['logical_key'], target['id'], resource_hash(target))
    if (json.loads(owned.document) != asdict(expected_owner) or
            not _converged(before, readback, binding, op['body']['data']['attributes']['schedule_preference'])):
        return FrequencyResult('held', 'verified_readback_or_ownership_changed')
    return FrequencyResult('verified', ownership=expected_owner)


def _finish(factory, key, config, preview, source_reader, remote, binding, row, clock, review):
    with factory() as session:
        mutex = _lock(session, preview.value)
        intent, persisted, _, before, op = _load(session, config, key)
        attempt = session.get(PCOFrequencyAttempt, key)
        if intent.state == 'verified':
            return _verified_result(session, config, intent, persisted, before, op, binding, review)
        if (not _attempt_matches(attempt, key, preview, op, review) or
                mutex.intent_key != key or intent.state != 'unknown'):
            raise PlanningCenterError('frequency_claim_or_attempt_changed')
        now = _aware(clock())
        current = source_reader(session)
        desired = op['body']['data']['attributes']['schedule_preference']
        if current.digest != persisted.value['source_hash']:
            attempt.reason = 'source_changed_after_claim'
        elif not _converged(before, remote, binding, desired):
            attempt.reason = 'native_readback_not_converged'
        else:
            owner = OwnedResource(config.organization_id, intent.person_id, 'membership_frequency',
                                  op['logical_key'], row['id'], resource_hash(row))
            owner_key = config.organization_id + ':' + intent.person_id + ':' + op['logical_key']
            baseline = session.get(PCOFrequencyOwnership, owner_key)
            if baseline is None:
                baseline = PCOFrequencyOwnership(key=owner_key)
                session.add(baseline)
            baseline.intent_key, baseline.document, baseline.verified_at = key, _json(asdict(owner)), now
            attempt.readback, attempt.verified_at, attempt.reason = remote.document, now, ''
            intent.state, mutex.intent_key = 'verified', ''
            session.commit()
            return FrequencyResult('verified', ownership=owner)
        session.commit()
        return FrequencyResult('unknown', attempt.reason)


def execute_frequency_intent(factory, client, config, key, *, source_reader, clock,
                             review=None, enabled=False, reconcile_only=False):
    """Process exactly one intent, disabled by default; no batch/runtime hook.

    source_reader(session) must recapture current committed availability and
    independently validate receipt provenance inside that transaction. It must
    not write. clock returns current aware time. A future caller authenticates
    FrequencyReview; constructing a dataclass is not authorization.
    """
    if enabled is not True:
        return FrequencyResult('disabled')
    config.require_scope()
    try:
        now = _aware(clock())
        with factory() as session:
            intent, preview, source, before, op = _load(session, config, key)
            binding, baseline = _supported(config, preview, op)
            _review(review, key, preview, op, now, write=intent.state == 'preview')
            if intent.state == 'verified':
                return _verified_result(session, config, intent, preview, before, op, binding, review)
            if intent.state not in {'preview', 'unknown'}:
                return FrequencyResult('held', 'intent_not_released')
            unknown = intent.state == 'unknown'
            if reconcile_only and not unknown:
                return FrequencyResult('held', 'no_unknown_outcome_to_reconcile')
            current = source_reader(session)
            if current.digest != source.digest:
                return FrequencyResult('unknown' if unknown else 'held', 'source_changed')
        remote = read_remote(client, config, current, _bindings(preview))
        target = next(m['resource'] for m in remote.value['memberships'] if m['binding'] == binding)
        if unknown:
            return _finish(factory, key, config, preview, source_reader, remote, binding, target, clock, review)
        verify_current(preview, current, remote)
        with factory() as session:
            mutex = _lock(session, preview.value)
            intent, persisted, _, _, locked_op = _load(session, config, key)
            now = _aware(clock())
            _review(review, key, persisted, locked_op, now, write=True)
            if intent.state != 'preview' or session.get(PCOFrequencyAttempt, key):
                return FrequencyResult('held', 'intent_already_claimed')
            other_unknown = session.scalar(select(PCOAvailabilityIntent.key).where(
                PCOAvailabilityIntent.organization_id == config.organization_id,
                PCOAvailabilityIntent.person_id == intent.person_id,
                PCOAvailabilityIntent.state == 'unknown'))
            if mutex.intent_key or other_unknown:
                return FrequencyResult('held', 'person_outcome_requires_reconciliation')
            verify_current(persisted, source_reader(session), remote)
            old_owner = session.get(PCOFrequencyOwnership,
                config.organization_id + ':' + intent.person_id + ':' + op['logical_key'])
            if old_owner and json.loads(old_owner.document)['snapshot_hash'] != resource_hash(baseline):
                return FrequencyResult('held', 'ownership_baseline_superseded')
            document = _json({'preview_hash': preview.digest, 'operation_hash': _hash(op),
                              'review_hash': _review_hash(review)})
            session.add(PCOFrequencyAttempt(intent_key=key, document=document,
                document_hash=_hash(json.loads(document)), claimed_at=now, reason='outcome_unknown'))
            intent.state, mutex.intent_key = 'unknown', key
            session.commit()  # Process death from here requires GET-only reconciliation.
        _review(review, key, preview, op, _aware(clock()), write=True)
        with factory() as session:
            if source_reader(session).digest != source.digest:
                return FrequencyResult('unknown', 'source_changed_before_write')
        _review(review, key, preview, op, _aware(clock()), write=True)
        returned = client.request('PATCH', op['path'], data=op['body'])['data']
        if (returned.get('type') != 'PersonTeamPositionAssignment' or returned.get('id') != baseline['id'] or
                relation(returned, 'person') != preview.value['person_id'] or
                relation(returned, 'team_position') != binding['position_id'] or
                returned.get('attributes', {}).get('schedule_preference') != op['body']['data']['attributes']['schedule_preference']):
            return FrequencyResult('unknown', 'write_response_identity_or_preference_unverified')
        remote = read_remote(client, config, source, _bindings(preview))
        target = next(m['resource'] for m in remote.value['memberships'] if m['binding'] == binding)
        return _finish(factory, key, config, preview, source_reader, remote, binding, target, clock, review)
    except IntegrityError:
        return FrequencyResult('held', 'concurrent_claim_requires_reload')
    except (PlanningCenterError, KeyError, TypeError, ValueError, StopIteration):
        # Responses, credentials and potentially private payloads never escape.
        with factory() as session:
            attempt = session.get(PCOFrequencyAttempt, key)
            return FrequencyResult('unknown' if attempt and not attempt.verified_at else 'held',
                                   'preflight_or_readback_not_verified')
