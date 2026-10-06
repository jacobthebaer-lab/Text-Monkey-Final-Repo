"""Operator-only, one-successor recovery of a proven pre-native Mac rejection.

No source/receipt replay, old-row reset, native retry or automatic hold release.
The ordinary current-input and delivery guards still validate the fresh reply.
"""
import hashlib
from copy import deepcopy
from sqlalchemy import select

from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim, MacInboundReceipt
from app.integrations.mac_progress import profile_hash
from app.core.conversational_signup import digest, source
from app.llm.gloo_client import GlooUnavailableError


REASONS = {'Conversational followup needs its current sender and validated draft',
           'Signup intake scope changed'}


def _donor(session, volunteer, incoming_id, blocked_id, guid, suppression_key, now):
    from app.core.outbound_conversation import _key
    incoming = source(session,volunteer,incoming_id,now)
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
    if session.scalar(select(m.Message.id).where(m.Message.phone==volunteer.phone,
            m.Message.direction=='out',m.Message.provider_sid.startswith(selected.outbound_prefix),
            m.Message.status.in_(('dispatching','uncertain'))).limit(1)):
        return None
    return {'incoming_id':incoming.id,'blocked_id':blocked.id,'receipt_guid':guid,
        'suppression_key':suppression.key,'session_id':selected.id,'phone':volunteer.phone,
        'original_turn_hash':digest(detail),'step_hash':digest(step.result),
        'blocked_body_hash':hashlib.sha256(blocked.body.encode()).hexdigest(),
        'source_metadata_hash':digest(meta),'receipt_hash':digest({'fingerprint':receipt.fingerprint,'result':receipt.result}),
        'reservation_hash':digest({'detail':reservation.detail,'state':reservation.state,'message_id':reservation.message_id}),
        'suppression_hash':digest({'detail':suppression.detail,'created_at':suppression.created_at})}


def _current_donor(session,volunteer,link,now):
    if not link or link.purpose!='mac_followup_recovery' or link.state!='pending':
        return None
    detail=link.detail or {}
    donor=detail.get('donor') or {}
    if (link.key!='mac-followup-recovery:'+str(donor.get('incoming_id'))
            or link.message_id!=donor.get('incoming_id') or link.volunteer_id!=volunteer.id
            or link.created_at>now):
        return None
    current=_donor(session,volunteer,donor.get('incoming_id'),donor.get('blocked_id'),
        donor.get('receipt_guid'),donor.get('suppression_key'),now)
    return current if current==donor else None


def context_valid(session,volunteer,incoming_id,now):
    link=session.get(m.Notification,session.info.get('mac_followup_recovery_key'))
    donor=_current_donor(session,volunteer,link,now)
    return bool(donor and donor['incoming_id']==incoming_id
        and link.detail.get('initial_profile_hash')==profile_hash(volunteer)
        and volunteer.preferences.get('onboarding_stage')=='availability'
        and session.get(m.Notification,'onboarding-turn-recovery:'+str(incoming_id)) is None)


def recovery_turn(session,volunteer,incoming_id,now):
    link=session.get(m.Notification,session.info.get('mac_followup_recovery_key'))
    donor=_current_donor(session,volunteer,link,now)
    if not donor or donor['incoming_id']!=incoming_id:
        raise GlooUnavailableError('Original pre-native rejection evidence changed')
    return ('onboarding-turn-recovery:'+str(incoming_id),
        {'recovery_key':link.key,'recovery_donor_hash':digest(donor)})


def recovery_binding(session,volunteer,turn,now):
    link=session.get(m.Notification,(turn.detail or {}).get('recovery_key'))
    donor=_current_donor(session,volunteer,link,now)
    return bool(donor and turn.message_id==donor['incoming_id']
        and turn.detail.get('recovery_donor_hash')==digest(donor))


def recover_blocked_followup(session,clock,gate,volunteer,gloo,*,blocked_id,receipt_guid,
        suppression_key,original_turn_hash,step_hash,blocked_body_hash,expected_profile_hash):
    """Trusted Python operator stages one newly interpreted/composed response.

    Inputs are independently reviewed private evidence, never model tool input.
    Commit through the caller's normal transaction. This does not submit native
    delivery, enable a worker, change a cursor, or remove any historical hold.
    """
    from app.core import onboarding
    from app.core.conversational_signup import resume_followup
    now=clock.now();incoming_id=gate.reply_to_message_id
    donor=_donor(session,volunteer,incoming_id,blocked_id,receipt_guid,suppression_key,now)
    if (not donor or donor['original_turn_hash']!=original_turn_hash or donor['step_hash']!=step_hash
            or donor['blocked_body_hash']!=blocked_body_hash):
        raise GlooUnavailableError('Reviewed original rejection proof changed or native submission was possible')
    key='mac-followup-recovery:'+str(incoming_id)
    link=session.get(m.Notification,key)
    if link is None:
        if expected_profile_hash!=profile_hash(volunteer) or volunteer.preferences.get('onboarding_stage')!='availability':
            raise GlooUnavailableError('Reviewed current profile changed')
        link=m.Notification(key=key,purpose='mac_followup_recovery',state='pending',
            volunteer_id=volunteer.id,message_id=incoming_id,body='',created_at=now,due_at=now,
            detail={'donor':deepcopy(donor),'initial_profile_hash':expected_profile_hash})
        session.add(link);session.flush()  # Unique source key bounds concurrent callers.
    elif link.detail.get('donor')!=donor or link.detail.get('initial_profile_hash')!=expected_profile_hash:
        raise GlooUnavailableError('A different recovery is already recorded for this input')
    previous=session.info.get('mac_followup_recovery_key')
    session.info['mac_followup_recovery_key']=key
    try:
        turn=session.get(m.Notification,'onboarding-turn-recovery:'+str(incoming_id))
        if turn:
            # Composition outage can continue only while there is no new
            # reservation; queued/blocked/uncertain/sent successors stay final.
            return resume_followup(session,clock,gate,volunteer,gloo,
                turn_key=turn.key,step_hash=turn.detail['step_hash'])
        if not context_valid(session,volunteer,incoming_id,now):
            raise GlooUnavailableError('Recovery scope or current profile changed')
        incoming=session.get(m.Message,incoming_id)
        return onboarding.handle(session,clock,gate,volunteer,incoming.body,gloo)
    finally:
        if previous is None:
            session.info.pop('mac_followup_recovery_key',None)
        else:
            session.info['mac_followup_recovery_key']=previous
