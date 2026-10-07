"""Three-way reconciliation of explicitly linked event metadata over the API.

The common baseline and any unknown PATCH outcome live in the same database as
the event. No names are used to guess links, and conflicting edits never win
solely because one side happened to be polled last.
"""
from datetime import timedelta
from sqlalchemy import select

from app.db.models import Event, Policy, Volunteer
from app.integrations.planning_center import (
    PCOClient, PCOEventLink, PCOVolunteerPerson, PlanningCenterError, _id, _time, relation, sync_schedule,
)
from app.integrations.planning_center_staffing import _claim, _release, _verify_org

PREFIX = 'pco_event_sync:'
AVAILABILITY_PREFIX = 'pco_native_availability:'
PROFILE_PREFIX = 'pco_profile_sync:'


def sync_metadata_tick(factory, settings, config, now, *, client_factory=PCOClient):
    if not settings.pco_sync_enabled:
        return {'disabled': True}
    with client_factory(config) as client:
        with factory() as session:
            imported = sync_schedule(session, client, config, create_only=True)
            availability = refresh_mapped_availability(session, client, config, now)
            session.commit()
        return {'import': imported, 'availability': availability,
            'events': sync_linked_events(factory, client, config, now,
                write_enabled=settings.pco_staffing_write_enabled),
            'profiles': sync_mapped_names(factory, client, config, now,
                write_enabled=settings.pco_staffing_write_enabled)}


def _remote_name(client, config, mapping, volunteer):
    # Cross-application numeric IDs alone never establish person identity.
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


def sync_mapped_names(factory, client, config, now, *, write_enabled=False):
    """Sync existing mapped profiles. No accounts, access grants or consent changes."""
    _verify_org(client, config)
    report = {'baselined': 0, 'pulled': 0, 'pushed': 0, 'unchanged': 0, 'held': 0}
    with factory() as session:
        ids = list(session.scalars(select(PCOVolunteerPerson.id).where(
            PCOVolunteerPerson.organization_id == config.organization_id)))
    for ident in ids:
        with factory() as session:
            mapping = session.get(PCOVolunteerPerson, ident)
            volunteer = session.get(Volunteer, mapping.volunteer_id)
            state = session.get(Policy, PROFILE_PREFIX + str(mapping.volunteer_id))
            if state is None:
                state = Policy(key=PROFILE_PREFIX + str(mapping.volunteer_id), value={}); session.add(state)
            value = dict(state.value)
            try:
                current = volunteer.name
                remote = _remote_name(client, config, mapping, volunteer)
                baseline, pending = value.get('baseline'), value.get('pending')
                if value.get('person_id', mapping.person_id) != mapping.person_id:
                    raise PlanningCenterError('Profile identity mapping changed')
                if pending and (current != pending or remote not in (baseline, pending)):
                    raise PlanningCenterError('Unknown profile write conflicts with newer edits')
                if current == remote:
                    value.update(baseline=current, pending=None, reason=None)
                    report['baselined' if baseline is None else 'unchanged'] += 1
                elif baseline is None:
                    raise PlanningCenterError('Initial profile mismatch requires explicit reconciliation')
                elif current == baseline and not pending:
                    volunteer.name = remote
                    value.update(baseline=remote, reason=None)
                    report['pulled'] += 1
                elif remote == baseline:
                    if not write_enabled:
                        raise PlanningCenterError('Local profile edit is waiting for API write activation')
                    pieces = current.strip().split()
                    if len(pieces) != 2:
                        raise PlanningCenterError('Structured first and last name review required')
                    value.update(pending=current, reason='verifying_profile_write')
                    state.value = value; session.commit(); value = dict(value)
                    if _remote_name(client, config, mapping, volunteer) != remote:
                        raise PlanningCenterError('Native profile changed before write')
                    client.request('PATCH', f'/people/v2/people/{mapping.person_id}', data={'data': {
                        'type': 'Person', 'id': mapping.person_id, 'attributes': {
                            'first_name': pieces[0], 'last_name': pieces[1]}}})
                    if _remote_name(client, config, mapping, volunteer) != current:
                        raise PlanningCenterError('Profile write did not verify')
                    value.update(baseline=current, pending=None, reason=None)
                    report['pushed'] += 1
                else:
                    raise PlanningCenterError('Concurrent local and native profile edits require resolution')
            except (PlanningCenterError, KeyError, TypeError, ValueError, AttributeError) as error:
                value['reason'] = str(error) if isinstance(error, PlanningCenterError) else 'Malformed mapped profile'
                report['held'] += 1
            value.update(person_id=mapping.person_id, checked_at=now.isoformat())
            state.value = value; session.commit()
    return report


