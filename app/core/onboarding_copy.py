"""Account-scoped copy drafts. Suggested wording never owns application facts."""
import json
import hashlib
import re
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from app.db import models as m
from app.core.church_labels import church_label

DEFAULTS = json.loads((Path(__file__).resolve().parents[2] / "web/texty/public/onboarding-copy-defaults.json").read_text())
FIELDS = tuple(DEFAULTS)
MAX_LENGTH = 600
PREFIX = "onboarding_copy:"
INTRO_ROLE_NAMES = ('Greeter', 'Usher', 'Production', 'Coffee', 'Child Care')
INTRO_ROLES = ', '.join(f'{number}: {name}' for number, name in enumerate(INTRO_ROLE_NAMES, 1))
LAST_INTERESTS_DEFAULT = 'Thanks {first_name}! What would you like to help with? {roles}. Reply with names or numbers, or "Anything". Some roles need coordinator clearance.'
PREVIOUS_DEFAULTS = {
    'interests': 'Thanks, {first_name}! What would you like to help with? {roles}. Reply with names or numbers, or Anything. Some roles need coordinator clearance.',
    'availability': "When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. Or say Flexible. Tell me any role, date or time preferences, too—just text me like you'd text a person.",
    'completion': "You're all set, {first_name}! We've saved your preferences. When a shift matches, we'll text you the details and ask if you can take it. Thanks for being willing to help out!",
}
INITIAL_DEFAULTS = {
    'interests': 'What would you like to help with? {roles}. Reply with names or numbers, or ANY. Some roles need coordinator clearance.',
    'availability': 'When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say FLEXIBLE.',
    'clarification': 'When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say FLEXIBLE.',
    'completion': 'You’re all set, {first_name}! We’ve saved your preferences. When a shift matches, we’ll text you the details and ask if you can take it.',
}


def upgrade_saved_defaults(messages):
    """Upgrade only the known old canonical strings; preserve custom edits."""
    merged = {**DEFAULTS, **messages}
    return {key: DEFAULTS[key] if text in (PREVIOUS_DEFAULTS.get(key), INITIAL_DEFAULTS.get(key),
                                            LAST_INTERESTS_DEFAULT if key == "interests" else None) else text
            for key,text in merged.items()}


def copy_key(owner_id):
    return PREFIX + str(UUID(owner_id))


def validate_messages(messages):
    if isinstance(messages,dict) and set(messages)==set(FIELDS)-{'welcome'}:
        # Preserve saved account drafts from the earlier editor schema.
        messages={'welcome':DEFAULTS['welcome'],**messages}
    if not isinstance(messages, dict) or set(messages) != set(FIELDS):
        raise ValueError("Provide all four onboarding messages, using supported fields only.")
    clean = {}
    for field, text in messages.items():
        if (not isinstance(text, str) or (field != "clarification" and not text.strip()) or len(text) > MAX_LENGTH
                or any(ord(c) < 32 and c not in "\n\t" for c in text)):
            raise ValueError(f"Use 1–{MAX_LENGTH} characters for {field}.")
        if re.search(r"https?://|www\.", text, re.I):
            raise ValueError("Onboarding messages cannot include links.")
        tokens = re.findall(r"\{[^{}]*\}", text)
        if any(token not in {"{roles}", "{first_name}"} for token in tokens):
            raise ValueError("Use only {roles} and {first_name} as placeholders.")
        if "{" in re.sub(r"\{(?:roles|first_name)\}", "", text) or "}" in re.sub(r"\{(?:roles|first_name)\}", "", text):
            raise ValueError("Check placeholder braces.")
        clean[field] = text.strip()
    return clean


def role_options(session):
    """Keep the intro to five standard choices, independent of imported roles.

    This changes presentation only. Saved role IDs and qualification rules
    remain owned by the full role catalog.
    """
    return INTRO_ROLES


def intro_choices(session):
    """Map the five visible menu numbers to saved roles, preferring canonical names."""
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    choices = []
    for number, name in enumerate(INTRO_ROLE_NAMES, 1):
        matching = [r for r in roles if church_label(r.name).casefold() == name.casefold()]
        matching.sort(key=lambda r: (r.name.strip().casefold() != name.casefold(), r.id))
        choices.append({'number': number, 'name': name, 'role_id': matching[0].id if matching else None})
    return choices


def record_intro_menu(session, clock, volunteer, outcome, body):
    if not (outcome.sent or outcome.approval_id):
        return
    selected = session.info.get('mac_test_session')
    session.add(m.Notification(key='onboarding-menu:' + str(uuid4()),
        purpose='onboarding_role_menu', state='recorded', volunteer_id=volunteer.id,
        created_at=clock.now(), due_at=clock.now(), message_id=outcome.message_id,
        detail={'phone':volunteer.phone, 'generation':volunteer.preferences.get('signup_generation'),
                'session_id':selected.id if selected else None,
                'body_hash':hashlib.sha256(body.encode()).hexdigest(), 'choices':intro_choices(session)}))
    session.flush()


def delivered_intro_choices(session, clock, volunteer, incoming_id):
    """Use a new menu only after its matching outbound receipt precedes the reply."""
    from app.core.conversation import scope
    selected = session.info.get('mac_test_session')
    rows = session.scalars(select(m.Notification).where(m.Notification.volunteer_id == volunteer.id,
        m.Notification.purpose == 'onboarding_role_menu').order_by(m.Notification.created_at.desc())).all()
    for row in rows:
        detail = row.detail
        if (detail.get('phone') != volunteer.phone or detail.get('generation') != volunteer.preferences.get('signup_generation')
                or detail.get('session_id') != (selected.id if selected else None)):
            continue
        query = scope(select(m.Message), selected).where(m.Message.volunteer_id == volunteer.id,
            m.Message.phone == volunteer.phone, m.Message.direction == 'out',
            m.Message.status.in_(['sent', 'submitted']), m.Message.created_at >= row.created_at,
            m.Message.created_at <= clock.now())
        if incoming_id is not None:
            query = query.where(m.Message.id < incoming_id)
        if row.message_id is not None:
            query = query.where(m.Message.id == row.message_id)
        messages = session.scalars(query.order_by(m.Message.id.desc()).limit(20)).all()
        if not any(hashlib.sha256(message.body.encode()).hexdigest() == detail['body_hash'] for message in messages):
            continue
        choices = []
        for choice in detail['choices']:
            role = session.get(m.Role, choice['role_id']) if choice['role_id'] is not None else None
            choices.append({**choice, 'role_id': role.id if role and church_label(role.name).casefold() == choice['name'].casefold() else None})
        return choices
    # Pre-upgrade conversations retain their advertised database-ID mapping.
    return None


def render_copy(text, *, first_name="Alex", roles=INTRO_ROLES):
    # Replace only known placeholders. No evaluation or arbitrary format access.
    return text.replace("{first_name}", first_name).replace("{roles}", roles)


def preferred_wording(session, field, volunteer):
    """Use drafts only for recipients explicitly bound by a trusted admin start.

    Never choose a first/most-recent owner from the shared single-church store.
    New inbound signups have no owner binding and retain canonical app facts.
    """
    owner_id = (volunteer.preferences or {}).get("onboarding_copy_owner") if volunteer else None
    if not owner_id:
        return None
    try:
        row = session.get(m.Policy, copy_key(owner_id))
    except (ValueError, TypeError, AttributeError):
        return None
    if row is None:
        return None
    try:
        messages = upgrade_saved_defaults(validate_messages(row.value.get("messages")))
    except (ValueError, AttributeError):
        return None
    return render_copy(messages[field], first_name=volunteer.name.split()[0], roles=role_options(session)) or None
