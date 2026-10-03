"""Bounded routine browser delivery: gate provenance plus mutable preflight.

Single coordinator backend/store scope; never a general send endpoint. Operational
proofs use existing Notification storage, atomically with SendGate's Message row.
"""
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import select

from app.db import models as m
from app.core import offer_windows as offers
from app.core.policies import PolicyStore, in_quiet_hours

ROUTINE_PURPOSES = frozenset({'outreach', 'reminder', 'booking_status', 'confirmation',
                              'cancellation_ack', 'filled_thanks'})


def is_voice(provider):
    return getattr(provider, 'requires_policy_preflight', False)


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def schedule(session, volunteer_id):
    rows = session.scalars(select(m.Assignment).where(m.Assignment.volunteer_id == volunteer_id)
                          .order_by(m.Assignment.id)).all()
    return [{'id': a.id, 'status': a.status, 'shift_id': a.shift_id,
             'event_status': a.shift.event.status, 'shift': offers.snapshot(a.shift)} for a in rows]


def context_for(gate, purpose, volunteer, outreach, now):
    session = gate.session
    if purpose not in ROUTINE_PURPOSES or volunteer is None:
        return None
    if purpose == 'outreach':
        if not outreach or not gate.provider.automation_enabled:
            return None
        fill = session.get(m.FillRequest, outreach.fill_request_id)
        shift = session.get(m.Shift, fill.shift_id)
        approval = next((a for a in session.scalars(select(m.Approval).where(m.Approval.kind=='send_outreach',
            m.Approval.status=='approved')) if a.payload.get('fill_request_id')==fill.id and
            a.payload.get('volunteer_id')==volunteer.id and a.payload.get('role_id')==shift.role_id),None)
        return {'outreach_id': outreach.id, 'fill_id': fill.id, 'shift': offers.snapshot(shift),
                'role_approval_id':approval.id if approval else None}
    if purpose == 'booking_status':
        incoming = session.get(m.Message, gate.reply_to_message_id) if gate.reply_to_message_id else None
        selected = gate.provider.test_sessions.get(volunteer.phone)
        if (not selected or not incoming or incoming.direction != 'in' or incoming.phone != volunteer.phone or
                incoming.purpose != 'test:' + selected.id or not timedelta(0) <= now-incoming.created_at <= timedelta(minutes=10)):
            return None
        from app.core.booking_status import requested
        if not requested(session, volunteer, incoming.body, now):
            return None
        active_offers = [{'id':o.id,'response':o.response,'fill_state':session.get(m.FillRequest,o.fill_request_id).state,
                          'deadline':offers.metadata(session,o).expires_at.isoformat() if offers.metadata(session,o) else None}
                         for o in session.scalars(select(m.Outreach).where(m.Outreach.volunteer_id==volunteer.id)).all()]
        return {'reply_to': incoming.id, 'schedule': schedule(session, volunteer.id), 'offers':active_offers}
    # Bind each update to a saved application notification and schedule facts.
    source = session.scalar(select(m.Notification).where(m.Notification.volunteer_id == volunteer.id,
        m.Notification.purpose == purpose, m.Notification.state == 'pending')
        .order_by(m.Notification.created_at.desc()).limit(1))
    if not source:
        return None
    facts = schedule(session, volunteer.id)
    if purpose == 'reminder':
        if not gate.provider.automation_enabled or not source.event_id:
            return None
        if not any(a['event_status']=='scheduled' and a['status'] in {'approved','confirmed'} and
                   session.get(m.Shift,a['shift_id']).event_id==source.event_id and
                   datetime.fromisoformat(a['shift']['start'])>now for a in facts):
            return None
    elif purpose == 'cancellation_ack':
        if not source.key.startswith('cancel:'):
            return None
        if not any(source.key == 'cancel:'+str(a['id']) and a['status']=='cancelled' for a in facts):
            return None
    elif purpose in {'confirmation','filled_thanks'}:
        prefix = 'winner:' if purpose=='confirmation' else 'closed:'
        try:
            _, fill_id, recipient_id = source.key.split(':')
            fill = session.get(m.FillRequest,int(fill_id))
            if not source.key.startswith(prefix) or int(recipient_id)!=volunteer.id or not fill or fill.state!='filled':
                return None
            if purpose=='confirmation' and not any(a['shift_id']==fill.shift_id and a['status']=='confirmed' for a in facts):
                return None
            if purpose=='filled_thanks':
                prior = session.scalar(select(m.Outreach).where(m.Outreach.fill_request_id==fill.id,
                    m.Outreach.volunteer_id==volunteer.id))
                msg = session.get(m.Message,prior.message_id) if prior and prior.message_id else None
                if not msg or msg.status not in {'sent','submitted'}:
                    return None
        except (ValueError,TypeError):
            return None
    return {'notification_key':source.key,'source_body':source.body,'event_id':source.event_id,'schedule':facts}


