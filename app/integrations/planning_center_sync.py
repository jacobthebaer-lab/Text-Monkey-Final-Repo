"""Three-way reconciliation of explicitly linked event metadata over the API.

The common baseline and any unknown PATCH outcome live in the same database as
the event. No names are used to guess links, and conflicting edits never win
solely because one side happened to be polled last.
"""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select

from app.db.models import Event, Policy, Volunteer
from app.integrations.planning_center import (
    PCOClient, PCOEventLink, PCOVolunteerPerson, PlanningCenterError, _id, _time, relation, sync_schedule,
)
from app.integrations.planning_center_staffing import _claim, _release, _verify_org

PREFIX = 'pco_event_sync:'
AVAILABILITY_PREFIX = 'pco_native_availability:'
PROFILE_PREFIX = 'pco_profile_sync:'
CURSOR_PREFIX = 'pco_event_sync_cursor:'


def sync_metadata_tick(factory, settings, config, now, *, client_factory=PCOClient):
    if not settings.pco_sync_enabled:
        return {'disabled': True}
    with client_factory(config) as client:
        with factory() as session:
            imported = sync_schedule(session, client, config, create_only=True)
            session.commit()
        availability = sync_mapped_availability(factory, client, config, now)
        return {'import': imported, 'availability': availability,
            'events': sync_linked_events(factory, client, config, now,
                write_enabled=settings.pco_staffing_write_enabled),
            'profiles': sync_mapped_names(factory, client, config, now,
                write_enabled=settings.pco_staffing_write_enabled)}


def _remote_name(client, config, mapping, volunteer):
    # Cross-application numeric IDs alone never establish person identity.
    if mapping.organization_id != config.organization_id:
        raise PlanningCenterError('Profile mapping belongs to another organization')
    if str(client.request('GET', '/people/v2')['data']['id']) != config.organization_id:
        raise PlanningCenterError('People application belongs to another organization')
    pid = _id(mapping.person_id)
    person = client.request('GET', f'/people/v2/people/{pid}')['data']
    phones = client.collection(f'/people/v2/people/{pid}/phone_numbers')
    matching = [p for p in phones if p.get('type') == 'PhoneNumber' and
                str(relation(p, 'person')) == pid and p['attributes'].get('e164') == volunteer.phone]
    if person.get('type') != 'Person' or person['id'] != pid or len(matching) != 1:
        raise PlanningCenterError('People identity needs an exact independently verified phone match')
    return person['attributes']['name']


def _load_sync_snapshot(session, kind, ident, *, locked=False):
    model = PCOEventLink if kind == 'event' else PCOVolunteerPerson
    column = model.key if kind == 'event' else model.id
    def row(model, condition):
        query = select(model).where(condition).execution_options(populate_existing=True)
        return session.scalar(query.with_for_update() if locked else query)
    link = row(model, column == ident)
    if link is None:
        raise PlanningCenterError('Sync identity mapping disappeared')
    if kind == 'event':
        record = row(Event, Event.id == link.event_id)
        if record is None or record.gcal_event_id != 'pco:' + link.key:
            raise PlanningCenterError('Local event identity changed')
        key = PREFIX + link.key
        identity = {k: getattr(link, k) for k in
                    ('key', 'organization_id', 'service_type_id', 'plan_id', 'event_id')}
        current = _local(record)
        extra = {'status': record.status, 'gcal_event_id': record.gcal_event_id}
    else:
        record = row(Volunteer, Volunteer.id == link.volunteer_id)
        if record is None:
            raise PlanningCenterError('Mapped local profile disappeared')
        key = (AVAILABILITY_PREFIX if kind == 'availability' else PROFILE_PREFIX) + str(link.volunteer_id)
        identity = {k: getattr(link, k) for k in ('id', 'organization_id', 'volunteer_id', 'person_id')}
        if kind == 'availability':
            identity['created_at'] = link.created_at
        current, extra = record.name, {'phone': record.phone}
    policy = row(Policy, Policy.key == key)
    if kind != 'availability' and policy and not isinstance(policy.value, dict):
        raise PlanningCenterError('Malformed saved sync baseline')
    return {'kind': kind, 'ident': ident, 'link': identity, 'record_id': record.id,
            'current': current, 'extra': extra, 'policy_key': key,
            'policy': deepcopy(policy.value) if policy else None}


