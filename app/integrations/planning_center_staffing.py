"""Scoped plan-wide staffing bridge. No people creation, consent or notifications.

Assignment observers enqueue in the caller's transaction. The worker owns separate
transactions, committing a serialized claim and unknown outcome before HTTP.
"""
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

from sqlalchemy import event, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import eligibility
from app.db.models import Assignment, Event, FillRequest, RoleRecipe, Shift, Volunteer
from app.integrations.planning_center import (
    PCOClient, PCOConfig, PCOEventLink, PCOPositionScope, PCOShiftLink,
    PCOStaffingIntent, PCOStaffingLease, PCOStaffingLink, PCOStaffingPoll,
    PCOVolunteerPerson, PlanningCenterError, _id, _time, relation, team_service_scope_matches,
)

CONTEXT = 'pco_staffing_context'
SUPPRESS = 'pco_staffing_suppress_echo'
ACTIVE_LOCAL = {'proposed', 'approved', 'confirmed'}
ACTION_STATUS = {'reserve': 'approved', 'accept': 'confirmed', 'cancel': 'cancelled'}
NATIVE_STATUS = {'reserve': 'U', 'accept': 'C', 'cancel': 'D'}
POLL_INTERVAL = timedelta(seconds=60)
LEASE_TIME = timedelta(minutes=10)


class StaffingPending(PlanningCenterError):
    """A prerequisite is still pending; retry without repeating an unknown write."""


class StaffingLeaseLost(PlanningCenterError):
    """Another worker owns this plan; do not mutate its intent or write remotely."""


def _status(row):
    return {'Confirmed': 'C', 'Unconfirmed': 'U', 'Declined': 'D'}.get(
        row.get('attributes', {}).get('status'), row.get('attributes', {}).get('status'))


def _snapshot(row):
    if row is None:
        return None
    attrs = row.get('attributes', {})
    return {'id': _id(row['id']), 'status': _status(row), 'updated_at': attrs.get('updated_at'),
            'person': relation(row, 'person'), 'team': relation(row, 'team'),
            'position': attrs.get('team_position_name')}


def _silent(row):
    attrs = row.get('attributes', {})
    return (attrs.get('prepare_notification') is False
            and 'notification_prepared_at' in attrs and attrs['notification_prepared_at'] is None)


def _base(scope):
    return f'/services/v2/service_types/{scope.service_type_id}/plans/{scope.plan_id}'


def _verify_org(client, config):
    config.require_scope()
    if str(client.organization()['id']) != config.organization_id:
        raise PlanningCenterError('Credential organization differs from staffing scope')


def _resource(client, path, kind, ident):
    row = client.request('GET', path)['data']
    if row.get('type') != kind or str(row.get('id')) != str(ident):
        raise PlanningCenterError('Planning Center resource identity differs from mapping')
    return row


def _position_memberships(client, scope):
    """Validate native identity and preference shape before considering eligibility."""
    rows = client.collection(f'/services/v2/service_types/{scope.service_type_id}/team_positions/'
                             f'{scope.position_id}/person_team_position_assignments')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or row.get('type') != 'PersonTeamPositionAssignment'
                or not isinstance(row.get('id'), str) or not row['id'].isdigit()
                or row['id'] in seen):
            raise PlanningCenterError('Malformed or duplicate native position membership')
        seen.add(row['id'])
        attrs = row.get('attributes')
        if (not isinstance(attrs, dict) or not isinstance(attrs.get('schedule_preference'), str)
                or not attrs['schedule_preference'].strip()):
            raise PlanningCenterError('Native position membership preference is missing or malformed')
        relationships = row.get('relationships')
        if not isinstance(relationships, dict):
            raise PlanningCenterError('Native position membership relationships are missing')
        for name, kind in (('person', 'Person'), ('team_position', 'TeamPosition')):
            relationship = relationships.get(name)
            data = relationship.get('data') if isinstance(relationship, dict) else None
            if (not isinstance(data, dict) or data.get('type') != kind
                    or not isinstance(data.get('id'), str) or not data['id'].isdigit()
                    or name == 'team_position' and data['id'] != scope.position_id):
                raise PlanningCenterError('Native position membership identity differs from exact scope')
    return rows


def map_volunteer(session, config, volunteer_id, person_id, now, *, client):
    """Explicit Services identity, verified remotely; never changes SMS consent."""
    _verify_org(client, config)
    person_id = _id(person_id)
    if session.get(Volunteer, volunteer_id) is None:
        raise PlanningCenterError('Local volunteer does not exist')
    _resource(client, f'/services/v2/people/{person_id}', 'Person', person_id)
    existing = session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == config.organization_id,
        PCOVolunteerPerson.volunteer_id == volunteer_id))
    collision = session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == config.organization_id,
        PCOVolunteerPerson.person_id == person_id))
    if collision and collision.volunteer_id != volunteer_id or existing and existing.person_id != person_id:
        raise PlanningCenterError('Ambiguous or changed Person mapping requires review')
    if existing is None:
        existing = PCOVolunteerPerson(organization_id=config.organization_id,
            volunteer_id=volunteer_id, person_id=person_id, created_at=now)
        session.add(existing)
    session.flush()
    return existing