def stamp(gate, message, context, now, *, urgent=False, approved=False):
    selected = gate.provider.test_sessions[message.phone]
    expires = min(selected.expires_at, now+timedelta(minutes=10))
    if message.purpose=='outreach':
        fill = gate.session.get(m.FillRequest,context['fill_id'])
        expires = min(selected.expires_at, offers.cutoff(gate.session,gate.session.get(m.Shift,fill.shift_id).event.starts_at))
    payload = {'mode':'routine','phone':message.phone,'body':message.body,'purpose':message.purpose,
               'volunteer_id':message.volunteer_id,'provider_sid':message.provider_sid,'message_id':message.id,
               'session_id':selected.id,'expires_at':expires.isoformat(),'context':context,
               'urgent':urgent,'restricted_approved':approved}
    gate.session.add(m.Notification(key=f'voice-policy:{message.id}',volunteer_id=message.volunteer_id,
        message_id=message.id,purpose=message.purpose,body='',state='authorized',due_at=now,created_at=now,
        expires_at=expires,detail={'payload':payload,'hash':digest(payload)}))
    gate.session.flush()


def proof_for(session,message):
    return session.get(m.Notification,f'voice-policy:{message.id}')


def refresh_offer(session,message):
    """Only called after the existing offer dispatcher rewrites its deadline."""
    proof = proof_for(session,message)
    payload = {**proof.detail['payload'],'body':message.body}
    outreach = session.get(m.Outreach,payload['context']['outreach_id'])
    meta = offers.metadata(session,outreach)
    payload['expires_at'] = min(proof.expires_at,meta.expires_at).isoformat()
    proof.detail = {'payload':payload,'hash':digest(payload)}
    proof.expires_at = datetime.fromisoformat(payload['expires_at'])
    session.flush()