def _read_snapshot(factory, kind, ident):
    # Close the read transaction before any remote call.
    with factory() as session:
        return _load_sync_snapshot(session, kind, ident)


_CHECK_ONLY = object()


def _fence(factory, snapshot, value=_CHECK_ONLY, *, local=None):
    """Compare identity, local fields and baseline in one short DB transaction.

    SQLite's immediate transaction serializes this compare/update; Postgres locks
    the mapping, record and policy rows. No network call runs inside this fence.
    """
    with factory() as session:
        if session.get_bind().dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        try:
            fresh = _load_sync_snapshot(session, snapshot['kind'], snapshot['ident'], locked=True)
        except (PlanningCenterError, KeyError, TypeError, ValueError):
            return False
        if fresh != snapshot:
            return False
        if value is not _CHECK_ONLY:
            policy = session.get(Policy, snapshot['policy_key'])
            if policy is None:
                policy = Policy(key=snapshot['policy_key'], value={}); session.add(policy)
            policy.value = deepcopy(value)
            if local is not None:
                if snapshot['kind'] == 'event':
                    record = session.get(Event, snapshot['record_id'])
                    record.title = local['title']
                    record.starts_at, record.ends_at = _time(local['starts_at']), _time(local['ends_at'])
                else:
                    session.get(Volunteer, snapshot['record_id']).name = local
        session.commit()
        return True


def _require_fence(factory, snapshot):
    if not _fence(factory, snapshot):
        raise PlanningCenterError('Local fields, identity or sync baseline changed during API operation')


def _pending_snapshot(snapshot, value):
    pending = deepcopy(snapshot)
    pending['policy'] = deepcopy(value)
    return pending


def sync_mapped_names(factory, client, config, now, *, write_enabled=False):
    """Sync existing mapped profiles. No accounts, access grants or consent changes."""
    _verify_org(client, config)
    report = {'baselined': 0, 'pulled': 0, 'pushed': 0, 'unchanged': 0, 'held': 0}
    with factory() as session:
        ids = list(session.scalars(select(PCOVolunteerPerson.id).where(
            PCOVolunteerPerson.organization_id == config.organization_id)))
    for ident in ids:
        snapshot, value = None, None
        try:
            snapshot = _read_snapshot(factory, 'profile', ident)
            mapping = SimpleNamespace(**snapshot['link'])
            volunteer = SimpleNamespace(name=snapshot['current'], phone=snapshot['extra']['phone'])
            value = deepcopy(snapshot['policy'] or {})
            current = snapshot['current']
            remote = _remote_name(client, config, mapping, volunteer)
            _require_fence(factory, snapshot)
            baseline, pending = value.get('baseline'), value.get('pending')
            if value.get('person_id', mapping.person_id) != mapping.person_id:
                raise PlanningCenterError('Profile identity mapping changed')
            if pending and (current != pending or remote not in (baseline, pending)):
                raise PlanningCenterError('Unknown profile write conflicts with newer edits')
            local, action = None, None
            if current == remote:
                value.update(baseline=current, pending=None, reason=None)
                action = 'baselined' if baseline is None else 'unchanged'
            elif baseline is None:
                raise PlanningCenterError('Initial profile mismatch requires explicit reconciliation')
            elif current == baseline and not pending:
                local = remote
                value.update(baseline=remote, reason=None)
                action = 'pulled'
            elif remote == baseline:
                if not write_enabled:
                    raise PlanningCenterError('Local profile edit is waiting for API write activation')
                pieces = current.strip().split()
                if len(pieces) != 2:
                    raise PlanningCenterError('Structured first and last name review required')
                value.update(pending=current, reason='verifying_profile_write')
                value.update(person_id=mapping.person_id, checked_at=now.isoformat())
                if not _fence(factory, snapshot, value):
                    raise PlanningCenterError('Local profile changed before pending write commit')
                snapshot = _pending_snapshot(snapshot, value)
                if _remote_name(client, config, mapping, volunteer) != remote:
                    raise PlanningCenterError('Native profile changed before write')
                _require_fence(factory, snapshot)
                client.request('PATCH', f'/people/v2/people/{mapping.person_id}', data={'data': {
                    'type': 'Person', 'id': mapping.person_id, 'attributes': {
                        'first_name': pieces[0], 'last_name': pieces[1]}}})
                if _remote_name(client, config, mapping, volunteer) != current:
                    raise PlanningCenterError('Profile write did not verify')
                value.update(baseline=current, pending=None, reason=None)
                action = 'pushed'
            else:
                raise PlanningCenterError('Concurrent local and native profile edits require resolution')
            value.update(person_id=mapping.person_id, checked_at=now.isoformat())
            if not _fence(factory, snapshot, value, local=local):
                raise PlanningCenterError('Local profile changed before reconciliation commit')
            report[action] += 1
        except (PlanningCenterError, KeyError, TypeError, ValueError, AttributeError) as error:
            report['held'] += 1
            if snapshot is not None and value is not None:
                # A failed compare must never replace the newer baseline/identity.
                failed = deepcopy(snapshot['policy'] or {})
                failed.update(reason=str(error) if isinstance(error, PlanningCenterError)
                    else 'Malformed mapped profile', checked_at=now.isoformat())
                _fence(factory, snapshot, failed)
    return report


