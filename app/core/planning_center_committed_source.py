"""Lock and verify existing same-store Mac/Gloo profile provenance, read only."""
import hashlib
import json
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import TextClause

from app.core import profile_sync
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.profile_models import ProfileOutbox
from app.integrations.planning_center import PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_availability import capture_source

AVAILABILITY_ROUTES = {'onboarding_availability', 'onboarding_complete', 'availability', 'onboarding_review'}

SOURCE_ROWS = (m.Volunteer, m.Availability, m.Role, m.EventType, m.Message, m.Policy,
               MacInboundReceipt, ProfileOutbox, PCOVolunteerPerson)


class CommittedSourceSession(Session):
    """Remember source writes even after flush makes Session.dirty empty.

    This explicit session class installs no global listeners or runtime hooks.
    Outbox/claim/review writes are allowed, but never become source evidence.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Instance-scoped public hook also covers scalar/scalars and legacy
        # Query.update/delete, which do not call Session.execute overrides.
        event.listen(self, 'do_orm_execute', self._track_direct_sql)

    def _track_direct_sql(self, state):
        # Do not parse SQL text or assume its first word proves a pure read.
        # Nested Core DML (e.g. a writable CTE) also taints this transaction.
        if (not getattr(state.statement, 'is_select', False) or
                any(isinstance(node, TextClause) or getattr(node, 'is_dml', False)
                    for node in visitors.iterate(state.statement))):
            self.info['pco_uncommitted_source_write'] = True

    def flush(self, objects=None):
        if any(isinstance(row, SOURCE_ROWS) for row in (*self.new, *self.dirty, *self.deleted)):
            self.info['pco_uncommitted_source_write'] = True
        return super().flush(objects)

    def bulk_save_objects(self, *args, **kwargs):
        self.info['pco_uncommitted_source_write'] = True
        return super().bulk_save_objects(*args, **kwargs)

    def bulk_insert_mappings(self, *args, **kwargs):
        self.info['pco_uncommitted_source_write'] = True
        return super().bulk_insert_mappings(*args, **kwargs)

    def bulk_update_mappings(self, *args, **kwargs):
        self.info['pco_uncommitted_source_write'] = True
        return super().bulk_update_mappings(*args, **kwargs)

    def commit(self):
        super().commit()
        self.info.pop('pco_uncommitted_source_write', None)

    def rollback(self):
        super().rollback()
        self.info.pop('pco_uncommitted_source_write', None)


def committed_source_factory(engine):
    return sessionmaker(bind=engine, class_=CommittedSourceSession, expire_on_commit=False)


def _locked(session, statement):
    return statement.with_for_update().execution_options(populate_existing=True)


class CommittedAvailabilityReader:
    """A callable for executor source_reader(session), bound to one revision.

    Every call rechecks current recipient scope and committed receipts/profile.
    It never imports receipts, replays Gloo, captures a queue row or mutates prefs.
    Caller supplies a dedicated transaction; uncommitted work is refused.
    """
    def __init__(self, settings, config, *, volunteer_id, profile_key, source_id, clock):
        self.settings, self.config = settings, config
        self.volunteer_id, self.profile_key, self.source_id = volunteer_id, profile_key, source_id
        self.clock = clock

    def __call__(self, session):
        if not isinstance(session, CommittedSourceSession):
            raise PlanningCenterError('committed_source_guarded_session_required')
        if session.new or session.dirty or session.deleted or session.info.get('pco_uncommitted_source_write'):
            raise PlanningCenterError('committed_source_requires_clean_session')
        try:
            return self._read(session)
        except (profile_sync.ProfileHeld, ValueError, TypeError, KeyError, AttributeError):
            raise PlanningCenterError('committed_source_provenance_not_verified') from None

    def _read(self, session):
        self.config.require_scope()
        if not self.settings.profile_sync_enabled or self.settings.sms_provider != 'mac_messages':
            raise PlanningCenterError('committed_mac_profile_source_not_enabled')
        # SQLite FOR UPDATE is ignored. Upgrade to a real write reservation even
        # if SQLAlchemy has already begun its logical transaction through SELECT.
        if session.get_bind().dialect.name == 'sqlite':
            connection = session.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
        volunteer = session.scalar(_locked(session, select(m.Volunteer).where(m.Volunteer.id == self.volunteer_id)))
        mapping = session.scalar(_locked(session, select(PCOVolunteerPerson).where(
            PCOVolunteerPerson.organization_id == self.config.organization_id,
            PCOVolunteerPerson.volunteer_id == self.volunteer_id)))
        if volunteer is None or mapping is None:
            raise PlanningCenterError('committed_source_verified_mapping_required')
        phones = {p.strip() for p in self.settings.mac_demo_phones.split(',') if p.strip()}
        if volunteer.phone not in profile_sync.approved_phones(self.settings) or volunteer.phone not in phones:
            raise PlanningCenterError('committed_source_recipient_not_allowed')
        source = session.scalar(_locked(session, select(m.Policy).where(m.Policy.key == 'profile_sync_source')))
        row = session.scalar(_locked(session, select(ProfileOutbox).where(ProfileOutbox.key == self.profile_key)))
        if (not source or not row or source.value.get('id') != self.source_id or row.source_id != self.source_id or
                row.phone != volunteer.phone or row.state not in {'pending', 'failed', 'synced'}):
            raise PlanningCenterError('committed_source_foreign_or_held_profile')
        if str(UUID(self.source_id)) != self.source_id:
            raise PlanningCenterError('committed_source_identity_invalid')
        newer = session.scalar(_locked(session, select(ProfileOutbox.key).where(
            ProfileOutbox.source_id == self.source_id, ProfileOutbox.phone == volunteer.phone,
            ProfileOutbox.key != row.key, ProfileOutbox.created_at >= row.created_at)))
        if newer:
            raise PlanningCenterError('committed_source_newer_or_ambiguous_revision')
        receipt = session.scalar(_locked(session, select(MacInboundReceipt).where(MacInboundReceipt.guid == row.source_guid)))
        route = row.payload.get('route')
        if (not receipt or route not in AVAILABILITY_ROUTES or receipt.result.get('intent') != route or
                not isinstance(receipt.result.get('session_id'), str) or not receipt.result['session_id']):
            raise PlanningCenterError('committed_source_availability_receipt_required')
        sid = receipt.result['session_id']
        messages = session.scalars(_locked(session, select(m.Message).where(
            m.Message.phone == volunteer.phone, m.Message.direction == 'in', m.Message.kind == 'mac_test_in',
            m.Message.purpose == 'test:' + sid, m.Message.status == 'received'))).all()
        matching = [message for message in messages if any(
            hashlib.sha256((volunteer.phone + '\0' + service + '\0' + sid + '\0' + message.body).encode()).hexdigest()
            == receipt.fingerprint for service in ('iMessage', 'SMS'))]
        if len(matching) != 1 or matching[0].volunteer_id != volunteer.id or matching[0].created_at > row.created_at:
            raise PlanningCenterError('committed_source_sender_history_not_verified')
        session.scalars(_locked(session, select(m.Availability).where(m.Availability.volunteer_id == volunteer.id))).all()
        session.scalars(_locked(session, select(m.Role))).all()
        session.scalars(_locked(session, select(m.EventType))).all()
        current = profile_sync.snapshot(session, volunteer.phone)
        if (not current or current != row.payload.get('profile') or
                current['preferences'].get('signup_source') != 'sms' or
                not current['sms_opt_in'] or not current['preferences'].get('consent_at') or
                not current['preferences'].get('consent_source') or current['status'] != 'active'):
            raise PlanningCenterError('committed_source_current_profile_or_consent_changed')
        revision = json.dumps(current, sort_keys=True, separators=(',', ':'), allow_nan=False)
        expected_key = hashlib.sha256((self.source_id + '\0' + row.source_guid + '\0' + revision).encode()).hexdigest()
        if row.key != expected_key:
            raise PlanningCenterError('committed_source_profile_digest_changed')
        return capture_source(session, self.config, volunteer_id=volunteer.id,
            provenance={'source_id': self.source_id, 'receipt_id': row.source_guid, 'revision': row.key},
            tz=self.settings.church_timezone, now=self.clock())
