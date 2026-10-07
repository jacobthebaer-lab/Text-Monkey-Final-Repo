"""Explicit welcome actions, with one durable successful receipt per identity."""
from copy import deepcopy
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.db import models as m
from app.core import confirmations
from app.core.onboarding import start
from app.core.send_gate import SendGate
from app.integrations import mac_roster
from app.llm.gloo_client import GlooUnavailableError
from app.sms.transport import transport_name,queue_result


def key(person): return 'volunteer-welcome:'+str(person.id)


def terminal_no_send(session,person,row):
    """Require affirmative terminal evidence, never infer it from a missing claim."""
    if not row or row.detail.get('phone')!=person.phone:
        return False
    incoming=session.scalar(select(m.Message.id).where(m.Message.phone==person.phone,
        m.Message.direction=='in',m.Message.status=='received',m.Message.created_at>=row.created_at,
        m.Message.kind!='synthetic').limit(1))
    if incoming: return False
    result=row.detail.get('result',{})
    approval_id=result.get('approval_id')
    message_id=result.get('message_id')
    approval=session.get(m.Approval,approval_id) if approval_id else None
    if approval_id:
        if not approval: return False
        message_id=approval.payload.get('message_id') or message_id
        if not message_id:
            # A rejected/expired review never entered the native queue. Cross-
            # check durable confirmation receipts rather than stage defaults.
            sent=session.scalar(select(m.Notification.key).where(m.Notification.key.startswith('confirmation:'),
                m.Notification.detail['approval_id'].as_integer()==approval_id).limit(1))
            return approval.status in {'rejected','expired','cancelled'} and sent is None
    message=session.get(m.Message,message_id) if message_id else None
    if (not message or message.phone!=person.phone or message.volunteer_id!=person.id
            or message.purpose!='signup_reply' or not message.status.startswith('blocked_')):
        return False
    from app.integrations.mac_models import MacDeliveryClaim
    if message.status=='blocked_native_route':
        route=session.get(m.Notification,'mac-native-route:'+str(message.id))
        return bool(route and route.purpose=='native_route_hold' and route.message_id==message.id
            and route.volunteer_id==person.id and route.detail.get('native_attempted') is False
            and session.get(MacDeliveryClaim,message.id) is not None)
    proof=session.get(m.Notification,'welcome-presend:'+str(message.id))
    if not proof: return False
    return (session.get(MacDeliveryClaim,message.id) is None and
        proof.detail=={'phone':message.phone,'provider_sid':message.provider_sid,'status':message.status,
                       'phase':'queued_before_claim'})


def retry_binding(session,person,proof_key):
    proof=session.get(m.Notification,proof_key) if isinstance(proof_key,str) else None
    old=session.get(m.Notification,proof.detail.get('previous')) if proof else None
    if (not proof or proof.purpose!='welcome_retry' or proof.volunteer_id!=person.id
            or proof.detail.get('phone')!=person.phone or not terminal_no_send(session,person,old)):
        return None
    return proof.key


def previous(session,person,*,receipts=None):
    row=receipts.get(person.id) if receipts is not None else session.get(m.Notification,key(person))
    if row and not terminal_no_send(session,person,row):
        if row.detail.get('phone')!=person.phone:
            return {'identity_changed':True}
        return deepcopy(row.detail['result'])
    # Legacy native intake evidence, never the mere default stage "complete".
    rows=session.execute(select(m.Message.id,m.Message.status,m.Notification.detail).join(
        m.Notification,m.Notification.message_id==m.Message.id).where(
        m.Message.volunteer_id==person.id,m.Message.phone==person.phone,
        m.Message.direction=='out',m.Message.purpose=='signup_reply',
        m.Message.provider_sid.startswith('MAC'),m.Message.status.in_(['queued','dispatching','submitted','uncertain']),
        m.Notification.key.startswith('conversation-message:'))).all()
    for message_id,status,metadata in rows:
        if 'interests' in metadata.get('intake_fields',[]):
            return {'delivery':'already_prepared','message_id':message_id,'approval_id':None}
    approvals=session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text',
        m.Approval.status.in_(['pending','approved']),m.Approval.payload['volunteer_id'].as_integer()==person.id,
        m.Approval.payload['phone'].as_string()==person.phone,
        m.Approval.payload['transport'].as_string()=='mac_messages',
        m.Approval.payload['purpose'].as_string()=='signup_reply')).all()
    for approval in approvals:
        if 'interests' in approval.payload.get('conversation',{}).get('intake_fields',[]):
            return {'delivery':'awaiting_confirmation','message_id':approval.payload.get('message_id'),'approval_id':approval.id}
    return None


