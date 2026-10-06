"""Exact coordinator-reviewed native position to existing local role bindings.

Native calls are GET-only. No profile, qualification, assignment or native write
is performed. Existing Policy and PCOPositionScope tables store the review.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

from sqlalchemy import select

from app.db.models import Assignment, Event, Policy, Role, Shift
from app.integrations.planning_center import (
    PCOEventLink, PCOPositionScope, PCOShiftLink, PlanningCenterError,
)

KEY = 'pco_role_mapping_signing_key'
PREFIX = 'pco_role_binding:'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _sign(session, document, purpose):
    key = session.info.get(KEY)
    if key is None:
        context = session.info.get('pco_staffing_context')
        if context:
            from app.core.planning_center_held_preview import signing_key
            key = signing_key(context[0].pco_review_signing_key_path)
    if not isinstance(key, bytes) or len(key) < 32:
        raise PlanningCenterError('role_binding_private_signing_key_required')
    return hmac.new(key, ('text-monkey:pco-role-' + purpose + ':v1\0').encode()
                    + document.encode(), hashlib.sha256).hexdigest()


def configure_session(session, settings):
    from app.core.planning_center_held_preview import signing_key
    session.info[KEY] = signing_key(settings.pco_review_signing_key_path)


def _role(role):
    return {k: getattr(role, k) for k in ('id', 'name', 'ministry',
        'required_qualifications', 'criticality', 'fill_policy')}


def _binding(scope):
    return {k: getattr(scope, k) for k in ('key', 'organization_id', 'service_type_id',
        'plan_id', 'event_id', 'role_id', 'team_id', 'position_id', 'position_name', 'plan_time_id')}


def verify_native_position(client, scope):
    from app.integrations.planning_center import relation
    rows = client.collection(f'/services/v2/service_types/{scope.service_type_id}/team_positions')
    matches = [r for r in rows if r.get('type') == 'TeamPosition' and
        str(relation(r, 'team')) == scope.team_id and r.get('attributes', {}).get('name') == scope.position_name]
    if len(matches) != 1 or matches[0]['id'] != scope.position_id:
        raise PlanningCenterError('role_binding_native_position_ambiguous_or_changed')


def verified_record(session, config, scope, *, require_current=True):
    if scope is None:
        raise PlanningCenterError('role_binding_scope_missing')
    row = session.get(Policy, PREFIX + scope.key)
    if row is None:
        return None  # Legacy namespace-only mapping, not canonical-role authority.
    try:
        value = row.value
        document = value['document']
        if not hmac.compare_digest(value['signature'], _sign(session, document, 'binding')):
            raise ValueError()
        data = json.loads(document)
        role = session.get(Role, scope.role_id)
        event = session.get(Event, scope.event_id)
        if (document != _json(data) or data['schema'] != 1 or
                data['action'] != 'bind_existing_local_role' or
                data['binding'] != _binding(scope) or not role or
                scope.organization_id != config.organization_id or
                scope.service_type_id not in config.service_type_ids):
            raise ValueError()
        if require_current and (data['role'] != _role(role) or not event or data['event'] != {
                'id': event.id, 'native_key': (event.gcal_event_id or '')[4:],
                'starts_at': event.starts_at.isoformat(), 'ends_at': event.ends_at.isoformat()}):
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCenterError('role_binding_review_or_local_policy_changed') from None


def _capture(session, client, config, *, shift_id, local_role_id, team_id, position_id, plan_time_id,
             native=None):
    from app.integrations.planning_center_staffing import _read_scope, _resource
    shift = session.get(Shift, shift_id)
    role = session.get(Role, local_role_id)
    link = session.scalar(select(PCOEventLink).where(PCOEventLink.event_id == shift.event_id)) if shift else None
    source = session.scalar(select(PCOShiftLink).where(PCOShiftLink.shift_id == shift_id))
    if (not role or not link or not source or source.event_key != link.key or
            shift.event.gcal_event_id != 'pco:' + link.key or
            link.organization_id != config.organization_id or link.service_type_id not in config.service_type_ids):
        raise PlanningCenterError('role_binding_exact_imported_shift_and_existing_role_required')
    prefix, need_id, slot = source.key.rsplit(':', 2)
    if prefix != link.key or not need_id.isdigit() or not slot.isdigit():
        raise PlanningCenterError('role_binding_import_link_invalid')
    group = list(session.scalars(select(PCOShiftLink).where(
        PCOShiftLink.event_key == link.key, PCOShiftLink.key.startswith(link.key + ':' + need_id + ':'))
        .order_by(PCOShiftLink.key)))
    shifts = [session.get(Shift, item.shift_id) for item in group]
    if (any(not s or s.event_id != link.event_id or s.role_id != shift.role_id for s in shifts) or
            shift.role_id != role.id and session.scalar(select(Assignment.id).where(
                Assignment.shift_id.in_([s.id for s in shifts])).limit(1))):
        raise PlanningCenterError('role_binding_existing_assignment_history_or_shift_drift')
    position_name = native['position_name'] if native else _resource(client,
        f'/services/v2/service_types/{link.service_type_id}/team_positions/{position_id}',
        'TeamPosition', position_id)['attributes']['name']
    scope = PCOPositionScope(key=f'{config.organization_id}:{link.service_type_id}:{link.plan_id}:{position_id}',
        organization_id=config.organization_id, service_type_id=link.service_type_id, plan_id=link.plan_id,
        event_id=link.event_id, role_id=role.id, team_id=team_id, position_id=position_id,
        position_name=position_name, plan_time_id=plan_time_id)
    existing = session.get(PCOPositionScope, scope.key)
    collisions = list(session.scalars(select(PCOPositionScope).where(
        PCOPositionScope.event_id == scope.event_id,
        PCOPositionScope.role_id.in_((role.id, shift.role_id)))))
    if any(item.key != scope.key for item in collisions):
        raise PlanningCenterError('role_binding_ambiguous_existing_position_or_role')
    prior = session.get(Policy, PREFIX + scope.key)
    if prior:
        verified_record(session, config, existing, require_current=False)
    if native is None:
        _read_scope(client, config, scope, event_row=shift.event)
        verify_native_position(client, scope)
        needs = client.collection(f'/services/v2/service_types/{link.service_type_id}/plans/{link.plan_id}/needed_positions')
        from app.integrations.planning_center import relation
        selected = [n for n in needs if str(n.get('id')) == need_id and
            str(relation(n, 'team')) == team_id and n['attributes'].get('team_position_name') == position_name]
        if len(selected) != 1:
            raise PlanningCenterError('role_binding_native_need_position_mismatch')
        native = {'position_name': position_name, 'need_id': need_id}
    local = {'binding': _binding(scope), 'role': _role(role), 'event': {
        'id': shift.event.id, 'native_key': link.key,
        'starts_at': shift.event.starts_at.isoformat(), 'ends_at': shift.event.ends_at.isoformat()},
        'shifts': [{'id': s.id, 'role_id': s.role_id, 'slot_index': s.slot_index} for s in shifts],
        'links': [{'key': item.key, 'shift_id': item.shift_id} for item in group],
        'prior_scope': _binding(existing) if existing else None, 'prior_review': prior.value if prior else None}
    return {'local': local, 'native': native}, scope, shifts


def propose_role_binding(session, client, config, settings, *, user, now, **mapping):
    from app.core.planning_center_frequency_reviews import _actor
    actor = _actor(user, settings)
    if session.new or session.dirty or session.deleted:
        raise PlanningCenterError('role_binding_clean_transaction_required')
    snapshot, _, _ = _capture(session, client, config, **mapping)
    digest = _hash(snapshot)
    token = _json({'actor': actor, 'hash': digest, 'issued_at': now.isoformat(),
        'expires_at': (now + timedelta(minutes=10)).isoformat()})
    return {'review_hash': digest, 'review_token': {'document': token,
        'signature': _sign(session, token, 'proposal')}, 'snapshot': snapshot,
        'native_writes': False, 'execution_enabled': False}


def apply_role_binding(session, client, config, settings, *, user, now, review_hash, review_token, **mapping):
    from app.core.planning_center_frequency_reviews import _actor
    actor = _actor(user, settings)
    try:
        document = review_token['document']; token = json.loads(document)
        expires = datetime.fromisoformat(token['expires_at'])
        issued = datetime.fromisoformat(token['issued_at'])
        if (document != _json(token) or token['actor'] != actor or token['hash'] != review_hash or
                not issued <= now < expires or not timedelta(0) < expires-issued <= timedelta(minutes=10) or
                not hmac.compare_digest(review_token['signature'], _sign(session, document, 'proposal'))):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PlanningCenterError('role_binding_exact_authenticated_review_required') from None
    if session.new or session.dirty or session.deleted:
        raise PlanningCenterError('role_binding_clean_transaction_required')
    snapshot, _, _ = _capture(session, client, config, **mapping)
    if _hash(snapshot) != review_hash:
        raise PlanningCenterError('role_binding_review_context_changed')
    session.rollback()  # Do not hold local locks across native HTTP.
    if session.get_bind().dialect.name == 'sqlite':
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    else:
        local = snapshot['local']; event_id = local['event']['id']
        ids = [s['id'] for s in local['shifts']]
        role_ids = {s['role_id'] for s in local['shifts']} | {local['role']['id']}
        locks = [(Role, Role.id.in_(role_ids)), (Event, Event.id == event_id),
            (Shift, Shift.id.in_(ids)), (PCOEventLink, PCOEventLink.event_id == event_id),
            (PCOShiftLink, PCOShiftLink.shift_id.in_(ids)),
            (PCOPositionScope, PCOPositionScope.event_id == event_id),
            (Policy, Policy.key == PREFIX + local['binding']['key']),
            (Assignment, Assignment.shift_id.in_(ids))]
        for model, condition in locks:
            session.scalars(select(model).where(condition).with_for_update()).all()
    session.expire_all()
    current, proposed, shifts = _capture(session, None, config, native=snapshot['native'], **mapping)
    if _hash(current) != review_hash:
        raise PlanningCenterError('role_binding_local_context_changed_during_native_reads')
    scope = session.get(PCOPositionScope, proposed.key)
    if scope:
        for key, value in _binding(proposed).items():
            setattr(scope, key, value)
    else:
        scope = proposed; session.add(scope)
    scope.verified_at = now
    for shift in shifts:
        shift.role_id = scope.role_id
    record = _json({'schema': 1, 'action': 'bind_existing_local_role', 'binding': _binding(scope),
        'role': current['local']['role'], 'native': snapshot['native'], 'event': current['local']['event'],
        'review_hash': review_hash, 'actor': actor, 'reviewed_at': now.astimezone(timezone.utc).isoformat()})
    value = {'document': record, 'signature': _sign(session, record, 'binding')}
    policy = session.get(Policy, PREFIX + scope.key)
    if policy:
        policy.value = value
    else:
        session.add(Policy(key=PREFIX + scope.key, value=value))
    session.flush()
    return {'scope_key': scope.key, 'local_role_id': scope.role_id,
        'shift_ids': [s.id for s in shifts], 'native_writes': False, 'execution_enabled': False}


def import_roles(session, client, config, snapshots):
    """Validate signed bindings and native scope before the importer changes rows."""
    from app.integrations.planning_center_staffing import _read_scope
    resolved = {}
    for snapshot in snapshots:
        scopes = session.scalars(select(PCOPositionScope).where(
            PCOPositionScope.organization_id == config.organization_id,
            PCOPositionScope.service_type_id == snapshot['service_type_id'],
            PCOPositionScope.plan_id == snapshot['plan_id'])).all()
        for scope in scopes:
            record = verified_record(session, config, scope)
            if record is None:
                continue
            event = session.get(Event, scope.event_id)
            if (not event or event.gcal_event_id != 'pco:' + snapshot['key'] or
                    (event.starts_at, event.ends_at) != (snapshot['start'], snapshot['end'])):
                raise PlanningCenterError('role_binding_plan_time_context_changed')
            _read_scope(client, config, scope, event_row=event)
            verify_native_position(client, scope)
            for need in snapshot['needs']:
                if need['team_id'] == scope.team_id and need['name'] == scope.position_name:
                    if need['id'] != record['native']['need_id']:
                        raise PlanningCenterError('role_binding_native_need_identity_changed')
                    key = (snapshot['key'], need['id'])
                    if key in resolved:
                        raise PlanningCenterError('role_binding_ambiguous_native_position')
                    resolved[key] = session.get(Role, scope.role_id)
    return resolved
