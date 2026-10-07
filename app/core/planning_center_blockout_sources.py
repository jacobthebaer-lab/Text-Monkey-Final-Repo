"""Transactional saved-availability receipts, independent of cloud and texts.

The save transaction records local intent only. A separate guarded transaction
revalidates the exact saved facts before the blockout executor can use them.
"""
from datetime import date, datetime
import re
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.planning_center_committed_source import CommittedSourceSession, committed_source_factory
from app.db import models as m
from app.integrations.planning_center import PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_availability import _hash, capture_source

CONTEXT = 'pco_blockout_source_context'
PREFERENCE_KEYS = {
    'consent_at', 'consent_source', 'consent_pending', 'onboarding_stage',
    'onboarding_completed_at', 'onboarding_availability_draft',
    'availability_weekdays', 'preferred_services', 'availability_all_day',
    'availability_frequency_known', 'max_per_month', 'recurring_windows',
    'role_frequency_caps',
}


def receipt_key(organization_id, volunteer_id):
    return 'pco_bs:' + _hash([organization_id, volunteer_id])


def queue_key(organization_id, volunteer_id):
    return 'pco_bq:' + _hash([organization_id, volunteer_id])


def _locked(statement):
    return statement.with_for_update().execution_options(populate_existing=True)


def lock_availability_person(session, volunteer_id):
    """Use the reader's parent-before-dates lock order in configured saves.

    The no-autoflush ID read preserves dirty caller values and avoids a
    PostgreSQL reader/save deadlock while the reader locks the same source.
    """
    context = session.info.get(CONTEXT)
    if context is None:
        return
    try:
        context[1].require_scope()
    except PlanningCenterError:
        return
    with session.no_autoflush:
        session.scalar(select(m.Volunteer.id).where(m.Volunteer.id == volunteer_id).with_for_update())


def _saved_document(session, volunteer, settings):
    """Fingerprint effective stored facts, including an explicit empty removal.

    Raw conversations, acknowledgments, delivery status and Supabase receipts
    cannot invalidate or establish this source. Pending signup drafts never
    replace committed effective availability for automatic writeback.
    """
    prefs = volunteer.preferences or {}
    rows = session.scalars(select(m.Availability).where(
        m.Availability.volunteer_id == volunteer.id).order_by(m.Availability.month, m.Availability.id)
        .execution_options(populate_existing=True)).all()
    if prefs.get('onboarding_availability_draft') is not None:
        raise PlanningCenterError('blockout_source_unfinished_preferences')
    if not rows and not prefs.get('onboarding_completed_at'):
        raise PlanningCenterError('blockout_source_saved_availability_required')
    months = set()
    for row in rows:
        if not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', row.month) or row.month in months:
            raise PlanningCenterError('blockout_source_ambiguous_availability')
        months.add(row.month)
        for days in (row.available_dates, row.unavailable_dates):
            if not isinstance(days, list) or any(
                    not isinstance(day, str) or date.fromisoformat(day).isoformat() != day
                    or not day.startswith(row.month + '-') for day in days):
                raise PlanningCenterError('blockout_source_invalid_saved_dates')
        if set(row.available_dates) & set(row.unavailable_dates):
            raise PlanningCenterError('blockout_source_conflicting_saved_dates')
    return {'volunteer_id': volunteer.id, 'name': volunteer.name, 'phone': volunteer.phone,
        'status': volunteer.status, 'sms_opt_in': volunteer.sms_opt_in,
        'preferences': {key: prefs[key] for key in sorted(PREFERENCE_KEYS) if key in prefs},
        'availability': [{'month': row.month, 'available_dates': sorted(set(row.available_dates)),
                          'unavailable_dates': sorted(set(row.unavailable_dates))} for row in rows],
        'timezone': settings.church_timezone}


def queue_saved_availability(session, volunteer_id, *, settings=None, config=None):
    """Queue completed saved facts in their existing transaction, never commit.

    The policy-enable endpoint may pass settings/config explicitly to seed an
    existing saved profile. Ordinary callers use the configured session context.
    Missing integration configuration leaves the local save fully usable.
    The caller invokes this only after validation/exact review has applied.
    """
    if settings is None or config is None:
        context = session.info.get(CONTEXT)
        if context is None:
            return None
        settings, config = context
    try:
        config.require_scope()
    except PlanningCenterError:
        return None
    # Serialize first-time receipt creation per person without refreshing away
    # the caller's dirty values. Flush only after acquiring the person lock.
    with session.no_autoflush:
        found = session.scalar(select(m.Volunteer.id).where(m.Volunteer.id == volunteer_id).with_for_update())
    if found is None:
        return None
    session.flush()
    volunteer = session.get(m.Volunteer, volunteer_id, populate_existing=True)
    try:
        revision = _hash(_saved_document(session, volunteer, settings))
    except (PlanningCenterError, ValueError, TypeError):
        return None  # Unfinished/ambiguous facts remain local, never native intent.
    key = receipt_key(config.organization_id, volunteer_id)
    receipt = session.scalar(_locked(select(m.Policy).where(m.Policy.key == key)))
    if receipt is None:
        receipt = m.Policy(key=key, value={})
        session.add(receipt)
    # Stable per-person source identity avoids a global first-save insert race.
    source_id = receipt.value.get('source_id') or str(uuid4())
    if receipt.value.get('revision') != revision:
        receipt.value = {'schema': 1, 'organization_id': config.organization_id,
            'volunteer_id': volunteer_id, 'source_id': source_id, 'revision': revision,
            'kind': 'saved_availability'}
    # Refresh the queue before calling the executor's transaction-only helper.
    session.scalar(_locked(select(m.Policy).where(m.Policy.key == queue_key(config.organization_id, volunteer_id))))
    from app.integrations.planning_center_blockouts import queue_blockout_sync
    return queue_blockout_sync(session, config.organization_id, volunteer_id, revision=revision)


