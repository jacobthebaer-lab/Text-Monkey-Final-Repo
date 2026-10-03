"""Reserve durable outbound IDs; only the cloud worker contacts Google Voice."""

from dataclasses import dataclass
import re
from uuid import uuid4

from app.core.message_style import validate_outbound_style
from app.integrations.test_sessions import TestSession, parse_sessions
from app.sms.mac_provider import demo_phones

VOICE_PHONE = re.compile(r"\+1[2-9][0-9]{9}\Z")


@dataclass(frozen=True)
class GoogleVoiceTestSession(TestSession):
    @property
    def outbound_prefix(self):
        return f"GV{self.id}:"


class GoogleVoiceProvider:
    transport_name = "google_voice"
    queues_delivery = True

    def __init__(self, settings):
        if settings.sms_provider != self.transport_name:
            raise ValueError("Google Voice requires explicit transport selection")
        self.enabled = settings.google_voice_enabled
        if self.enabled and not settings.competition_confirmation_required:
            raise ValueError("Google Voice testing requires exact human confirmation")
        if self.enabled and len(settings.google_voice_connector_token) < 32:
            raise ValueError("Google Voice connector token must contain at least 32 characters")
        if settings.live_sms and (not settings.google_voice_expected_email or
                not VOICE_PHONE.fullmatch(settings.google_voice_expected_number)):
            raise ValueError("Configure the expected Google account and Voice number")
        if not 1 <= settings.google_voice_max_queue_age_seconds <= 3600:
            raise ValueError("Google Voice queue age must be between 1 and 3600 seconds")
        self.phones = demo_phones(settings.google_voice_demo_phones) if settings.google_voice_demo_phones else frozenset()
        if len(self.phones) > 20 or any(not VOICE_PHONE.fullmatch(phone) for phone in self.phones):
            raise ValueError("Google Voice testing supports at most 20 exact US/Canada +1 numbers")
        sessions = parse_sessions(settings.google_voice_test_sessions, self.phones)
        self.test_sessions = {phone: GoogleVoiceTestSession(s.id, s.starts_at, s.expires_at)
                              for phone, s in sessions.items()}

    def allows(self, phone):
        return phone in self.phones

    def allows_test_signup_reply(self, phone, purpose, now):
        return False

    def send(self, to, body):
        validate_outbound_style(body)
        if not self.enabled:
            raise ValueError("Google Voice transport is disabled")
        if to not in self.phones or to not in self.test_sessions:
            raise ValueError("Recipient needs an explicitly configured Google Voice test session")
        if not isinstance(body, str) or not 0 < len(body.strip()) <= 1600 or "\0" in body:
            raise ValueError("Google Voice requires a nonempty text under 1600 characters")
        return self.test_sessions[to].outbound_prefix + uuid4().hex
