"""Provenance-bound Services availability PREVIEW/outbox. No remote writer.

Nothing imports this module automatically. A caller must explicitly supply the
verified person/membership mappings, source receipt and complete remote reads.
Unsupported availability stays local; neither consent nor staffing is changed.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.core.onboarding import availability_context
from app.core.recurring_availability import normalize_recurring_windows, normalize_role_frequency_caps
from app.db.models import Availability, EventType, Role, Volunteer
from app.integrations.planning_center import (
    PCOBase, PCOVolunteerPerson, PlanningCenterError, _id, _time, relation, team_service_scope_matches,
)

MONTHLY = {1: 'Once a month', 2: 'Twice a month', 3: 'Three times a month'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class FrozenSnapshot:
    """A JSON string cannot be mutated through a caller's original dictionary."""
    document: str

    @classmethod
    def capture(cls, value):
        return cls(_json(value))

    @property
    def value(self):
        return json.loads(self.document)

    @property
    def digest(self):
        return _hash(self.value)


@dataclass(frozen=True)
class MembershipBinding:
    role_id: int
    role_name: str
    service_type_id: str
    team_id: str
    position_id: str
    position_name: str
    membership_id: str


@dataclass(frozen=True)
class OwnedResource:
    """Only a previously reviewed, verified write can establish ownership.

    An identical unowned blockout can satisfy an exclusion, but cannot be
    adopted, changed or deleted. snapshot_hash includes the remote revision.
    """
    organization_id: str
    person_id: str
    kind: str
    logical_key: str
    remote_id: str
    snapshot_hash: str


@dataclass(frozen=True)
class PreviewPolicy:
    """Review evidence is bound to this exact complete remote snapshot.

    These flags do not authorize execution. Blockout notifications/automatic
    declines and date-boundary semantics need separate native acceptance proof.
    """
    remote_hash: str = ''
    evidence_hash: str = ''
    blockout_silence_verified: bool = False
    membership_silence_verified: bool = False
    date_contract: str = ''


class PCOAvailabilityPreview(PCOBase):
    __tablename__ = 'pco_availability_previews'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    person_id: Mapped[str] = mapped_column(String(40), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    remote_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class PCOAvailabilityIntent(PCOBase):
    __tablename__ = 'pco_availability_intents'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    preview_key: Mapped[str] = mapped_column(String(64), index=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    person_id: Mapped[str] = mapped_column(String(40), index=True)
    resource_key: Mapped[str] = mapped_column(String(180), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))  # preview/held/noop/conflict; never runnable
    document: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


def capture_source(session, config, *, volunteer_id, provenance, tz, now):
    """Reuse validated saved availability, without phones or raw conversations.

    The caller obtains provenance from the committed interpreter/profile receipt,
    not from a guessed timestamp. No mappings or schema are created here.
    """
    config.require_scope()
    if (not isinstance(provenance, dict) or set(provenance) != {'source_id', 'receipt_id', 'revision'} or
            not all(isinstance(v, str) and 0 < len(v) <= 180 for v in provenance.values())):
        raise PlanningCenterError('availability_source_provenance_required')
    if now.tzinfo is None:
        raise PlanningCenterError('availability_source_clock_requires_timezone')
    try:
        zone = ZoneInfo(tz)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise PlanningCenterError('availability_source_timezone_invalid') from exc
    volunteer = session.get(Volunteer, volunteer_id)
    mapping = session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == config.organization_id,
        PCOVolunteerPerson.volunteer_id == volunteer_id))
    if volunteer is None or mapping is None:
        raise PlanningCenterError('availability_verified_person_mapping_required')
    today = now.astimezone(zone).date()
    try:
        saved = availability_context(session, volunteer, today)
        roles = session.scalars(select(Role)).all()
        windows = normalize_recurring_windows(saved.get('recurring_windows', []), roles,
                                              session.scalars(select(EventType)).all())
        caps = normalize_role_frequency_caps(saved.get('role_frequency_caps', []), roles)
    except (TypeError, ValueError) as exc:
        raise PlanningCenterError('availability_saved_source_invalid') from exc
    dates = saved.get('unavailable_dates', [])
    try:
        valid_dates = (isinstance(dates, list) and len(dates) <= 366 and
            all(isinstance(d, str) and date.fromisoformat(d).isoformat() == d and
                0 <= (date.fromisoformat(d) - today).days <= 366 for d in dates))
    except ValueError:
        valid_dates = False
    if not valid_dates:
        raise PlanningCenterError('availability_invalid_exclusion_dates')
    maximum = saved.get('max_per_month')
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= 8):
        raise PlanningCenterError('availability_invalid_global_frequency')
    draft = (volunteer.preferences or {}).get('onboarding_availability_draft')
    authoritative_dates = ('unavailable_dates' in draft if isinstance(draft, dict) else
        session.scalar(select(Availability.id).where(Availability.volunteer_id == volunteer_id)) is not None)
    return FrozenSnapshot.capture({'organization_id': config.organization_id,
        'person_id': _id(mapping.person_id), 'volunteer_id': volunteer_id,
        'provenance': provenance, 'timezone': tz,
        'unavailable_dates': sorted(set(dates)), 'dates_authoritative': authoritative_dates,
        'role_frequency_caps': sorted(caps, key=lambda c: c['role_id']),
        'global_max_per_month': maximum, 'recurring_windows': windows})


