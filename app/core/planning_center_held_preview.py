"""Explicit authenticated GET-only capture, never a native write or release."""
import json
import os
import stat

from sqlalchemy import select

from app.core.planning_center_committed_source import CommittedAvailabilityReader
from app.core.planning_center_frequency_reviews import _actor, RELEASE_HOLDS
from app.db import models as m
from app.integrations.planning_center import PCOClient, PlanningCenterError
from app.integrations.planning_center_availability import (
    MembershipBinding, PreviewPolicy, PCOAvailabilityIntent, build_preview,
    enqueue_preview, read_remote, verify_current, _hash,
)
from app.integrations.profile_models import ProfileOutbox


def private_file(path, limit):
    """Read only an absolute, regular, private file owned by this backend user."""
    try:
        if not path or not os.path.isabs(path):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            saved = os.fstat(fd)
            if not stat.S_ISREG(saved.st_mode) or saved.st_uid != os.getuid() or saved.st_mode & 0o077:
                raise ValueError()
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                data = stream.read(limit + 1)
            if not data or len(data) > limit:
                raise ValueError()
            return data
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        raise PlanningCenterError('private_review_configuration_unavailable') from None


def signing_key(path):
    if not path:
        return None
    try:
        key = private_file(path, 4096)
        return key if len(key) >= 32 else None
    except PlanningCenterError:
        return None  # Preview is useful without signing; acknowledgement stays held.


def require_review_schema(engine):
    """Schema-only inspection, no startup DDL or fallback schema creation."""
    try:
        if engine.url.get_backend_name() != 'sqlite' or engine.url.database in (None, '', ':memory:'):
            raise ValueError()
        from scripts.pco_sqlite_review_preflight import inspect_database
        report = inspect_database(engine.url.database)
        if report['feature_state'] != 'complete' or not report['schema_compatible']:
            raise ValueError()
    except Exception:
        raise PlanningCenterError('review_schema_incomplete_or_incompatible') from None


def _bindings(session, settings, config, source):
    try:
        data = json.loads(private_file(settings.pco_review_bindings_path, 65536))
        if (set(data) != {'schema', 'organization_id', 'volunteer_id', 'person_id', 'memberships'} or
                data['schema'] != 1 or type(data['volunteer_id']) is not int or
                (data['organization_id'], data['volunteer_id'], data['person_id']) !=
                (config.organization_id, source.value['volunteer_id'], source.value['person_id']) or
                not isinstance(data['memberships'], list) or not 1 <= len(data['memberships']) <= 30):
            raise ValueError()
        bindings = [MembershipBinding(**row) for row in data['memberships']]
        roles = {role.id: role.name for role in session.scalars(select(m.Role))}
        if any(type(b.role_id) is not int or roles.get(b.role_id) != b.role_name or
               b.service_type_id not in config.service_type_ids for b in bindings):
            raise ValueError()
        return bindings
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCenterError('review_operator_binding_scope_invalid') from None


def _capture(session, settings, config, volunteer_id, clock):
    try:
        return _capture_source(session, settings, config, volunteer_id, clock)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCenterError('review_committed_source_not_verified') from None


def _capture_source(session, settings, config, volunteer_id, clock):
    volunteer = session.get(m.Volunteer, volunteer_id)
    source = session.get(m.Policy, 'profile_sync_source')
    if volunteer is None or source is None:
        raise PlanningCenterError('review_committed_source_missing')
    row = session.scalar(select(ProfileOutbox).where(
        ProfileOutbox.source_id == source.value.get('id'), ProfileOutbox.phone == volunteer.phone
    ).order_by(ProfileOutbox.created_at.desc(), ProfileOutbox.key).limit(1))
    if row is None:
        raise PlanningCenterError('review_committed_profile_missing')
    reader = CommittedAvailabilityReader(settings, config, volunteer_id=volunteer_id,
        profile_key=row.key, source_id=source.value.get('id'), clock=clock)
    current = reader(session)
    if session.scalar(select(PCOAvailabilityIntent.key).where(
            PCOAvailabilityIntent.organization_id == config.organization_id,
            PCOAvailabilityIntent.person_id == current.value['person_id'],
            PCOAvailabilityIntent.state == 'unknown')):
        raise PlanningCenterError('review_previous_outcome_requires_reconciliation')
    return current, _bindings(session, settings, config, current)


def capture_held_preview(factory, settings, config, *, volunteer_id, user, clock, client_factory=None):
    """Server selects genuine current source; caller supplies no remote facts.

    Locks are released for HTTP. Capture again under the final local transaction
    before persisting. Native edits after the last GET remain an explicit hold.
    No ownership is inferred from a GET or from operator configuration.
    """
    _actor(user, settings)
    require_review_schema(factory.kw['bind'])
    with factory() as session:
        source, bindings = _capture(session, settings, config, volunteer_id, clock)
    with (client_factory or PCOClient)(config) as client:
        remote = read_remote(client, config, source, bindings)
        preview = build_preview(source, remote, owned=(), policy=PreviewPolicy())
        with factory() as session:
            current, current_bindings = _capture(session, settings, config, volunteer_id, clock)
            if current_bindings != bindings or current.digest != source.digest:
                raise PlanningCenterError('review_source_or_bindings_changed')
        fresh_remote = read_remote(client, config, current, bindings)
    with factory() as session:
        current, current_bindings = _capture(session, settings, config, volunteer_id, clock)
        if current_bindings != bindings:
            raise PlanningCenterError('review_operator_bindings_changed')
        verify_current(preview, current, fresh_remote)
        record = enqueue_preview(session, preview, source=current, remote=fresh_remote, now=clock())
        session.commit()
    return {'preview_id': record.key, 'organization_id': source.value['organization_id'],
        'person_id': source.value['person_id'], 'source_hash': source.digest, 'remote_hash': remote.digest,
        'native_snapshot_saved_at': record.created_at.isoformat(),
        'operations': [{**op, 'intent_key': _hash([preview.digest, op])} for op in preview.value['operations']],
        'holds': preview.value['holds'], 'release_holds': RELEASE_HOLDS.copy(), 'execution_enabled': False}
