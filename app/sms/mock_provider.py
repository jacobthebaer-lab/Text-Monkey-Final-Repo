"""In-memory SMS provider for tests, evals, and the phone simulator."""

from dataclasses import dataclass


@dataclass
class SentSMS:
    to: str
    body: str
    sid: str


class MockSMSProvider:
    def __init__(self) -> None:
        self.sent: list[SentSMS] = []

    def send(self, to: str, body: str) -> str:
        sid = f"MOCK{len(self.sent) + 1:06d}"
        self.sent.append(SentSMS(to=to, body=body, sid=sid))
        return sid

    def sent_to(self, phone: str) -> list[SentSMS]:
        return [s for s in self.sent if s.to == phone]