def _read_scope(client, config, scope, *, event_row=None):
    for value in (scope.organization_id, scope.service_type_id, scope.plan_id,
                  scope.team_id, scope.position_id, scope.plan_time_id):
        _id(value)
    _verify_org(client, config)
    if scope.organization_id != config.organization_id or scope.service_type_id not in config.service_type_ids:
        raise PlanningCenterError('Position is outside configured organization/service scope')
    plan = _resource(client, _base(scope), 'Plan', scope.plan_id)
    if str(relation(plan, 'service_type')) != scope.service_type_id:
        raise PlanningCenterError('Plan service-type mapping changed')
    times = [x for x in client.collection(_base(scope)+'/plan_times')
             if x.get('attributes', {}).get('time_type') == 'service']
    if len(times) != 1 or str(times[0]['id']) != scope.plan_time_id:
        raise PlanningCenterError('Unsupported split/multiple service times or changed time mapping')
    if event_row is not None:
        attrs = times[0]['attributes']
        if (_time(attrs['starts_at']) != event_row.starts_at
                or _time(attrs['ends_at']) != event_row.ends_at):
            raise PlanningCenterError('Service time changed; refresh schedule before staffing')
    team = _resource(client, f'/services/v2/teams/{scope.team_id}', 'Team', scope.team_id)
    if (team['attributes'].get('schedule_to') != 'plan'
            or not team_service_scope_matches(team, scope.service_type_id)
            or team['attributes'].get('archived_at') or team['attributes'].get('deleted_at')):
        raise PlanningCenterError('Unsupported split, archived or changed team')
    position = _resource(client, f'/services/v2/service_types/{scope.service_type_id}/team_positions/{scope.position_id}',
                         'TeamPosition', scope.position_id)
    if relation(position, 'team') != scope.team_id or position['attributes'].get('name') != scope.position_name:
        raise PlanningCenterError('Team position mapping changed')
    rows = client.collection(_base(scope)+'/team_members')
    for row in rows:
        if row.get('type') != 'PlanPerson' or _status(row) not in {'C', 'U', 'D'}:
            raise PlanningCenterError('Malformed or unsupported PlanPerson staffing')
        if (str(relation(row, 'plan')) != scope.plan_id
                or str(relation(row, 'service_type')) != scope.service_type_id):
            raise PlanningCenterError('PlanPerson belongs to another plan/service scope')
        if row.get('attributes', {}).get('can_accept_partial'):
            raise PlanningCenterError('Partial PlanPerson acceptance requires review')
        times_data = row.get('relationships', {}).get('service_times', {}).get('data')
        if not isinstance(times_data, list) or {str(x['id']) for x in times_data} != {scope.plan_time_id}:
            raise PlanningCenterError('PlanPerson service time is missing or ambiguous')
    needs = client.collection(_base(scope)+'/needed_positions')
    relevant = [n for n in needs if str(relation(n, 'team')) == scope.team_id
                and n.get('attributes', {}).get('team_position_name') == scope.position_name]
    if any(relation(n, 'time') or relation(n, 'plan_time') for n in relevant):
        raise PlanningCenterError('Per-time staffing need is unsupported')
    quantities = [n.get('attributes', {}).get('quantity') for n in relevant]
    if any(type(q) is not int or q < 0 for q in quantities):
        raise PlanningCenterError('Malformed open need quantity')
    return rows, sum(quantities)


def map_position(session, client, config, *, shift_id, team_id, position_id, plan_time_id, now):
    """Review a specific imported role/plan against real IDs, never its label."""
    shift = session.get(Shift, shift_id)
    event_link = session.scalar(select(PCOEventLink).where(PCOEventLink.event_id == shift.event_id)) if shift else None
    if not event_link or event_link.organization_id != config.organization_id:
        raise PlanningCenterError('Position mapping requires an imported scoped shift')
    shift_link = session.scalar(select(PCOShiftLink).where(PCOShiftLink.shift_id == shift_id))
    if not shift_link or shift_link.event_key != event_link.key or shift.event.gcal_event_id != 'pco:'+event_link.key:
        raise PlanningCenterError('Imported event/shift mapping is stale')
    position = _resource(client, f'/services/v2/service_types/{event_link.service_type_id}/team_positions/{_id(position_id)}',
                         'TeamPosition', position_id)
    scope = PCOPositionScope(key=f'{config.organization_id}:{event_link.service_type_id}:{event_link.plan_id}:{position_id}',
        organization_id=config.organization_id, service_type_id=event_link.service_type_id,
        plan_id=event_link.plan_id, event_id=shift.event_id, role_id=shift.role_id,
        team_id=_id(team_id), position_id=_id(position_id), position_name=position['attributes']['name'],
        plan_time_id=_id(plan_time_id), verified_at=now)
    _read_scope(client, config, scope, event_row=shift.event)
    collision = session.scalar(select(PCOPositionScope).where(
        PCOPositionScope.event_id == scope.event_id, PCOPositionScope.role_id == scope.role_id))
    if collision and (collision.key != scope.key or collision.team_id != scope.team_id
                      or collision.plan_time_id != scope.plan_time_id):
        raise PlanningCenterError('Role already has a different position/time mapping')
    if collision:
        from app.integrations.planning_center_role_bindings import verified_record
        verified_record(session, config, collision)
        return collision
    session.add(scope); session.flush()
    return scope


def _local_snapshot(assignment):
    return {'status': assignment.status, 'updated_at': assignment.updated_at.astimezone(timezone.utc).isoformat(),
            'shift_id': assignment.shift_id, 'volunteer_id': assignment.volunteer_id}


def enqueue_staffing_intent(session, config, *, assignment_id, action, now,
                            replaced_assignment_id=None, reconcile_existing=False, flush=True):
    config.require_scope()
    assignment = session.get(Assignment, assignment_id)
    if not assignment or action not in ACTION_STATUS:
        raise PlanningCenterError('Staffing intent needs an existing assignment and supported action')
    if assignment.shift.parent_shift_id is not None:
        raise PlanningCenterError('Reviewed child intervals require a separately verified Planning Center partial-time contract')
    if assignment.status != ACTION_STATUS[action]:
        raise PlanningCenterError('Local assignment has not made the requested authoritative transition')
    event_link = session.scalar(select(PCOEventLink).where(PCOEventLink.event_id == assignment.shift.event_id))
    if not event_link or event_link.organization_id != config.organization_id or event_link.service_type_id not in config.service_type_ids:
        raise PlanningCenterError('Assignment is outside imported service scope')
    scope = session.scalar(select(PCOPositionScope).where(
        PCOPositionScope.event_id == assignment.shift.event_id,
        PCOPositionScope.role_id == assignment.shift.role_id,
        PCOPositionScope.organization_id == config.organization_id))
    mapping = session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == config.organization_id,
        PCOVolunteerPerson.volunteer_id == assignment.volunteer_id))
    previous = session.scalar(select(PCOStaffingIntent).where(
        PCOStaffingIntent.organization_id == config.organization_id,
        PCOStaffingIntent.assignment_id == assignment_id).order_by(PCOStaffingIntent.id.desc()))
    local = _local_snapshot(assignment)
    if (previous and previous.action == action and previous.expected.get('local') == local
            and not (reconcile_existing and previous.state == 'held')):
        return previous
    link = session.get(PCOStaffingLink, assignment_id)
    reason = None if scope and mapping else 'Explicit Person and plan/team/position mapping required'
    revision = (previous.expected.get('revision', 0)+1) if previous else 1
    intent = PCOStaffingIntent(idempotency_key=f'{config.organization_id}:{assignment_id}:{revision}',
        organization_id=config.organization_id, service_type_id=event_link.service_type_id,
        plan_id=event_link.plan_id, team_id=scope.team_id if scope else '', assignment_id=assignment_id,
        person_id=mapping.person_id if mapping else '', action=action,
        state='held' if reason else 'pending', reason=reason, attempts=0, created_at=now, updated_at=now,
        expected={'local': local, 'revision': revision, 'scope_key': scope.key if scope else None,
                  'remote': (link.remote_snapshot or None) if link else None, 'adopt_existing': reconcile_existing},
        depends_on=None)
    if replaced_assignment_id:
        prior = session.scalar(select(PCOStaffingIntent).where(
            PCOStaffingIntent.assignment_id == replaced_assignment_id,
            PCOStaffingIntent.action == 'cancel').order_by(PCOStaffingIntent.id.desc()))
        if prior:
            intent.depends_on = prior.id
    session.add(intent)
    if action == 'reserve' and scope:
        intent.expected = {**intent.expected,
            'reservation_capacity': {'required_total': _local_capacity(session, scope)}}
    if flush:
        session.flush()
    return intent