def _resource(client, path, kind, identifier):
    row = client.request('GET', path)['data']
    if row.get('type') != kind or row.get('id') != str(identifier):
        raise PlanningCenterError('availability_remote_identity_changed')
    return row


def read_remote(client, config, source, bindings=()):
    """Only GET calls, through the existing scoped/paginated PCO client."""
    config.require_scope()
    src = source.value
    if src['organization_id'] != config.organization_id or client.organization()['id'] != config.organization_id:
        raise PlanningCenterError('availability_organization_mismatch')
    pid = _id(src['person_id'])
    _resource(client, f'/services/v2/people/{pid}', 'Person', pid)
    base = f'/services/v2/people/{pid}/blockouts'
    blocks = client.collection(base)  # No future filter: never mistake hidden rows for absence.
    dates = {}
    for block in blocks:
        if (block.get('type') != 'Blockout' or relation(block, 'person') != pid or
                relation(block, 'organization') != config.organization_id):
            raise PlanningCenterError('availability_blockout_scope_mismatch')
        identifier = _id(block['id'])
        if identifier in dates:
            raise PlanningCenterError('availability_duplicate_remote_blockout')
        dates[identifier] = client.collection(base + '/' + identifier + '/blockout_dates')
        if any(d.get('type') != 'BlockoutDate' for d in dates[block['id']]):
            raise PlanningCenterError('availability_invalid_generated_blockout_date')
    memberships, seen, resources = [], set(), set()
    for binding in sorted(bindings, key=lambda b: b.role_id):
        if (type(binding.role_id) is not int or binding.role_id <= 0 or binding.role_id in seen or
                binding.service_type_id not in config.service_type_ids or
                (binding.service_type_id, binding.position_id, binding.membership_id) in resources):
            raise PlanningCenterError('availability_membership_binding_invalid')
        seen.add(binding.role_id)
        resources.add((binding.service_type_id, binding.position_id, binding.membership_id))
        for identifier in (binding.team_id, binding.position_id, binding.membership_id):
            _id(identifier)
        team = _resource(client, f'/services/v2/teams/{binding.team_id}', 'Team', binding.team_id)
        if (not team_service_scope_matches(team, binding.service_type_id) or
                team['attributes'].get('deleted_at') or team['attributes'].get('archived_at')):
            raise PlanningCenterError('availability_team_binding_changed')
        base = f'/services/v2/service_types/{binding.service_type_id}/team_positions/{binding.position_id}'
        position = _resource(client, base, 'TeamPosition', binding.position_id)
        if (relation(position, 'team') != binding.team_id or
                position['attributes'].get('name') != binding.position_name):
            raise PlanningCenterError('availability_position_binding_changed')
        row = _resource(client, base + '/person_team_position_assignments/' + binding.membership_id,
                        'PersonTeamPositionAssignment', binding.membership_id)
        if relation(row, 'person') != pid or relation(row, 'team_position') != binding.position_id:
            raise PlanningCenterError('availability_membership_person_or_position_changed')
        memberships.append({'binding': asdict(binding), 'resource': row})
    return FrozenSnapshot.capture({'organization_id': config.organization_id, 'person_id': pid,
        'blockouts': blocks, 'blockout_dates': dates, 'memberships': memberships})


