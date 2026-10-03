"""Gloo interprets signup; names and explicit consent are collected by text."""

from pathlib import Path
import json
import re
from datetime import timedelta
from sqlalchemy import select
from app.config import get_settings
from app.db import models as m
from app.llm.agent_loop import RunLogger
from app.llm.parser import _extract_json, keyword_sensitive
from app.llm.gloo_client import GlooUnavailableError
from app.core.signup_responder import compose_signup_reply
from app.core.care import escalate_sensitive
from app.core.signup_copy import compose_welcome, exact_enabled, delivered_exact_invitation
from app.core.signup_delivery import intake_context, send_intake

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup.md"
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
CONTROLS = {"STOP", "STOPALL", "START", "HELP", "UNSTOP", "UNSUBSCRIBE", "END", "QUIT"}


def identity_parts(session,clock,phone):
    """Only actual name parts after this recipient's scoped invitation count."""
    row=session.get(m.Policy,'signup_identity_draft:'+phone)
    if row is None:
        return {}
    state=row.value
    selected=session.info.get('mac_test_session')
    if state.get('session_id')!=(selected.id if selected else None):
        return {}
    if not delivered_exact_invitation(session,clock,phone,
            reply_message_id=state.get('message_id'),body=state.get('body')):
        return {}
    return {key:state[key] for key in ('first_name','last_name') if state.get(key)}


def recover_name(session,clock,gate,gloo,phone,body,*,volunteer=None):
    from app.core.signup_recovery import redirect
    saved=identity_parts(session,clock,phone) if volunteer is None else {}
    missing=[key for key in ('first_name','last_name') if key not in saved]
    question={('first_name','last_name'):"What's your first and last name?",
              ('first_name',):"What's your first name?",('last_name',):"What's your last name?"}[tuple(missing)]
    return redirect(session,clock,gate,gloo,phone=phone,body=body,stage='name',
        missing=missing,question=question,saved=saved,volunteer=volunteer)