def _native_availability(client, config, mapping, phone, now):
    if mapping.organization_id != config.organization_id:
        raise PlanningCenterError('Availability mapping belongs to another organization')
    pid = _id(mapping.person_id)
    base = f'/services/v2/people/{pid}/blockouts'
    intervals, seen = [], set()
    for row in client.collection(base):
        ident = _id(row['id'])
        if (row.get('type') != 'Blockout' or ident in seen or
                str(relation(row, 'person')) != pid or
                str(relation(row, 'organization')) != config.organization_id):
            raise PlanningCenterError('Native blockout identity differs from mapped person')
        seen.add(ident)
        dates = client.collection(base + '/' + ident + '/blockout_dates')
        if not dates and row['attributes'].get('repeat_frequency') == 'no_repeat':
            dates = [{'type': 'BlockoutDate', 'attributes': {
                'starts_at_utc': row['attributes']['starts_at'],
                'ends_at_utc': row['attributes']['ends_at']}}]
        for date in dates:
            if date.get('type') != 'BlockoutDate':
                raise PlanningCenterError('Malformed native blockout occurrence')
            attrs = date['attributes']
            start, end = _time(attrs['starts_at_utc']), _time(attrs['ends_at_utc'])
            if end <= start:
                raise PlanningCenterError('Invalid native blockout interval')
            # Native finite all-day ranges include their final second. Local
            # eligibility uses half-open intervals, so retain that second while
            # leaving the following midnight available. Do not reinterpret
            # timed or recurring exclusions without this verified boundary.
            zone_name = attrs.get('time_zone') or row['attributes'].get('time_zone')
            if row['attributes'].get('repeat_frequency') == 'no_repeat' and zone_name:
                try:
                    local_end = end.astimezone(ZoneInfo(zone_name))
                except (ZoneInfoNotFoundError, TypeError) as error:
                    raise PlanningCenterError('Invalid native blockout timezone') from error
                if (local_end.hour, local_end.minute, local_end.second, local_end.microsecond) == (23, 59, 59, 0):
                    end += timedelta(seconds=1)
            intervals.append({'id': ident, 'starts_at': start.isoformat(), 'ends_at': end.isoformat()})
    return {'organization_id': config.organization_id, 'person_id': pid,
            'mapping_id': mapping.id,
            'mapping_created_at': _time(mapping.created_at.isoformat()).isoformat(),
            'phone_sha256': sha256(phone.encode()).hexdigest(),
            'intervals': intervals, 'reason': None, 'checked_at': now.isoformat()}


