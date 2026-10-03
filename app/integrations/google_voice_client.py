"""Narrow, private connector client. Error text never includes credentials."""

from urllib.parse import urlsplit
import hashlib
import secrets

import httpx


class ConnectorUnavailable(Exception):
    pass


class GoogleVoiceConnector:
    def __init__(self, settings):
        url = settings.google_voice_connector_url.rstrip("/")
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in {"", "/"} or len(settings.google_voice_connector_token) < 32):
            raise ValueError("Configure a private Google Voice connector URL and credential")
        # HTTP is restricted to loopback or the private Compose service name.
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "google-voice"}:
            raise ValueError("Remote Google Voice connector URLs require HTTPS")
        self.url = url
        self.token = settings.google_voice_connector_token

    def _request(self, method, path, **kwargs):
        try:
            with httpx.Client(timeout=httpx.Timeout(45, connect=5), follow_redirects=False) as client:
                response = client.request(method, self.url + path,
                    headers={"Authorization": "Bearer " + self.token}, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("Invalid response")
            return value
        except (httpx.HTTPError, ValueError, TypeError):
            raise ConnectorUnavailable("Google Voice connector is unavailable") from None

    def health(self):
        return self._request("GET", "/health")

    def import_session(self, cookies):
        return self._request("POST", "/session", json={"cookies": cookies})

    def send(self, *, idempotency_key, to, body, not_after):
        result = self._request("POST", "/send", json={
            "idempotency_key": idempotency_key, "to": to, "body": body, "not_after": not_after})
        if result.get("status") not in {"submitted", "uncertain", "rejected"}:
            raise ConnectorUnavailable("Google Voice submission outcome is uncertain")
        return result["status"]

    def inbound(self, cursor):
        return self._request("GET", "/inbound", params={"cursor": cursor})


def connector_for(state):
    # Injection keeps synthetic tests completely offline.
    return getattr(state, "google_voice_connector", None) or GoogleVoiceConnector(state.settings)


def verified_health(health, settings):
    # The private connector verifies both identities against the same deployment
    # configuration and returns only masked identifiers.
    expected = hashlib.sha256((settings.google_voice_expected_email.lower() + "\n" +
                              settings.google_voice_expected_number).encode()).hexdigest()
    return bool(settings.google_voice_expected_email and settings.google_voice_expected_number and
                isinstance(health, dict) and health.get("ready") is True and
                health.get("identity_verified") is True and
                health.get("expected_identity_match") is True and
                isinstance(health.get("identity_fingerprint"), str) and
                secrets.compare_digest(health["identity_fingerprint"], expected))
