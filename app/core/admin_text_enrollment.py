"""Exact roster review and auditable admin consent, never signup/send authority."""
import hashlib
import json
from datetime import timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from app.db import models as m

ADMIN_PURPOSES = frozenset({'coordinator_notify', 'escalation_notify', 'admin_reply'})


def evidence_hash(detail):
    return hashlib.sha256(json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()


def record_hash(row):
    fields = {key: getattr(row, key) for key in
        ('id', 'name', 'phone', 'status', 'sms_opt_in', 'is_coordinator', 'is_pastor', 'preferences')}
    fields['qualifications'] = [
        {key: getattr(q, key) for key in ('id', 'type', 'status', 'verified_by', 'verified_at', 'expires_on')}
        for q in sorted(row.qualifications, key=lambda q: q.id)]
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def primary_hash(workspace, recipients):
    source = [workspace.id, workspace.owner_id, workspace.revision, workspace.details,
        [(row.id, record_hash(row)) for row in sorted(recipients, key=lambda row: row.id)]]
    return hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()


def review_existing(session, owner_id, workspace, target, recipients, now):
    if (not target or (target.preferences or {}).get('admin_text_owner') not in (None, owner_id) or
            target.status != 'active' or not target.sms_opt_in or session.get(m.Policy, 'sms_opt_out:' + target.phone)):
        raise HTTPException(409, 'Only an active, unowned or own roster record without an opt-out can be reviewed.')
    key = 'admin-recipient-review:' + uuid4().hex
    detail = {'owner_id': owner_id, 'target_id': target.id, 'phone': target.phone,
        'record_hash': record_hash(target), 'primary_hash': primary_hash(workspace, recipients)}
    receipt = m.Notification(key=key, purpose='human_review', body='', state='awaiting_review',
        due_at=now, created_at=now, expires_at=now + timedelta(minutes=10), detail=detail)
    session.add(receipt)
    session.flush()
    return {'review_id': key, **{key: detail[key] for key in ('record_hash', 'primary_hash')},
        'recipient': {'id': target.id, 'name': target.name, 'phone': target.phone},
        'replacing': [{'id': row.id, 'name': row.name, 'phone': row.phone}
            for row in recipients if row.status == 'active' and row.id != target.id], 'expires_at': receipt.expires_at.isoformat()}


def consume_review(session, owner_id, workspace, target, recipients, data, now):
    key = data.get('review_id')
    review = session.scalar(select(m.Notification).where(m.Notification.key == key).with_for_update()) if isinstance(key, str) else None
    expected = {'owner_id': owner_id, 'target_id': target.id, 'phone': target.phone,
        'record_hash': record_hash(target), 'primary_hash': primary_hash(workspace, recipients)}
    if (data.get('operator_consent') is not True or data.get('consent') is not False or
            not review or review.state != 'awaiting_review' or review.purpose != 'human_review' or
            not key.startswith('admin-recipient-review:') or not review.expires_at or now >= review.expires_at or
            review.detail != expected or data.get('record_hash') != expected['record_hash'] or
            data.get('primary_hash') != expected['primary_hash'] or
            (target.preferences or {}).get('admin_text_owner') not in (None, owner_id) or
            target.status != 'active' or not target.sms_opt_in or session.get(m.Policy, 'sms_opt_out:' + target.phone)):
        raise HTTPException(409, 'Review changed, expired or lacks confirmed recipient consent. Review the exact record again.')
    claimed = session.execute(update(m.Notification).where(m.Notification.key == key,
        m.Notification.state == 'awaiting_review').values(state='approved'))
    if claimed.rowcount != 1:
        raise HTTPException(409, 'This review was already consumed. Review the record again.')
    return key


def record_consent(session, owner_id, recipient, provider, settings, now, *, mode, review_id=None):
    """Distinguish an operator's attestation from the recipient's own checkbox."""
    now = now.astimezone(timezone.utc)
    session.flush()  # New self-service recipients need their durable row identity.
    selected = getattr(provider, 'test_sessions', {}).get(recipient.phone)
    google = getattr(provider, 'transport_name', '') == 'google_voice'
    from app.integrations.google_voice_demo import sender_fingerprint
    key = 'admin-text-consent:' + uuid4().hex
    review = session.get(m.Notification, review_id) if review_id else None
    detail = {'owner_id': owner_id, 'recipient_id': recipient.id, 'phone': recipient.phone,
        'name': recipient.name, 'mode': mode, 'review_id': review_id,
        'review_hash': evidence_hash(review.detail) if review else None,
        'purposes': sorted(ADMIN_PURPOSES), 'consent_at': now.isoformat(),
        'sender_fingerprint': sender_fingerprint(settings) if google else None,
        'session': {'id': selected.id, 'starts_at': selected.starts_at.isoformat(),
            'expires_at': selected.expires_at.isoformat()} if google and selected else None}
    session.add(m.Notification(key=key, purpose='human_review', body='', state='enrolled',
        due_at=now, created_at=now, volunteer_id=recipient.id, detail=detail))
    recipient.preferences = {**recipient.preferences, 'admin_text_consent_at': now.isoformat(),
        'admin_text_consent_key': key, 'admin_text_consent_mode': mode}


def google_consent_problem(session, provider, recipient, now):
    if not recipient or not recipient.is_coordinator or recipient.status != 'active' or not recipient.sms_opt_in:
        return 'Admin recipient is not active and consenting'
    prefs = recipient.preferences or {}
    key = prefs.get('admin_text_consent_key')
    receipt = session.get(m.Notification, key) if isinstance(key, str) and key.startswith('admin-text-consent:') else None
    detail = receipt.detail if receipt else {}
    selected = provider.test_sessions.get(recipient.phone)
    from app.integrations.google_voice_demo import sender_fingerprint
    if (not receipt or receipt.state != 'enrolled' or receipt.purpose != 'human_review' or
            receipt.volunteer_id != recipient.id or detail.get('recipient_id') != recipient.id or
            not prefs.get('admin_text_owner') or detail.get('owner_id') != prefs.get('admin_text_owner') or
            detail.get('phone') != recipient.phone or detail.get('name') != recipient.name or
            detail.get('mode') not in {'self_service', 'operator_attested'} or
            detail.get('mode') != prefs.get('admin_text_consent_mode') or
            detail.get('consent_at') != prefs.get('admin_text_consent_at') or
            detail.get('consent_at') != receipt.created_at.isoformat() or receipt.created_at > now or
            detail.get('purposes') != sorted(ADMIN_PURPOSES) or
            detail.get('sender_fingerprint') != sender_fingerprint(provider.settings) or
            session.get(m.Policy, 'sms_opt_out:' + recipient.phone) or not selected or not selected.active(now) or
            not selected.active(receipt.created_at) or
            detail.get('session') != {'id': selected.id, 'starts_at': selected.starts_at.isoformat(),
                'expires_at': selected.expires_at.isoformat()}):
        return 'Admin updates require their recorded consent and exact current cloud session'
    if detail['mode'] == 'operator_attested':
        review = session.get(m.Notification, detail.get('review_id'))
        if (not review or review.state != 'approved' or review.purpose != 'human_review' or
                review.detail.get('owner_id') != detail['owner_id'] or
                review.detail.get('target_id') != recipient.id or
                review.detail.get('phone') != recipient.phone or
                evidence_hash(review.detail) != detail.get('review_hash')):
            return 'Admin enrollment review is missing or revoked'
    return None