def request_signup(session, clock, gloo, phone, body, gate=None):
    if not PHONE.fullmatch(phone) or body.strip().upper() in CONTROLS:
        return None
    from app.core.signup_recovery import privacy_hold
    if privacy_hold(session,phone) and not keyword_sensitive(body):
        return 'signup_identity_review'
    sensitive_rows = session.scalars(select(m.Escalation.related_ids).where(
        m.Escalation.category == "sensitive", m.Escalation.status.in_(("open", "acknowledged"))
    ))
    if any(e.get("phone") == phone for e in sensitive_rows):
        return "escalated_sensitive"
    existing = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    if existing:
        return (
            "signup_consent_pending"
            if existing.preferences.get("consent_pending")
            else "signup_complete"
        )
    # A short conversation lets JOIN followed by a name complete signup.
    from app.core.conversation import scope
    messages = session.scalars(
        scope(select(m.Message), session.info.get("mac_test_session"))
        .where(
            m.Message.phone == phone,
            m.Message.created_at >= clock.now() - timedelta(hours=24),
        )
        .order_by(m.Message.id.desc())
        .limit(8)
    ).all()
    conversation = [
        {"direction": msg.direction, "body": msg.body} for msg in reversed(messages)
    ]
    if not conversation or conversation[-1]["body"] != body:
        conversation.append({"direction": "in", "body": body})
    logger = RunLogger(
        session,
        clock,
        agent="signup",
        trigger="SMS signup",
        model=getattr(gloo, "settings", get_settings()).parser_model,
    )
    if keyword_sensitive(body):
        escalate_sensitive(session, gate, None, body, clock.now(), phone=phone)
        logger.close("sensitive")
        return "escalated_sensitive"
    from app.core.signup_recovery import PRIVACY
    if exact_enabled(session,phone) and PRIVACY.search(body) and gate:
        logger.close('privacy_review')
        return recover_name(session,clock,gate,gloo,phone,body)
    try:
        if gloo is None:
            raise GlooUnavailableError('Gloo is required to interpret signup')
        response = gloo.create_response(
            model=getattr(gloo, "settings", get_settings()).parser_model,
            instructions=PROMPT.read_text(),
            input=json.dumps(conversation),
        )
        logger.add_usage(getattr(response, "usage", None))
        data = _extract_json(getattr(response, "output_text", "") or "") or {}
    except GlooUnavailableError:
        session.add(
            m.Escalation(
                category="system_error",
                severity="normal",
                summary="Gloo could not process text signup.",
                related_ids={"phone": phone},
                status="open",
                created_at=clock.now(),
            )
        )
        logger.close("gloo_unavailable")
        return "escalated_signup"
    if data.get("sensitive") is True or keyword_sensitive(body):
        escalate_sensitive(session, gate, None, body, clock.now(), phone=phone, severity=data.get("severity"))
        logger.close("sensitive")
        return "escalated_sensitive"
    exact=exact_enabled(session,phone)
    invited=bool(exact and gate and delivered_exact_invitation(session,clock,phone,
        reply_message_id=gate.reply_to_message_id,body=body))
    if exact and (body.strip().upper() in {'NO','N'} or re.search(
            r"\b(?:don't|do not|not|never)\s+(?:want to\s+)?(?:sign|join|volunteer)",body,re.I)):
        logger.close('signup_declined')
        return 'signup_declined'
    if data.get("signup") is not True:
        logger.close("not_signup")
        if invited:
            return recover_name(session,clock,gate,gloo,phone,body)
        return None
    first, last = data.get("first_name"), data.get("last_name")
    parts=identity_parts(session,clock,phone) if exact else {}
    prior_parts=dict(parts)
    if exact and invited and data.get('identity_reply') is True:
        supplied={key:value.strip() for key,value in (('first_name',first),('last_name',last))
            if isinstance(value,str) and 0<len(value.strip())<=80}
        if len(supplied)==1 and re.fullmatch(r"\s*(?:(?:my name is|i am|i'm)\s+)?"+
                re.escape(next(iter(supplied.values())))+r"[.!]?\s*",body,re.I):
            parts={**parts,**supplied}
            if parts!=prior_parts:
                from app.core.signup_recovery import reset_attempts
                reset_attempts(session,phone,'name')
            if len(parts)<2:
                row=session.get(m.Policy,'signup_identity_draft:'+phone)
                if row is None:
                    row=m.Policy(key='signup_identity_draft:'+phone,value={})
                    session.add(row)
                selected=session.info.get('mac_test_session')
                row.value={**parts,'message_id':gate.reply_to_message_id,'body':body,
                           'session_id':selected.id if selected else None}
        first=supplied.get('first_name') or parts.get('first_name')
        last=supplied.get('last_name') or parts.get('last_name')
    if not all(isinstance(n, str) and 0 < len(n.strip()) <= 80 for n in (first, last)):
        if invited:
            logger.close('partial_name')
            return recover_name(session,clock,gate,gloo,phone,body)
        if gate:
            send_intake(session, clock, gate,
                compose=lambda: compose_welcome(session, clock, gloo, phone),
                purpose="signup_reply",
                phone=phone,
                conversation=intake_context(session,phone,'name',['name']),
            )
        logger.close("name_needed")
        return "signup_name_needed"
    from app.core.confirmations import enabled
    own_inputs = [msg["body"] for msg in conversation if msg["direction"] == "in"]
    explicit_join = any(re.match(r"^\s*(?:join\b|sign me up\b|i want to volunteer\b)", text, re.I) for text in own_inputs)
    explicit_name = re.fullmatch(r"\s*(?:(?:join|my name is|i am|i'm)\s+)?" + re.escape(first.strip()) + r"\s+" + re.escape(last.strip()) + r"[.!]?\s*", body, re.I)
    if exact and prior_parts:
        # A missing-part answer may complete a previously validated name.
        key='last_name' if 'first_name' in prior_parts else 'first_name'
        prior_key='first_name' if key=='last_name' else 'last_name'
        current=last.strip() if key=='last_name' else first.strip()
        prior=first.strip() if prior_key=='first_name' else last.strip()
        explicit_name=explicit_name or (prior_parts.get(prior_key)==prior and re.fullmatch(r'\s*'+re.escape(current)+r'[.!]?\s*',body,re.I))
    # Consent is read from the actual sender text, never from Gloo's claims.
    # Names alone, a name containing Yes, quoted consent and incidental YES
    # elsewhere do not qualify. One clear name + YES reply saves a round trip.
    name_and_yes = re.fullmatch(r"\s*(?:(?:join|my name is|i am|i'm)\s+)?" +
        re.escape(first.strip()) + r"\s+" + re.escape(last.strip()) + r"\s*[,;]?\s+(?:YES|Y)[.!]?\s*", body, re.I)
    name_and_yes = name_and_yes or re.fullmatch(r"\s*(?:YES|Y)\s+" +
        re.escape(first.strip()) + r"\s+" + re.escape(last.strip()) + r"[.!]?\s*", body, re.I)
    declined = re.search(r"\b(?:don't|do not|not|never)\s+(?:sign|join|volunteer)", body, re.I)
    if exact and declined:
        logger.close('signup_declined')
        return 'signup_declined'
    if exact and (data.get('identity_reply') is not True or not explicit_name):
        logger.close('exact_identity_not_supplied')
        if invited:
            return recover_name(session,clock,gate,gloo,phone,body)
        return 'signup_identity_review'
    if enabled(session) and (declined or not (explicit_join or explicit_name or name_and_yes) or not all(re.search(r"\b" + re.escape(n.strip()) + r"\b", " ".join(own_inputs), re.I) for n in (first, last))):
        session.add(m.Escalation(category="unclear", severity="normal", summary="Signup identity needs human clarification; no roster record created.", related_ids={"phone": phone}, status="open", created_at=clock.now()))
        logger.close("human_review")
        return "signup_identity_review"
    volunteer = m.Volunteer(
        name=f"{first.strip()} {last.strip()}",
        phone=phone,
        sms_opt_in=False,
        status="inactive",
        is_coordinator=False,
        is_pastor=False,
        preferences={"signup_source": "sms", "consent_pending": True},
        created_at=clock.now(),
    )
    from app.core.confirmations import authorize_sender_fields
    authorize_sender_fields(session, volunteer, {"name", "phone", "status", "sms_opt_in", "preferences", "is_coordinator", "is_pastor"})
    session.add(volunteer)
    session.flush()
    if exact_enabled(session, phone):
        if gate and explicit_name and delivered_exact_invitation(session, clock, phone,
                reply_message_id=gate.reply_to_message_id,body=body):
            result = activate_signup(session, clock, gate, volunteer, gloo,
                consent_source='sms_name_reply_to_exact_invitation')
            logger.close('name_reply_signup_saved')
            return result
        if gate:
            # Names before an app-delivered invitation are not consent. The
            # exact invitation is the only opening copy; no YES step is added.
            send_intake(session, clock, gate,compose=lambda: compose_welcome(session,clock,gloo,phone),purpose='signup_reply',volunteer=volunteer,
                conversation=intake_context(session,phone,'name',['name']))
        logger.close('exact_invitation_needed')
        return 'signup_consent_pending'
    if name_and_yes and gate:
        result = activate_signup(session, clock, gate, volunteer, gloo, consent_source='sms_name_and_yes')
        logger.close("name_and_consent_saved")
        return result
    if gate:
        disclosed = any(msg['direction'] == 'out' and 'Message frequency varies' in msg['body']
            and 'message/data rates may apply' in msg['body'] for msg in conversation)
        consent_copy = f"Thanks, {first.strip()}! Reply YES to receive volunteer scheduling texts from Text Monkey."
        required = ["Reply YES"]
        if not disclosed:
            consent_copy += " Message frequency varies; message/data rates may apply."
            required += ["Message frequency varies", "message/data rates may apply"]
        consent_copy += " Reply STOP to stop or HELP for help."
        required += ["STOP", "HELP"]
        send_intake(session, clock, gate,
            compose=lambda: compose_signup_reply(session, clock, gloo,
                consent_copy, tuple(required), volunteer=volunteer, signup_conversation=True, require_gloo=True),
            purpose="signup_reply",
            volunteer=volunteer,
            conversation=intake_context(session,phone,'name',['name']),
        )
    logger.close("consent_pending")
    return "signup_consent_pending"


