"""First-party Mac transport. Only SendGate creates the durable message row.

send() reserves an ID, never contacts Messages itself. Delivery happens after
the application's transaction commits, through the authenticated Mac worker.
"""

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")


def demo_phones(raw: str) -> frozenset[str]:
    phones = frozenset(p.strip() for p in raw.split(",") if p.strip())
    if not phones or not all(PHONE.fullmatch(p) for p in phones):
        raise ValueError("Configure MAC_DEMO_PHONES with exact international demo numbers")
    return phones


class MacMessagesProvider:
    def __init__(self, settings):
        if not settings.mac_bridge_enabled or settings.sms_provider != "mac_messages":
            raise ValueError("Mac transport requires explicit MAC_BRIDGE_ENABLED and SMS_PROVIDER")
        if len(settings.mac_bridge_token) < 32:
            raise ValueError("MAC_BRIDGE_TOKEN must contain at least 32 characters")
        if len(settings.admin_password) < 16:
            raise ValueError("Set a strong ADMIN_PASSWORD before exposing the Mac backend")
        self.phones = demo_phones(settings.mac_demo_phones)
        self.test_signup_until = None
        if settings.mac_test_signup_reply_until:
            until = datetime.fromisoformat(settings.mac_test_signup_reply_until)
            if until.tzinfo is None or until > datetime.now(timezone.utc) + timedelta(hours=2):
                raise ValueError("Test signup reply window must be timezone-aware and at most two hours")
            self.test_signup_until = until

    def send(self, to: str, body: str) -> str:
        if to not in self.phones:
            raise ValueError("Recipient is outside the configured Mac demo numbers")
        if not isinstance(body, str) or not 0 < len(body.strip()) <= 1600:
            raise ValueError("Mac transport requires a nonempty text under 1,600 characters")
        return "MAC" + uuid4().hex

    def allows(self, phone: str) -> bool:
        return phone in self.phones

    def allows_test_signup_reply(self, phone, purpose, now):
        return (phone in self.phones and purpose == "signup_reply"
                and self.test_signup_until is not None and now < self.test_signup_until)
