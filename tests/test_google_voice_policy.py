"""Production policy hold, tested without replacing its permanent decision."""
from datetime import timedelta
from dataclasses import replace
from itertools import product
from types import SimpleNamespace
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.integrations import google_voice_policy
from app.integrations.google_voice_client import ConnectorUnavailable, GoogleVoiceConnector
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim, GoogleVoiceInboundReceipt
from app.integrations.google_voice_runtime import dispatch_outbound, get_cloud_status, set_paused, tick_google_voice
from app.main import create_app
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.web.texty import admin
from tests.session_fixtures import session_json

PHONE = "+15555550101"
EMAIL = "owner@example.test"
NUMBER = "+15555550202"


class ForbiddenService:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError("The production provider hold must precede Gloo or connector access")
        return forbidden


@pytest.fixture
def policy_app(tmp_path, clock, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/policy.db",
        sms_provider="google_voice", google_voice_enabled=True, live_sms=True,
        automation_enabled=True, allow_text_signup=True, gloo_signup_replies=True,
        competition_confirmation_required=True,
        google_voice_connector_token="synthetic-connector-" + "x" * 40,
        google_voice_expected_email=EMAIL, google_voice_expected_number=NUMBER,
        google_voice_demo_phones=PHONE, google_voice_test_sessions=session_json([PHONE], clock.now()),
        gloo_api_key="synthetic-gloo-key", admin_email_allowlist=EMAIL, superadmin_email_allowlist=EMAIL,
        demo_mode=True,
    )
    gloo = ForbiddenService()
    monkeypatch.setattr("app.main.build_gloo", lambda settings: gloo)
    app = create_app(settings)
    app.state.clock = app.state.mac_delivery_clock = app.state.google_voice_clock = clock
    app.state.google_voice_connector = ForbiddenService()
    app.state.google_voice_status = {"connected": True, "checked_monotonic": time.monotonic(),
                                    "last_checked_at": clock.now().isoformat()}
    app.dependency_overrides[admin] = lambda: {"email": EMAIL, "email_confirmed_at": "synthetic"}
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        session.add(m.Volunteer(name="Synthetic Policy Tester", phone=PHONE, sms_opt_in=True,
                                status="active", preferences={}, created_at=clock.now()))
        set_paused(session, False)
        session.commit()
    return app


def assert_no_activity(app):
    assert app.state.gloo.calls == []
    assert app.state.google_voice_connector.calls == []
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(GoogleVoiceDeliveryClaim)) is None
        assert session.scalar(select(GoogleVoiceInboundReceipt)) is None


def test_environment_flags_cannot_override_permanent_provider_decision(monkeypatch):
    for key in ("GOOGLE_VOICE_ENABLED", "LIVE_SMS", "VOICE_ENABLED", "AUTOMATION_ENABLED",
                "ALLOW_TEXT_SIGNUP", "GOOGLE_VOICE_AUTOMATION_ALLOWED",
                "GOOGLE_VOICE_POLICY_OVERRIDE", "GOOGLE_VOICE_ID_VERIFIED"):
        monkeypatch.setenv(key, "true")
    assert google_voice_policy.google_voice_automation_allowed() is False


def test_every_historical_demo_flag_combination_remains_held():
    fields = ('google_voice_demo_mode', 'google_voice_enabled', 'demo_mode',
              'automation_enabled', 'mac_bridge_enabled', 'profile_sync_enabled',
              'pco_staffing_poll_enabled', 'pco_staffing_write_enabled',
              'competition_confirmation_required', 'google_voice_signup_enabled')
    for flags in product((False, True), repeat=len(fields)):
        settings = SimpleNamespace(sms_provider='google_voice', **dict(zip(fields, flags)))
        assert google_voice_policy.google_voice_demo_allowed(settings) is False
        assert google_voice_policy.google_voice_steps_allowed(settings) is False


def historical_settings(settings):
    return replace(settings, google_voice_demo_mode=True, demo_mode=False,
                   automation_enabled=False, mac_bridge_enabled=False,
                   profile_sync_enabled=False, pco_staffing_poll_enabled=False,
                   pco_staffing_write_enabled=False, google_voice_signup_enabled=True)


def test_historical_demo_cannot_construct_an_active_provider(policy_app):
    with pytest.raises(ValueError, match='prohibits automated texting'):
        GoogleVoiceProvider(historical_settings(policy_app.state.settings))
    assert_no_activity(policy_app)


def test_preexisting_provider_cannot_release_hold_after_legacy_flag_change(policy_app):
    state = policy_app.state
    state.settings = state.provider.settings = historical_settings(state.settings)
    with pytest.raises(ValueError, match='prohibits automated texting'):
        state.provider.send(PHONE, 'Synthetic exact text.')
    with state.session_factory() as session:
        gate = SendGate(session, state.clock, state.provider)
        gate.gloo = state.gloo
        result = gate.send(body='Synthetic exact text.', purpose='manual',
                           volunteer=session.scalar(select(m.Volunteer)), kind='ai')
        # Real-auth mode may reject missing sender evidence before transport.
        # Both boundaries must prevent composition, queueing and service access.
        assert result.status in {SendStatus.BLOCKED_POLICY, SendStatus.BLOCKED_TRANSPORT}
        if result.status == SendStatus.BLOCKED_TRANSPORT:
            assert result.reason == google_voice_policy.POLICY_HOLD_MESSAGE
        assert not result.approval_id and not result.message_id
        session.commit()
    test_worker_and_dispatch_do_not_contact_services_with_all_flags_enabled(policy_app)
    test_verified_superadmin_cannot_import_or_resume(policy_app)
    test_status_ignores_ready_cache_and_live_flags(policy_app)