def enqueue_current_availability(session, settings, config, *, volunteer_id, user, clock):
    """Authenticated policy-enable action validates and binds current records.

    This is honest new coordinator provenance for existing saved facts, rather
    than claiming an old SMS receipt that was never recorded. It is local only
    and must commit atomically with the enabled standing policy.
    """
    from app.core.planning_center_frequency_reviews import _actor
    actor = _actor(user, settings)
    with session.no_autoflush:
        session.scalar(select(m.Volunteer.id).where(m.Volunteer.id == volunteer_id).with_for_update())
    session.flush()
    volunteer = session.get(m.Volunteer, volunteer_id, populate_existing=True)
    if volunteer is None:
        raise PlanningCenterError('blockout_source_saved_availability_required')
    revision = _hash(_saved_document(session, volunteer, settings))
    # Shared validated source schema verifies finite dates, catalog references,
    # timezone and mapping. The signed policy separately verifies consent.
    capture_source(session, config, volunteer_id=volunteer_id,
        provenance={'source_id': 'coordinator-policy-enable', 'receipt_id': actor['id'], 'revision': revision},
        tz=settings.church_timezone, now=clock())
    key = queue_saved_availability(session, volunteer_id, settings=settings, config=config)
    if key is None:
        raise PlanningCenterError('blockout_source_saved_availability_required')
    receipt = session.get(m.Policy, receipt_key(config.organization_id, volunteer_id))
    receipt.value = {**receipt.value, 'kind': 'coordinator_policy_enable',
                     'actor_id': actor['id'], 'recorded_at': clock().isoformat()}
    return key


blockout_source_factory = committed_source_factory


class CommittedBlockoutReader:
    """Read current committed queued facts without depending on a cloud mirror."""
    def __init__(self, settings, config, *, volunteer_id, clock):
        self.settings, self.config, self.volunteer_id, self.clock = settings, config, volunteer_id, clock

    def __call__(self, session):
        if not isinstance(session, CommittedSourceSession):
            raise PlanningCenterError('blockout_source_guarded_session_required')
        if session.new or session.dirty or session.deleted or session.info.get('pco_uncommitted_source_write'):
            raise PlanningCenterError('blockout_source_requires_clean_session')
        try:
            return self._read(session)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PlanningCenterError('blockout_source_provenance_not_verified') from None

    def _read(self, session):
        self.config.require_scope()
        if session.get_bind().dialect.name == 'sqlite':
            connection = session.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
        volunteer = session.scalar(_locked(select(m.Volunteer).where(m.Volunteer.id == self.volunteer_id)))
        receipt = session.scalar(_locked(select(m.Policy).where(
            m.Policy.key == receipt_key(self.config.organization_id, self.volunteer_id))))
        queue = session.scalar(_locked(select(m.Policy).where(
            m.Policy.key == queue_key(self.config.organization_id, self.volunteer_id))))
        if not volunteer or not receipt or not queue:
            raise PlanningCenterError('blockout_source_committed_receipt_required')
        saved, queued = receipt.value, queue.value
        if (saved.get('schema') != 1 or saved.get('kind') not in {'saved_availability', 'coordinator_policy_enable'}
                or saved.get('organization_id') != self.config.organization_id
                or saved.get('volunteer_id') != self.volunteer_id
                or queued.get('schema') != 1 or queued.get('organization_id') != self.config.organization_id
                or queued.get('volunteer_id') != self.volunteer_id
                or queued.get('revision') != saved.get('revision')
                or not re.fullmatch('[a-f0-9]{64}', saved.get('revision', ''))
                or str(UUID(saved['source_id'])) != saved['source_id']):
            raise PlanningCenterError('blockout_source_receipt_or_queue_changed')
        session.scalars(_locked(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id))).all()
        session.scalars(_locked(select(m.Role))).all()
        session.scalars(_locked(select(m.EventType))).all()
        mapping = session.scalar(_locked(select(PCOVolunteerPerson).where(
            PCOVolunteerPerson.organization_id == self.config.organization_id,
            PCOVolunteerPerson.volunteer_id == volunteer.id)))
        if not mapping:
            raise PlanningCenterError('blockout_source_verified_mapping_required')
        prefs = volunteer.preferences or {}
        if (volunteer.status != 'active' or volunteer.sms_opt_in is not True
                or prefs.get('consent_pending') is True
                or not prefs.get('consent_at') or not prefs.get('consent_source')
                or session.get(m.Policy, 'sms_opt_out:' + volunteer.phone)):
            raise PlanningCenterError('blockout_source_current_consent_required')
        consent_at = datetime.fromisoformat(prefs['consent_at'])
        if (consent_at.tzinfo is None or consent_at > self.clock()
                or not isinstance(prefs['consent_source'], str) or not 0 < len(prefs['consent_source']) <= 120):
            raise PlanningCenterError('blockout_source_current_consent_required')
        if _hash(_saved_document(session, volunteer, self.settings)) != saved['revision']:
            raise PlanningCenterError('blockout_source_saved_revision_changed')
        return capture_source(session, self.config, volunteer_id=volunteer.id,
            provenance={'source_id': saved['source_id'], 'receipt_id': receipt.key, 'revision': saved['revision']},
            tz=self.settings.church_timezone, now=self.clock())
