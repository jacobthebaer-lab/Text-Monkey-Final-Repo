"""Narrow, private connector client. Error text never includes credentials."""

from urllib.parse import urlsplit
import hashlib
import secrets

from app.integrations.google_voice_policy import POLICY_HOLD_MESSAGE, google_voice_demo_allowed
import httpx
import json


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
        self.settings = settings
        self.url = url
        self.token = settings.google_voice_connector_token

    def _request(self, method, path, **kwargs):
        if not google_voice_demo_allowed(self.settings):
            raise ConnectorUnavailable(POLICY_HOLD_MESSAGE)
        if (method, path) not in {("GET", "/health"), ("GET", "/inbound"),
                ("POST", "/session"), ("POST", "/demo/verify-profile"), ("POST", "/demo/recipients"), ("POST", "/demo/intake"), ("POST", "/demo/reconcile"), ("POST", "/demo/signup-input"), ("POST", "/prepare"), ("POST", "/send")}:
            raise ConnectorUnavailable("Unsupported demo step")
        try:
            # No redirects, proxy inheritance or HTTP retries. Never echo provider errors.
            with httpx.Client(timeout=httpx.Timeout(90, connect=5), trust_env=False,
                              follow_redirects=False) as client:
                with client.stream(method, self.url + path,
                        headers={"Authorization": "Bearer " + self.token}, **kwargs) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(chunk_size=65536):
                        size += len(chunk)
                        if size > 512 * 1024:
                            raise ValueError("Oversized connector response")
                        chunks.append(chunk)
                    result = json.loads(b"".join(chunks))
                if not isinstance(result, dict):
                    raise ValueError("Invalid connector response")
                return result
        except (httpx.HTTPError, ValueError):
            raise ConnectorUnavailable("Private demo connector unavailable; do not retry an uncertain submission") from None

    def register_recipient(self, registration):
        return self._request("POST", "/demo/recipients", json=registration)

    def reconcile_submission(self, proof_request):
        return self._request("POST", "/demo/reconcile", json=proof_request)

    def intake(self, phone=None, *, phones=None):
        return self._request("POST", "/demo/intake", json={"phones": phones} if phones is not None else {"phone": phone} if phone else {})

    def health(self):
        return self._request("GET", "/health")

    def import_session(self, cookies):
        return self._request("POST", "/session", json={"cookies": cookies})

    def verify_profile(self):
        return self._request("POST", "/demo/verify-profile", json={})

    def prepare(self, *, idempotency_key, to, body, not_after):
        result = self._request("POST", "/prepare", json={
            "idempotency_key": idempotency_key, "to": to, "body": body, "not_after": not_after})
        if result.get("status") not in {"prepared", "submitted", "uncertain", "rejected"}:
            raise ConnectorUnavailable("Google Voice preparation outcome is uncertain")
        return result["status"]

    def send(self, *, idempotency_key, to, body, not_after):
        result = self._request("POST", "/send", json={
            "idempotency_key": idempotency_key, "to": to, "body": body, "not_after": not_after})
        if result.get("status") not in {"submitted", "uncertain", "rejected"}:
            raise ConnectorUnavailable("Google Voice submission outcome is uncertain")
        return result["status"]

    def inbound(self, cursor):
        return self._request("GET", "/inbound", params={"cursor": cursor})

    def stored_signup_input(self, *, id, phone, session_id):
        return self._request("POST", "/demo/signup-input", json={"id": id, "phone": phone, "session_id": session_id})


def connector_for(state):
    # Injection keeps synthetic tests completely offline.
    return getattr(state, "google_voice_connector", None) or GoogleVoiceConnector(state.settings)


def verified_identity(health, settings, provider=None):
    # The private connector verifies both identities against the same deployment
    # configuration and returns only masked identifiers.
    expected = hashlib.sha256((settings.google_voice_expected_email.lower() + "\n" +
                              settings.google_voice_expected_number).encode()).hexdigest()
    if settings.google_voice_demo_mode:
        from app.integrations.google_voice_demo import scope_fingerprint
        if provider is None:
            return False
        if (not isinstance(health, dict) or health.get("demo_mode") is not True or
                health.get("scope_fingerprint") != scope_fingerprint(provider.test_sessions)):
            return False
    return bool(settings.google_voice_expected_email and settings.google_voice_expected_number and
                isinstance(health, dict) and
                health.get("identity_verified") is True and
                health.get("expected_identity_match") is True and
                isinstance(health.get("identity_fingerprint"), str) and
                secrets.compare_digest(health["identity_fingerprint"], expected))


def verified_health(health, settings, provider=None):
    return bool(isinstance(health, dict) and health.get("ready") is True and verified_identity(health, settings, provider))
