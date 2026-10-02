"""Twilio SMS provider. Only the send gate ever holds this object, and
get_provider() only builds it when SMS_PROVIDER=twilio AND LIVE_SMS=true."""

from twilio.rest import Client

from app.config import Settings


class TwilioSMSProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.sms_is_live:
            raise RuntimeError("TwilioSMSProvider requires SMS_PROVIDER=twilio and LIVE_SMS=true")
        if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
            raise RuntimeError("Twilio credentials are incomplete; check .env")
        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._from = settings.twilio_from_number

    def send(self, to: str, body: str) -> str:
        message = self._client.messages.create(to=to, from_=self._from, body=body)
        return message.sid
