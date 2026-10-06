"""One new human review for a proved pre-send rejection, never reset its claim."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.core import confirmations, outbound_conversation
from app.core.cloud_composition import reviewed_composition
from app.db import models as m
from app.integrations.google_voice_client import connector_for, verified_identity, ConnectorUnavailable
from app.integrations.google_voice_demo import demo_text_problem, sender_fingerprint, scope_fingerprint
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
from app.integrations.google_voice_runtime import _clock, is_paused

REASON = 'recipient_choice_wait_unavailable'


def held():
    raise HTTPException(409, 'This rejection has no matching proof of no submission. The original record stays held.')


def original(session, state, message_id, content_hash):
    now = _clock(state).now()
    row = session.get(m.Message, message_id)
    claim = session.get(GoogleVoiceDeliveryClaim, message_id)
    approval = confirmations.proof_for(session, row) if row else None
    selected = state.provider.test_sessions.get(row.phone) if row else None
    receipt = session.get(m.Notification, f'google-voice-gloo:{approval.id}') if approval else None
    if (not row or row.direction != 'out' or row.status != 'rejected' or not claim or
            claim.idempotency_key != row.provider_sid or not selected or not selected.active(now) or
            not row.provider_sid.startswith(selected.outbound_prefix) or not approval or approval.status != 'approved' or
            approval.via != 'web' or approval.decided_at is None or approval.decided_at > claim.created_at or
            approval.payload.get('message_id') != row.id or approval.payload.get('content_hash') != content_hash or
            approval.payload.get('session_id') != selected.id or approval.payload.get('transport') != 'google_voice' or
            approval.payload.get('phone') != row.phone or approval.payload.get('body') != row.body or
            approval.payload.get('purpose') != row.purpose or approval.payload.get('volunteer_id') != row.volunteer_id or
            row.purpose not in {'signup_reply','admin_reply','coordinator_notify','escalation_notify'} or
            row.purpose == 'signup_reply' and not approval.payload.get('reply_to_message_id') or
            not confirmations.valid(approval, now, content_hash) or not receipt or
            receipt.detail.get('presend_predecessor_id') is not None or
            not reviewed_composition(session, approval, selected) or
            session.get(m.Notification, f'google-demo-submission:{row.id}') or
            session.scalar(select(m.Message.id).where(m.Message.direction == 'out',
                m.Message.provider_sid.startswith('GV'), m.Message.status.in_(('dispatching','uncertain'))).limit(1))):
        held()
    decision = session.get(m.Notification, f'review:{approval.id}:approve')
    if (not decision or decision.state != 'sent' or decision.purpose != 'human_review' or
            decision.detail.get('action') != 'approve' or decision.detail.get('approval_id') != approval.id or
            decision.detail.get('actor') != approval.decided_by or
            decision.created_at != approval.decided_at or decision.detail.get('content_hash') != content_hash):
        held()
    return row, claim, approval, selected


def successor_composition_valid(session, approval, selected):
    """Copied proof is dependent on immutable donor, rejected claim and audit."""
    receipt = session.get(m.Notification, f'google-voice-gloo:{approval.id}')
    predecessor_id = receipt.detail.get('presend_predecessor_id') if receipt else None
    donor = session.get(m.Approval, predecessor_id) if type(predecessor_id) is int else None
    link = session.get(m.Policy, f'google-presend-review:{predecessor_id}') if donor else None
    audit = session.get(m.Notification, link.key) if link else None
    row = session.get(m.Message, donor.payload.get('message_id')) if donor else None
    claim = session.get(GoogleVoiceDeliveryClaim, row.id) if row else None
    donor_receipt = session.get(m.Notification, f'google-voice-gloo:{donor.id}') if donor else None
    decision = session.get(m.Notification, f'review:{donor.id}:approve') if donor else None
    if (not donor or donor.status != 'approved' or not row or row.status != 'rejected' or not claim or
            donor.via != 'web' or donor.decided_at is None or donor.decided_at > claim.created_at or
            not decision or decision.state != 'sent' or decision.purpose != 'human_review' or
            decision.created_at != donor.decided_at or decision.detail.get('actor') != donor.decided_by or
            decision.detail.get('action') != 'approve' or decision.detail.get('approval_id') != donor.id or
            decision.detail.get('content_hash') != donor.payload.get('content_hash') or
            claim.idempotency_key != row.provider_sid or not donor_receipt or
            donor_receipt.detail.get('presend_predecessor_id') is not None or
            not reviewed_composition(session, donor, selected) or not link or not audit or
            audit.state != 'pending' or audit.detail != link.value or
            link.value.get('successor_id') != approval.id or link.value.get('message_id') != row.id or
            link.value.get('original_payload') != donor.payload or
            {k:v for k,v in donor.payload.items() if k != 'message_id'} !=
                {k:v for k,v in approval.payload.items() if k != 'message_id'} or
            link.value.get('claim_key') != claim.idempotency_key or
            link.value.get('claim_created_at') != claim.created_at.isoformat() or
            link.value.get('donor_decision') != {'via':donor.via,'actor':donor.decided_by,
                'at':donor.decided_at.isoformat(),'audit':decision.detail} or
            link.value.get('body_hash') != hashlib.sha256(row.body.encode()).hexdigest() or
            row.body != donor.payload['body'] or row.phone != donor.payload['phone'] or
            row.purpose != donor.payload['purpose'] or row.volunteer_id != donor.payload.get('volunteer_id') or
            link.value.get('session') != {'id':selected.id,'starts_at':selected.starts_at.isoformat(),
                'expires_at':selected.expires_at.isoformat(),'continuous':selected.continuous} or
            session.get(m.Notification, f'google-demo-submission:{row.id}')):
        return False
    proof = link.value.get('native_absence', {})
    return (proof.get('ledger_absent') is True and proof.get('original_key_disabled') is True and
        proof.get('native_submission_attempted') is False and proof.get('reason_code') == REASON and
        proof.get('submission_key_hash') == hashlib.sha256(claim.idempotency_key.encode()).hexdigest() and
        proof.get('body_hash') == link.value['body_hash'] and proof.get('session_id') == selected.id and
        link.value.get('reason_code') == REASON and link.value.get('confirmed') is True and
        re.fullmatch('[a-f0-9]{64}', link.value.get('prepare_receipt_sha256', '')) is not None)


def original_reservation_allowed(session, approval, receipt):
    """Keep the original reservation intact; authorize only its audited successor."""
    selected = session.info.get('mac_test_session')
    if not approval or not selected or not successor_composition_valid(session,approval,selected):
        return False
    composed = session.get(m.Notification,f'google-voice-gloo:{approval.id}')
    donor = session.get(m.Approval,composed.detail['presend_predecessor_id'])
    return (receipt.message_id == donor.payload['message_id'] and receipt.purpose == 'conversation_delivery' and
        receipt.key in donor.payload.get('conversation',{}).get('keys',[]) and
        receipt.detail == donor.payload.get('conversation',{}))


def successor_review(session, state, actor, message_id, expected, prepare_receipt_sha256):
    now = _clock(state).now()
    if is_paused(session) or not state.settings.gloo_api_key:
        held()
    row, claim, donor, selected = original(session, state, message_id, expected)
    key = f'google-presend-review:{donor.id}'
    link = session.get(m.Policy, key)
    if link:
        successor = session.get(m.Approval, link.value.get('successor_id'))
        if (not successor or successor.status != 'pending' or not confirmations.valid(successor, now, expected) or
                link.value.get('prepare_receipt_sha256') != prepare_receipt_sha256 or
                not reviewed_composition(session, successor, selected)):
            held()
        return {'approval_id':successor.id,'content_hash':expected,'already_staged':True,
            'original_message_id':row.id,'native_submission_attempted':False}
    session.info['mac_test_session'] = selected
    volunteer = session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else None
    if (confirmations.delivery_problem(session, state.provider, donor, now, row) or
            demo_text_problem(session, state.provider, row.phone, row.body, row.purpose, now,
                reply_id=donor.payload.get('reply_to_message_id')) or
            outbound_conversation.problem(session, purpose=row.purpose, volunteer=volunteer, phone=row.phone,
                body=row.body, now=now, meta=donor.payload.get('conversation',{}), approval=donor,message=row)):
        held()
    from app.core.policies import PolicyStore, in_quiet_hours
    policies = PolicyStore(session)
    hours = policies.urgent_quiet_hours() if donor.payload.get('urgent') else policies.quiet_hours()
    if in_quiet_hours(now.astimezone(policies.church_tz()), *hours):
        from app.integrations.google_voice_quiet_test import deadline
        if not deadline(session, state.provider, row.phone, row.purpose, now, approval=donor): held()
    connector = connector_for(state)
    try:
        result = connector.observe_presend_absence({'idempotency_key':claim.idempotency_key,'to':row.phone,
            'body_hash':hashlib.sha256(row.body.encode()).hexdigest(),'session_id':selected.id,'reason_code':REASON})
        if not verified_identity(connector.health(), state.settings, state.provider): raise ConnectorUnavailable()
        proof = result.get('proof',{})
        observed = datetime.fromisoformat(proof.get('observed_at','').replace('Z','+00:00'))
        if (result.get('status') != 'unsubmitted' or not now - timedelta(seconds=30) <= observed <= _clock(state).now()+timedelta(seconds=30) or
                proof.get('ledger_absent') is not True or proof.get('original_key_disabled') is not True or
                proof.get('native_submission_attempted') is not False or proof.get('reason_code') != REASON or
                proof.get('submission_key_hash') != hashlib.sha256(claim.idempotency_key.encode()).hexdigest() or
                proof.get('body_hash') != hashlib.sha256(row.body.encode()).hexdigest() or
                proof.get('session_id') != selected.id or proof.get('sender_fingerprint') != sender_fingerprint(state.settings) or
                proof.get('scope_fingerprint') != scope_fingerprint(state.provider.test_sessions)):
            held()
    except (ConnectorUnavailable, ValueError, TypeError, AttributeError):
        held()
    payload = deepcopy(donor.payload);payload.pop('message_id',None)
    successor = m.Approval(kind='confirm_text',payload=payload,status='pending',requested_at=now)
    session.add(successor);session.flush()
    composition = session.get(m.Notification,f'google-voice-gloo:{donor.id}')
    session.add(m.Notification(key=f'google-voice-gloo:{successor.id}',purpose='human_review',state='composed',
        body='',due_at=now,created_at=now,detail={'composition':composition.detail['composition'],
            'presend_predecessor_id':donor.id}))
    value = {'original_id':donor.id,'successor_id':successor.id,'message_id':row.id,'original_payload':deepcopy(donor.payload),
        'claim_key':claim.idempotency_key,'claim_created_at':claim.created_at.isoformat(),
        'body_hash':hashlib.sha256(row.body.encode()).hexdigest(),'native_absence':proof,'reason_code':REASON,
        'prepare_receipt_sha256':prepare_receipt_sha256,'confirmed':True,'actor':actor,'at':now.isoformat(),
        'donor_decision':{'via':donor.via,'actor':donor.decided_by,'at':donor.decided_at.isoformat(),
            'audit':deepcopy(session.get(m.Notification,f'review:{donor.id}:approve').detail)},
        'session':{'id':selected.id,'starts_at':selected.starts_at.isoformat(),'expires_at':selected.expires_at.isoformat(),
            'continuous':selected.continuous}}
    session.add(m.Policy(key=key,value=value))
    session.add(m.Notification(key=key,purpose='human_review',state='pending',body='',due_at=now,created_at=now,
        message_id=row.id,detail=value))
    return {'approval_id':successor.id,'content_hash':expected,'already_staged':False,
        'original_message_id':row.id,'native_submission_attempted':False}