def refresh_mapped_availability(session, client, config, now):
    """Import real generated blockout intervals without broadening local consent.

    These native constraints supplement local preferences. Removing a native
    blockout cannot erase an independently stated local unavailable date.
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
        value = dict(state.value)
        try:
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
                    intervals.append({'id': ident, 'starts_at': start.isoformat(), 'ends_at': end.isoformat()})
            value.update(organization_id=config.organization_id, person_id=pid,
                         intervals=intervals, reason=None, checked_at=now.isoformat())
            report['refreshed'] += 1
        except (PlanningCenterError, KeyError, TypeError, ValueError) as error:
            value['reason'] = str(error) if isinstance(error, PlanningCenterError) else 'Malformed native availability'
            report['held'] += 1
        state.value = value
    session.flush()
    return report


def native_availability_problem(session, volunteer, shift):
    state = session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id))
    if state is None:
        return None
    try:
        if state.value.get('reason'):
            return 'Planning Center availability needs reconciliation'
        clock = session.info.get('pco_availability_clock')
        if clock:
            checked = _time(state.value['checked_at'])
            now = clock.now()
            if checked > now or now - checked > timedelta(minutes=5):
                return 'Planning Center availability refresh is overdue'
        for interval in state.value['intervals']:
            if (_time(interval['starts_at']) < shift.ends_at and
                    _time(interval['ends_at']) > shift.starts_at):
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
    if (len(parts) != 4 or not all(p.isdigit() for p in parts) or
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
    return {'title': plan['attributes']['title'], 'starts_at': start.isoformat(),
            'ends_at': end.isoformat()}


def _patch(client, link, current, desired):
    root = f'/services/v2/service_types/{link.service_type_id}'
    if current['title'] != desired['title']:
        client.request('PATCH', root + '/plans/' + link.plan_id, data={'data': {
            'type': 'Plan', 'id': link.plan_id, 'attributes': {'title': desired['title']}}})
    interval = {k: desired[k] for k in ('starts_at', 'ends_at') if desired[k] != current[k]}
    if interval:
        ident = link.key.rsplit(':', 1)[1]
        client.request('PATCH', root + '/plan_times/' + ident, data={'data': {
            'type': 'PlanTime', 'id': ident, 'attributes': interval}})


def sync_linked_events(factory, client, config, now, *, write_enabled=False, limit=100):
    """Reconcile titles and exact intervals. No people, consent or delivery writes."""
    _verify_org(client, config)
    if not 1 <= limit <= 100:
        raise PlanningCenterError('Event sync limit must be between 1 and 100')
    with factory() as session:
        keys = list(session.scalars(select(PCOEventLink.key).where(
            PCOEventLink.organization_id == config.organization_id,
            PCOEventLink.service_type_id.in_(config.service_type_ids)).order_by(PCOEventLink.key).limit(limit)))
    report = {'baselined': 0, 'pulled': 0, 'pushed': 0, 'unchanged': 0, 'held': 0}
    for key in keys:
        plan_key = ':'.join(key.split(':')[:3])
        owner = _claim(factory, plan_key, now)
        if owner is None:
            continue
        try:
            with factory() as session:
                link = session.get(PCOEventLink, key)
                event = session.get(Event, link.event_id)
                if not event or event.gcal_event_id != 'pco:' + key:
                    raise PlanningCenterError('Local event identity changed')
                state = session.get(Policy, PREFIX + key)
                if state is None:
                    state = Policy(key=PREFIX + key, value={}); session.add(state)
                value = dict(state.value)
                try:
                    current, remote = _local(event), _remote(client, config, link)
                    baseline = value.get('baseline')
                    target = value.get('pending')
                    if event.status != 'scheduled':
                        raise PlanningCenterError('Event cancellation requires an explicit native action')
                    if target:
                        # A process can stop between either PATCH and readback.
                        # Only the original or desired field values are admissible.
                        if (current != target or not baseline or any(
                                remote[k] not in (baseline[k], target[k]) for k in target)):
                            raise PlanningCenterError('Unknown event write conflicts with newer edits')
                        if remote != target:
                            if not write_enabled:
                                raise PlanningCenterError('Unknown event write requires reconciliation')
                            _patch(client, link, remote, target)
                            remote = _remote(client, config, link)
                            if remote != target:
                                raise PlanningCenterError('Event write did not verify')
                        value.update(baseline=target, pending=None, reason=None)
                        report['pushed'] += 1
                    elif baseline is None:
                        if current != remote:
                            raise PlanningCenterError('Initial event mismatch requires explicit reconciliation')
                        value.update(baseline=current, reason=None)
                        report['baselined'] += 1
                    elif current == remote:
                        value.update(baseline=current, reason=None)
                        report['unchanged'] += 1
                    elif current == baseline:
                        event.title = remote['title']
                        event.starts_at, event.ends_at = _time(remote['starts_at']), _time(remote['ends_at'])
                        value.update(baseline=remote, reason=None)
                        report['pulled'] += 1
                    elif remote == baseline:
                        if not write_enabled:
                            raise PlanningCenterError('Local event edit is waiting for API write activation')
                        # Commit the intended fields before HTTP; never infer that
                        # a timeout means the API did not receive the mutation.
                        value.update(pending=current, reason='verifying_event_write')
                        state.value = value; session.commit()
                        value = dict(value)  # JSON mutations after commit need a fresh value.
                        fresh = _remote(client, config, link)
                        if fresh != remote:
                            raise PlanningCenterError('Native event changed before write')
                        _patch(client, link, fresh, current)
                        if _remote(client, config, link) != current:
                            raise PlanningCenterError('Event write did not verify')
                        value.update(baseline=current, pending=None, reason=None)
                        report['pushed'] += 1
                    else:
                        raise PlanningCenterError('Concurrent local and native event edits require resolution')
                except (PlanningCenterError, KeyError, TypeError, ValueError) as error:
                    value['reason'] = str(error) if isinstance(error, PlanningCenterError) else 'Malformed event metadata'
                    report['held'] += 1
                value['checked_at'] = now.isoformat()
                state.value = value; session.commit()
        finally:
            _release(factory, plan_key, owner)
    return report