def prepare(session,state,person,user,preflight,*,real_only=False):
    if real_only and not mac_roster.eligible(session,person):
        raise HTTPException(409,'Welcome texts require a real, active volunteer with recorded text consent and no opt-out.')
    if not person.sms_opt_in or person.status!='active':
        raise HTTPException(409,'Text consent and an active volunteer profile are required before sending a welcome text.')
    saved=previous(session,person)
    if saved:
        if saved.get('identity_changed'): raise HTTPException(409,'The volunteer identity changed; a separate identity review is required.')
        return {**saved,'duplicate':True}
    if blocked:=preflight(state,session,person): raise HTTPException(blocked[0],blocked[2])
    old=session.get(m.Notification,key(person))
    retry=None
    if old and terminal_no_send(session,person,old):
        now=state.clock.now();attempt=str(uuid4())
        archive='welcome-attempt:'+attempt
        session.add(m.Notification(key=archive,volunteer_id=person.id,purpose='volunteer_welcome_attempt',
            state='terminal_no_send',created_at=old.created_at,due_at=old.due_at,message_id=old.message_id,detail=deepcopy(old.detail)))
        retry='welcome-retry:'+attempt
        session.add(m.Notification(key=retry,volunteer_id=person.id,purpose='welcome_retry',state='authorized',
            created_at=now,due_at=now,detail={'phone':person.phone,'previous':archive}))
        session.flush();session.info['welcome_retry']=retry
    selected=state.provider.test_sessions.get(person.phone)
    session.info['mac_test_session']=selected
    session.info['conversation_origin']=transport_name(state.provider)
    session.info['confirmation_now']=state.clock.now()
    from app.web.admin_setup import owner
    try:
        outcome=start(session,state.clock,SendGate(session,state.clock,state.provider),person,state.gloo,
                      copy_owner=owner(user) if user.get('id') else None)
    except GlooUnavailableError:
        raise HTTPException(503,'Gloo could not compose the welcome text. Nothing was sent; try again.')
    finally:
        session.info.pop('welcome_retry',None)
    if not outcome.sent and not outcome.approval_id:
        raise HTTPException(409,outcome.reason or 'The welcome text is held by the texting rules.')
    result={'delivery':'awaiting_confirmation' if outcome.approval_id else queue_result(state.provider),
            'approval_id':outcome.approval_id,'message_id':outcome.message_id,'duplicate':False}
    now=state.clock.now()
    if old:
        old.state='prepared';old.created_at=now;old.due_at=now;old.message_id=outcome.message_id
        old.detail={'phone':person.phone,'result':deepcopy(result),'retry_proof':retry}
    else:
        session.add(m.Notification(key=key(person),volunteer_id=person.id,purpose='volunteer_welcome',
            state='prepared',created_at=now,due_at=now,message_id=outcome.message_id,
            detail={'phone':person.phone,'result':deepcopy(result)}))
    session.info.pop('welcome_retry',None)
    session.flush();return result


def batch_step(state,user,request_id,ids,preflight):
    from app.core.offer_windows import begin_decision
    batch_key='welcome-batch:'+request_id
    actor=str(user.get('id') or user['email'].lower())
    with state.session_factory() as session:
        session.info['record_authorized']=True
        session.info['confirmation_now']=state.clock.now()
        begin_decision(session)
        if state.settings.competition_confirmation_required!=confirmations.enabled(session):
            raise HTTPException(409,'Texting mode changed. Reload before preparing welcomes.')
        def locked_record():
            return session.scalar(select(m.Notification).where(m.Notification.key==batch_key)
                .with_for_update().execution_options(populate_existing=True))
        record=locked_record()
        if record is None:
            now=state.clock.now()
            detail={'actor':actor,'volunteer_ids':ids,'results':[],'next':0}
            try:
                with session.begin_nested():
                    record=m.Notification(key=batch_key,purpose='welcome_batch',state='processing',
                        due_at=now,created_at=now,detail=deepcopy(detail))
                    session.add(record);session.flush()
            except IntegrityError:
                record=locked_record()
                if record is None: raise
        if record.detail['actor']!=actor or record.detail['volunteer_ids']!=ids:
            raise HTTPException(409,'This welcome request belongs to a different selection or administrator.')
        detail=deepcopy(record.detail)
        if detail['next']<len(ids):
            person=session.scalar(select(m.Volunteer).where(m.Volunteer.id==ids[detail['next']]).with_for_update())
            name=person.name if person else 'Unavailable volunteer'
            result={'volunteer_id':str(ids[detail['next']]),'name':name}
            try:
                with session.begin_nested():
                    if person is None: raise HTTPException(404,'Volunteer not found.')
                    outcome=prepare(session,state,person,user,preflight,real_only=True)
                    result={**result,**outcome,'status':'already_prepared' if outcome.get('duplicate') else 'prepared'}
            except HTTPException as error:
                result={**result,'status':'failed' if error.status_code>=500 else 'held','reason':str(error.detail)}
            detail['results'].append(result);detail['next']+=1
        done=detail['next']==len(ids)
        record.detail=deepcopy(detail);record.state='completed' if done else 'processing'
        session.commit()
        return {'request_id':request_id,'done':done,'completed':detail['next'],'total':len(ids),'results':detail['results']}
