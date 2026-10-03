"""Account-scoped copy drafts. Suggested wording never owns application facts."""
import json
import re
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from app.db import models as m

DEFAULTS = json.loads((Path(__file__).resolve().parents[2] / "web/texty/public/onboarding-copy-defaults.json").read_text())
FIELDS = tuple(DEFAULTS)
MAX_LENGTH = 600
PREFIX = "onboarding_copy:"


def copy_key(owner_id):
    return PREFIX + str(UUID(owner_id))


def validate_messages(messages):
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
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    return ", ".join(f"{role.id}: {role.name}" for role in roles)[:360]


def render_copy(text, *, first_name="Alex", roles="1: Greeter, 2: Usher, 3: Production"):
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
        messages = validate_messages(row.value.get("messages"))
    except (ValueError, AttributeError):
        return None
    return render_copy(messages[field], first_name=volunteer.name.split()[0], roles=role_options(session)) or None