def test_provider_and_send_gate_hold_before_gloo_or_queue(policy_app):
    state = policy_app.state
    with pytest.raises(ValueError, match="prohibits automated texting"):
        state.provider.send(PHONE, "Synthetic exact text.")
    with state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        gate = SendGate(session, state.clock, state.provider)
        gate.gloo = state.gloo
        result = gate.send(body="Synthetic exact text.", purpose="manual", volunteer=volunteer, kind="ai")
        assert result.status == SendStatus.BLOCKED_TRANSPORT
        assert result.reason == google_voice_policy.POLICY_HOLD_MESSAGE
        assert not result.approval_id and not result.message_id
        session.commit()
    assert_no_activity(policy_app)


def test_worker_and_dispatch_do_not_contact_services_with_all_flags_enabled(policy_app):
    state = policy_app.state
    tick_google_voice(state)
    dispatch_outbound(state, state.google_voice_connector)
    assert_no_activity(policy_app)


def test_preexisting_queue_cannot_be_claimed_or_dispatched(policy_app):
    state = policy_app.state
    with state.session_factory() as session:
        row = m.Message(direction="out", phone=PHONE, body="Synthetic previously queued text.",
                        kind="ai", purpose="manual", status="queued",
                        provider_sid=state.provider.test_sessions[PHONE].outbound_prefix + "historical",
                        created_at=state.clock.now())
        session.add(row)
        session.commit()
        message_id = row.id
    tick_google_voice(state)
    dispatch_outbound(state, state.google_voice_connector)
    assert state.gloo.calls == state.google_voice_connector.calls == []
    with state.session_factory() as session:
        assert session.get(m.Message, message_id).status == "queued"
        assert session.scalar(select(GoogleVoiceDeliveryClaim)) is None
    report = TestClient(policy_app).get("/api/cloud-texting").json()
    assert report["queue"]["queued"] == 1
    assert report["state"] == "policy_hold" and not report["live_enabled"]


def test_verified_superadmin_cannot_import_or_resume(policy_app):
    client = TestClient(policy_app)
    cookies = [{"name": name, "value": "synthetic-private-cookie", "domain": ".google.com", "path": "/"}
               for name in ("SID", "HSID", "SSID", "APISID", "SAPISID")]
    response = client.post("/api/cloud-texting/session", json={"cookies": cookies})
    assert response.status_code == 503
    assert response.json()["detail"] == google_voice_policy.POLICY_HOLD_MESSAGE
    assert "synthetic-private-cookie" not in response.text
    response = client.post("/api/cloud-texting/pause", json={"paused": False})
    assert response.status_code == 503
    assert response.json()["detail"] == google_voice_policy.POLICY_HOLD_MESSAGE
    assert_no_activity(policy_app)


def test_status_ignores_ready_cache_and_live_flags(policy_app):
    status = get_cloud_status(policy_app.state)
    assert status["state"] == "policy_hold"
    assert status["reason_code"] == google_voice_policy.POLICY_HOLD_CODE
    assert not status["ready"] and not status["connected"] and status["paused"]
    response = TestClient(policy_app).get("/api/cloud-texting")
    assert response.status_code == 200
    report = response.json()
    assert report["state"] == "policy_hold" and report["paused"]
    assert not report["connection"]["connected"] and not report["live_enabled"] and not report["enabled"]
    assert report["test_recipients"] == 0
    public = TestClient(policy_app).get("/api/config").json()
    assert public["providerPolicyHold"] is True and public["automationEnabled"] is False
    assert_no_activity(policy_app)


def test_manual_dashboard_cannot_create_a_google_draft(policy_app):
    with policy_app.state.session_factory() as session:
        volunteer_id = session.scalar(select(m.Volunteer.id))
    response = TestClient(policy_app).post("/api/reply",
        json={"volunteer_id": volunteer_id, "body": "Synthetic reviewed message."})
    assert response.status_code in {409, 503}
    assert google_voice_policy.POLICY_HOLD_MESSAGE in response.text
    assert_no_activity(policy_app)


@pytest.mark.parametrize("operation", ["health", "import_session", "prepare", "send", "inbound"])
@pytest.mark.parametrize('historical_demo', [False, True])
def test_shipped_client_never_attempts_network(policy_app, monkeypatch, operation, historical_demo):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("No HTTP/socket call is permitted by the shipped connector client")
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "stream", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    settings = historical_settings(policy_app.state.settings) if historical_demo else policy_app.state.settings
    client = GoogleVoiceConnector(settings)
    request = {"idempotency_key": "synthetic-request-001", "to": PHONE, "body": "Synthetic text.",
               "not_after": (policy_app.state.clock.now() + timedelta(seconds=30)).isoformat()}
    with pytest.raises(ConnectorUnavailable, match="prohibits automated texting"):
        if operation in {"prepare", "send"}:
            getattr(client, operation)(**request)
        elif operation == "import_session":
            client.import_session([])
        elif operation == "inbound":
            client.inbound("0")
        else:
            client.health()
    assert calls == []
