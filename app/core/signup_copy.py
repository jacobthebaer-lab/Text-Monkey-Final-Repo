"""Original user-approved copy. Exact mode is explicitly scoped per demo phone."""
from sqlalchemy import or_, select
from app.db import models as m

WELCOME = "Welcome to Text Monkey 🐵 Text us your FIRST and LAST name to sign up and receive scheduling texts. Message/data rates may apply🐒"
MAC_DEMO_WELCOME = WELCOME + " Text STOP to stop."
EXACT_COPY = {
    "welcome": WELCOME,
    "consent": None,
    "interests": 'Thanks [First Name]! What would you like to help with? 1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care. Reply with names or numbers, or "Anything". Some roles need coordinator clearance.',
    "availability": 'When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say "Flexible". 🐒 You can also tell me if you would like certain roles on certain dates or times. Just text me like you\'d text a person 🐵',
    "clarification": None,
    "completion": "You're all set, Noah! We've saved your preferences. When a shift matches, we'll text you the details and ask if you can take it 🐵 Thanks for being willing to help out!",
}
LEGACY_WELCOME = (
    "Welcome to Text Monkey! To sign up and receive volunteer scheduling texts, "
    "reply with your FIRST and LAST name and YES. Message frequency varies; "
    "message/data rates may apply. STOP to stop, HELP for help."
)
WELCOME_REQUIRED = ()
LEGACY_WELCOME_REQUIRED = ("FIRST and LAST name", "YES", "Message frequency varies",
                    "message/data rates may apply", "STOP", "HELP")


def exact_enabled(session, phone):
    row = session.get(m.Policy, "signup_exact_copy:" + phone)
    return bool(row and row.value.get("value") is True)


def mac_demo_invitation_enabled(session, phone):
    """Explicit first-invitation copy for one phone's current Mac session."""
    selected = session.info.get('mac_test_session')
    row = session.get(m.Policy, 'mac_demo_invitation:' + phone)
    return bool(selected and selected.outbound_prefix.startswith('MAC') and row and
                row.value.get('value') is True and row.value.get('session_id') == selected.id)


def exact_message(field, first_name=None):
    text = EXACT_COPY[field]
    if text is None:
        return None
    if first_name is not None:
        text = text.replace('[First Name]', first_name)
        if field == 'completion':
            text = text.replace('Noah', first_name, 1)
    return text


def delivered_exact_invitation(session, clock, phone, *, reply_message_id=None, body=None):
    from datetime import timedelta
    from app.core.conversation import scope
    selected = session.info.get('mac_test_session')
    if selected and selected.outbound_prefix.startswith("GV"):
        from app.integrations.google_voice_demo import RECIPIENT_KEY, demo_invitation_proof
        if session.get(m.Policy, RECIPIENT_KEY + phone):
            return bool(demo_invitation_proof(session, clock, phone, reply_message_id=reply_message_id, body=body))
    if selected is not None and not selected.active(clock.now()):
        return False
    bodies = [WELCOME]
    if mac_demo_invitation_enabled(session, phone):
        bodies.append(MAC_DEMO_WELCOME)
    invitation = session.scalar(scope(select(m.Message), selected).where(
        m.Message.phone == phone, m.Message.direction == 'out', m.Message.body.in_(bodies),
        m.Message.purpose == 'signup_reply', m.Message.status.in_(('sent', 'submitted')),
        m.Message.created_at >= clock.now()-timedelta(hours=24),
        m.Message.created_at <= clock.now()).order_by(m.Message.id.desc()).limit(1))
    if invitation is None or reply_message_id is None:
        return False
    reply = session.scalar(scope(select(m.Message), selected).where(
        m.Message.id == reply_message_id, m.Message.phone == phone,
        m.Message.direction == 'in', m.Message.status == 'received',
        m.Message.body == body, m.Message.id > invitation.id,
        m.Message.created_at >= invitation.created_at,
        m.Message.created_at <= clock.now()))
    if reply is None:
        return False
    stopped = session.scalar(select(m.Message.id).where(
        m.Message.phone == phone, m.Message.direction == 'in',
        m.Message.id > invitation.id, m.Message.id <= reply.id,
        m.Message.body.in_(('STOP','STOPALL','UNSUBSCRIBE','END','QUIT'))).limit(1))
    return stopped is None


def recorded_legacy_reply(session, clock, phone, *, reply_message_id, body, volunteer_id):
    """Bind legacy consent to the current app-recorded sender, never parser claims."""
    from datetime import timedelta
    from app.core.conversation import scope
    selected = session.info.get('mac_test_session')
    if selected is not None and not selected.active(clock.now()):
        return None
    reply = session.scalar(scope(select(m.Message), selected).where(
        m.Message.id == reply_message_id, m.Message.phone == phone,
        m.Message.volunteer_id == volunteer_id, m.Message.direction == 'in',
        m.Message.status == 'received', m.Message.body == body,
        m.Message.kind == ('mac_test_in' if selected else 'inbound'),
        m.Message.created_at >= clock.now()-timedelta(minutes=10),
        m.Message.created_at <= clock.now())) if reply_message_id is not None else None
    if selected and reply and reply.purpose != 'test:'+selected.id:
        return None
    return reply