def finish_signup(session, clock, gate, volunteer, body, gloo=None):
    """Only a pending signup can consume a consent reply; no role grants."""
    if not volunteer.preferences.get("consent_pending"):
        return None
    from app.core.signup_recovery import privacy_hold
    if privacy_hold(session,volunteer.phone,volunteer) and not keyword_sensitive(body):
        return 'signup_identity_review'
    word = body.strip().upper()
    if keyword_sensitive(body):
        escalate_sensitive(session, gate, volunteer, body, clock.now())
        return "escalated_sensitive"
    if exact_enabled(session, volunteer.phone):
        match = re.fullmatch(r"\s*(?:(?:join|my name is|i am|i'm)\s+)?"+
            re.escape(volunteer.name)+r"[.!]?\s*", body, re.I)
        if match and delivered_exact_invitation(session, clock, volunteer.phone,
                reply_message_id=gate.reply_to_message_id,body=body):
            return activate_signup(session,clock,gate,volunteer,gloo,
                consent_source='sms_name_reply_to_exact_invitation')
        # Do not infer consent from a different name, roles or a model claim.
        # The removed consent/clarification texts are never sent in exact mode.
        if word in {'NO','N'}:
            from app.core.confirmations import authorize_sender_fields
            authorize_sender_fields(session,volunteer,{'preferences'})
            volunteer.preferences={**volunteer.preferences,'consent_pending':False}
            return 'signup_declined'
        if match:
            return 'signup_consent_pending'
        return recover_name(session,clock,gate,gloo,volunteer.phone,body,volunteer=volunteer)
    if word in {"YES", "Y", "START", "UNSTOP"}:
        return activate_signup(session,clock,gate,volunteer,gloo,consent_source='sms_reply')
    if word in {"NO", "N"}:
        from app.core.confirmations import authorize_sender_fields
        authorize_sender_fields(session, volunteer, {"preferences"})
        volunteer.preferences = {**volunteer.preferences, "consent_pending": False}
        return "signup_declined"
    if word == "HELP":
        send_intake(session, clock, gate,
            compose=lambda: compose_signup_reply(session, clock, gloo,
                "Text Monkey coordinates volunteer shifts by text. Reply YES to complete signup. Contact your ministry coordinator for other help.", ("Reply YES",), volunteer=volunteer, signup_conversation=True, require_gloo=True, allow_emoji=False),
            purpose="signup_reply",
            volunteer=volunteer,
            conversation=intake_context(session,volunteer.phone,'name',['name']),
        )
        return "signup_help"
    # Avoid messaging someone who stopped the signup.
    stopped = session.scalar(
        select(m.Message.id).where(
            m.Message.phone == volunteer.phone,
            m.Message.direction == "in",
            m.Message.body.in_(["STOP", "STOPALL", "UNSUBSCRIBE", "QUIT", "END"]),
        )
    )
    if not stopped:
        send_intake(session, clock, gate,
            compose=lambda: compose_signup_reply(session, clock, gloo,
                "Reply YES to receive volunteer scheduling texts and finish signing up, or STOP to stop.", ("Reply YES", "STOP"), volunteer=volunteer, signup_conversation=True, require_gloo=True, allow_emoji=False),
            purpose="signup_reply",
            volunteer=volunteer,
            conversation=intake_context(session,volunteer.phone,'name',['name']),
        )
    return "signup_consent_pending"


