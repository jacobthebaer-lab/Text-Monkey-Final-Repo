"""Cloud SMS reservations and Twilio submission, enabled by two explicit flags."""

from uuid import uuid4
from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient

from app.config import Settings


class TwilioSMSProvider:
    transport = "twilio"
    queued_transport = True
    requires_gloo = True

    def __init__(self, settings: Settings) -> None:
        if not settings.sms_is_live:
            raise RuntimeError("TwilioSMSProvider requires SMS_PROVIDER=twilio and LIVE_SMS=true")
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
            raise RuntimeError("Twilio credentials are incomplete; check .env")
        # A timeout is ambiguous; hold for review instead of retrying the POST.
        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token,
                              http_client=TwilioHttpClient(timeout=15, max_retries=0))
        self._from = settings.twilio_from_number
        self.settings = settings

    def send(self, to: str, body: str) -> str:
        # SendGate records this reservation in its transaction. The cloud
        # dispatcher submits only committed messages, with fresh policy checks.
        return "CLOUD" + uuid4().hex

    def submit(self, message_id: int, to: str, body: str):
        return self._client.messages.create(
            to=to, from_=self._from, body=body,
            status_callback=self.settings.public_base_url.rstrip("/")
            + f"/sms/status?message_id={message_id}",
        )
