"""Cloud transport tests use synthetic accounts, numbers and connector responses."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from app.config import Settings
from app.core import confirmations
from app.core.send_gate import SendGate
from app.db import models as m
from app.integrations.google_voice_client import ConnectorUnavailable, verified_health
from app.integrations.google_voice_models import (GoogleVoiceBase, GoogleVoiceDeliveryClaim,
    GoogleVoiceInboundReceipt, prepare_google_voice_schema)
from app.integrations.google_voice_runtime import CURSOR_KEY, dispatch_outbound, set_paused, tick_google_voice
from app.llm.gloo_client import GlooUnavailableError
from app.main import create_app
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.web.texty import admin
from tests.session_fixtures import session_json

PHONE = "+15555550101"
EMAIL = "owner@example.test"
NUMBER = "+15555550202"
TOKEN = "synthetic-only-" + "x" * 40


class FakeConnector:
    def __init__(self):
        self.calls = []
        self.imports = []
        self.messages = []
        self.cursor = 0
        self.ready = True
        self.outcome = "submitted"

    def health(self):
        return {"ready": self.ready, "identity_verified": self.ready,
                "expected_identity_match": self.ready,
                "identity_fingerprint": hashlib.sha256((EMAIL + "\n" + NUMBER).encode()).hexdigest()}

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def inbound(self, cursor):
        return {"messages": self.messages if cursor < self.cursor else [], "cursor": self.cursor}

    def import_session(self, cookies):
        self.imports.append(cookies)
        return self.health()


@pytest.fixture
def cloud(tmp_path, clock):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/cloud.db", sms_provider="google_voice",
        google_voice_enabled=True, google_voice_connector_token=TOKEN, live_sms=True,
        google_voice_expected_email=EMAIL, google_voice_expected_number=NUMBER,
        google_voice_demo_phones=PHONE, google_voice_test_sessions=session_json([PHONE], clock.now()),
        competition_confirmation_required=True, gloo_api_key="synthetic-gloo",
        superadmin_email_allowlist=EMAIL, admin_email_allowlist=EMAIL,
        automation_enabled=False)
    application = create_app(settings)
    state = application.state
    state.clock = state.google_voice_clock = clock
    state.google_voice_connector = FakeConnector()
    with state.session_factory() as session:
        session.info["record_authorized"] = True
        session.add(m.Volunteer(name="Synthetic Tester", phone=PHONE, sms_opt_in=True,
            status="active", preferences={}, created_at=clock.now()))
        set_paused(session, False)
        session.commit()
    application.dependency_overrides[admin] = lambda: {"email": EMAIL, "email_confirmed_at": "synthetic"}
    return application


def queued(cloud):
    state = cloud.state
    with state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        gate = SendGate(session, state.clock, state.provider)
        result = gate.send(body="Synthetic approved message", purpose="thanks", kind="ai", volunteer=volunteer)
        approval = session.get(m.Approval, result.approval_id)
        confirmations.decide(session, gate, approval, approve=True, actor=EMAIL,
            expected=approval.payload["content_hash"], now=state.clock.now())
        session.commit()
        return approval.payload["message_id"]


def status(cloud, message_id):
    with cloud.state.session_factory() as session:
        return session.get(m.Message, message_id).status


def incoming(cloud, body="What time?", *, marker=True):
    state = cloud.state
    selected = state.provider.test_sessions[PHONE]
    return {"id": "synthetic-inbound", "phone": PHONE,
            "body": selected.prefix + body if marker else body,
            "received_at": state.clock.now().isoformat()}


def test_disabled_provider_can_boot_without_credentials():
    provider = GoogleVoiceProvider(Settings(sms_provider="google_voice"))
    assert provider.phones == frozenset()
    with pytest.raises(ValueError, match="disabled"):
        provider.send(PHONE, "No send")


@pytest.mark.parametrize("provider", ["mock", "mac_messages"])
def test_non_cloud_startup_excludes_cloud_schema_and_status_is_safe(tmp_path, clock, provider):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/{provider}.db", sms_provider=provider,
        mac_bridge_enabled=provider == "mac_messages", mac_bridge_token=TOKEN,
        mac_demo_phones=PHONE, mac_test_sessions=session_json([PHONE], clock.now()),
        admin_password="synthetic-admin-password", superadmin_email_allowlist=EMAIL,
        automation_enabled=False)
    application = create_app(settings)
    tables = set(inspect(application.state.engine).get_table_names())
    assert "google_voice_delivery_claims" not in tables
    assert "google_voice_inbound_receipts" not in tables
    assert not any(name.startswith("google_voice_") for name in m.Base.metadata.tables)
    application.dependency_overrides[admin] = lambda: {"email": EMAIL, "email_confirmed_at": "synthetic"}
    with TestClient(application) as client:
        response = client.get("/api/cloud-texting")
        assert response.status_code == 200
        assert response.json()["state"] == "disabled"
        assert response.json()["held_inbound"] == {"held_gloo": 0, "held_expired_session": 0}


def test_cloud_sqlite_startup_initializes_only_its_separate_schema(cloud):
    tables = set(inspect(cloud.state.engine).get_table_names())
    assert set(GoogleVoiceBase.metadata.tables) <= tables
    assert next(iter(GoogleVoiceDeliveryClaim.__table__.foreign_keys)).column is m.Message.__table__.c.id
    with TestClient(cloud) as client:
        assert client.get("/api/cloud-texting").status_code == 200


@pytest.mark.parametrize("exists", [True, False])
def test_hosted_cloud_schema_requires_migration_without_attempting_ddl(monkeypatch, exists):
    checked = []
    class Inspector:
        def has_table(self, name, schema):
            checked.append((name, schema))
            return exists
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                             get_execution_options=lambda: {"schema_translate_map": {None: "texty"}})
    monkeypatch.setattr("app.integrations.google_voice_models.inspect", lambda engine: Inspector())
    def forbidden(*args, **kwargs):
        raise AssertionError("Cloud Postgres startup must not execute DDL")
    monkeypatch.setattr(GoogleVoiceBase.metadata, "create_all", forbidden)
    if exists:
        prepare_google_voice_schema(engine)
    else:
        with pytest.raises(RuntimeError, match="supabase/google_voice_transport.sql"):
            prepare_google_voice_schema(engine)
    assert checked and all(schema == "texty" for _, schema in checked)


def test_enqueue_is_commit_only_and_submission_never_claims_delivery(cloud):
    message_id = queued(cloud)
    assert status(cloud, message_id) == "queued"
    assert cloud.state.google_voice_connector.calls == []
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "submitted"
    tick_google_voice(cloud.state)
    assert len(cloud.state.google_voice_connector.calls) == 1
    with cloud.state.session_factory() as session:
        claim = session.get(GoogleVoiceDeliveryClaim, message_id)
        assert claim.idempotency_key == cloud.state.google_voice_connector.calls[0]["idempotency_key"]
    deadline = datetime.fromisoformat(cloud.state.google_voice_connector.calls[0]["not_after"])
    assert deadline == cloud.state.clock.now() + timedelta(seconds=30)


def test_ambiguous_send_is_never_retried(cloud):
    message_id = queued(cloud)
    cloud.state.google_voice_connector.outcome = ConnectorUnavailable("synthetic timeout")
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "uncertain"
    cloud.state.google_voice_connector.outcome = "submitted"
    tick_google_voice(cloud.state)
    assert len(cloud.state.google_voice_connector.calls) == 1


@pytest.mark.parametrize("change,expected", [("content", "blocked_confirmation"),
    ("consent", "blocked_confirmation"), ("stale", "blocked_stale"),
    ("expired_session", "blocked_test_session"), ("quiet_hours", "blocked_quiet_hours")])
def test_revalidates_before_submission(cloud, monkeypatch, change, expected):
    message_id = queued(cloud)
    if change == "stale":
        cloud.state.clock.advance(timedelta(minutes=16))
    elif change == "expired_session":
        cloud.state.clock.advance(timedelta(hours=3))
    elif change == "quiet_hours":
        monkeypatch.setattr("app.integrations.google_voice_runtime.in_quiet_hours", lambda *a: True)
    else:
        with cloud.state.session_factory() as session:
            session.info["record_authorized"] = True
            if change == "content":
                session.get(m.Message, message_id).body = "Changed after review"
            else:
                session.scalar(select(m.Volunteer)).sms_opt_in = False
            session.commit()
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == expected
    assert cloud.state.google_voice_connector.calls == []


@pytest.mark.parametrize("condition", ["paused", "live_disabled", "no_gloo", "reconnect"])
def test_closed_gates_never_claim_or_send(cloud, condition):
    message_id = queued(cloud)
    if condition == "paused":
        with cloud.state.session_factory() as session:
            set_paused(session, True)
            session.commit()
    elif condition == "live_disabled":
        cloud.state.settings = replace(cloud.state.settings, live_sms=False)
    elif condition == "no_gloo":
        cloud.state.settings = replace(cloud.state.settings, gloo_api_key="")
    else:
        cloud.state.google_voice_connector.ready = False
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "queued"
    assert cloud.state.google_voice_connector.calls == []


def test_existing_dispatching_claim_survives_restart_without_resend(cloud):
    message_id = queued(cloud)
    with cloud.state.session_factory() as session:
        row = session.get(m.Message, message_id)
        row.status = "dispatching"
        session.add(GoogleVoiceDeliveryClaim(message_id=row.id, idempotency_key=row.provider_sid,
                                            created_at=cloud.state.clock.now()))
        session.commit()
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "dispatching"
    assert cloud.state.google_voice_connector.calls == []


def test_health_bound_to_expected_account(cloud):
    health = cloud.state.google_voice_connector.health()
    assert verified_health(health, cloud.state.settings)
    assert not verified_health(health, replace(cloud.state.settings, google_voice_expected_email="other@example.test"))


def test_normal_admin_cannot_control_or_read_cloud_session(cloud):
    cloud.dependency_overrides[admin] = lambda: {"email": "admin@example.test", "email_confirmed_at": "yes"}
    with TestClient(cloud) as client:
        assert client.get("/api/cloud-texting").status_code == 403
        assert client.post("/api/cloud-texting/pause", json={"paused": False}).status_code == 403
        assert client.post("/api/cloud-texting/session", json={"cookies": []}).status_code == 403


def test_superadmin_cookies_are_not_echoed_and_import_stays_paused(cloud):
    cookies = [{"name": name, "value": "synthetic-secret-never-echo", "domain": ".google.com", "path": "/"}
               for name in ("SID", "HSID", "SSID", "APISID", "SAPISID")]
    with TestClient(cloud) as client:
        bad = client.post("/api/cloud-texting/session", json={"cookies": [{"value": "secret-bad-input"}]})
        assert bad.status_code == 400 and "secret-bad-input" not in bad.text
        good = client.post("/api/cloud-texting/session", json={"cookies": cookies})
        assert good.status_code == 200 and good.json()["paused"] is True
        assert "synthetic-secret-never-echo" not in good.text
    assert len(cloud.state.google_voice_connector.imports) == 1


def test_failed_identity_cannot_resume(cloud):
    cloud.state.google_voice_connector.ready = False
    with TestClient(cloud) as client:
        assert client.post("/api/cloud-texting/pause", json={"paused": False}).status_code == 409


@pytest.mark.parametrize("marker", [True, False])
def test_expired_session_stop_persists_without_acknowledgment(cloud, marker):
    message_id = queued(cloud)
    cloud.state.clock.advance(timedelta(hours=3))
    connector = cloud.state.google_voice_connector
    connector.messages = [incoming(cloud, "STOP", marker=marker)]
    connector.cursor = 1
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer)).sms_opt_in is False
        assert session.get(m.Policy, "sms_opt_out:" + PHONE).value["value"] is True
        assert len(session.scalars(select(m.Message).where(m.Message.direction == "out")).all()) == 1
        assert session.get(GoogleVoiceInboundReceipt, "synthetic-inbound").result["intent"] == "stop"
    assert status(cloud, message_id) == "blocked_opt_out"
    assert connector.calls == []


def test_inbound_receipt_and_business_changes_are_atomic_and_retry_gloo(cloud, monkeypatch):
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud)], 1
    def unavailable(session, *args, **kwargs):
        session.add(m.Message(direction="in", phone=PHONE, body="Must roll back", kind="inbound",
                              status="received", created_at=cloud.state.clock.now()))
        raise GlooUnavailableError("synthetic outage")
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound", unavailable)
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None
        assert session.get(GoogleVoiceInboundReceipt, "synthetic-inbound").result["state"] == "held_gloo"
        assert session.get(m.Policy, CURSOR_KEY).value["value"] == 1
    calls = []
    def success(*args, **kwargs):
        calls.append(args[4])
        return SimpleNamespace(routed_to="question")
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound", success)
    tick_google_voice(cloud.state)
    tick_google_voice(cloud.state)
    assert calls == ["What time?"]


def test_gloo_hold_preserved_after_session_expires(cloud, monkeypatch):
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud)], 1
    cloud.state.settings = replace(cloud.state.settings, gloo_api_key="")
    tick_google_voice(cloud.state)
    cloud.state.clock.advance(timedelta(hours=3))
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        receipt = session.get(GoogleVoiceInboundReceipt, "synthetic-inbound")
        assert receipt.result["state"] == "held_expired_session"
        assert "What time?" in receipt.result["incoming"]["body"]


def test_gloo_outage_holds_already_approved_outbound(cloud, monkeypatch):
    message_id = queued(cloud)
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud)], 1
    def unavailable(*args, **kwargs):
        raise GlooUnavailableError("synthetic outage")
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound", unavailable)
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "queued"
    assert connector.calls == []


def test_real_parser_cannot_swallow_gloo_outage_and_consume_inbound(cloud):
    from app.llm.gloo_client import NullGloo
    cloud.state.gloo = NullGloo()
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud)], 1
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.get(GoogleVoiceInboundReceipt, "synthetic-inbound").result["state"] == "held_gloo"
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(m.Escalation)) is None


def test_active_marked_stop_works_without_gloo(cloud):
    cloud.state.settings = replace(cloud.state.settings, gloo_api_key="")
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud, "STOP")], 1
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer)).sms_opt_in is False
        assert session.get(GoogleVoiceInboundReceipt, "synthetic-inbound").result.get("state") != "held_gloo"


def test_unknown_active_sender_stop_does_not_depend_on_signup(cloud):
    with cloud.state.session_factory() as session:
        session.info["record_authorized"] = True
        session.delete(session.scalar(select(m.Volunteer)))
        session.commit()
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud, "STOP", marker=False)], 1
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.get(m.Policy, "sms_opt_out:" + PHONE).value["value"] is True
        assert session.get(GoogleVoiceInboundReceipt, "synthetic-inbound").result["intent"] == "stop"
        assert session.scalar(select(m.Message).where(m.Message.direction == "out")) is None


def test_submission_deadline_stops_at_quiet_hour_boundary(cloud):
    message_id = queued(cloud)
    # The synthetic clock is 10:00. Set the next quiet window to begin in one
    # minute, then cross to within the connector's 30-second submission budget.
    with cloud.state.session_factory() as session:
        session.add(m.Policy(key="quiet_hours", value={"value": {"start": "10:01", "end": "11:00"}}))
        session.commit()
    cloud.state.clock.advance(timedelta(seconds=45))
    tick_google_voice(cloud.state)
    deadline = datetime.fromisoformat(cloud.state.google_voice_connector.calls[0]["not_after"])
    assert deadline == cloud.state.clock.now() + timedelta(seconds=15)


def test_completed_receipt_does_not_replay_when_gloo_disappears(cloud, monkeypatch):
    connector = cloud.state.google_voice_connector
    item = incoming(cloud)
    connector.messages, connector.cursor = [item], 1
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound",
                        lambda *a, **k: SimpleNamespace(routed_to="question"))
    tick_google_voice(cloud.state)
    cloud.state.settings = replace(cloud.state.settings, gloo_api_key="")
    # A retried backend cursor may legitimately see an already handled item.
    with cloud.state.session_factory() as session:
        session.get(m.Policy, CURSOR_KEY).value = {"value": 0}
        session.commit()
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.get(GoogleVoiceInboundReceipt, item["id"]).result.get("intent") == "question"


def test_unmarked_personal_message_not_persisted(cloud):
    connector = cloud.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(cloud, marker=False)], 1
    tick_google_voice(cloud.state)
    with cloud.state.session_factory() as session:
        assert session.scalar(select(GoogleVoiceInboundReceipt)) is None
        assert session.scalar(select(m.Message)) is None
