"""Durable actual-input read authority for internal preference review only.

This module never authorizes intake, composition, native delivery or scheduling.
The original texting epoch remains expired. All comparisons use actual now.
"""
import hashlib
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core.conversation import inbound_scope
from app.core.conversational_signup import digest
from app.integrations.mac_models import MacInboundReceipt,MacDeliveryClaim


def received(session, volunteer, incoming_id, now):
    from app.core.signup_recovery import privacy_hold
    from app.core.send_gate import has_open_sensitive_escalation
    selected=session.info.get('mac_test_session')
    policy=session.get(m.Policy,'conversational_signup:'+volunteer.phone)
    bounded_end=(selected.original_expires_at or selected.expires_at) if selected else None
    fresh=(selected and selected.expires_at is None and getattr(selected,'enrolled_at',None)==selected.starts_at
        and selected.ongoing_since==selected.starts_at)
    if (not selected or not selected.outbound_prefix.startswith('MAC') or now<selected.starts_at
            or not (fresh or (bounded_end and timedelta(0)<bounded_end-selected.starts_at<=timedelta(hours=2)))
            or not policy or policy.value.get('value') is not True or policy.value.get('session_id')!=selected.id
            or not volunteer.sms_opt_in or volunteer.status!='active'):
        return None
    optout=session.get(m.Policy,'sms_opt_out:'+volunteer.phone)
    if ((optout and optout.value.get('value')) or privacy_hold(session,volunteer.phone,volunteer)
            or has_open_sensitive_escalation(session,volunteer.id)):
        return None
    incoming=session.scalar(select(m.Message).where(m.Message.id==incoming_id,inbound_scope(selected),
        m.Message.phone==volunteer.phone,m.Message.volunteer_id==volunteer.id,
        m.Message.direction=='in',m.Message.status=='received',m.Message.created_at>=selected.starts_at,
        selected.window(m.Message.created_at),m.Message.created_at<=now))
    latest=session.scalar(select(m.Message.id).where(m.Message.phone==volunteer.phone,
        m.Message.direction=='in',m.Message.status=='received').order_by(m.Message.id.desc()).limit(1))
    return incoming if incoming and latest==incoming.id else None


def history(session, volunteer, now):
    from app.core.privacy import safe_message_history
    selected=session.info['mac_test_session']
    rows=session.scalars(select(m.Message).where(inbound_scope(selected),m.Message.phone==volunteer.phone,
        m.Message.volunteer_id==volunteer.id,m.Message.direction=='in',m.Message.status=='received',
        m.Message.created_at>=selected.starts_at,selected.window(m.Message.created_at),
        m.Message.created_at<=now).order_by(m.Message.id.desc()).limit(8)).all()
    return [{'incoming_id':r.id,'body':r.body[:4000]} for r in safe_message_history(session,reversed(rows))]


def binding(session, volunteer, proof, now):
    if not isinstance(proof, dict) or set(proof) != {'incoming_id', 'turn_key'}:
        return None
    incoming = received(session, volunteer, proof['incoming_id'], now) if volunteer else None
    row = session.get(m.Notification, proof['turn_key'])
    if not incoming or not row:
        return None
    recovery = row.key == 'onboarding-turn-recovery:' + str(incoming.id)
    if row.key != 'onboarding-turn:' + str(incoming.id) and not recovery:
        return None
    if recovery:
        if not _recovery_binding(session,volunteer,row,now):
            return None
    detail = row.detail or {}
    complete = detail.get('stage') == 'complete'
    interests = detail.get('stage') == 'interests'
    draft = volunteer.preferences if complete or interests else (volunteer.preferences or {}).get('onboarding_availability_draft')
    step = session.get(m.AgentStep,detail.get('step_id'))
    if (row.purpose != 'onboarding_turn' or row.message_id != incoming.id
            or row.volunteer_id != volunteer.id or detail.get('phone') != volunteer.phone
            or detail.get('session_id') != session.info['mac_test_session'].id
            or detail.get('body_hash') != hashlib.sha256(incoming.body.encode()).hexdigest()
            or detail.get('stage') not in {'interests','availability','complete'}
            or (volunteer.preferences or {}).get('onboarding_stage') != ('complete' if complete else 'interests' if interests else 'availability')
            or detail.get('draft_hash') != digest(draft)):
        return None
    extraction=step.result.get('extraction') if step and isinstance(step.result,dict) else None
    if (not step or not step.run or not isinstance(step.result,dict) or step.type!='decision' or step.run.agent!='onboarding'
            or step.result.get('stage')!=('interests' if interests else 'availability') or digest(step.result)!=detail.get('step_hash')
            or not isinstance(extraction,dict) or extraction.get('understood') is not True or extraction.get('sensitive') is not False
            or not step.run.ended_at or not incoming.created_at<=step.run.started_at<=step.created_at<=step.run.ended_at<=now):
        return None
    if detail.get('coordinator_review_key'):
        review = session.get(m.Notification, detail['coordinator_review_key'])
        escalation = session.get(m.Escalation, (review.detail or {}).get('escalation_id')) if review else None
        expected = {k: v for k, v in detail.items() if k != 'coordinator_review_key'}
        if (not review or review.key != 'onboarding-coordinator:' + str(incoming.id)
                or review.purpose != 'onboarding_review' or review.state != 'pending'
                or review.message_id != incoming.id or review.volunteer_id != volunteer.id
                or {k: v for k, v in review.detail.items() if k != 'escalation_id'} != expected
                or not escalation or escalation.category != 'planning_preferences'
                or escalation.status not in {'open', 'acknowledged'}
                or escalation.related_ids != {'phone': volunteer.phone, 'volunteer_id': volunteer.id,
                    'incoming_id': incoming.id, 'session_id': detail['session_id'],
                    'turn_key': row.key, 'draft_hash': detail['draft_hash']}):
            return None
    return dict(detail)


