"""Authenticated durable review acknowledgement, with unmet release holds.

No FrequencyReview constructor is accepted as authorization. No native client,
executor, worker or migration is called. The separately mounted router supplies
the existing authenticated Supabase principal, never request-supplied actor data.
"""
from datetime import timedelta, timezone
import hashlib
import hmac
import json
import re
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.core.planning_center_committed_source import CommittedAvailabilityReader
from app.integrations.planning_center import PlanningCenterError
from app.integrations.planning_center_availability import _hash, _json, verify_current, PCOAvailabilityPreview
from app.integrations.planning_center_frequency_executor import _aware, _load
from app.integrations.planning_center_review_models import PCOFrequencyReviewReceipt
from app.web.texty import check_user

RELEASE_HOLDS = ['native_notification_silence_unverified', 'native_edit_coordination_unverified',
                 'fresh_native_preflight_required']


def release_holds(preview, op=None):
    return [*RELEASE_HOLDS, *(['audited_local_correction_preview_only']
            if 'correction_lineage' in preview.value['source'] else []), *(op['holds'] if op else [])]


def _signature(document, signing_key):
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise PlanningCenterError('frequency_review_signing_key_not_configured')
    return hmac.new(signing_key, b'text-monkey:pco-frequency-review:v1\0' + document.encode(), hashlib.sha256).hexdigest()


def _actor(user, settings):
    try:
        check_user(user, settings)
        identifier = str(UUID(user['id']))
        email = user['email'].lower()
        if identifier != user['id'] or len(email) > 120:
            raise ValueError()
        return {'id': identifier, 'email': email}
    except (HTTPException, KeyError, TypeError, ValueError, AttributeError):
        raise PlanningCenterError('frequency_review_verified_coordinator_required') from None


def _context(session, settings, config, intent_key, clock):
    try:
        return _context_read(session, settings, config, intent_key, clock)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCenterError('frequency_review_bound_preview_invalid') from None


def _context_read(session, settings, config, intent_key, clock):
    intent, preview, source, remote, op = _load(session, config, intent_key)
    if intent.state not in {'preview', 'held', 'noop', 'conflict'}:
        raise PlanningCenterError('frequency_review_intent_already_consumed')
    if op['kind'] != 'membership_frequency' or op['method'] not in {'PATCH', 'NONE'}:
        raise PlanningCenterError('frequency_review_supported_membership_required')
    members = [m for m in remote.value['memberships'] if 'role:' + str(m['binding']['role_id']) == op['logical_key']]
    if len(members) != 1 or members[0]['binding']['service_type_id'] not in config.service_type_ids:
        raise PlanningCenterError('frequency_review_membership_scope_invalid')
    binding = members[0]['binding']
    provenance = source.value['provenance']
    reader = CommittedAvailabilityReader(settings, config, volunteer_id=source.value['volunteer_id'],
        profile_key=provenance['revision'], source_id=provenance['source_id'], clock=clock,
        allow_audited_correction=True)
    current = reader(session)
    verify_current(preview, current, remote)  # Native side is stored, not a fresh GET.
    return intent, preview, op, binding, reader


def review_proposal(session, settings, config, *, intent_key, user, clock):
    _actor(user, settings)
    intent, preview, op, binding, _ = _context(session, settings, config, intent_key, clock)
    member = next(m['resource'] for m in preview.value['remote']['memberships'] if m['binding'] == binding)
    saved = session.get(PCOAvailabilityPreview, intent.preview_key)
    return {'intent_key': intent_key, 'preview_hash': preview.digest,
        'source_hash': preview.value['source_hash'], 'remote_hash': preview.value['remote_hash'],
        'operation_hash': _hash(op), 'organization_id': intent.organization_id, 'person_id': intent.person_id,
        'membership': binding, 'operation': op,
        'native_snapshot': {'schedule_preference': member['attributes'].get('schedule_preference'),
                            'saved_at': saved.created_at.isoformat()},
        'release_holds': release_holds(preview, op), 'execution_enabled': False}


