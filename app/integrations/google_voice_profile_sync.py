"""Explicit single-recipient Google profile mirror, never a messaging worker."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import select

from app.core import profile_sync as sync
from app.db import models as m
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim, GoogleVoiceInboundReceipt


def scoped_settings(settings):
    if not settings.google_voice_profile_sync_enabled:
        raise sync.ProfileHeld('google_profile_sync_disabled')
    path = Path(settings.google_voice_profile_sync_scope_file)
    if not path.is_file() or path.is_symlink():
        raise sync.ProfileHeld('google_profile_scope_required')
    scope = json.loads(path.read_text())
    names = scope.get('expected_name')
    if (scope.get('transport') != 'google_voice' or
            not isinstance(scope.get('phones'), list) or len(scope['phones']) != 1 or
            not isinstance(scope['phones'][0], str) or
            not re.fullmatch(r'\+[1-9][0-9]{7,14}', scope['phones'][0]) or
            not isinstance(scope.get('project_ref'), str) or
            not re.fullmatch(r'[a-z]{20}', scope['project_ref']) or
            not isinstance(names, dict) or set(names) != {'first_name', 'last_name'} or
            any(not isinstance(value, str) or not value.strip() for value in names.values()) or
            not isinstance(scope.get('session_id'), str) or not scope['session_id'] or
            not isinstance(scope.get('sender_fingerprint'), str) or
            not re.fullmatch(r'[a-f0-9]{64}', scope['sender_fingerprint'])):
        raise sync.ProfileHeld('invalid_google_profile_scope')
    return replace(settings, profile_sync_enabled=True,
        profile_sync_phones=scope['phones'][0], profile_sync_project_ref=scope['project_ref'],
        profile_sync_role_map=json.dumps(scope.get('role_map', {}))), scope


def provenance(session, settings, *, phone, guid, route):
    from app.core.consent_controls import control_action
    from app.integrations.google_voice_demo import (RECIPIENT_KEY, expected_name_matches,
        registered_consent_provenance)
    configured, scope = scoped_settings(settings)
    if phone not in sync.approved_phones(configured):
        raise sync.ProfileHeld('google_profile_recipient_not_approved')
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    record = session.get(m.Policy, RECIPIENT_KEY + phone)
    receipt = session.get(GoogleVoiceInboundReceipt, guid)
    incoming = session.get(m.Message, receipt.result.get('source_message_id')) if receipt else None
    proof = (record.value.get('consent') or {}) if record else {}
    invitation = (record.value.get('invitation') or {}) if record else {}
    approval = session.get(m.Approval, invitation.get('approval_id'))
    disclosure = session.get(m.Message, proof.get('disclosure_message_id'))
    claim = session.get(GoogleVoiceDeliveryClaim, disclosure.id) if disclosure else None
    submitted = session.get(m.Notification, 'google-demo-submission:' + str(disclosure.id)) if disclosure else None
    stop = route == 'stop'
    if (not volunteer or not record or not receipt or not incoming or
            record.value.get('state') != 'active' or
            record.value.get('phone') != phone or
            record.value.get('session', {}).get('id') != scope['session_id'] or
            proof.get('session_id') != scope['session_id'] or
            record.value.get('sender_fingerprint') != scope['sender_fingerprint'] or
            not expected_name_matches({'expected_name': scope['expected_name']}, record.value.get('expected_name')) or
            receipt.result.get('intent') != route or receipt.result.get('session_id') != scope['session_id'] or
            incoming.phone != phone or incoming.kind != 'google_voice_test_in' or
            incoming.direction != 'in' or incoming.status != 'received' or
            incoming.purpose != 'test:' + scope['session_id'] or
            not approval or approval.status != 'approved' or
            approval.payload.get('message_id') != proof.get('disclosure_message_id') or
            approval.payload.get('content_hash') != invitation.get('content_hash') or
            not claim or claim.idempotency_key != disclosure.provider_sid or
            not submitted or submitted.state != 'submitted' or
            receipt.fingerprint != hashlib.sha256((phone + '\0' + scope['session_id'] + '\0' + incoming.body).encode()).hexdigest() or
            not registered_consent_provenance(session, volunteer, require_current_consent=not stop)):
        raise sync.ProfileHeld('google_profile_provenance_changed')
    if stop and (volunteer.sms_opt_in or control_action(incoming.body) != 'stop' or
            not session.get(m.Policy, 'sms_opt_out:' + phone)):
        raise sync.ProfileHeld('google_profile_stop_required')
    # A later withdrawal takes precedence even over a previously valid queued row.
    if not stop and (not volunteer.sms_opt_in or session.get(m.Policy, 'sms_opt_out:' + phone)):
        raise sync.ProfileHeld('newer_local_opt_out')
    return {'transport': 'google_voice', 'project_ref': scope['project_ref'],
        'session_id': scope['session_id'], 'sender_fingerprint': scope['sender_fingerprint'],
        'expected_name': scope['expected_name'], 'receipt_fingerprint': receipt.fingerprint,
        'source_message_id': incoming.id,
        'consent_hash': hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}


def capture_google_profile(session, settings, *, phone, guid, route, before, effective_at):
    if not settings.google_voice_profile_sync_enabled or route not in sync.PROFILE_ROUTES:
        return None
    try:
        proof = provenance(session, settings, phone=phone, guid=guid, route=route)
        configured, _ = scoped_settings(settings)
    except (sync.ProfileHeld, OSError, ValueError, TypeError, KeyError):
        # An invalid profile never blocks local consent withdrawal or creates an outbox row.
        return None
    row = sync.capture(session, configured, phone=phone, guid=guid, route=route,
        before=before, effective_at=effective_at, consent_only=route == 'stop')
    if row:
        row.payload = {**row.payload, 'google_voice_provenance': proof}
        session.flush()
    return row


def validate_publication(session, settings, row):
    try:
        current = provenance(session, settings, phone=row.phone,
            guid=row.source_guid, route=row.payload['route'])
        if current != row.payload.get('google_voice_provenance'):
            raise sync.ProfileHeld('google_profile_provenance_changed')
        configured, _ = scoped_settings(settings)
        if configured.profile_sync_project_ref != settings.profile_sync_project_ref:
            raise sync.ProfileHeld('target_scope_changed')
    except sync.ProfileHeld:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise sync.ProfileHeld('google_profile_provenance_changed') from None