def enqueue_replacement(session, config, *, cancelled_assignment_id, replacement_assignment_id, now):
    cancelled = enqueue_staffing_intent(session, config, assignment_id=cancelled_assignment_id, action='cancel', now=now)
    accepted = enqueue_staffing_intent(session, config, assignment_id=replacement_assignment_id,
        action='accept', now=now, replaced_assignment_id=cancelled_assignment_id)
    return cancelled, accepted


def catch_up_assignments(session, config, assignment_ids, now):
    """Explicit bounded catch-up only for named confirmed assignments; no backfill."""
    ids = tuple(dict.fromkeys(assignment_ids))
    if not 1 <= len(ids) <= 25:
        raise PlanningCenterError('Catch-up requires 1 to 25 explicit assignment IDs')
    return [enqueue_staffing_intent(session, config, assignment_id=ident, action='accept', now=now,
                                   reconcile_existing=True) for ident in ids]


def catch_up_approved_assignments(session, config, assignment_ids, now):
    """Reserve only the named approved bookings; never infer confirmations."""
    ids = tuple(dict.fromkeys(assignment_ids))
    if not 1 <= len(ids) <= 25 or any(type(ident) is not int or ident <= 0 for ident in ids):
        raise PlanningCenterError('Approved catch-up requires 1 to 25 explicit assignment IDs')
    assignments = [session.get(Assignment, ident) for ident in ids]
    if any(not assignment or assignment.status != 'approved' for assignment in assignments):
        raise PlanningCenterError('Approved catch-up requires every named assignment to remain approved')
    return [enqueue_staffing_intent(session, config, assignment_id=ident, action='reserve', now=now,
        reconcile_existing=True) for ident in ids]


@event.listens_for(Session, 'before_flush')
def _capture_assignments(session, flush_context, instances):
    if CONTEXT not in session.info or session.info.get(SUPPRESS):
        return
    settings, config = session.info[CONTEXT]
    if not settings.pco_staffing_write_enabled:
        return
    captured = session.info.setdefault('pco_transitions', [])
    for assignment in session.new | session.dirty:
        if not isinstance(assignment, Assignment):
            continue
        history = inspect(assignment).attrs.status.history
        changed = (assignment in session.new and assignment.status in {'approved', 'confirmed'}
                   or history.has_changes() and assignment not in session.new)
        if changed and assignment.status in ACTION_STATUS.values():
            captured.append((assignment, assignment.status))


@event.listens_for(Session, 'after_flush_postexec')
def _enqueue_captured(session, flush_context):
    transitions = session.info.pop('pco_transitions', [])
    if not transitions:
        return
    _, config = session.info[CONTEXT]
    for assignment, status in transitions:
        event_link = session.scalar(select(PCOEventLink).where(PCOEventLink.event_id == assignment.shift.event_id))
        if not event_link or event_link.organization_id != config.organization_id:
            continue
        replacement = session.scalar(select(FillRequest).where(
            FillRequest.shift_id == assignment.shift_id, FillRequest.state == 'filled').order_by(
            FillRequest.id.desc())) if status in {'approved', 'confirmed'} else None
        enqueue_staffing_intent(session, config, assignment_id=assignment.id,
            action=next(action for action, local in ACTION_STATUS.items() if local == status), now=assignment.updated_at,
            replaced_assignment_id=replacement.cancelled_assignment_id if replacement else None, flush=False)


@event.listens_for(Session, 'after_rollback')
def _clear_capture(session):
    session.info.pop('pco_transitions', None)


def _claim(factory, key, now):
    owner = str(uuid4())
    with factory() as session:
        lease = session.get(PCOStaffingLease, key)
        if lease is None:
            session.add(PCOStaffingLease(key=key, owner='', expires_at=now))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
        changed = session.execute(update(PCOStaffingLease).where(
            PCOStaffingLease.key == key,
            (PCOStaffingLease.owner == '') | (PCOStaffingLease.expires_at <= now)
        ).values(owner=owner, expires_at=now+LEASE_TIME)).rowcount
        session.commit()
        return owner if changed == 1 else None


def _release(factory, key, owner):
    with factory() as session:
        session.execute(update(PCOStaffingLease).where(PCOStaffingLease.key == key,
            PCOStaffingLease.owner == owner).values(owner=''))
        session.commit()


def _matches(rows, scope, person_id):
    return [r for r in rows if str(relation(r, 'person')) == person_id
            and str(relation(r, 'team')) == scope.team_id
            and r['attributes'].get('team_position_name') == scope.position_name]


def _native_capacity(scope, rows, open_needs):
    scheduled = sum(_status(row) in {'C', 'U'} for row in rows
        if str(relation(row, 'team')) == scope.team_id
        and row['attributes'].get('team_position_name') == scope.position_name)
    return open_needs + scheduled


def _local_capacity(session, scope):
    event_row = session.get(Event, scope.event_id)
    recipes = list(session.scalars(select(RoleRecipe).where(
        RoleRecipe.event_type_id == event_row.event_type_id, RoleRecipe.role_id == scope.role_id)))
    if len(recipes) > 1:
        raise PlanningCenterError('Ambiguous local position staffing target')
    if recipes:
        return recipes[0].count
    return len(list(session.scalars(select(Shift.id).where(Shift.event_id == scope.event_id,
        Shift.role_id == scope.role_id, Shift.parent_shift_id.is_(None)))))