def sync_mapped_availability(factory, client, config, now):
    """Refresh runtime caches without holding DB transactions during native GETs."""
    _verify_org(client, config)
    with factory() as session:
        ids = list(session.scalars(select(PCOVolunteerPerson.id).where(
            PCOVolunteerPerson.organization_id == config.organization_id)))
    report = {'refreshed': 0, 'held': 0}
    for ident in ids:
        snapshot = None
        try:
            snapshot = _read_snapshot(factory, 'availability', ident)
            value = _native_availability(client, config, SimpleNamespace(**snapshot['link']),
                snapshot['extra']['phone'], now)
            if not _fence(factory, snapshot, value):
                raise PlanningCenterError('Availability identity or cache changed during native GET')
            report['refreshed'] += 1
        except (PlanningCenterError, KeyError, TypeError, ValueError, AttributeError) as error:
            report['held'] += 1
            if snapshot is not None:
                value = deepcopy(snapshot['policy']) if isinstance(snapshot['policy'], dict) else {}
                value['reason'] = str(error) if isinstance(error, PlanningCenterError) else 'Malformed native availability'
                _fence(factory, snapshot, value)
    return report


def refresh_mapped_availability(session, client, config, now):
    """Import real generated blockout intervals without broadening local consent.

    Explicit one-shot callers own this transaction. Runtime polling uses
    sync_mapped_availability's separate read/GET/fence transactions. Native
    constraints supplement local preferences; removing one cannot erase an
    independently stated local unavailable date.
    """
    _verify_org(client, config)
    report = {'refreshed': 0, 'held': 0}
    mappings = session.scalars(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == config.organization_id)).all()
    for mapping in mappings:
        key = AVAILABILITY_PREFIX + str(mapping.volunteer_id)
        state = session.get(Policy, key)
        if state is None:
            state = Policy(key=key, value={}); session.add(state)
        value = deepcopy(state.value) if isinstance(state.value, dict) else {}
        try:
            volunteer = session.get(Volunteer, mapping.volunteer_id)
            if volunteer is None:
                raise PlanningCenterError('Mapped availability profile disappeared')
            value.update(_native_availability(client, config, mapping, volunteer.phone, now))
            report['refreshed'] += 1
        except (PlanningCenterError, KeyError, TypeError, ValueError, AttributeError) as error:
            value['reason'] = str(error) if isinstance(error, PlanningCenterError) else 'Malformed native availability'
            report['held'] += 1
        state.value = value
    session.flush()
    return report


def native_availability_problem(session, volunteer, shift):
    state = session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id))
    if state is None:
        # Runtime sessions carry the decision clock and startup creates the PCO
        # tables. A mapped person must not be cleared before the first GET.
        if session.info.get('pco_availability_clock') and session.scalar(
                select(PCOVolunteerPerson.id).where(
                    PCOVolunteerPerson.volunteer_id == volunteer.id).limit(1)) is not None:
            return 'Planning Center availability has not been refreshed'
        return None
    try:
        value = state.value
        if not isinstance(value, dict) or value.get('reason'):
            return 'Planning Center availability needs reconciliation'
        organization_id = value.get('organization_id')
        if not isinstance(organization_id, str) or not organization_id.isdigit():
            return 'Planning Center availability needs reconciliation'
        # A fresh empty cache for a former identity is not clearance for its
        # replacement. Old cache versions hold until the normal refresh updates
        # them; no cloud/local account IDs or phone changes are silently adopted.
        mapping = session.scalar(select(PCOVolunteerPerson).where(
            PCOVolunteerPerson.organization_id == organization_id,
            PCOVolunteerPerson.volunteer_id == volunteer.id).execution_options(populate_existing=True))
        if (mapping is None or mapping.id != value['mapping_id'] or
                mapping.person_id != value['person_id'] or
                _time(mapping.created_at.isoformat()).isoformat() != value['mapping_created_at'] or
                sha256(volunteer.phone.encode()).hexdigest() != value['phone_sha256']):
            return 'Planning Center availability identity changed'
        clock = session.info.get('pco_availability_clock')
        if clock:
            checked = _time(state.value['checked_at'])
            now = clock.now()
            if checked > now or now - checked > timedelta(minutes=5):
                return 'Planning Center availability refresh is overdue'
        intervals = value['intervals']
        if not isinstance(intervals, list):
            raise PlanningCenterError('Malformed cached availability intervals')
        for interval in intervals:
            start, end = _time(interval['starts_at']), _time(interval['ends_at'])
            if end <= start:
                raise PlanningCenterError('Invalid cached availability interval')
            if start < shift.ends_at and end > shift.starts_at:
                return 'Unavailable in Planning Center for this interval'
    except (PlanningCenterError, KeyError, TypeError, ValueError):
        return 'Planning Center availability needs reconciliation'
    return None


