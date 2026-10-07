"""Scoped automatic finite blockouts, with durable unknown outcomes.

Only a verified committed source reader and an authenticated standing policy
can release new writes. Existing preview/frequency reviews remain unchanged.
All journals use existing Policy rows; no schema or native notification setting
is changed. Native requests run outside local database transactions.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Policy, Volunteer
from app.integrations.planning_center import PCOVolunteerPerson, PlanningCenterError, _id, _time, relation
from app.integrations.planning_center_availability import (
    FrozenSnapshot, OwnedResource, PreviewPolicy, _hash, _json, build_preview,
    read_remote, resource_hash,
)

QUEUE_PREFIX = 'pco_bq:'
NOTIFICATION_MODE = 'provider_managed'
CONFLICT_STRATEGY = 'preserve_existing_bookings'
DATE_CONTRACT = 'inclusive_local_end_second'


def _key(kind, organization_id, volunteer_id):
    return 'pco_b' + kind + ':' + _hash([organization_id, volunteer_id])


def queue_blockout_sync(session, organization_id, volunteer_id, *, revision):
    """Transaction-local save hook, never commits or performs network I/O.

    revision identifies the exact committed source, not a claimed native result.
    The caller must roll this row back whenever the underlying save rolls back.
    """
    if (not isinstance(organization_id, str) or not organization_id.isdigit() or
            type(volunteer_id) is not int or volunteer_id <= 0 or
            not isinstance(revision, str) or not 0 < len(revision) <= 180):
        raise PlanningCenterError('blockout_queue_scope_or_revision_invalid')
    key = _key('q', organization_id, volunteer_id)
    row = session.get(Policy, key)
    if row is None:
        row = Policy(key=key, value={}); session.add(row)
    value = row.value or {}
    if (value.get('revision') != revision or value.get('organization_id') != organization_id
            or value.get('volunteer_id') != volunteer_id):
        row.value = {'schema': 1, 'organization_id': organization_id, 'volunteer_id': volunteer_id,
                     'revision': revision, 'state': 'pending'}
    return key


def _signature(key, document, signing_key):
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise PlanningCenterError('blockout_signing_key_required')
    return hmac.new(signing_key, ('text-monkey:blockouts:v1\0' + key + '\0' + _json(document)).encode(), sha256).hexdigest()


def _signed(session, key, signing_key, *, lock=False):
    row = session.scalar(select(Policy).where(Policy.key == key).with_for_update() if lock
                         else select(Policy).where(Policy.key == key))
    if row is None:
        return None, None
    value = row.value
    if (not isinstance(value, dict) or set(value) != {'document', 'signature'} or
            not isinstance(value['signature'], str) or not isinstance(value['document'], dict) or
            not hmac.compare_digest(value['signature'], _signature(key, value['document'], signing_key))):
        raise PlanningCenterError('blockout_signed_state_changed')
    return row, deepcopy(value['document'])


def _put(session, key, document, signing_key, row=None):
    if row is None:
        row = session.get(Policy, key)
    if row is None:
        row = Policy(key=key); session.add(row)
    row.value = {'document': deepcopy(document), 'signature': _signature(key, document, signing_key)}
    return row


def _clock(clock):
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PlanningCenterError('blockout_clock_requires_timezone')
    return value.astimezone(timezone.utc)


def _lock(session):
    if session.get_bind().dialect.name == 'sqlite':
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')


def _identity(session, config, volunteer_id, *, consent):
    config.require_scope()
    volunteer = session.scalar(select(Volunteer).where(Volunteer.id == volunteer_id).with_for_update()
                               .execution_options(populate_existing=True))
    mapping = session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == config.organization_id,
        PCOVolunteerPerson.volunteer_id == volunteer_id).with_for_update().execution_options(populate_existing=True))
    if volunteer is None or mapping is None:
        raise PlanningCenterError('blockout_verified_identity_required')
    prefs = volunteer.preferences or {}
    draft = prefs.get('onboarding_availability_draft') or {}
    if consent and (volunteer.status != 'active' or volunteer.sms_opt_in is not True or
            prefs.get('consent_pending') or not prefs.get('consent_at') or not prefs.get('consent_source') or
            prefs.get('pending_constraints') or draft.get('pending_constraints')):
        raise PlanningCenterError('blockout_current_consent_or_constraints_held')
    return {'organization_id': config.organization_id, 'service_type_ids': list(config.service_type_ids),
            'volunteer_id': volunteer_id, 'person_id': _id(mapping.person_id), 'mapping_id': mapping.id,
            'mapping_created_at': mapping.created_at.isoformat(), 'phone_hash': sha256(volunteer.phone.encode()).hexdigest()}


def issue_blockout_policy(session, settings, config, volunteer_id, *, user, clock, signing_key,
                          enabled, acceptance=None):
    """Authenticated standing finite-date authority; operator supplies real proof.

    Evidence acknowledges provider-managed notifications and the independently
    accepted booking-conflict contract. It never means notifications are muted.
    The server selects identity; client-supplied native IDs are not accepted.
    """
    from app.core.planning_center_frequency_reviews import _actor
    actor = _actor(user, settings)
    if type(enabled) is not bool or type(volunteer_id) is not int or volunteer_id <= 0:
        raise PlanningCenterError('blockout_policy_contract_required')
    _lock(session)
    stop_key = 'pco_bstop:' + _hash(volunteer_id)
    stop = session.get(Policy, stop_key)
    if not enabled:
        if stop is None:
            stop = Policy(key=stop_key); session.add(stop)
        stop.value = {'disabled': True, 'actor': actor, 'at': _clock(clock).isoformat()}
        if isinstance(signing_key, bytes) and len(signing_key) >= 32:
            try:
                old_row, old_grant = _signed(session, _key('p', config.organization_id, volunteer_id), signing_key)
                if old_grant:
                    old_grant.update(enabled=False, disabled_at=_clock(clock).isoformat(), disabled_by=actor)
                    _put(session, old_row.key, old_grant, signing_key, old_row)
            except PlanningCenterError:
                pass  # Kill switch still stops effects with corrupt/stale signing state.
        return {'policy_enabled': False, 'state': 'disabled', 'reason': 'blockout_scoped_policy_disabled'}
    acceptance = verify_blockout_acceptance(acceptance, config, signing_key)
    timezone_name, evidence_hash = acceptance['timezone'], acceptance['evidence_hash']
    if timezone_name != settings.church_timezone:
        raise PlanningCenterError('blockout_server_acceptance_timezone_changed')
    notification_mode, conflict_strategy = acceptance['notification_mode'], acceptance['conflict_strategy']
    identity = _identity(session, config, volunteer_id, consent=True)
    key = _key('p', config.organization_id, volunteer_id)
    _signed(session, key, signing_key, lock=True)
    document = {'schema': 1, 'id': str(uuid4()), **identity, 'actor': actor, 'enabled': enabled,
        'timezone': timezone_name, 'notification_mode': notification_mode,
        'conflict_strategy': conflict_strategy, 'date_contract': DATE_CONTRACT,
        'effect_scope': 'all_services_teams', 'evidence_hash': evidence_hash,
        'issued_at': _clock(clock).isoformat()}
    _put(session, key, document, signing_key)
    if stop is not None:
        stop.value = {'disabled': False}
    queue = session.get(Policy, _key('q', config.organization_id, volunteer_id))
    if queue is not None:
        queue.value = {**queue.value, 'state': 'pending'}
    return {'policy_enabled': enabled, 'policy_id': document['id'],
            'notification_mode': notification_mode, 'conflict_strategy': conflict_strategy,
            'effect_scope': document['effect_scope']}


def _authority(session, config, volunteer_id, signing_key, *, write):
    stop = session.get(Policy, 'pco_bstop:' + _hash(volunteer_id))
    if write and stop is not None and stop.value.get('disabled') is not False:
        raise PlanningCenterError('blockout_scoped_policy_disabled')
    _, grant = _signed(session, _key('p', config.organization_id, volunteer_id), signing_key)
    if grant is None or grant['schema'] != 1:
        raise PlanningCenterError('blockout_scoped_policy_required')
    identity = _identity(session, config, volunteer_id, consent=write)
    if any(grant.get(k) != value for k, value in identity.items()):
        raise PlanningCenterError('blockout_policy_identity_changed')
    if (grant.get('notification_mode') != NOTIFICATION_MODE or grant.get('conflict_strategy') != CONFLICT_STRATEGY
            or grant.get('date_contract') != DATE_CONTRACT or grant.get('effect_scope') != 'all_services_teams'
            or not re.fullmatch('[a-f0-9]{64}', grant.get('evidence_hash', ''))):
        raise PlanningCenterError('blockout_policy_contract_changed')
    if write and grant.get('enabled') is not True:
        raise PlanningCenterError('blockout_scoped_policy_disabled')
    return grant



def _read_committed(session, source_reader):
    from app.core.planning_center_committed_source import CommittedSourceSession
    if not isinstance(session,CommittedSourceSession):
        raise PlanningCenterError('blockout_guarded_source_session_required')
    if session.new or session.dirty or session.deleted or session.info.get('pco_uncommitted_source_write'):
        raise PlanningCenterError('blockout_uncommitted_source_write')
    source = source_reader(session)
    if session.new or session.dirty or session.deleted or session.info.get('pco_uncommitted_source_write'):
        raise PlanningCenterError('blockout_uncommitted_source_write')
    return source

def _source(session, config, volunteer_id, source_reader, grant, now):
    source = source_reader(session)  # Already guarded by the caller phase.
    if not isinstance(source, FrozenSnapshot):
        raise PlanningCenterError('blockout_committed_source_required')
    value = source.value
    if ((value['organization_id'], value['person_id'], value['volunteer_id'], value['timezone']) !=
            (config.organization_id, grant['person_id'], volunteer_id, grant['timezone']) or
            value.get('dates_authoritative') is not True):
        raise PlanningCenterError('blockout_source_scope_or_authority_changed')
    if 'correction_lineage' in value:
        # Existing audited correction previews have their own held review flow.
        raise PlanningCenterError('blockout_corrected_preview_not_released')
    dates = value['unavailable_dates']; today = now.astimezone(ZoneInfo(grant['timezone'])).date()
    if (not isinstance(dates, list) or len(dates) > 366 or dates != sorted(set(dates)) or
            any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or
                not 0 <= (date.fromisoformat(day) - today).days <= 366 for day in dates)):
        raise PlanningCenterError('blockout_finite_dates_invalid')
    provenance = value['provenance']
    if (not isinstance(provenance, dict) or set(provenance) != {'source_id', 'receipt_id', 'revision'} or
            any(not isinstance(x, str) or not 0 < len(x) <= 180 for x in provenance.values())):
        raise PlanningCenterError('blockout_source_provenance_required')
    queue = session.get(Policy, _key('q', config.organization_id, volunteer_id))
    if (not queue or queue.value.get('organization_id') != config.organization_id or
            queue.value.get('volunteer_id') != volunteer_id or queue.value.get('revision') != provenance['revision']):
        raise PlanningCenterError('blockout_queued_revision_changed')
    return source


def _journal(session, config, volunteer_id, signing_key, *, lock=False):
    key = _key('j', config.organization_id, volunteer_id)
    row, value = _signed(session, key, signing_key, lock=lock)
    if value is None:
        value = {'schema': 1, 'owned': [], 'unknown': None, 'state': 'pending', 'reason': '', 'verified_revision': None}
    if value['schema'] != 1 or not isinstance(value['owned'], list):
        raise PlanningCenterError('blockout_journal_invalid')
    return key, row, value


def _booking_snapshot(client, person_id):
    """Observe all native person bookings, never mutate/decline any of them."""
    rows = client.collection(f'/services/v2/people/{person_id}/plan_people')
    result, seen = [], set()
    for row in rows:
        identifier = _id(row['id'])
        status = {'Confirmed': 'C', 'Unconfirmed': 'U', 'Declined': 'D'}.get(
            row.get('attributes', {}).get('status'), row.get('attributes', {}).get('status'))
        if row.get('type') != 'PlanPerson' or identifier in seen or relation(row, 'person') != person_id or status not in {'C', 'U', 'D'}:
            raise PlanningCenterError('blockout_native_booking_identity_invalid')
        seen.add(identifier)
        result.append({'id': identifier, 'status': status, 'relationships': row.get('relationships', {})})
    return sorted(result, key=lambda row: row['id'])


def _native(client, config, source):
    remote = read_remote(client, config, source, ())
    return remote, _booking_snapshot(client, source.value['person_id'])


def _operations(source, remote, owned):
    rows = {row['id']: row for row in remote.value['blockouts']}
    for owner in owned:
        if owner['kind'] != 'blockout' or owner['organization_id'] != source.value['organization_id'] or owner['person_id'] != source.value['person_id']:
            raise PlanningCenterError('blockout_owned_scope_changed')
        row = rows.get(owner['remote_id'])
        if row is None or resource_hash(row) != owner['snapshot_hash']:
            raise PlanningCenterError('blockout_owned_native_baseline_changed')
    # Keep the legacy preview truthful and held; standing runtime authority is
    # separate, and never consumes or rewrites its reviewed operation hashes.
    preview = build_preview(source, remote, owned=[OwnedResource(**item) for item in owned], policy=PreviewPolicy())
    operations = []
    for op in preview.value['operations']:
        if op['kind'] != 'blockout' or op['method'] == 'NONE':
            continue
        if op['method'] == 'PATCH':
            target = rows[op['path'].rsplit('/', 1)[-1]]
            desired = op['body']['data']['attributes']
            # A historical Text Monkey-owned reason is not an availability
            # change. Full ownership hashes were checked above; a coordinator
            # edit still holds even when its dates happen to match. Leave the
            # legacy held preview/operation hash untouched.
            if all(target['attributes'].get(field) == value for field, value in desired.items() if field != 'reason'):
                continue
        operations.append(op)
    for op in operations:
        if op['state'] == 'conflict' or any(reason not in {'notification_policy_not_verified', 'blockout_date_contract_not_verified'} for reason in op['holds']):
            raise PlanningCenterError('blockout_preview_conflict_or_unsupported')
        if op['method'] not in {'POST', 'PATCH', 'DELETE'}:
            raise PlanningCenterError('blockout_operation_unsupported')
    return sorted(operations, key=lambda op: ({'POST': 0, 'PATCH': 1, 'DELETE': 2}[op['method']], op['logical_key']))


def _matches(row, body):
    attrs = row['attributes']
    return all(attrs.get(key) == value for key, value in body['data']['attributes'].items())


def _generated_matches(remote, row, body, zone_name):
    desired = body['data']['attributes']
    start, end = _time(desired['starts_at']), _time(desired['ends_at']) + timedelta(seconds=1)
    intervals = []
    for occurrence in remote.value['blockout_dates'].get(row['id'], []):
        attrs = occurrence['attributes']
        if attrs.get('time_zone') != zone_name:
            return False
        a, b = _time(attrs['starts_at_utc']), _time(attrs['ends_at_utc']) + timedelta(seconds=1)
        if a < start or b > end or b <= a:
            return False
        intervals.append((a, b))
    cursor = start
    for a, b in sorted(intervals):
        if a > cursor:
            return False
        cursor = max(cursor, b)
    return bool(intervals) and cursor == end


def _converged(document, remote, bookings, returned_id=None):
    op, before = document['operation'], document['remote']['blockouts']
    after = remote.value['blockouts']
    before_ids = {row['id'] for row in before}
    if bookings != document['bookings']:
        return None, 'native_bookings_changed'
    if op['method'] == 'POST':
        if not returned_id:
            return None, 'post_identity_unknown'
        targets = [row for row in after if row['id']==returned_id and row['id'] not in before_ids]
        if len(targets)!=1 or not _matches(targets[0],op['body']):
            return None, 'post_returned_identity_not_verified'
        target = targets[0]
    else:
        identifier = op['path'].rsplit('/', 1)[-1]
        targets = [row for row in after if row['id'] == identifier]
        if op['method'] == 'DELETE':
            if targets:
                return None, 'delete_not_verified'
            target = None
        elif len(targets) == 1 and _matches(targets[0], op['body']):
            target = targets[0]
        else:
            return None, 'patch_not_verified'
    changed_id = target['id'] if target else op['path'].rsplit('/', 1)[-1]
    if (sorted([row for row in before if row['id'] != changed_id],key=lambda row:row['id']) !=
            sorted([row for row in after if row['id'] != changed_id],key=lambda row:row['id']) or
            {key:value for key,value in document['remote']['blockout_dates'].items() if key!=changed_id} !=
            {key:value for key,value in remote.value['blockout_dates'].items() if key!=changed_id}):
        return None, 'unrelated_native_blockouts_changed'
    if target and not _generated_matches(remote, target, op['body'], document['timezone']):
        return None, 'native_generated_dates_not_verified'
    return target, ''



def _record_post_identity(factory, config, volunteer_id, signing_key, attempt_key, returned):
    """Persist provider-correlated ownership proof before any readback HTTP.

    Content, creation timestamp or a new collection ID alone cannot prove a
    lost POST belongs to Text Monkey. No provider idempotency marker is assumed.
    """
    with factory() as session:
        _lock(session)
        _, attempt = _signed(session,attempt_key,signing_key,lock=True)
        _, _, journal = _journal(session,config,volunteer_id,signing_key,lock=True)
        if not attempt or attempt['state']!='unknown' or journal['unknown']!=attempt_key:
            raise PlanningCenterError('blockout_attempt_claim_changed')
        document=attempt['document'];op=document['operation']
        identifier=_id(returned['id'])
        if (op['method']!='POST' or returned.get('type')!='Blockout' or
                relation(returned,'person')!=document['identity']['person_id'] or
                relation(returned,'organization')!=config.organization_id or
                identifier in {row['id'] for row in document['remote']['blockouts']} or
                not _matches(returned,op['body'])):
            raise PlanningCenterError('blockout_post_returned_identity_invalid')
        attempt.update(returned_id=identifier,returned_hash=resource_hash(returned))
        _put(session,attempt_key,attempt,signing_key);session.commit()

def _complete(factory, config, volunteer_id, signing_key, attempt_key, remote, bookings, clock):
    with factory() as session:
        _lock(session)
        _, attempt = _signed(session, attempt_key, signing_key, lock=True)
        key, journal_row, journal = _journal(session, config, volunteer_id, signing_key, lock=True)
        if not attempt or journal['unknown'] != attempt_key or attempt['state'] != 'unknown':
            raise PlanningCenterError('blockout_attempt_claim_changed')
        identity = _identity(session, config, volunteer_id, consent=False)
        if identity != attempt['document']['identity']:
            raise PlanningCenterError('blockout_attempt_identity_changed')
        target, reason = _converged(attempt['document'], remote, bookings, attempt.get('returned_id'))
        if reason:
            journal.update(state='unknown', reason=reason)
        else:
            op = attempt['document']['operation']
            # Only this verified mutation establishes/changes an owned baseline.
            owned = [owner for owner in journal['owned'] if owner['logical_key'] != op['logical_key']]
            if target:
                owned.append({'organization_id': config.organization_id, 'person_id': identity['person_id'],
                    'kind': 'blockout', 'logical_key': op['logical_key'], 'remote_id': target['id'], 'snapshot_hash': resource_hash(target)})
            journal.update(owned=sorted(owned, key=lambda row: row['logical_key']), unknown=None, state='pending', reason='')
            attempt.update(state='verified', verified_at=_clock(clock).isoformat(), readback_hash=remote.digest)
            _put(session, attempt_key, attempt, signing_key)
        _put(session, key, journal, signing_key, journal_row)
        session.commit()
        return {'state': 'unknown' if reason else 'verified', 'reason': reason}


def _reconcile(factory, client, config, volunteer_id, signing_key, clock):
    with factory() as session:
        _, _, journal = _journal(session, config, volunteer_id, signing_key)
        attempt_key = journal['unknown']
        _, attempt = _signed(session, attempt_key, signing_key)
        identity = _identity(session, config, volunteer_id, consent=False)
        if not attempt or identity != attempt['document']['identity']:
            raise PlanningCenterError('blockout_attempt_identity_changed')
        source = FrozenSnapshot.capture(attempt['document']['source'])
    remote, bookings = _native(client, config, source)
    return _complete(factory, config, volunteer_id, signing_key, attempt_key, remote, bookings, clock)


def _set_reason(factory, config, volunteer_id, signing_key, reason):
    with factory() as session:
        _lock(session)
        key, row, journal = _journal(session, config, volunteer_id, signing_key, lock=True)
        journal.update(state='unknown' if journal['unknown'] else 'held', reason=reason)
        _put(session, key, journal, signing_key, row); session.commit()
        return {'state': journal['state'], 'reason': reason}



def _abort_before_http(factory, config, volunteer_id, signing_key, attempt_key, reason, clock):
    """Only the claiming stack can attest it never called the mutation API.

    Process death or errors once request() begins retain unknown forever until
    verified GET reconciliation. An explicitly aborted claim stays as history;
    a later tick can create a new claim after fresh preflight/source checks.
    """
    with factory() as session:
        _lock(session)
        _, attempt = _signed(session,attempt_key,signing_key,lock=True)
        key,row,journal = _journal(session,config,volunteer_id,signing_key,lock=True)
        if not attempt or attempt['state']!='unknown' or journal['unknown']!=attempt_key:
            raise PlanningCenterError('blockout_attempt_claim_changed')
        attempt.update(state='aborted_before_http',reason=reason,aborted_at=_clock(clock).isoformat())
        journal.update(unknown=None,state='held',reason=reason)
        _put(session,attempt_key,attempt,signing_key)
        _put(session,key,journal,signing_key,row);session.commit()
        return {'state':'held','reason':reason}

def sync_person_blockouts(factory, client, config, volunteer_id, *, source_reader, clock,
                         enabled=False, signing_key=None, acceptance=None, reconcile_only=False, limit=10):
    """Asynchronous caller entry, after committed saved-preference transaction.

    source_reader revalidates current committed revision/receipt/consent with no
    mutation/network. Claims are committed before native writes, never retried.
    Old unknowns can reconcile via GET even when automation or consent is off.
    """
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        return {'state': 'held', 'reason': 'blockout_signing_key_required'}
    attempt_key, http_started = None, False
    try:
        config.require_scope()
        for _ in range(min(25, max(1, limit))):
            attempt_key, http_started = None, False
            with factory() as session:
                _, _, journal = _journal(session, config, volunteer_id, signing_key)
                unknown = journal['unknown']
            if unknown:
                result = _reconcile(factory, client, config, volunteer_id, signing_key, clock)
                if result['state'] != 'verified' or reconcile_only or enabled is not True:
                    return result
                continue
            if enabled is not True:
                return {'state': 'disabled', 'reason': 'blockout_runtime_disabled'}
            if reconcile_only:
                return {'state': 'held', 'reason': 'blockout_no_unknown_outcome'}
            now = _clock(clock)
            accepted = verify_blockout_acceptance(acceptance, config, signing_key, now=now)
            with factory() as session:
                source = _read_committed(session, source_reader)
                grant = _authority(session, config, volunteer_id, signing_key, write=True)
                source = _source(session, config, volunteer_id, lambda _: source, grant, now)
                if any(grant[field] != accepted[field] for field in ('evidence_hash','timezone','notification_mode','conflict_strategy','date_contract')):
                    raise PlanningCenterError('blockout_policy_acceptance_changed')
                _, _, journal = _journal(session, config, volunteer_id, signing_key)
            remote, bookings = _native(client, config, source)
            operations = _operations(source, remote, journal['owned'])
            with factory() as session:
                _lock(session)
                current = _read_committed(session, source_reader)
                current_grant = _authority(session, config, volunteer_id, signing_key, write=True)
                current = _source(session, config, volunteer_id, lambda _: current, current_grant, _clock(clock))
                key, journal_row, current_journal = _journal(session, config, volunteer_id, signing_key, lock=True)
                if current.digest != source.digest or current_grant != grant or current_journal != journal:
                    raise PlanningCenterError('blockout_source_or_policy_changed')
                if not operations:
                    current_journal.update(state='verified', reason='', verified_revision=source.value['provenance']['revision'])
                    _put(session, key, current_journal, signing_key, journal_row)
                    queue = session.get(Policy, _key('q', config.organization_id, volunteer_id))
                    queue.value = {**queue.value, 'state': 'verified'}
                    session.commit()
                    return {'state': 'verified', 'reason': '', 'owned_count': len(journal['owned'])}
                op = operations[0]
                document = {'schema': 1, 'source': source.value, 'source_hash': source.digest,
                    'identity': {k: grant[k] for k in _identity(session, config, volunteer_id, consent=True)},
                    'policy_hash': _hash(grant), 'timezone': grant['timezone'], 'operation': op,
                    'remote': remote.value, 'bookings': bookings, 'claim_id':str(uuid4())}
                attempt_key = 'pco_ba:' + _hash(document)
                previous, _ = _signed(session, attempt_key, signing_key)
                if previous:
                    raise PlanningCenterError('blockout_attempt_already_exists')
                attempt = {'document': document, 'document_hash': _hash(document), 'state': 'unknown', 'claimed_at': now.isoformat()}
                _put(session, attempt_key, attempt, signing_key)
                current_journal.update(unknown=attempt_key, state='unknown', reason='outcome_unknown')
                _put(session, key, current_journal, signing_key, journal_row)
                session.commit()  # Death after this point requires GET-only recovery.
            # Fresh GET and source checks reduce concurrent native edit races;
            # API provides no documented atomic conditional-write contract.
            fresh_remote, fresh_bookings = _native(client, config, source)
            with factory() as session:
                current = _read_committed(session, source_reader)
                latest_grant = _authority(session, config, volunteer_id, signing_key, write=True)
                current = _source(session, config, volunteer_id, lambda _: current, latest_grant, _clock(clock))
                _, _, latest_journal = _journal(session, config, volunteer_id, signing_key)
                _, latest_attempt = _signed(session, attempt_key, signing_key)
                if latest_journal['unknown'] != attempt_key or not latest_attempt or latest_attempt['state'] != 'unknown':
                    raise PlanningCenterError('blockout_attempt_claim_changed')
                if (current.digest != source.digest or latest_grant != grant or
                        fresh_remote.digest != remote.digest or fresh_bookings != bookings):
                    raise PlanningCenterError('blockout_pre_http_source_or_native_changed')
            http_started = True  # From here any failure is an unknown write.
            response = client.request(op['method'], op['path'], data=op['body'] if op['method'] != 'DELETE' else None)
            if op['method']=='POST':
                _record_post_identity(factory,config,volunteer_id,signing_key,attempt_key,response['data'])
            after, after_bookings = _native(client, config, source)
            result = _complete(factory, config, volunteer_id, signing_key, attempt_key, after, after_bookings, clock)
            if result['state'] != 'verified':
                return result
        return {'state': 'pending', 'reason': 'blockout_tick_limit'}
    except IntegrityError:
        return {'state': 'held', 'reason': 'blockout_concurrent_claim'}
    except (PlanningCenterError, ValueError, TypeError, KeyError, AttributeError) as error:
        # Never expose provider response bodies, credentials or private source.
        reason = str(error) if isinstance(error, PlanningCenterError) and re.fullmatch(r'blockout_[a-z_]+', str(error)) else 'blockout_preflight_or_readback_held'
        try:
            if attempt_key and not http_started:
                return _abort_before_http(factory,config,volunteer_id,signing_key,attempt_key,reason,clock)
            return _set_reason(factory, config, volunteer_id, signing_key, reason)
        except (PlanningCenterError, IntegrityError, ValueError, TypeError, KeyError):
            return {'state': 'held', 'reason': 'blockout_state_requires_reconciliation'}


def blockout_status(session, config, volunteer_id, *, signing_key, runtime_enabled=False, acceptance=None, now=None):
    stop = session.get(Policy, 'pco_bstop:' + _hash(volunteer_id))
    disabled = bool(stop and stop.value.get('disabled') is not False)
    key_ready = isinstance(signing_key, bytes) and len(signing_key) >= 32
    # Presentation of the saved choice is separate from executable authority.
    # Unverified documents below can describe configuration only, never release.
    raw_policy = session.get(Policy, _key('p',config.organization_id,volunteer_id))
    raw_grant = (raw_policy.value or {}).get('document',{}) if raw_policy and isinstance(raw_policy.value,dict) else {}
    raw_grant = raw_grant if isinstance(raw_grant,dict) else {}
    configured_choice = raw_grant.get('enabled') is True
    raw_state = session.get(Policy, _key('j',config.organization_id,volunteer_id))
    raw_journal = (raw_state.value or {}).get('document',{}) if raw_state and isinstance(raw_state.value,dict) else {}
    raw_journal = raw_journal if isinstance(raw_journal,dict) else {}
    grant = None
    journal = {'state':'pending','reason':'','owned':[],'unknown':None,'verified_revision':None}
    journal_ready = raw_state is None and key_ready
    state_error = '' if key_ready else 'blockout_signing_key_required'
    if key_ready:
        try:
            _, grant = _signed(session, _key('p', config.organization_id, volunteer_id), signing_key)
            _, _, journal = _journal(session, config, volunteer_id, signing_key)
            journal_ready = True
        except (PlanningCenterError,ValueError,TypeError,KeyError,AttributeError):
            state_error = 'blockout_signed_state_changed'
    acceptance_document = None
    try:
        acceptance_document = verify_blockout_acceptance(acceptance, config, signing_key, now=now)
    except (PlanningCenterError, KeyError, TypeError, ValueError) as error:
        if key_ready:
            state_error = str(error) if isinstance(error,PlanningCenterError) else 'blockout_server_acceptance_required'
    mapping_ready = consent_ready = False
    try:
        with session.begin_nested():
            _identity(session, config, volunteer_id, consent=False); mapping_ready = True
            _identity(session, config, volunteer_id, consent=True); consent_ready = True
    except (PlanningCenterError, IntegrityError):
        pass
    authority_ready = False
    if grant and not disabled:
        try:
            _authority(session, config, volunteer_id, signing_key, write=True)
            if acceptance_document is None or any(grant[field] != acceptance_document[field] for field in ('evidence_hash','timezone','notification_mode','conflict_strategy','date_contract')):
                raise PlanningCenterError('blockout_policy_acceptance_changed')
            authority_ready = True
        except PlanningCenterError as error:
            state_error = state_error or str(error)
    queue = session.get(Policy, _key('q', config.organization_id, volunteer_id))
    state = 'disabled' if disabled or runtime_enabled is not True else journal['state']
    reason = 'blockout_scoped_policy_disabled' if disabled else ('blockout_runtime_disabled' if runtime_enabled is not True else state_error or journal['reason'])
    if not disabled and runtime_enabled is True and not authority_ready and not journal['unknown']:
        state, reason = 'held', state_error or 'blockout_scoped_policy_required'
    # Current validated global exclusions only, no raw conversations/drafts.
    from app.db.models import Availability
    saved = session.scalars(select(Availability).where(Availability.volunteer_id==volunteer_id)).all()
    desired_dates = sorted({day for row in saved for day in (row.unavailable_dates or []) if isinstance(day,str)})
    return {'organization_id': config.organization_id, 'volunteer_id': volunteer_id,
        'person_id': grant['person_id'] if grant else None, 'policy_enabled': configured_choice and not disabled,
        'runtime_enabled': runtime_enabled is True, 'state': state, 'reason': reason,
        'desired_revision': queue.value['revision'] if queue else None, 'verified_revision': journal['verified_revision'],
        'owned_count': len(journal['owned']) if journal_ready else None,
        'unknown_attempt': bool(journal['unknown']) if journal_ready else bool(raw_journal.get('unknown')),
        'notification_mode': raw_grant.get('notification_mode'), 'conflict_strategy': raw_grant.get('conflict_strategy'),
        'effect_scope': 'all_services_teams','global_unavailable_dates':desired_dates,
        'acceptance': {'available': acceptance_document is not None, **({key:acceptance_document[key] for key in
            ('evidence_hash','verified_at','expires_at','notification_mode','conflict_strategy','date_contract','timezone')} if acceptance_document else {})},
        'readiness': {'signing_ready': key_ready, 'acceptance_ready': acceptance_document is not None,
                      'mapping_ready':mapping_ready, 'consent_ready':consent_ready,'authority_ready':authority_ready,
                      'journal_ready':journal_ready}}


def sign_blockout_acceptance(config, *, timezone_name, evidence_hash, verified_at, signing_key, expires_at=None):
    """Root installation helper AFTER independent native acceptance review.

    Not exposed by HTTP. A hash alone is not proof; root verifies the original
    private evidence and signs this scoped descriptor as the evidence authority.
    """
    config.require_scope(); ZoneInfo(timezone_name)
    if not re.fullmatch('[a-f0-9]{64}', evidence_hash):
        raise PlanningCenterError('blockout_acceptance_evidence_required')
    document = {'schema':1,'organization_id':config.organization_id,
        'service_type_ids':list(config.service_type_ids),'timezone':timezone_name,
        'notification_mode':NOTIFICATION_MODE,'conflict_strategy':CONFLICT_STRATEGY,
        'date_contract':DATE_CONTRACT,'effect_scope':'all_services_teams',
        'evidence_hash':evidence_hash,'verified_at':_time(verified_at).isoformat(),
        'expires_at':(_time(expires_at) if expires_at else _time(verified_at)+timedelta(days=90)).isoformat()}
    if not timedelta(0) < _time(document['expires_at'])-_time(document['verified_at']) <= timedelta(days=365):
        raise PlanningCenterError('blockout_acceptance_lifetime_invalid')
    return {'document':document,'signature':_signature('acceptance',document,signing_key)}


def verify_blockout_acceptance(value, config, signing_key, *, now=None):
    if (not isinstance(value,dict) or set(value)!={'document','signature'} or
            not isinstance(value['signature'],str) or not isinstance(value['document'],dict) or
            not hmac.compare_digest(value['signature'],_signature('acceptance',value['document'],signing_key))):
        raise PlanningCenterError('blockout_server_acceptance_required')
    document = value['document']
    if (document.get('schema')!=1 or document.get('organization_id')!=config.organization_id or
            document.get('service_type_ids')!=list(config.service_type_ids) or
            document.get('notification_mode')!=NOTIFICATION_MODE or document.get('conflict_strategy')!=CONFLICT_STRATEGY or
            document.get('date_contract')!=DATE_CONTRACT or document.get('effect_scope')!='all_services_teams' or
            not re.fullmatch('[a-f0-9]{64}',document.get('evidence_hash',''))):
        raise PlanningCenterError('blockout_server_acceptance_scope_changed')
    ZoneInfo(document['timezone']); config.require_scope()
    verified, expires = _time(document['verified_at']), _time(document['expires_at'])
    if not timedelta(0) < expires-verified <= timedelta(days=365) or not verified <= (now or datetime.now(timezone.utc)) < expires:
        raise PlanningCenterError('blockout_acceptance_expired_or_future')
    return deepcopy(document)


def load_blockout_acceptance(path, config, signing_key):
    from app.core.planning_center_held_preview import private_file
    try:
        value = json.loads(private_file(path,16384))
        verify_blockout_acceptance(value,config,signing_key)
        return value
    except (PlanningCenterError,TypeError,ValueError,KeyError):
        return None


def bootstrap_owned_blockout(factory, client, config, volunteer_id, *, receipt_verifier,
                             signing_key, clock):
    """Root-only upgrade, never infer ownership from a GET or UI-supplied ID.

    receipt_verifier() must independently validate an ORIGINAL successful native
    Text Monkey write and its verified readback, returning its exact scoped ID,
    body, full native hash and receipt hash. The operator implements this trusted
    adapter over existing private receipts; ordinary API callers cannot adopt.
    """
    proof = receipt_verifier()
    if (not isinstance(proof, dict) or set(proof) != {'organization_id', 'person_id', 'volunteer_id',
            'remote_id', 'body', 'snapshot_hash', 'receipt_hash', 'logical_key'} or
            proof['organization_id'] != config.organization_id or proof['volunteer_id'] != volunteer_id or
            not re.fullmatch('[a-f0-9]{64}', proof['receipt_hash']) or
            not re.fullmatch('[a-f0-9]{64}', proof['snapshot_hash'])):
        raise PlanningCenterError('blockout_original_write_receipt_required')
    with factory() as session:
        grant = _authority(session, config, volunteer_id, signing_key, write=True)
        identity = _identity(session, config, volunteer_id, consent=True)
        if proof['person_id'] != identity['person_id']:
            raise PlanningCenterError('blockout_prior_write_identity_changed')
    # Historical body must itself describe one supported finite local-day range.
    attrs = proof['body']['data']['attributes']; zone = ZoneInfo(grant['timezone'])
    start, end = _time(attrs['starts_at']).astimezone(zone), _time(attrs['ends_at']).astimezone(zone)
    if (proof['body']['data']['type'] != 'Blockout' or attrs.get('repeat_frequency') != 'no_repeat' or
            attrs.get('share') is not False or start.time().isoformat() != '00:00:00' or
            end.time().isoformat() != '23:59:59' or end < start or proof['logical_key'] != 'date:' + start.date().isoformat()):
        raise PlanningCenterError('blockout_prior_write_finite_contract_invalid')
    source = FrozenSnapshot.capture({'organization_id': config.organization_id, 'person_id': identity['person_id']})
    remote = read_remote(client, config, source, ())
    rows = [row for row in remote.value['blockouts'] if row['id'] == _id(proof['remote_id'])]
    if (len(rows) != 1 or resource_hash(rows[0]) != proof['snapshot_hash'] or not _matches(rows[0], proof['body'])
            or not _generated_matches(remote, rows[0], proof['body'], grant['timezone'])):
        raise PlanningCenterError('blockout_prior_write_readback_changed')
    owner = OwnedResource(config.organization_id, identity['person_id'], 'blockout', proof['logical_key'],
                          proof['remote_id'], proof['snapshot_hash'])
    with factory() as session:
        _lock(session)
        if _authority(session, config, volunteer_id, signing_key, write=True) != grant:
            raise PlanningCenterError('blockout_bootstrap_policy_changed')
        key, row, journal = _journal(session, config, volunteer_id, signing_key, lock=True)
        if journal['unknown'] or any(item['logical_key'] == owner.logical_key or item['remote_id'] == owner.remote_id for item in journal['owned']):
            raise PlanningCenterError('blockout_bootstrap_existing_claim_or_owner')
        journal['owned'].append(owner.__dict__)
        journal['bootstrap_receipts'] = [*journal.get('bootstrap_receipts', []), proof['receipt_hash']]
        _put(session, key, journal, signing_key, row); session.commit()
    return {'state': 'owned', 'remote_id': owner.remote_id, 'receipt_hash': proof['receipt_hash']}
