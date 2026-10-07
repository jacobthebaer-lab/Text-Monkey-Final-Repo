"""Account-scoped copy drafts. Suggested wording never owns application facts."""
import json
import re
from pathlib import Path
from uuid import UUID

from app.db import models as m

DEFAULTS = json.loads((Path(__file__).resolve().parents[2] / "web/texty/public/onboarding-copy-defaults.json").read_text())
FIELDS = tuple(DEFAULTS)
MAX_LENGTH = 600
PREFIX = "onboarding_copy:"
INTRO_ROLES = '1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care'
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