def problem(session,provider,message,now):
    proof = proof_for(session,message)
    if not proof or proof.state!='authorized' or not isinstance(proof.detail,dict):
        return 'missing gate policy proof'
    p = proof.detail.get('payload',{})
    if proof.detail.get('hash')!=digest(p) or p.get('mode')!='routine':
        return 'changed policy proof'
    expected = {'phone':message.phone,'body':message.body,'purpose':message.purpose,'volunteer_id':message.volunteer_id,
                'provider_sid':message.provider_sid,'message_id':message.id}
    if any(p.get(k)!=v for k,v in expected.items()) or message.purpose not in ROUTINE_PURPOSES:
        return 'message changed after gate authorization'
    selected = provider.test_sessions.get(message.phone)
    if (not selected or not selected.active(now) or p.get('session_id')!=selected.id or
            not message.provider_sid.startswith(selected.outbound_prefix) or not provider.allows(message.phone)):
        return 'recipient session changed'
    try:
        expires = datetime.fromisoformat(p['expires_at'])
    except (TypeError,ValueError,KeyError):
        return 'invalid policy expiry'
    if (expires.tzinfo is None or proof.message_id!=message.id or proof.volunteer_id!=message.volunteer_id or
            proof.expires_at!=expires or message.direction!='out' or
            not selected.starts_at<=message.created_at<selected.expires_at):
        return 'policy provenance changed'
    if now>=proof.expires_at:
        return 'policy proof expired'
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.id==message.volunteer_id)
                               .with_for_update().execution_options(populate_existing=True))
    optout = session.get(m.Policy,'sms_opt_out:'+message.phone)
    if (not volunteer or volunteer.phone!=message.phone or not volunteer.sms_opt_in or volunteer.status!='active' or
            (optout and optout.value.get('value'))):
        return 'recipient no longer consenting'
    from app.core.send_gate import has_open_sensitive_escalation
    if has_open_sensitive_escalation(session,volunteer.id) or any(h.get('phone')==message.phone
        for h in session.scalars(select(m.Escalation.related_ids).where(m.Escalation.category=='sensitive',
            m.Escalation.status.in_(('open','acknowledged'))))):
        return 'care hold requires human review'
    if session.scalar(select(m.Message.id).where(m.Message.phone==message.phone,m.Message.id!=message.id,
                         m.Message.direction=='out',m.Message.status.in_(('dispatching','uncertain'))).limit(1)):
        return 'delivery pending reconciliation'
    context = p['context']
    reply = session.get(m.Message,context.get('reply_to')) if context.get('reply_to') else None
    if reply and (reply.purpose!='test:'+selected.id or reply.phone!=message.phone or
                  not timedelta(0)<=now-reply.created_at<=timedelta(minutes=10) or
                  session.scalar(select(m.Message.id).where(m.Message.phone==message.phone,
                    m.Message.direction=='in',m.Message.purpose=='test:'+selected.id,m.Message.id>reply.id).limit(1))):
        return 'reply input changed or expired'
    if 'schedule' in context and schedule(session,volunteer.id)!=context['schedule']:
        return 'schedule facts changed'
    if context.get('notification_key'):
        source = session.get(m.Notification,context['notification_key'])
        if (not source or source.message_id!=message.id or source.volunteer_id!=volunteer.id or source.state!='sent' or
                source.purpose!=message.purpose or source.body!=context['source_body'] or
                (source.expires_at and now>=source.expires_at)):
            return 'notification provenance changed'
        if message.purpose in {'reminder','confirmation'} and not any(a['status'] in {'approved','confirmed'} and
                a['event_status']=='scheduled' and datetime.fromisoformat(a['shift']['start'])>now for a in context['schedule']):
            return 'scheduled update is no longer current'
    if context.get('offers') is not None:
        actual = [{'id':o.id,'response':o.response,'fill_state':session.get(m.FillRequest,o.fill_request_id).state,
                   'deadline':offers.metadata(session,o).expires_at.isoformat() if offers.metadata(session,o) else None}
                  for o in session.scalars(select(m.Outreach).where(m.Outreach.volunteer_id==volunteer.id)).all()]
        if actual!=context['offers'] or any(o['deadline'] and datetime.fromisoformat(o['deadline'])<=now and
            o['response'] in offers.OPEN_RESPONSES and o['fill_state'] in offers.OPEN_FILLS for o in actual):
            return 'booking offer facts changed or expired'
    if message.purpose in {'outreach','reminder','filled_thanks'} and not provider.automation_enabled:
        return 'routine automation is disabled'
    policies = PolicyStore(session)
    start,end = policies.urgent_quiet_hours() if p['urgent'] else policies.quiet_hours()
    from app.core.send_gate import SendGate, ASK_PURPOSES, UNSENT_STATUSES
    gate = SendGate(session,None,provider,context.get('reply_to'))
    if in_quiet_hours(now.astimezone(policies.church_tz()),start,end) and not gate._immediate_reply(message.phone,message.purpose,now):
        return 'sending hours changed'
    if message.purpose in ASK_PURPOSES:
        others = session.scalars(select(m.Message.created_at).where(m.Message.volunteer_id==volunteer.id,
            m.Message.id!=message.id,m.Message.direction=='out',m.Message.purpose.in_(ASK_PURPOSES),
            m.Message.status.not_in(UNSENT_STATUSES+('blocked_policy',)))).all()
        month = now.astimezone(policies.church_tz()).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        if sum(a>=month for a in others)>=policies.ask_budget():
            return 'monthly budget changed'
        if any(a>now-timedelta(hours=int(policies.get('outreach_cooldown_hours'))) for a in others):
            return 'outreach cooldown changed'
    if message.purpose=='outreach':
        outreach = session.get(m.Outreach,context['outreach_id'])
        fill = session.get(m.FillRequest,context['fill_id'])
        shift = session.get(m.Shift,fill.shift_id) if fill else None
        from app.core import eligibility
        if (not outreach or outreach.message_id!=message.id or not shift or context['shift']!=offers.snapshot(shift) or
                shift.event.status!='scheduled' or now>=offers.cutoff(session,shift.event.starts_at) or
                not eligibility.check(session,volunteer,shift,tz=policies.get('church_timezone'))):
            return 'offer context or eligibility changed'
        if shift.role.fill_policy!='auto':
            approval = session.get(m.Approval,context.get('role_approval_id')) if context.get('role_approval_id') else None
            if (not p['restricted_approved'] or not approval or approval.status!='approved' or
                    approval.payload.get('fill_request_id')!=fill.id or approval.payload.get('volunteer_id')!=volunteer.id):
                return 'restricted role needs approval'
    return None


def wire(proof):
    return {'delivery_mode':'routine','confirmation_required':False,'policy_hash':proof.detail['hash'],
            'policy_expires_at':proof.expires_at.isoformat(),'purpose':proof.purpose}