def resource_hash(row):
    """Includes revision and all fields: user edits cannot be overwritten."""
    return _hash(row)


def _runs(dates):
    result = []
    for value in dates:
        day = date.fromisoformat(value)
        if result and day == result[-1][1] + timedelta(days=1):
            result[-1] = (result[-1][0], day)
        else:
            result.append((day, day))
    return result


def _utc(day, zone):
    return datetime.combine(day, time.min, zone).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _covers(intervals, start, end):
    cursor = start
    for a, b in sorted(intervals):
        if a > cursor:
            break
        if b > cursor:
            cursor = b
        if cursor >= end:
            return True
    return False


def build_preview(source, remote, *, owned=(), policy=PreviewPolicy()):
    """Diff immutable source/native state; safe operations remain PREVIEW only.

    Native changes yield conflicts, never silently rewrite local preferences.
    Preview does not declare any application limit to be a native hard cap.
    """
    src, native = source.value, remote.value
    if (src['organization_id'], src['person_id']) != (native['organization_id'], native['person_id']):
        raise PlanningCenterError('availability_preview_scope_mismatch')
    owner_rows = [asdict(x) for x in owned]
    owners = {}
    for item in owned:
        if ((item.organization_id, item.person_id) != (src['organization_id'], src['person_id']) or
                item.kind not in {'blockout', 'membership_frequency'} or
                not re.fullmatch('[a-f0-9]{64}', item.snapshot_hash) or
                (item.kind, item.logical_key) in owners):
            raise PlanningCenterError('availability_ownership_scope_invalid')
        _id(item.remote_id)
        if ((item.kind == 'membership_frequency' and not re.fullmatch(r'role:[1-9][0-9]*', item.logical_key)) or
                (item.kind == 'blockout' and not re.fullmatch(r'date:\d{4}-\d{2}-\d{2}', item.logical_key))):
            raise PlanningCenterError('availability_ownership_key_invalid')
        owners[item.kind, item.logical_key] = item
    if len({(o.kind, o.remote_id) for o in owned}) != len(owned):
        raise PlanningCenterError('availability_ownership_duplicate_resource')
    evidence = (policy.remote_hash == remote.digest and
                bool(re.fullmatch('[a-f0-9]{64}', policy.evidence_hash)))
    operations, holds = [], []
    pid, org = src['person_id'], src['organization_id']
    base = f'/services/v2/people/{pid}/blockouts'
    blocks = {row['id']: row for row in native['blockouts']}
    if len(blocks) != len(native['blockouts']):
        raise PlanningCenterError('availability_duplicate_remote_blockout')
    owned_ids = {o.remote_id for o in owned if o.kind == 'blockout'}
    intervals, unknown_coverage = [], False
    for identifier, row in blocks.items():
        if row.get('type') != 'Blockout' or relation(row, 'person') != pid or relation(row, 'organization') != org:
            raise PlanningCenterError('availability_preview_blockout_scope_mismatch')
        if identifier in owned_ids:
            continue
        generated = native['blockout_dates'].get(identifier, [])
        if generated:
            for generated_row in generated:
                if generated_row.get('type') != 'BlockoutDate':
                    raise PlanningCenterError('availability_invalid_generated_blockout_date')
                a = generated_row['attributes']
                intervals.append((_time(a['starts_at_utc']), _time(a['ends_at_utc'])))
        elif row['attributes'].get('repeat_frequency') == 'no_repeat':
            a = row['attributes']
            intervals.append((_time(a['starts_at']), _time(a['ends_at'])))
        else:
            unknown_coverage = True
    def add(kind, logical, method, path, body=None, expected=None, reasons=(), noop=False):
        reasons = list(reasons)
        if not noop and not any(r.startswith('conflict_') for r in reasons):
            flag = policy.blockout_silence_verified if kind == 'blockout' else policy.membership_silence_verified
            if not evidence or not flag:
                reasons.append('notification_policy_not_verified')
            if kind == 'blockout' and (not evidence or policy.date_contract != 'exclusive_local_midnight'):
                reasons.append('blockout_date_contract_not_verified')
        state = ('noop' if noop else 'conflict' if any(r.startswith('conflict_') for r in reasons)
                 else 'held' if reasons else 'preview')
        operations.append({'kind': kind, 'logical_key': logical, 'method': method, 'path': path,
            'body': body, 'expected_remote_hash': resource_hash(expected) if expected else None,
            'state': state, 'holds': reasons})
    desired_keys = set()
    if src['dates_authoritative']:
        zone = ZoneInfo(src['timezone'])
        for first, last in _runs(src['unavailable_dates']):
            logical = 'date:' + first.isoformat()
            desired_keys.add(logical)
            body = {'data': {'type': 'Blockout', 'attributes': {
                'starts_at': _utc(first, zone), 'ends_at': _utc(last + timedelta(days=1), zone),
                'repeat_frequency': 'no_repeat', 'share': False, 'reason': 'Text Monkey unavailable dates'}}}
            owner = owners.get(('blockout', logical))
            if owner:
                current = blocks.get(owner.remote_id)
                path = base + '/' + owner.remote_id
                if current is None or resource_hash(current) != owner.snapshot_hash:
                    add('blockout', logical, 'PATCH', path, body, current,
                        ('conflict_owned_blockout_missing_or_edited',))
                elif all(current['attributes'].get(k) == v for k, v in body['data']['attributes'].items()):
                    add('blockout', logical, 'NONE', path, expected=current, noop=True)
                else:
                    add('blockout', logical, 'PATCH', path, body, current)
            elif _covers(intervals, _time(body['data']['attributes']['starts_at']),
                         _time(body['data']['attributes']['ends_at'])):
                add('blockout', logical, 'NONE', base, noop=True)
            else:
                add('blockout', logical, 'POST', base, body,
                    reasons=('recurring_blockout_coverage_unresolved',) if unknown_coverage else ())
        for owner in owned:
            if owner.kind != 'blockout' or owner.logical_key in desired_keys:
                continue
            current = blocks.get(owner.remote_id)
            if current is None:
                add('blockout', owner.logical_key, 'NONE', base + '/' + owner.remote_id, noop=True)
            else:
                reasons = () if resource_hash(current) == owner.snapshot_hash else ('conflict_owned_blockout_edited',)
                add('blockout', owner.logical_key, 'DELETE', base + '/' + owner.remote_id, expected=current, reasons=reasons)
    else:
        holds.append({'kind': 'unavailable_dates', 'reason': 'absence_is_not_an_authoritative_clear'})
    memberships = {row['binding']['role_id']: row for row in native['memberships']}
    if (len(memberships) != len(native['memberships']) or
            len({(item['binding']['service_type_id'], item['binding']['position_id'],
                  item['binding']['membership_id']) for item in native['memberships']}) != len(memberships)):
        raise PlanningCenterError('availability_membership_binding_invalid')
    for item in memberships.values():
        binding, row = item['binding'], item['resource']
        if (row.get('type') != 'PersonTeamPositionAssignment' or row['id'] != binding['membership_id'] or
                relation(row, 'person') != pid or relation(row, 'team_position') != binding['position_id']):
            raise PlanningCenterError('availability_preview_membership_scope_mismatch')
    desired_roles = set()
    for cap in src['role_frequency_caps']:
        rid = cap['role_id']
        desired_roles.add(rid)
        logical = 'role:' + str(rid)
        item = memberships.get(rid)
        if item is None or item['binding']['role_name'] != cap['role_name']:
            holds.append({'kind': 'role_frequency', 'role_id': rid, 'reason': 'verified_membership_mapping_required'})
            continue
        if cap['max_per_month'] not in MONTHLY:
            holds.append({'kind': 'role_frequency', 'role_id': rid, 'reason': 'native_frequency_not_exactly_representable'})
            continue
        binding, row = item['binding'], item['resource']
        path = (f"/services/v2/service_types/{binding['service_type_id']}/team_positions/"
                f"{binding['position_id']}/person_team_position_assignments/{binding['membership_id']}")
        value = MONTHLY[cap['max_per_month']]
        if row['attributes'].get('schedule_preference') == value:
            add('membership_frequency', logical, 'NONE', path, expected=row, noop=True)
            continue
        owner = owners.get(('membership_frequency', logical))
        reasons = ('unowned_remote_frequency_preserved',) if owner is None else (
            ('conflict_owned_membership_changed',) if owner.remote_id != row['id'] or
             owner.snapshot_hash != resource_hash(row) else ())
        add('membership_frequency', logical, 'PATCH', path,
            {'data': {'type': 'PersonTeamPositionAssignment', 'attributes': {'schedule_preference': value}}}, row, reasons)
    for owner in owned:
        if owner.kind == 'membership_frequency' and int(owner.logical_key.removeprefix('role:')) not in desired_roles:
            holds.append({'kind': 'role_frequency', 'reason': 'removed_cap_requires_reviewed_restore',
                          'logical_key': owner.logical_key})
    for window in src['recurring_windows']:
        holds.append({'kind': 'recurring_window', 'reason': 'native_role_hours_or_group_context_unsupported', 'window': window})
    if src['global_max_per_month'] is not None:
        holds.append({'kind': 'global_frequency', 'reason': 'no_supported_global_frequency_write'})
    return FrozenSnapshot.capture({'schema': 1, 'organization_id': org, 'person_id': pid,
        'source': src, 'source_hash': source.digest, 'remote': native, 'remote_hash': remote.digest,
        'ownership': sorted(owner_rows, key=lambda o: (o['kind'], o['logical_key'])),
        'policy': asdict(policy), 'operations': operations, 'holds': holds,
        'preview_only': True, 'native_frequency_is_preference_not_hard_cap': True})