def _reservation_capacity_conflict(session, scope, rows, open_needs):
    """An unresolved reservation owns its capacity expectation during polling."""
    intents = session.scalars(select(PCOStaffingIntent).where(
        PCOStaffingIntent.organization_id == scope.organization_id,
        PCOStaffingIntent.service_type_id == scope.service_type_id,
        PCOStaffingIntent.plan_id == scope.plan_id, PCOStaffingIntent.team_id == scope.team_id,
        PCOStaffingIntent.action == 'reserve',
        PCOStaffingIntent.state.in_(('pending', 'unknown', 'held')))).all()
    intents = [intent for intent in intents if intent.expected.get('scope_key') == scope.key]
    current = []
    for intent in intents:
        later_verified = session.scalars(select(PCOStaffingIntent).where(
            PCOStaffingIntent.organization_id == scope.organization_id,
            PCOStaffingIntent.assignment_id == intent.assignment_id,
            PCOStaffingIntent.id > intent.id, PCOStaffingIntent.state == 'verified')).all()
        if (intent.state == 'held' and intent.attempts == 0 and any(
                later.expected.get('scope_key') == scope.key for later in later_verified)):
            continue  # A named, verified reconciliation supersedes an unwritten hold.
        current.append(intent)
    intents = current
    if not intents:
        return None
    local = _local_capacity(session, scope)
    native = _native_capacity(scope, rows, open_needs)
    targets = [(intent.expected.get('reservation_capacity') or {}).get('required_total') for intent in intents]
    if any(type(target) is not int or target < 1 or target != local or target != native for target in targets):
        return 'Unresolved reservation capacity differs from saved local/native target; polling cannot resize it'
    return None


def _finish_link(session, intent, scope, row, now):
    ident = _id(row['id'])
    collision = session.scalar(select(PCOStaffingLink).where(
        PCOStaffingLink.organization_id == intent.organization_id,
        PCOStaffingLink.plan_person_id == ident, PCOStaffingLink.assignment_id != intent.assignment_id))
    if collision:
        raise PlanningCenterError('PlanPerson already maps to another local assignment')
    link = session.get(PCOStaffingLink, intent.assignment_id)
    if link is None:
        link = PCOStaffingLink(assignment_id=intent.assignment_id, organization_id=intent.organization_id,
            service_type_id=scope.service_type_id, plan_id=scope.plan_id, team_id=scope.team_id,
            person_id=intent.person_id, plan_person_id=ident, remote_status=_status(row), verified_at=now)
        session.add(link)
    elif (link.organization_id, link.service_type_id, link.plan_id, link.team_id, link.person_id) != (
            intent.organization_id, scope.service_type_id, scope.plan_id, scope.team_id, intent.person_id):
        raise PlanningCenterError('Existing staffing link scope differs from intent')
    link.plan_person_id, link.remote_status, link.verified_at = ident, _status(row), now
    link.remote_snapshot = _snapshot(row)
    intent.plan_person_id, intent.state, intent.reason = ident, 'verified', None


def _set_requirement(session, scope, rows, open_needs, now):
    """Unfilled needs plus scheduled C/U slots; U never counts as coverage."""
    relevant = [r for r in rows if str(relation(r, 'team')) == scope.team_id
                and r['attributes'].get('team_position_name') == scope.position_name]
    scope.required_count = open_needs + sum(_status(r) in {'C', 'U'} for r in relevant)
    event_row = session.get(Event, scope.event_id)
    from app.db.models import EventType
    name = f'PCO plan {scope.organization_id}/{scope.plan_id}'
    if event_row.event_type_id is None:
        kind = EventType(name=name, title_patterns=[])
        session.add(kind); session.flush(); event_row.event_type_id = kind.id
    elif session.get(EventType, event_row.event_type_id).name != name:
        raise PlanningCenterError('Imported event has another staffing recipe; review required')
    recipe = session.scalar(select(RoleRecipe).where(RoleRecipe.event_type_id == event_row.event_type_id,
        RoleRecipe.role_id == scope.role_id))
    if recipe is None:
        session.add(RoleRecipe(event_type_id=event_row.event_type_id, role_id=scope.role_id, count=scope.required_count))
    else:
        recipe.count = scope.required_count
    scope.verified_at = now


def verified_coverage_counts(session, events, counts):
    """Only confirmed, reconciled PCO coverage; all other event counts unchanged."""
    event_ids = [event.id for event in events if (event.gcal_event_id or '').startswith('pco:')]
    if not event_ids:
        return counts
    result = dict(counts)
    shifts = list(session.scalars(select(Shift).where(Shift.event_id.in_(event_ids))))
    for shift in shifts:
        assignments = session.scalars(select(Assignment).where(Assignment.shift_id == shift.id,
            Assignment.status == 'confirmed')).all()
        covered = 0
        for assignment in assignments:
            link = session.get(PCOStaffingLink, assignment.id)
            scope = session.scalar(select(PCOPositionScope).where(
                PCOPositionScope.event_id == shift.event_id, PCOPositionScope.role_id == shift.role_id))
            mapping = session.scalar(select(PCOVolunteerPerson).where(
                PCOVolunteerPerson.organization_id == scope.organization_id,
                PCOVolunteerPerson.volunteer_id == assignment.volunteer_id)) if scope else None
            latest = session.scalar(select(PCOStaffingIntent).where(
                PCOStaffingIntent.assignment_id == assignment.id).order_by(PCOStaffingIntent.id.desc()))
            if (scope and mapping and link and link.remote_status == 'C'
                    and (link.organization_id, link.service_type_id, link.plan_id, link.team_id, link.person_id)
                    == (scope.organization_id, scope.service_type_id, scope.plan_id, scope.team_id, mapping.person_id)
                    and (latest is None or latest.state == 'verified')):
                covered += 1
        result[shift.id] = covered
    return result