def issue_review_receipt(session, settings, config, *, intent_key, user, expected, clock, signing_key):
    actor = _actor(user, settings)
    _signature('', signing_key)  # Reject missing private key before any receipt write.
    intent, preview, op, binding, _ = _context(session, settings, config, intent_key, clock)
    exact = {'preview_hash': preview.digest, 'source_hash': preview.value['source_hash'],
             'remote_hash': preview.value['remote_hash'], 'operation_hash': _hash(op)}
    if expected != exact:
        raise PlanningCenterError('frequency_review_stale_or_changed_proposal')
    now = _aware(clock()).astimezone(timezone.utc)
    identifier, expiry = str(uuid4()), now + timedelta(minutes=10)
    document = _json({'schema': 1, 'id': identifier, 'actor': actor, 'action': 'review_membership_frequency_patch',
        'decision': 'reviewed_held', 'intent_key': intent_key, **exact,
        'organization_id': intent.organization_id, 'person_id': intent.person_id,
        'volunteer_id': preview.value['source']['volunteer_id'], 'membership': binding,
        'source_provenance': preview.value['source']['provenance'],
        'release_holds': release_holds(preview, op), 'execution_enabled': False,
        'issued_at': now.isoformat(), 'expires_at': expiry.isoformat()})
    row = PCOFrequencyReviewReceipt(id=identifier, intent_key=intent_key, organization_id=intent.organization_id,
        person_id=intent.person_id, actor_id=actor['id'], actor_email=actor['email'], state='reviewed_held',
        document=document, signature=_signature(document, signing_key), created_at=now, expires_at=expiry)
    session.add(row); session.flush()  # Caller owns transaction; never commit here.
    return {'receipt_id': identifier, 'receipt_hash': _hash(json.loads(document)), 'state': row.state,
            'expires_at': expiry.isoformat(), 'release_holds': json.loads(document)['release_holds'],
            'execution_enabled': False}


def verify_review_receipt(session, settings, config, *, receipt_id, user, clock, signing_key):
    try:
        return _verify_review_receipt(session, settings, config, receipt_id=receipt_id, user=user,
                                      clock=clock, signing_key=signing_key)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlanningCenterError('frequency_review_durable_document_invalid') from None


def _verify_review_receipt(session, settings, config, *, receipt_id, user, clock, signing_key):
    actor = _actor(user, settings)
    try:
        if not isinstance(receipt_id, str) or str(UUID(receipt_id)) != receipt_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PlanningCenterError('frequency_review_durable_receipt_id_required') from None
    row = session.get(PCOFrequencyReviewReceipt, receipt_id, populate_existing=True, with_for_update=True)
    if (not row or row.state != 'reviewed_held' or not re.fullmatch('[a-f0-9]{64}', row.signature) or
            not hmac.compare_digest(row.signature, _signature(row.document, signing_key))):
        raise PlanningCenterError('frequency_review_durable_signature_required')
    data = json.loads(row.document)
    if _json(data) != row.document:
        raise PlanningCenterError('frequency_review_noncanonical_document')
    now = _aware(clock())
    if (data['id'] != row.id or data['actor'] != actor or row.actor_id != actor['id'] or row.actor_email != actor['email'] or
            (data['intent_key'], data['organization_id'], data['person_id']) !=
            (row.intent_key, row.organization_id, row.person_id) or row.organization_id != config.organization_id or
            data['issued_at'] != row.created_at.isoformat() or data['expires_at'] != row.expires_at.isoformat() or
            not row.created_at <= now < row.expires_at or row.expires_at - row.created_at != timedelta(minutes=10) or
            data['action'] != 'review_membership_frequency_patch' or data['decision'] != 'reviewed_held' or
            data['execution_enabled'] is not False):
        raise PlanningCenterError('frequency_review_actor_scope_or_expiry_changed')
    _, preview, op, binding, _ = _context(session, settings, config, row.intent_key, clock)
    if (data['preview_hash'] != preview.digest or data['source_hash'] != preview.value['source_hash'] or
            data['remote_hash'] != preview.value['remote_hash'] or data['operation_hash'] != _hash(op) or
            data['membership'] != binding or data['source_provenance'] != preview.value['source']['provenance'] or
            data['volunteer_id'] != preview.value['source']['volunteer_id'] or
            data['release_holds'] != release_holds(preview, op)):
        raise PlanningCenterError('frequency_review_bound_evidence_changed')
    return {'receipt_id': row.id, 'receipt_hash': _hash(data), 'state': 'reviewed_held',
            'release_holds': data['release_holds'], 'execution_enabled': False}


def authorize_frequency_execution(session, settings, config, *, receipt_id, user, clock, signing_key):
    """Fail closed: no established native evidence authority exists in this batch.

    Production wiring must use a receipt authority, never construct the raw
    executor's trusted FrequencyReview from UI JSON or a caller-provided hash.
    No approved release is fabricated to make the low-level executor callable.
    """
    verify_review_receipt(session, settings, config, receipt_id=receipt_id, user=user,
                          clock=clock, signing_key=signing_key)
    raise PlanningCenterError('frequency_live_release_evidence_unavailable')
