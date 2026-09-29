"""SMS provider interface and selection.

Only the send gate may hold a provider. Real SMS requires SMS_PROVIDER=twilio
AND LIVE_SMS=true (both set by the human); anything else gets the mock.
"""

from typing import Protocol

from app.config import Settings


class SMSProvider(Protocol):
    def send(self, to: str, body: str) -> str:
        """Send one SMS; returns a provider message id."""
        ...


def get_provider(settings: Settings) -> SMSProvider:
    from app.sms.mock_provider import MockSMSProvider

    if settings.sms_is_live:
        # Phase 6 adds the Twilio provider; failing loudly beats a silent mock
        # when the human believes real texts are going out.
        raise NotImplementedError("Twilio provider lands in Phase 6")
    return MockSMSProvider()