def _process_intent(factory, client, config, ident, now, lease_key, owner):
    started = monotonic()
    # Read authoritative state before any write. Session carries no observer context.
    with factory() as session:
        session.info[SUPPRESS] = True
        intent = session.get(PCOStaffingIntent, ident)
        if intent.state not in {'pending', 'unknown'}:
            return 'skipped'
        scope = session.get(PCOPositionScope, intent.expected.get('scope_key'))
        assignment = session.get(Assignment, intent.assignment_id)
        if not scope or not assignment or intent.service_type_id not in config.service_type_ids:
            raise PlanningCenterError('Intent scope or local assignment is missing/outside allowlist')
        from app.integrations.planning_center_role_bindings import verified_record, verify_native_position
        if verified_record(session, config, scope):
            verify_native_position(client, scope)
        if intent.action not in ACTION_STATUS:
            raise PlanningCenterError('Unsupported staffing action')
        if _local_snapshot(assignment) != intent.expected['local']:
            raise PlanningCenterError('Local assignment changed after intent; stale write held')
        event_link = session.scalar(select(PCOEventLink).where(PCOEventLink.event_id == scope.event_id))
        if (assignment.shift.event_id != scope.event_id or assignment.shift.role_id != scope.role_id
                or not event_link or assignment.shift.event.gcal_event_id != 'pco:'+event_link.key
                or (event_link.organization_id, event_link.service_type_id, event_link.plan_id) != (
                    config.organization_id, scope.service_type_id, scope.plan_id)):
            raise PlanningCenterError('Local event/role mapping changed after review')
        if (intent.organization_id, intent.service_type_id, intent.plan_id, intent.team_id) != (
                scope.organization_id, scope.service_type_id, scope.plan_id, scope.team_id):
            raise PlanningCenterError('Intent scope differs from reviewed position mapping')
        mapping = session.scalar(select(PCOVolunteerPerson).where(
            PCOVolunteerPerson.organization_id == config.organization_id,
            PCOVolunteerPerson.volunteer_id == assignment.volunteer_id))
        if not mapping or mapping.person_id != intent.person_id:
            raise PlanningCenterError('Person mapping changed after intent')
        if intent.depends_on:
            previous = session.get(PCOStaffingIntent, intent.depends_on)
            if not previous or previous.state != 'verified':
                raise StaffingPending('Replacement cancellation is not verified; saga remains partial')
        earlier_unknown = session.scalar(select(PCOStaffingIntent.id).where(
            PCOStaffingIntent.organization_id == config.organization_id,
            PCOStaffingIntent.service_type_id == intent.service_type_id,
            PCOStaffingIntent.plan_id == intent.plan_id, PCOStaffingIntent.team_id == intent.team_id,
            PCOStaffingIntent.person_id == intent.person_id, PCOStaffingIntent.id < intent.id,
            PCOStaffingIntent.state == 'unknown'))
        if earlier_unknown:
            raise StaffingPending('Earlier unknown staffing write must reconcile before another revision')
        rows, open_needs = _read_scope(client, config, scope, event_row=assignment.shift.event)
        _resource(client, f'/services/v2/people/{intent.person_id}', 'Person', intent.person_id)
        matches = _matches(rows, scope, intent.person_id)
        if len(matches) > 1:
            raise PlanningCenterError('Duplicate external Person/team/position assignments')
        row = matches[0] if matches else None
        desired = NATIVE_STATUS[intent.action]
        expected_remote = intent.expected.get('remote')
        known_id = intent.plan_person_id or (expected_remote or {}).get('id')
        moved = next((r for r in rows if str(r['id']) == str(known_id)), None) if known_id else None
        if moved and (not row or str(row['id']) != str(known_id)):
            raise PlanningCenterError('Known PlanPerson identity/position changed; external conflict')
        if intent.action in {'reserve', 'accept'}:
            check = eligibility.check(session, assignment.volunteer, assignment.shift,
                _exclude_assignment_id=assignment.id)
            from app.core.send_gate import has_open_sensitive_escalation
            if (not assignment.volunteer.sms_opt_in or not check or assignment.shift.event.starts_at <= now
                    or has_open_sensitive_escalation(session, assignment.volunteer_id)):
                raise PlanningCenterError('Local serving eligibility/consent/time no longer permits acceptance')
            memberships = _position_memberships(client, scope)
            eligible = [m for m in memberships if str(relation(m, 'person')) == intent.person_id
                        and str(relation(m, 'team_position')) == scope.position_id
                        and m.get('attributes', {}).get('schedule_preference') != 'Unavailable']
            if len(eligible) != 1:
                raise PlanningCenterError('Person lacks unambiguous current position membership/eligibility')
        # Remote reads can be slow. Discard the earlier local snapshot and make
        # the durable decision under database locks, without holding them over HTTP.
        reviewed_scope = (scope.organization_id, scope.service_type_id, scope.plan_id,
            scope.team_id, scope.position_id, scope.position_name, scope.plan_time_id,
            scope.event_id, scope.role_id)
        reviewed_time = (assignment.shift.event.starts_at, assignment.shift.event.ends_at)
        session.rollback()
        if session.get_bind().dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        session.expire_all()
        intent = session.scalar(select(PCOStaffingIntent).where(PCOStaffingIntent.id == ident).with_for_update())
        if intent.state not in {'pending', 'unknown'}:
            return 'skipped'
        assignment = session.scalar(select(Assignment).where(Assignment.id == intent.assignment_id).with_for_update())
        scope = session.scalar(select(PCOPositionScope).where(
            PCOPositionScope.key == intent.expected.get('scope_key')).with_for_update())
        if not assignment or not scope or _local_snapshot(assignment) != intent.expected['local']:
            raise PlanningCenterError('Local assignment changed during preflight; stale write held')
        verified_record(session, config, scope)
        shift = session.scalar(select(Shift).where(Shift.id == assignment.shift_id).with_for_update())
        event_row = session.scalar(select(Event).where(Event.id == shift.event_id).with_for_update())
        volunteer = session.scalar(select(Volunteer).where(Volunteer.id == assignment.volunteer_id).with_for_update())
        event_link = session.scalar(select(PCOEventLink).where(PCOEventLink.event_id == shift.event_id).with_for_update())
        mapping = session.scalar(select(PCOVolunteerPerson).where(
            PCOVolunteerPerson.organization_id == config.organization_id,
            PCOVolunteerPerson.volunteer_id == assignment.volunteer_id).with_for_update())
        if ((scope.organization_id, scope.service_type_id, scope.plan_id, scope.team_id,
                scope.position_id, scope.position_name, scope.plan_time_id, scope.event_id, scope.role_id) != reviewed_scope
                or (event_row.starts_at, event_row.ends_at) != reviewed_time
                or shift.event_id != scope.event_id or shift.role_id != scope.role_id
                or not event_link or event_row.gcal_event_id != 'pco:'+event_link.key
                or (event_link.organization_id, event_link.service_type_id, event_link.plan_id) != (
                    config.organization_id, scope.service_type_id, scope.plan_id)
                or not mapping or mapping.person_id != intent.person_id):
            raise PlanningCenterError('Local event/position/Person mapping changed during preflight')
        if intent.action in {'reserve', 'accept'}:
            if (not volunteer.sms_opt_in or not eligibility.check(session, volunteer, shift,
                    _exclude_assignment_id=assignment.id) or event_row.starts_at <= now
                    or has_open_sensitive_escalation(session, volunteer.id)):
                raise PlanningCenterError('Local serving eligibility/consent/time changed during preflight')
        if intent.depends_on and session.get(PCOStaffingIntent, intent.depends_on).state != 'verified':
            raise StaffingPending('Replacement cancellation changed during preflight')
        lease = session.scalar(select(PCOStaffingLease).where(PCOStaffingLease.key == lease_key).with_for_update())
        decision_time = now+timedelta(seconds=max(0, monotonic()-started))
        if not lease or lease.owner != owner or lease.expires_at <= decision_time:
            raise StaffingLeaseLost('Serialized staffing lease expired or changed during preflight')
        old_unknown = intent.state == 'unknown'
        capacity = intent.expected.get('reservation_capacity')
        if intent.action == 'reserve':
            native_total = _native_capacity(scope, rows, open_needs)
            local_total = _local_capacity(session, scope)
            if (native_total != local_total or local_total < 1
                    or capacity and capacity.get('required_total') != native_total
                    or old_unknown and not capacity):
                raise PlanningCenterError('Reservation capacity differs from reviewed local/native target; explicit reconciliation required')
            capacity = {'required_total': local_total}
        if row and _status(row) == desired and _silent(row):
            if intent.plan_person_id and str(row['id']) != intent.plan_person_id:
                raise PlanningCenterError('External PlanPerson changed after the write response')
            if old_unknown or expected_remote == _snapshot(row) or intent.expected.get('adopt_existing'):
                _finish_link(session, intent, scope, row, now)
                _set_requirement(session, scope, rows, open_needs, now)
                session.commit(); return 'verified'
            raise PlanningCenterError('New external staffing conflicts with expected prior state')
        if intent.action == 'cancel' and not row:
            link = session.get(PCOStaffingLink, intent.assignment_id)
            if link:
                link.remote_status, link.remote_snapshot, link.verified_at = 'removed', {}, now
            intent.state, intent.reason = 'verified', None
            session.commit(); return 'verified'
        if old_unknown:
            raise PlanningCenterError('Unknown write has not converged; reconcile manually, never repeat create')
        if expected_remote != _snapshot(row):
            raise PlanningCenterError('External staffing changed since last verified state')
        if intent.action in {'reserve', 'accept'} and (row is None or _status(row) == 'D') and open_needs < 1:
            raise PlanningCenterError('No current open need for this exact position')
        if intent.action == 'cancel' and not expected_remote:
            raise PlanningCenterError('Cancellation has no verified external assignment')
        # Durable pre-HTTP state: process death/readback failure cannot replay a create.
        intent.state, intent.reason = 'unknown', 'Write claimed; result must be reconciled before any retry'
        if intent.action == 'reserve':
            intent.expected = {**intent.expected, 'reservation_capacity': capacity}
        intent.attempts += 1; intent.updated_at = now
        session.commit()
        lease = session.get(PCOStaffingLease, lease_key)
        if not lease or lease.owner != owner or lease.expires_at <= now:
            raise PlanningCenterError('Serialized staffing lease expired before write')
        attrs = {'status': desired, 'prepare_notification': False, 'notification_prepared_at': None}
        if row is None:
            attrs.update(person_id=intent.person_id, team_id=scope.team_id, team_position_name=scope.position_name)
            method, path = 'POST', _base(scope)+'/team_members'
            body = {'data': {'type': 'PlanPerson', 'attributes': attrs}}
        else:
            method, path = 'PATCH', f'/services/v2/people/{intent.person_id}/plan_people/{_id(row["id"])}'
            body = {'data': {'type': 'PlanPerson', 'id': _id(row['id']), 'attributes': attrs}}
        returned = client.request(method, path, data=body)['data']
        returned_id = _id(returned['id'])
        intent.plan_person_id = returned_id
        session.commit()  # Keep the known response ID even if the readback fails.
        fresh, fresh_open_needs = _read_scope(client, config, scope, event_row=assignment.shift.event)
        matches = _matches(fresh, scope, intent.person_id)
        if (len(matches) != 1 or str(matches[0]['id']) != returned_id
                or _status(matches[0]) != desired or not _silent(matches[0])):
            intent.reason = 'Post-write staffing/notification readback not verified; outcome unknown'
            session.commit(); return 'held'
        if intent.action == 'reserve' and (
                _native_capacity(scope, fresh, fresh_open_needs) != capacity['required_total']
                or _local_capacity(session, scope) != capacity['required_total']):
            intent.reason = 'Reservation open-needs readback differs from target; outcome held without resizing local staffing'
            session.commit(); return 'held'
        _finish_link(session, intent, scope, matches[0], now)
        _set_requirement(session, scope, fresh, fresh_open_needs, now)
        session.commit()
        return 'verified'