def legacy_consent_proof(session, clock, phone, *, reply_message_id, body, volunteer_id, name, combined=False):
    """A delivered legacy disclosure must precede this actual affirmative reply.

    Retain the original name+YES invitation and the application's personalized
    YES followup. A shorter followup retains its earlier full disclosure as
    the consent source. Queues and unrelated signup prompts cannot grant consent.
    """
    import re
    from datetime import timedelta
    from app.core.conversation import scope
    from app.core.consent_controls import control_action
    from app.core.signup_responder import _without_monkey_emoji
    reply = recorded_legacy_reply(session, clock, phone, reply_message_id=reply_message_id,
                                  body=body, volunteer_id=volunteer_id)
    if reply is None:
        return None
    if combined:
        affirmative = re.fullmatch(r"\s*(?:(?:join|my name is|i am|i'm)\s+)?" + re.escape(name) +
            r"\s*[,;]?\s+(?:YES|Y)[.!]?\s*", body, re.I) or re.fullmatch(
            r"\s*(?:YES|Y)\s+" + re.escape(name) + r"[.!]?\s*", body, re.I)
    else:
        affirmative = body.strip().upper() in {'YES', 'Y'}
    if not affirmative:
        return None
    candidates = session.scalars(scope(select(m.Message), session.info.get('mac_test_session')).where(
        m.Message.phone == phone, m.Message.direction == 'out', m.Message.purpose == 'signup_reply',
        or_(m.Message.volunteer_id.is_(None), m.Message.volunteer_id == volunteer_id),
        m.Message.kind.in_(('ai', 'template')),
        m.Message.status.in_(('sent', 'submitted')), m.Message.id < reply.id,
        m.Message.created_at >= reply.created_at-timedelta(hours=24),
        m.Message.created_at <= reply.created_at).order_by(m.Message.id)).all()
    # These are the two code-owned legacy disclosure forms, without decoration.
    parts = name.split()
    prompts = {f"Thanks, {' '.join(parts[:i])}! Reply YES to receive volunteer scheduling texts from Text Monkey."
               for i in range(1, len(parts))}
    commands = ' Reply STOP to stop or HELP for help.'
    disclosure = ' Message frequency varies; message/data rates may apply.'
    full = {LEGACY_WELCOME, *(prompt+disclosure+commands for prompt in prompts)}
    for invitation in candidates:
        text = ' '.join(_without_monkey_emoji(invitation.body).split())
        if text not in full:
            continue
        # A STOP anywhere after disclosure revokes that invitation's authority.
        inputs = session.scalars(select(m.Message).where(m.Message.phone == phone,
            m.Message.direction == 'in', m.Message.id > invitation.id, m.Message.id <= reply.id,
            m.Message.created_at <= reply.created_at))
        if any(control_action(message.body) == 'stop' for message in inputs):
            continue
        proof = {'disclosure_message_id': invitation.id, 'reply_message_id': reply.id,
                 'consent_at': reply.created_at.isoformat(),
                 'session_id': getattr(session.info.get('mac_test_session'), 'id', None)}
        return proof
    return None


def compose_welcome(session, clock, gloo, phone):
    from app.core.signup_responder import compose_signup_reply
    exact = exact_enabled(session, phone)
    welcome = MAC_DEMO_WELCOME if exact and mac_demo_invitation_enabled(session, phone) else WELCOME
    return compose_signup_reply(session, clock, gloo,
        welcome if exact else LEGACY_WELCOME, WELCOME_REQUIRED if exact else LEGACY_WELCOME_REQUIRED,
        phone=phone, signup_conversation=True, require_gloo=True, exact_copy=exact)


def ensure_exact_role_menu(session):
    """Match the user's numbered demo choices; never grant qualifications."""
    choices = [(1,'Greeter','Welcome',[],'auto'), (2,'Usher','Welcome',[],'auto'),
               (3,'Production','Production',['sound_training'],'auto'),
               (4,'Coffee','Hospitality',[],'auto'),
               (5,'Child Care','Kids',['background_check','child_safety_training'],'needs_approval')]
    existing = {role.id: role for role in session.scalars(select(m.Role).where(m.Role.id.in_([c[0] for c in choices])))}
    for identifier, name, ministry, required, fill_policy in choices:
        role = existing.get(identifier)
        if role is not None and role.name.casefold() != name.casefold():
            raise ValueError('Exact signup menu conflicts with an existing role ID; coordinator mapping required.')
    for identifier, name, ministry, required, fill_policy in choices:
        role = existing.get(identifier)
        if role is None:
            session.add(m.Role(id=identifier,name=name,ministry=ministry,
                required_qualifications=required,criticality='critical' if name=='Child Care' else 'standard',fill_policy=fill_policy))
        elif identifier == 3:
            # The reserved Production choice keeps its saved training requirement,
            # including when the matching role predates this exact signup menu.
            missing = [qualification for qualification in required
                       if qualification not in role.required_qualifications]
            if missing:
                role.required_qualifications = [*role.required_qualifications, *missing]
    session.flush()
