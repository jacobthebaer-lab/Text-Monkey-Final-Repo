"""First-contact admin disclosure, bound before Gloo and exact review."""
import hashlib
import re
from sqlalchemy import select, or_
from app.db import models as m

BASE = 'Text Monkey admin connection check. Event updates include coverage, open roles, and your next step.'
NOTICE = 'Text STOP to stop.'


def binding(session, volunteer, selected, key, now):
    owner = (volunteer.preferences or {}).get('admin_text_owner') if volunteer else None
    consent = session.get(m.Notification, (volunteer.preferences or {}).get('admin_text_consent_key')) if volunteer else None
    sender = consent.detail.get('sender_fingerprint') if consent else None
    if (not owner or not sender or consent.detail.get('owner_id') != owner or
            consent.detail.get('recipient_id') != volunteer.id or consent.detail.get('phone') != volunteer.phone or
            not isinstance(key, str) or not key.startswith(f'admin-check:{owner}:') or
            not selected or not selected.outbound_prefix.startswith('GV') or not selected.active(now)):
        return None
    # Native submission receipts retain the dedicated sender across renewals.
    # Queued/draft/uncertain rows, another transport, and unaudited flags do not
    # establish prior contact. The roster identity must remain this same record.
    prior = False
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    disclosure_id = (volunteer.preferences or {}).get('consent_disclosure_message_id')
    if type(disclosure_id) is int:
        from app.integrations.google_voice_demo import registered_consent_provenance
        disclosure = session.get(m.Message, disclosure_id)
        if (not disclosure or disclosure.volunteer_id is not None or disclosure.purpose != 'signup_reply' or
                disclosure.kind != 'ai' or NOTICE not in disclosure.body or
                not registered_consent_provenance(session, volunteer, require_current_consent=False)):
            disclosure_id = None
    else:
        disclosure_id = None
    history = session.execute(select(m.Message, m.Notification).join(GoogleVoiceDeliveryClaim,
        GoogleVoiceDeliveryClaim.message_id == m.Message.id).join(m.Notification,
        m.Notification.message_id == m.Message.id).where(m.Message.phone == volunteer.phone,
        or_(m.Message.volunteer_id == volunteer.id,
            (m.Message.volunteer_id.is_(None) & (m.Message.id == disclosure_id))),
        m.Message.direction == 'out', m.Message.created_at <= now,
        GoogleVoiceDeliveryClaim.idempotency_key == m.Message.provider_sid,
        m.Message.status.in_(('submitted', 'sent', 'delivered')),
        m.Notification.key.startswith('google-demo-submission:'),
        m.Notification.created_at >= m.Message.created_at, m.Notification.created_at <= now,
        m.Notification.state == 'submitted', m.Notification.purpose == 'human_review'))
    for message, receipt in history:
        detail = receipt.detail or {}
        conversation = session.get(m.Notification, f'conversation-message:{message.id}')
        old_source = (conversation.detail or {}).get('admin_check') if conversation else None
        if old_source is not None and not isinstance(old_source, dict):
            continue
        old_owner = old_source.get('owner_id') if old_source else None
        if (receipt.key == f'google-demo-submission:{message.id}' and
                isinstance(detail.get('session_id'), str) and re.fullmatch(r'[0-9a-f]{32}', detail['session_id']) and
                detail.get('sender_fingerprint') == sender and old_owner in (None, owner) and
                detail.get('body_hash') == hashlib.sha256(message.body.encode()).hexdigest() and
                message.provider_sid.startswith('GV' + str(detail.get('session_id')) + ':')):
            prior = True
            break
    return {'notification_key': key, 'owner_id': owner, 'volunteer_id': volunteer.id,
        'phone': volunteer.phone, 'sender_fingerprint': sender,
        'session_id': selected.id, 'include_notice': not prior}


def copy_for(source):
    return BASE + (' ' + NOTICE if source['include_notice'] else '')


def problem(session, volunteer, selected, source, body, now):
    fresh = binding(session, volunteer, selected, source.get('notification_key') if isinstance(source, dict) else None, now)
    if not fresh or source != fresh or body != copy_for(fresh):
        return 'Admin contact history or disclosure changed; fresh Gloo composition and exact review required'
    row = session.get(m.Notification, fresh['notification_key'])
    if (not row or row.purpose != 'coordinator_notify' or row.volunteer_id != volunteer.id or
            ((row.detail or {}).get('conversation') or {}).get('admin_check') != fresh['notification_key']):
        return 'Admin connection check has no matching saved request'
    return None


def approval_problem(session, provider, approval, now):
    p = approval.payload
    source = (p.get('conversation') or {}).get('admin_check')
    if getattr(provider, 'transport_name', '') != 'google_voice':
        return None
    if source is None:
        return 'Admin connection check needs a fresh disclosure review' if p.get('purpose') == 'coordinator_notify' and BASE in p.get('body', '') else None
    selected = getattr(provider, 'test_sessions', {}).get(p.get('phone'))
    volunteer = session.get(m.Volunteer, p.get('volunteer_id'))
    return problem(session, volunteer, selected, source, p.get('body'), now)