def process_staffing_outbox(factory, client, config, now, *, enabled=False, limit=25):
    """Separate-session worker; requires a session factory, never a live transaction."""
    if not enabled:
        return {'disabled': True, 'verified': 0, 'held': 0}
    if not 1 <= limit <= 25:
        raise PlanningCenterError('Outbox batch limit must be between 1 and 25')
    with factory() as session:
        ids = list(session.scalars(select(PCOStaffingIntent.id).where(
            PCOStaffingIntent.organization_id == config.organization_id,
            PCOStaffingIntent.state.in_(('pending', 'unknown')),
            (PCOStaffingIntent.retry_at.is_(None)) | (PCOStaffingIntent.retry_at <= now)
        ).order_by(PCOStaffingIntent.id).limit(limit)))
    report = {'disabled': False, 'verified': 0, 'held': 0, 'skipped': 0}
    for ident in ids:
        with factory() as session:
            intent = session.get(PCOStaffingIntent, ident)
            key = f'{intent.organization_id}:{intent.service_type_id}:{intent.plan_id}'
        owner = _claim(factory, key, now)
        if owner is None:
            continue
        try:
            result = _process_intent(factory, client, config, ident, now, key, owner)
            report[result] += 1
        except (PlanningCenterError, KeyError, TypeError, ValueError, AttributeError) as error:
            if isinstance(error, StaffingLeaseLost):
                report['skipped'] += 1
                continue
            with factory() as session:
                intent = session.get(PCOStaffingIntent, ident)
                delay = getattr(error, 'retry_after', None)
                if isinstance(error, StaffingPending):
                    intent.state = 'pending'
                elif delay and intent.state != 'unknown':
                    intent.state = 'pending'
                elif intent.state != 'unknown':
                    intent.state = 'held'
                intent.reason = str(error) if isinstance(error, PlanningCenterError) else 'Malformed staffing response; review required'
                intent.retry_at = now+timedelta(seconds=max(60, delay or 60)) if intent.state in {'pending', 'unknown'} else None
                session.commit()
            report['held'] += 1
            if delay:
                report['rate_limited'] = True
                break
        finally:
            _release(factory, key, owner)
    return report