def _local(event):
    if not event.title or event.ends_at <= event.starts_at:
        raise PlanningCenterError('Invalid local event metadata')
    return {'title': event.title, 'starts_at': _time(event.starts_at.isoformat()).isoformat(),
            'ends_at': _time(event.ends_at.isoformat()).isoformat()}


def _remote(client, config, link):
    parts = link.key.split(':')
    if (link.organization_id != config.organization_id or link.service_type_id not in config.service_type_ids
            or len(parts) != 4 or not all(p.isdigit() for p in parts) or
            parts[:3] != [config.organization_id, link.service_type_id, link.plan_id]):
        raise PlanningCenterError('Event mapping is outside the exact native scope')
    root = f'/services/v2/service_types/{link.service_type_id}/plans/{link.plan_id}'
    plan = client.request('GET', root)['data']
    if str(plan['id']) != link.plan_id or str(relation(plan, 'service_type')) != link.service_type_id:
        raise PlanningCenterError('Plan identity changed')
    times = [t for t in client.collection(root + '/plan_times') if t['id'] == parts[3]
             and t['attributes'].get('time_type') == 'service']
    if len(times) != 1:
        raise PlanningCenterError('Linked service time is missing or ambiguous')
    attrs = times[0]['attributes']
    start, end = _time(attrs['starts_at']), _time(attrs['ends_at'])
    if end <= start:
        raise PlanningCenterError('Native event interval is invalid')
    title = plan['attributes'].get('title')
    if not title:
        # Use the same projection as fetch_schedule. Planning Center permits
        # untitled plans; importing a service name then pulling an empty title
        # would otherwise corrupt the local event on its first metadata tick.
        service = client.request('GET', f'/services/v2/service_types/{link.service_type_id}')['data']
        if service.get('type') != 'ServiceType' or str(service.get('id')) != link.service_type_id:
            raise PlanningCenterError('Service identity changed')
        title = service['attributes']['name']
    if not isinstance(title, str) or not title:
        raise PlanningCenterError('Native event title is invalid')
    return {'title': title[:200], 'starts_at': start.isoformat(),
            'ends_at': end.isoformat()}


def _patch(client, link, current, desired, *, fence):
    root = f'/services/v2/service_types/{link.service_type_id}'
    if current['title'] != desired['title']:
        fence()
        client.request('PATCH', root + '/plans/' + link.plan_id, data={'data': {
            'type': 'Plan', 'id': link.plan_id, 'attributes': {'title': desired['title']}}})
    interval = {k: desired[k] for k in ('starts_at', 'ends_at') if desired[k] != current[k]}
    if interval:
        fence()
        ident = link.key.rsplit(':', 1)[1]
        client.request('PATCH', root + '/plan_times/' + ident, data={'data': {
            'type': 'PlanTime', 'id': ident, 'attributes': interval}})


def _event_batch(factory, config, now, limit):
    """Rotate a durable, scope-specific cursor so older links cannot starve others.

    Selection and cursor persistence are short local operations. The lease only
    serializes batch allocation; plan leases still guard individual API writes.
    Advancing before HTTP is safe: failed/leased rows return on the next cycle.
    """
    scope = ':'.join([config.organization_id, *sorted(set(config.service_type_ids))])
    cursor_key = CURSOR_PREFIX + sha256(scope.encode()).hexdigest()
    owner = _claim(factory, cursor_key, now)
    if owner is None:
        return []
    try:
        with factory() as session:
            cursor = session.get(Policy, cursor_key)
            after = cursor.value.get('after', '') if cursor and isinstance(cursor.value, dict) else ''
            if not isinstance(after, str):
                after = ''
            query = select(PCOEventLink.key).where(
                PCOEventLink.organization_id == config.organization_id,
                PCOEventLink.service_type_id.in_(config.service_type_ids)).order_by(PCOEventLink.key)
            keys = list(session.scalars(query.where(PCOEventLink.key > after).limit(limit)))
            if after and len(keys) < limit:
                keys.extend(session.scalars(query.where(PCOEventLink.key <= after).limit(limit-len(keys))))
            if keys:
                if cursor is None:
                    cursor = Policy(key=cursor_key, value={}); session.add(cursor)
                cursor.value = {'after': keys[-1]}
            session.commit()
            return keys
    finally:
        _release(factory, cursor_key, owner)