def activate_signup(session, clock, gate, volunteer, gloo, *, consent_source):
    """Called only after code validates the real sender's affirmative action."""
    from app.core.send_gate import has_open_sensitive_escalation
    from app.core.confirmations import authorize_sender_fields
    from app.core.policies import PolicyStore
    if has_open_sensitive_escalation(session, volunteer.id):
        return 'escalated_sensitive'
    if session.get(m.Policy,'sms_opt_out:'+volunteer.phone):
        return 'stop'
    if exact_enabled(session,volunteer.phone):
        from app.core.signup_copy import ensure_exact_role_menu
        ensure_exact_role_menu(session)
    authorize_sender_fields(session,volunteer,{'sms_opt_in','status','preferences'})
    volunteer.sms_opt_in=True
    volunteer.status='active'
    volunteer.preferences={**volunteer.preferences,'consent_pending':False,
        'consent_at':clock.now().isoformat(),'consent_source':consent_source}
    from app.core.signup_recovery import reset_attempts
    reset_attempts(session,volunteer.phone,'name')
    draft=session.get(m.Policy,'signup_identity_draft:'+volunteer.phone)
    if draft is not None:
        session.delete(draft)
    if exact_enabled(session, volunteer.phone) or PolicyStore(session).get('full_text_onboarding'):
        from app.core.onboarding import start
        start(session,clock,gate,volunteer,gloo)
        return 'onboarding_interests'
    return 'signup_complete'


def approve_signup(session, clock, approval):
    """Compatibility for signup approvals created before text-only onboarding."""
    data = approval.payload
    if session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data["phone"])):
        raise ValueError("A volunteer already uses this phone.")
    volunteer = m.Volunteer(
        name=f"{data['first_name']} {data['last_name']}",
        phone=data["phone"],
        sms_opt_in=False,
        status="inactive",
        is_coordinator=False,
        is_pastor=False,
        preferences={"signup_source": "sms", "consent_pending": True},
        created_at=clock.now(),
    )
    session.add(volunteer)
    session.flush()
    return volunteer
