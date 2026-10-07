"""Code-owned essential-intake metadata; no routine signup commentary."""
def intake_context(session,phone,stage,missing,saved=None):
    aliases={'first_name':'name','last_name':'name','name':'name',
        'interests':'interests','availability':'availability',
        'availability_correction':'availability','window_times':'availability',
        'frequency':'frequency'}
    fields=list(dict.fromkeys(aliases[field] for field in missing))
    return {'intake_fields':fields, **({'intake_progress': True} if saved else {}),
            **({'welcome_retry':session.info['welcome_retry']} if session is not None and session.info.get('welcome_retry') else {})}


def intake_block(session,clock,gate,*,phone,volunteer,conversation):
    """Suppress known/repeated intake before paying for outgoing composition."""
    from app.core import outbound_conversation as policy
    from app.core.send_gate import SendOutcome, SendStatus
    from app.db import models as m
    from sqlalchemy import select
    if volunteer is None:
        volunteer=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==phone))
    meta,error=policy.metadata(session,purpose='signup_reply',volunteer=volunteer,
        phone=phone,now=clock.now(),supplied=conversation,reply_id=gate.reply_to_message_id)
    error=error or policy.problem(session,purpose='signup_reply',volunteer=volunteer,
        phone=phone,body='',now=clock.now(),meta=meta)
    if error:
        policy.record_suppression(session,phone,'signup_reply','',clock.now(),error)
        return SendOutcome(SendStatus.BLOCKED_POLICY,reason=error)
    return None


def send_intake(session,clock,gate,*,compose,purpose,conversation,phone=None,volunteer=None):
    if purpose!='signup_reply':
        raise ValueError('Intake helper only supports signup replies')
    phone=phone or volunteer.phone
    blocked=intake_block(session,clock,gate,phone=phone,volunteer=volunteer,conversation=conversation)
    if blocked is not None:
        return blocked
    return gate.send(body=compose(),purpose=purpose,phone=phone,volunteer=volunteer,conversation=conversation)
