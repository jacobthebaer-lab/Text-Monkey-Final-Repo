"""Exceptional signup redirects; Gloo composes, code owns the missing question."""
import json
import re
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from sqlalchemy import select
from app.core.message_style import outbound_style_problem

PRIVACY = re.compile(r'\b(?:privacy|forget me|(?:delete|erase|export) (?:all )?my (?:data|information|info|account|record))\b',re.I)
CLAIMS = re.compile(r'\b(?:YES|consent|opt(?:ed)?[ -]?in|signed up|all set|complete[ds]?|saved|recorded|noted|booked|scheduled|approved|verified|qualified|reply|text me|tell me|provide|choose|answer|send me)\b',re.I)
CONTROLS = re.compile(r'\b(?:STOP|HELP)\b')


def reset_attempts(session,phone,stage):
    row=session.get(m.Policy,'signup_recovery:'+phone+':'+stage)
    if row is not None:
        session.delete(row)


def privacy_hold(session,phone,volunteer=None):
    rows=session.scalars(select(m.Escalation).where(m.Escalation.category=='privacy',
        m.Escalation.status.in_(('open','acknowledged'))))
    return any(row.related_ids.get('phone')==phone or
        (volunteer is not None and row.related_ids.get('volunteer_id')==volunteer.id) for row in rows)


def validate_reply(output,recovery,question):
    try:
        data=json.loads(output)
    except (ValueError,TypeError) as exc:
        raise ValueError('Recovery must be structured JSON') from exc
    if (not isinstance(data,dict) or set(data)!={'stage','missing','acknowledgment','question'}
            or data['stage']!=recovery['stage'] or data['missing']!=recovery['missing']
            or data['question']!=question):
        raise ValueError('Recovery must ask only the current missing question')
    ack=data['acknowledgment']
    if (not isinstance(ack,str) or len(ack)>120
            or re.search(r'[?\r\n{}\d\u2014]|https?://|www\.|[\U0001F000-\U0001FAFF\u2600-\u27BF]',ack)
            or CLAIMS.search(ack) or CONTROLS.search(ack)):
        raise ValueError('Acknowledgment cannot add questions, commands or operational claims')
    # No progress acknowledgments. Only the essential missing intake question
    # reaches the volunteer, even if Gloo returns a harmless acknowledgment.
    text=question
    if len(text)>400 or outbound_style_problem(text):
        raise ValueError('Recovery too long')
    return text


def redirect(session,clock,gate,gloo,*,phone,body,stage,missing,question,saved=None,volunteer=None,name_correction=None):
    """No advancement, roster activation or factual invention in this helper."""
    selected=session.info.get('mac_test_session')
    session_id=selected.id if selected else None
    key='signup_recovery:'+phone+':'+stage
    row=session.get(m.Policy,key)
    state=row.value if row else {}
    attempts=state.get('attempts',0) if state.get('session_id')==session_id else 0
    if PRIVACY.search(body) or (attempts>=2 and not name_correction):
        category='privacy' if PRIVACY.search(body) else 'unclear'
        session.add(m.Escalation(category=category,severity='normal',
            summary='Signup needs coordinator review; no automatic signup redirect sent.',
            related_ids={'phone':phone,**({'volunteer_id':volunteer.id} if volunteer else {})},
            status='open',created_at=clock.now()))
        return 'onboarding_review' if volunteer and stage!='name' else 'signup_identity_review'
    from app.core.signup_responder import compose_signup_reply
    recovery={'stage':stage,'missing':missing,'actual_reply':body[:1000],
        'saved_answers':saved or {},'question':question}
    from app.core.signup_delivery import intake_context, intake_block
    conversation=intake_context(session,phone,stage,missing,saved)
    if name_correction:
        conversation['name_correction'] = name_correction
    if intake_block(session,clock,gate,phone=phone,volunteer=volunteer,conversation=conversation):
        return 'signup_intake_suppressed' if stage=='name' else 'onboarding_suppressed'
    try:
        reply=compose_signup_reply(session,clock,gloo,question,phone=phone,volunteer=volunteer,
            signup_conversation=True,require_gloo=True,allow_emoji=False,recovery=recovery)
    except GlooUnavailableError:
        session.add(m.Escalation(category='system_error',severity='normal',
            summary='Gloo could not compose a signup redirect; no substitute sent.',
            related_ids={'phone':phone,**({'volunteer_id':volunteer.id} if volunteer else {})},
            status='open',created_at=clock.now()))
        return 'onboarding_review' if stage!='name' else 'signup_identity_review'
    result=gate.send(body=reply,purpose='signup_reply',phone=phone,volunteer=volunteer,
        conversation=conversation)
    if result.status.value=='blocked_policy':
        return 'signup_intake_suppressed' if stage=='name' else 'onboarding_suppressed'
    if row is None:
        row=m.Policy(key=key,value={})
        session.add(row)
    row.value={'session_id':session_id,'attempts':attempts+1}
    return 'signup_name_needed' if stage=='name' else 'onboarding_clarify'