def verify_current(preview, source, remote):
    """Reject a stale or tampered preview before review/outbox consumption."""
    data = preview.value
    if (data['source_hash'] != _hash(data['source']) or data['remote_hash'] != _hash(data['remote']) or
            data['source_hash'] != source.digest or data['remote_hash'] != remote.digest):
        raise PlanningCenterError('availability_source_or_remote_changed')
    ownership = [OwnedResource(**item) for item in data['ownership']]
    rebuilt = build_preview(source, remote, owned=ownership, policy=PreviewPolicy(**data['policy']))
    if rebuilt.digest != preview.digest:
        raise PlanningCenterError('availability_preview_operations_changed')


def enqueue_preview(session, preview, *, source, remote, now):
    """Durable, transaction-local PREVIEW rows; no worker or remote apply exists.

    Unknown outcomes are barriers, not permission to repeat a POST. A newer
    revision supersedes only untouched preview/held rows for the same resource.
    The caller commits; this function never commits or creates tables.
    """
    verify_current(preview, source, remote)
    data = preview.value
    existing = session.get(PCOAvailabilityPreview, preview.digest)
    if existing:
        if existing.document != preview.document:
            raise PlanningCenterError('availability_stored_preview_changed')
        return existing
    record = PCOAvailabilityPreview(key=preview.digest, organization_id=data['organization_id'],
        person_id=data['person_id'], source_hash=source.digest, remote_hash=remote.digest,
        document=preview.document, created_at=now)
    session.add(record)
    unresolved = session.scalar(select(PCOAvailabilityIntent.key).where(
        PCOAvailabilityIntent.organization_id == data['organization_id'],
        PCOAvailabilityIntent.person_id == data['person_id'],
        PCOAvailabilityIntent.state == 'unknown')) is not None
    for operation in data['operations']:
        resource_key = operation['kind'] + ':' + operation['logical_key']
        previous = session.scalars(select(PCOAvailabilityIntent).where(
            PCOAvailabilityIntent.organization_id == data['organization_id'],
            PCOAvailabilityIntent.person_id == data['person_id'],
            PCOAvailabilityIntent.resource_key == resource_key)).all()
        document = dict(operation)
        if unresolved:
            document = {**document, 'state': 'held',
                        'holds': [*document['holds'], 'previous_outcome_requires_reconciliation']}
        else:
            for prior in previous:
                if prior.state in {'preview', 'held'}:
                    prior.state = 'superseded'
        session.add(PCOAvailabilityIntent(key=_hash([preview.digest, operation]),
            preview_key=preview.digest, organization_id=data['organization_id'], person_id=data['person_id'],
            resource_key=resource_key, source_hash=source.digest, state=document['state'],
            document=_json(document), created_at=now))
    session.flush()
    return record