def _recovery_donor(session, volunteer, incoming_id, blocked_id, guid, suppression_key, now, *, own_dispatch_id=None):
    from app.core.outbound_conversation import _key
    from app.core.mac_followup_recovery import REASONS
    incoming = received(session,volunteer,incoming_id,now)
    original = session.get(m.Notification,'onboarding-turn:'+str(incoming_id))
    blocked = session.get(m.Message,blocked_id)
    receipt = session.get(MacInboundReceipt,guid)
    suppression = session.get(m.Notification,suppression_key)
    recorded = session.get(m.Notification,'conversation-message:'+str(blocked_id))
    selected = session.info.get('mac_test_session')
    if not all((incoming,original,blocked,receipt,suppression,recorded,selected)):
        return None
    detail = original.detail or {}
    step = session.get(m.AgentStep,detail.get('step_id'))
    proof = {'incoming_id':incoming_id,'turn_key':original.key}
    meta = recorded.detail or {}
    if (original.purpose!='onboarding_turn' or original.message_id!=incoming_id
            or original.volunteer_id!=volunteer.id or detail.get('stage')!='availability'
            or detail.get('phone')!=volunteer.phone or detail.get('session_id')!=selected.id
            or detail.get('body_hash')!=hashlib.sha256(incoming.body.encode()).hexdigest()
            or not step or step.type!='decision' or step.run.agent!='onboarding'
            or step.result.get('stage')!='availability' or digest(step.result)!=detail.get('step_hash')
            or step.run.started_at<incoming.created_at or step.created_at>now
            or not step.run.ended_at or step.run.ended_at>now
            or blocked.direction!='out' or blocked.phone!=volunteer.phone
            or blocked.volunteer_id!=volunteer.id or blocked.purpose!='signup_reply'
            or blocked.status!='blocked_policy' or not blocked.provider_sid
            or not blocked.provider_sid.startswith(selected.outbound_prefix)
            or not incoming.created_at<=blocked.created_at<=now
            or session.get(MacDeliveryClaim,blocked.id) is not None
            or recorded.purpose!='conversation_source' or recorded.state!='recorded'
            or recorded.message_id!=blocked.id or recorded.volunteer_id!=volunteer.id
            or meta.get('signup_followup')!=proof or meta.get('binding')!=detail
            or not isinstance(meta.get('keys'),list) or len(meta['keys'])!=1
            or suppression.purpose!='conversation_suppression' or suppression.state!='blocked_policy'
            or suppression.detail.get('purpose')!='signup_reply'
            or suppression.detail.get('reason') not in REASONS
            or not blocked.created_at<=suppression.created_at<=now
            or suppression.key!=_key(['suppressed',blocked.phone,blocked.purpose,blocked.body,
                suppression.created_at.date().isoformat(),suppression.detail['reason']])
            or receipt.result.get('session_id')!=selected.id):
        return None
    fingerprints={hashlib.sha256((incoming.phone+'\0'+service+'\0'+selected.id+'\0'+incoming.body).encode()).hexdigest()
        for service in ('SMS','iMessage')}
    if receipt.fingerprint not in fingerprints:
        return None
    reservation=session.get(m.Notification,meta['keys'][0])
    if (not reservation or reservation.purpose!='conversation_delivery' or reservation.message_id!=blocked.id
            or reservation.volunteer_id!=volunteer.id or reservation.state!='queued' or reservation.detail!=meta):
        return None
    # A different uncertain delivery cannot be used to justify another native
    # attempt while recovering this rejected conversation.
    unresolved=select(m.Message.id).where(m.Message.phone==volunteer.phone,
            m.Message.direction=='out',m.Message.provider_sid.startswith(selected.outbound_prefix),
            m.Message.status.in_(('dispatching','uncertain')))
    if own_dispatch_id is not None:
        unresolved=unresolved.where(m.Message.id!=own_dispatch_id)
    if session.scalar(unresolved.limit(1)):
        return None
    return {'incoming_id':incoming.id,'blocked_id':blocked.id,'receipt_guid':guid,
        'suppression_key':suppression.key,'session_id':selected.id,'phone':volunteer.phone,
        'original_turn_hash':digest(detail),'step_hash':digest(step.result),
        'blocked_body_hash':hashlib.sha256(blocked.body.encode()).hexdigest(),
        'source_metadata_hash':digest(meta),'receipt_hash':digest({'fingerprint':receipt.fingerprint,'result':receipt.result}),
        'reservation_hash':digest({'detail':reservation.detail,'state':reservation.state,'message_id':reservation.message_id}),
        'suppression_hash':digest({'detail':suppression.detail,'created_at':suppression.created_at})}


def _recovery_binding(session,volunteer,turn,now):
    from app.core.mac_followup_recovery import _own_dispatch
    link=session.get(m.Notification,(turn.detail or {}).get('recovery_key'))
    if not link or link.purpose!='mac_followup_recovery' or link.state!='pending':return False
    donor=(link.detail or {}).get('donor',{})
    if (link.key!='mac-followup-recovery:'+str(donor.get('incoming_id'))
            or link.message_id!=donor.get('incoming_id') or link.volunteer_id!=volunteer.id or link.created_at>now):return False
    current=_recovery_donor(session,volunteer,donor.get('incoming_id'),donor.get('blocked_id'),
        donor.get('receipt_guid'),donor.get('suppression_key'),now,own_dispatch_id=_own_dispatch(session,volunteer,link))
    return bool(current==donor and turn.message_id==donor.get('incoming_id')
        and turn.detail.get('recovery_donor_hash')==digest(donor))