def _queue_decline_fill(session, assignment, now):
    """Start the ordinary fill scheduler after a verified external decline.

    This is a native API transition, never an invented inbound text. The fill
    scheduler still applies Gloo, consent, eligibility, review and delivery gates.
    Historical declines imported at startup must not contact anybody.
    """
    shift = assignment.shift
    if shift.starts_at <= now or shift.event.status != 'scheduled':
        return
    existing = session.scalar(select(FillRequest).where(
        FillRequest.cancelled_assignment_id == assignment.id))
    if existing:
        return
    session.flush()
    from app.agents.fill_agent import compute_urgency
    urgency = compute_urgency(session, shift, now)
    skipped = urgency == 'skip'
    session.add(FillRequest(shift_id=shift.id, cancelled_assignment_id=assignment.id,
        urgency=urgency, state='skipped' if skipped else 'in_progress',
        current_tranche=0, next_action_at=None if skipped else now,
        created_at=now, closed_at=now if skipped else None))


def refresh_staffing(session, client, config, now, *, service_type_id, plan_id):
    """Authoritative mapped coverage; retain declines/history, no outbound echo."""
    if service_type_id not in config.service_type_ids:
        raise PlanningCenterError('Staffing refresh outside service-type scope')
    scopes = list(session.scalars(select(PCOPositionScope).where(
        PCOPositionScope.organization_id == config.organization_id,
        PCOPositionScope.service_type_id == service_type_id, PCOPositionScope.plan_id == plan_id)))
    if not scopes:
        raise PlanningCenterError('Staffing refresh needs explicitly reviewed position mappings')
    report = {'status': 'reconciled', 'created': 0, 'conflicts': 0, 'declined': 0}
    previous_suppression = session.info.get(SUPPRESS)
    session.info[SUPPRESS] = True
    try:
        for scope in scopes:
            from app.integrations.planning_center_role_bindings import verified_record
            canonical = verified_record(session, config, scope)
            if canonical:
                from app.integrations.planning_center_role_bindings import verify_native_position
                verify_native_position(client, scope)
            event_row = session.get(Event, scope.event_id)
            rows, open_needs = _read_scope(client, config, scope, event_row=event_row)
            relevant = [r for r in rows if str(relation(r, 'team')) == scope.team_id
                        and r['attributes'].get('team_position_name') == scope.position_name]
            conflict = _reservation_capacity_conflict(session, scope, rows, open_needs)
            if conflict:
                report['conflicts'] += 1
                report.setdefault('capacity_conflicts', []).append({'scope_key': scope.key, 'reason': conflict})
                continue
            scope.required_count = open_needs + sum(_status(r) in {'C', 'U'} for r in relevant)
            present = set()
            for row in relevant:
                ident = _id(row['id']); present.add(ident)
                person_id = str(relation(row, 'person'))
                if len(_matches(relevant, scope, person_id)) != 1:
                    report['conflicts'] += 1; continue
                mapping = session.scalar(select(PCOVolunteerPerson).where(
                    PCOVolunteerPerson.organization_id == config.organization_id,
                    PCOVolunteerPerson.person_id == person_id))
                if mapping is None:
                    report['conflicts'] += 1; continue
                if canonical and _status(row) in {'C', 'U'}:
                    from types import SimpleNamespace
                    from app.db.models import Role
                    existing_link = session.scalar(select(PCOStaffingLink).where(
                        PCOStaffingLink.organization_id == config.organization_id,
                        PCOStaffingLink.plan_person_id == ident))
                    candidate = SimpleNamespace(id=None, event_id=event_row.id, role_id=scope.role_id,
                        event=event_row, role=session.get(Role, scope.role_id),
                        interval_event=event_row, starts_at=event_row.starts_at,
                        ends_at=event_row.ends_at, parent_shift_id=None)
                    volunteer = session.get(Volunteer, mapping.volunteer_id)
                    if (not volunteer or not volunteer.sms_opt_in or not eligibility.check(session,
                            volunteer, candidate, _exclude_assignment_id=existing_link.assignment_id if existing_link else None)):
                        report['conflicts'] += 1; continue
                link = session.scalar(select(PCOStaffingLink).where(
                    PCOStaffingLink.organization_id == config.organization_id,
                    PCOStaffingLink.plan_person_id == ident))
                if link and (link.service_type_id, link.plan_id, link.team_id, link.person_id) != (
                        scope.service_type_id, scope.plan_id, scope.team_id, person_id):
                    report['conflicts'] += 1; continue
                assignment = session.get(Assignment, link.assignment_id) if link else None
                if assignment is None:
                    existing_local = session.scalar(select(Assignment.id).join(Shift).where(
                        Assignment.volunteer_id == mapping.volunteer_id,
                        Assignment.status.in_(ACTIVE_LOCAL), Shift.event_id == scope.event_id))
                    if existing_local:
                        # Adoption must name the existing local assignment via
                        # catch-up; polling must never create a duplicate commitment.
                        report['conflicts'] += 1; continue
                if assignment and (assignment.shift.event_id != scope.event_id or assignment.shift.role_id != scope.role_id):
                    report['conflicts'] += 1; continue
                pending = session.scalar(select(PCOStaffingIntent.id).where(
                    PCOStaffingIntent.assignment_id == assignment.id if assignment else False,
                    PCOStaffingIntent.state.in_(('pending', 'unknown', 'held'))))
                if pending:
                    report['conflicts'] += 1; continue
                if _status(row) == 'D':
                    if assignment is None:
                        shifts = list(session.scalars(select(Shift).where(
                            Shift.event_id == scope.event_id, Shift.role_id == scope.role_id).order_by(Shift.slot_index)))
                        free = next((s for s in shifts if not any(a.status in ACTIVE_LOCAL for a in s.assignments)), None)
                        if free is None:
                            free = Shift(event_id=scope.event_id, role_id=scope.role_id,
                                slot_index=max((s.slot_index for s in shifts), default=-1)+1)
                            session.add(free); session.flush()
                        assignment = Assignment(shift_id=free.id, volunteer_id=mapping.volunteer_id,
                            status='cancelled', source='planner', created_at=now, updated_at=now)
                        session.add(assignment); session.flush()
                        link = PCOStaffingLink(assignment_id=assignment.id, organization_id=config.organization_id,
                            service_type_id=scope.service_type_id, plan_id=scope.plan_id, team_id=scope.team_id,
                            person_id=person_id, plan_person_id=ident, remote_status='D', verified_at=now)
                        session.add(link)
                        report['declined'] += 1
                    if assignment and assignment.status in ACTIVE_LOCAL:
                        from app.core.cancellation_refusal import record_native_decline
                        try:
                            record_native_decline(session, assignment, link, scope, _snapshot(row), now)
                        except ValueError:
                            report['conflicts'] += 1
                            continue
                        assignment.status, assignment.updated_at = 'cancelled', now
                        _queue_decline_fill(session, assignment, now)
                        report['declined'] += 1
                else:
                    if assignment is None:
                        shifts = list(session.scalars(select(Shift).where(
                            Shift.event_id == scope.event_id, Shift.role_id == scope.role_id).order_by(Shift.slot_index)))
                        free = next((s for s in shifts if not any(a.status in ACTIVE_LOCAL for a in s.assignments)), None)
                        if free is None:
                            free = Shift(event_id=scope.event_id, role_id=scope.role_id,
                                slot_index=max((s.slot_index for s in shifts), default=-1)+1)
                            session.add(free); session.flush()
                        assignment = Assignment(shift_id=free.id, volunteer_id=mapping.volunteer_id,
                            status='confirmed' if _status(row) == 'C' else 'approved', source='planner', created_at=now, updated_at=now)
                        session.add(assignment); session.flush()
                        report['created'] += 1
                    else:
                        occupied = [a for a in assignment.shift.assignments
                                    if a.id != assignment.id and a.status in ACTIVE_LOCAL]
                        if occupied or assignment.volunteer_id != mapping.volunteer_id:
                            report['conflicts'] += 1; continue
                        assignment.status = 'confirmed' if _status(row) == 'C' else 'approved'
                        assignment.updated_at = now
                    if link is None:
                        link = PCOStaffingLink(assignment_id=assignment.id, organization_id=config.organization_id,
                            service_type_id=scope.service_type_id, plan_id=scope.plan_id, team_id=scope.team_id,
                            person_id=person_id, plan_person_id=ident, remote_status=_status(row), verified_at=now)
                        session.add(link)
                if link:
                    link.remote_status, link.remote_snapshot, link.verified_at = _status(row), _snapshot(row), now
            for link in session.scalars(select(PCOStaffingLink).where(
                    PCOStaffingLink.organization_id == config.organization_id,
                    PCOStaffingLink.service_type_id == service_type_id, PCOStaffingLink.plan_id == plan_id,
                    PCOStaffingLink.team_id == scope.team_id)):
                assignment = session.get(Assignment, link.assignment_id)
                if assignment and assignment.shift.role_id == scope.role_id and link.plan_person_id not in present:
                    pending = session.scalar(select(PCOStaffingIntent.id).where(
                        PCOStaffingIntent.assignment_id == assignment.id, PCOStaffingIntent.state.in_(('pending', 'unknown', 'held'))))
                    if pending:
                        report['conflicts'] += 1; continue
                    if assignment.status in ACTIVE_LOCAL:
                        assignment.status, assignment.updated_at = 'cancelled', now
                        _queue_decline_fill(session, assignment, now)
                        report['declined'] += 1
                    link.remote_status, link.remote_snapshot, link.verified_at = 'removed', {}, now
            _set_requirement(session, scope, rows, open_needs, now)
        session.flush()
    finally:
        if previous_suppression is None:
            session.info.pop(SUPPRESS, None)
        else:
            session.info[SUPPRESS] = previous_suppression
    return report


