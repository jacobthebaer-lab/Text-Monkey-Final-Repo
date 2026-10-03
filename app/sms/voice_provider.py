"""Google Voice queue adapter using the reviewed bridge protocol.

MAC IDs and /mac endpoints are compatibility wire names, not Apple delivery.
Use fresh session IDs when changing transport; never reuse a Mac test session.
Only SendGate reserves IDs; the separate browser worker performs delivery.
"""
from dataclasses import replace

from app.sms.mac_provider import MacMessagesProvider


class GoogleVoiceProvider(MacMessagesProvider):
    requires_policy_preflight = True

    def __init__(self, settings):
        if settings.sms_provider != "google_voice" or not settings.mac_bridge_enabled:
            raise ValueError("Google Voice requires explicit provider and bridge activation")
        if settings.mac_message_services != "SMS":
            raise ValueError("Google Voice supports the SMS bridge service only")
        self.exact_review = settings.competition_confirmation_required
        self.automation_enabled = settings.automation_enabled and not settings.demo_mode
        super().__init__(replace(settings, sms_provider="mac_messages"))