def sync_linked_events(factory, client, config, now, *, write_enabled=False, limit=100):
    """Reconcile titles and exact intervals. No people, consent or delivery writes."""
    _verify_org(client, config)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise PlanningCenterError('Event sync limit must be between 1 and 100')
    keys = _event_batch(factory, config, now, limit)
    report = {'baselined': 0, 'pulled': 0, 'pushed': 0, 'unchanged': 0, 'held': 0}
    for key in keys:
        plan_key = ':'.join(key.split(':')[:3])
        owner = _claim(factory, plan_key, now)
        if owner is None:
            continue
        snapshot, value = None, None
        try:
            snapshot = _read_snapshot(factory, 'event', key)
            link = SimpleNamespace(**snapshot['link'])
            value = deepcopy(snapshot['policy'] or {})
            current, remote = snapshot['current'], _remote(client, config, link)
            _require_fence(factory, snapshot)
            baseline, target = value.get('baseline'), value.get('pending')
            if snapshot['extra']['status'] != 'scheduled':
                raise PlanningCenterError('Event cancellation requires an explicit native action')
            local, action = None, None
            if target:
                if (current != target or not baseline or any(
                        remote[k] not in (baseline[k], target[k]) for k in target)):
                    raise PlanningCenterError('Unknown event write conflicts with newer edits')
                if remote != target:
                    if not write_enabled:
                        raise PlanningCenterError('Unknown event write requires reconciliation')
                    _patch(client, link, remote, target, fence=lambda: _require_fence(factory, snapshot))
                    remote = _remote(client, config, link)
                    if remote != target:
                        raise PlanningCenterError('Event write did not verify')
                value.update(baseline=target, pending=None, reason=None)
                action = 'pushed'
            elif baseline is None:
                if current != remote:
                    raise PlanningCenterError('Initial event mismatch requires explicit reconciliation')
                value.update(baseline=current, reason=None)
                action = 'baselined'
            elif current == remote:
                value.update(baseline=current, reason=None)
                action = 'unchanged'
            elif current == baseline:
                local = remote
                value.update(baseline=remote, reason=None)
                action = 'pulled'
            elif remote == baseline:
                if not write_enabled:
                    raise PlanningCenterError('Local event edit is waiting for API write activation')
                value.update(pending=current, reason='verifying_event_write', checked_at=now.isoformat())
                if not _fence(factory, snapshot, value):
                    raise PlanningCenterError('Local event changed before pending write commit')
                snapshot = _pending_snapshot(snapshot, value)
                fresh = _remote(client, config, link)
                if fresh != remote:
                    raise PlanningCenterError('Native event changed before write')
                _patch(client, link, fresh, current, fence=lambda: _require_fence(factory, snapshot))
                if _remote(client, config, link) != current:
                    raise PlanningCenterError('Event write did not verify')
                value.update(baseline=current, pending=None, reason=None)
                action = 'pushed'
            else:
                raise PlanningCenterError('Concurrent local and native event edits require resolution')
            value['checked_at'] = now.isoformat()
            if not _fence(factory, snapshot, value, local=local):
                raise PlanningCenterError('Local event changed before reconciliation commit')
            report[action] += 1
        except (PlanningCenterError, KeyError, TypeError, ValueError) as error:
            report['held'] += 1
            if snapshot is not None and value is not None:
                failed = deepcopy(snapshot['policy'] or {})
                failed.update(reason=str(error) if isinstance(error, PlanningCenterError)
                    else 'Malformed event metadata', checked_at=now.isoformat())
                _fence(factory, snapshot, failed)
        finally:
            _release(factory, plan_key, owner)
    return report