def staffing_tick(factory, settings, config, now, *, client_factory=PCOClient, limit=10):
    """Bounded worker entry, independent of enabling general scheduling."""
    if not settings.pco_staffing_write_enabled and not settings.pco_staffing_poll_enabled:
        return {'disabled': True}
    if not 1 <= limit <= 10:
        raise PlanningCenterError('Staffing poll limit must be between 1 and 10 plans')
    from app.integrations.planning_center_role_bindings import configure_session
    original_factory = factory
    def factory():
        session = original_factory()
        configure_session(session, settings)
        return session
    with client_factory(config) as client:
        result = process_staffing_outbox(factory, client, config, now,
            enabled=settings.pco_staffing_write_enabled, limit=min(25, limit))
        if settings.pco_staffing_poll_enabled and not result.get('rate_limited'):
            with factory() as session:
                plans = list(session.execute(select(PCOPositionScope.service_type_id, PCOPositionScope.plan_id).where(
                    PCOPositionScope.organization_id == config.organization_id,
                    PCOPositionScope.service_type_id.in_(config.service_type_ids)).distinct().limit(1000)).all())
                polls = {poll.key: poll.next_at for poll in session.scalars(select(PCOStaffingPoll))}
                plans.sort(key=lambda plan: polls.get(f'{config.organization_id}:{plan[0]}:{plan[1]}', now))
                plans = plans[:limit]
            for service_type_id, plan_id in plans:
                key = f'{config.organization_id}:{service_type_id}:{plan_id}'
                owner = _claim(factory, key, now)
                if owner is None:
                    continue
                try:
                    with factory() as session:
                        poll = session.get(PCOStaffingPoll, key)
                        if poll and poll.next_at > now:
                            continue
                        if poll is None:
                            poll = PCOStaffingPoll(key=key, next_at=now, reason=None); session.add(poll)
                        poll.next_at = now+POLL_INTERVAL
                        session.commit()
                        try:
                            refreshed = refresh_staffing(session, client, config, now,
                                service_type_id=service_type_id, plan_id=plan_id)
                            conflicts = refreshed.get('capacity_conflicts', [])
                            poll.reason = conflicts[0]['reason'] if conflicts else None
                            if conflicts:
                                result.setdefault('capacity_conflicts', []).extend(conflicts)
                        except (PlanningCenterError, KeyError, TypeError, ValueError, AttributeError) as error:
                            session.rollback(); poll = session.get(PCOStaffingPoll, key)
                            poll.reason = str(error) if isinstance(error, PlanningCenterError) else 'Malformed staffing response; review required'
                            poll.next_at = now+timedelta(seconds=max(60, getattr(error, 'retry_after', None) or 60))
                        session.commit()
                finally:
                    _release(factory, key, owner)
        return result
