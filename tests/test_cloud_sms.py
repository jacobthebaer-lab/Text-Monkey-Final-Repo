"""Cloud transport integration tests: synthetic Gloo and carrier, no real texts."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.clock import FakeClock
from app.config import Settings
from app.core import confirmations
from app.core.send_gate import SendGate
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.main import create_app
from app.sms import cloud_delivery as delivery
from app.web.texty import admin
from tests.conftest import NOW

PUBLIC = "https://cloud.example.test"
SID = "SM" + "1" * 32


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/cloud.db", demo_mode=False,
        automation_enabled=False, sms_provider="twilio", live_sms=True,
        twilio_account_sid="AC" + "0" * 32, twilio_auth_token="synthetic-token",
        twilio_from_number="+15555550100", public_base_url=PUBLIC,
        admin_password="synthetic-password-only", gloo_api_key="synthetic-key",
        gloo_signup_replies=True)
    app = create_app(settings)
    app.state.clock = FakeClock(NOW)
    calls = []
    def compose(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.loads(kwargs["input"])["approved_message"], usage=None)
    app.state.gloo = SimpleNamespace(settings=settings, create_response=compose)
    submitted = []
    def submit(message_id, phone, body):
        # Submission can see the committed claim from a different transaction.
        with app.state.session_factory() as session:
            assert session.get(m.Message, message_id).status == "dispatching"
        submitted.append((message_id, phone, body))
        return SimpleNamespace(sid=SID, status="queued")
    monkeypatch.setattr(app.state.provider, "submit", submit)
    with app.state.session_factory() as session:
        v = m.Volunteer(name="Synthetic Person", phone="+15555550101", sms_opt_in=True,
                        status="active", preferences={}, created_at=NOW-timedelta(days=100))
        session.add(v)
        session.commit()
    app.dependency_overrides[admin] = lambda: {"email": "admin@example.test"}
    return app, v, submitted, calls


def reserve(cloud, *, commit=True, body="Synthetic exact message."):
    app, v, _, _ = cloud
    with app.state.session_factory() as session:
        gate = SendGate(session, app.state.clock, app.state.provider)
        gate.gloo = app.state.gloo
        result = gate.send(body=body, purpose="admin_reply", volunteer=session.get(m.Volunteer, v.id))
        if commit:
            session.commit()
        return result


def post(app, path, form, signature=None):
    signature = signature or RequestValidator(app.state.settings.twilio_auth_token).compute_signature(PUBLIC+path, form)
    return TestClient(app).post(path, data=form, headers={"X-Twilio-Signature": signature})


def test_committed_queue_dispatches_once_and_does_not_claim_delivery(cloud):
    app, _, submitted, calls = cloud
    result = reserve(cloud, body="  Exact wording.\n🐒  ")
    assert result.sent and not submitted and len(calls) == 1
    delivery.dispatch_pending(app.state)
    delivery.dispatch_pending(app.state)
    assert submitted == [(result.message_id, cloud[1].phone, "  Exact wording.\n🐒  ")]
    with app.state.session_factory() as session:
        assert session.get(m.Message, result.message_id).status == "submitted"


def test_rollback_cannot_send_and_gloo_failure_never_falls_back(cloud):
    app, _, submitted, _ = cloud
    reserve(cloud, commit=False)
    delivery.dispatch_pending(app.state)
    assert not submitted
    app.state.gloo.create_response = lambda **kwargs: SimpleNamespace(output_text="Changed words", usage=None)
    with pytest.raises(GlooUnavailableError):
        reserve(cloud)
    delivery.dispatch_pending(app.state)
    assert not submitted


@pytest.mark.parametrize("change", ["optout", "phone", "body", "quiet", "expiry", "inactive"])
def test_mutable_guards_rechecked_before_carrier_submission(cloud, change):
    app, v, submitted, _ = cloud
    result = reserve(cloud)
    with app.state.session_factory() as session:
        if change == "optout": session.get(m.Volunteer, v.id).sms_opt_in = False
        if change == "phone": session.get(m.Volunteer, v.id).phone = "+15555550102"
        if change == "inactive": session.get(m.Volunteer, v.id).status = "inactive"
        if change == "body": session.get(m.Message, result.message_id).body = "Changed"
        session.commit()
    if change == "quiet": app.state.clock = FakeClock(NOW.replace(hour=22))
    if change == "expiry": app.state.clock = FakeClock(NOW + timedelta(hours=3))
    delivery.dispatch_pending(app.state)
    assert not submitted


def test_ambiguous_timeout_and_restart_are_never_automatically_retried(cloud, monkeypatch):
    app, _, submitted, _ = cloud
    result = reserve(cloud)
    def timeout(*args):
        submitted.append(args)
        raise TimeoutError("synthetic")
    monkeypatch.setattr(app.state.provider, "submit", timeout)
    delivery.dispatch_pending(app.state)
    delivery.dispatch_pending(app.state)
    assert len(submitted) == 1
    with app.state.session_factory() as session:
        assert session.get(m.Message, result.message_id).status == "uncertain"
    restarted = reserve(cloud)
    with app.state.session_factory() as session:
        session.get(m.Message, restarted.message_id).status = "dispatching"
        session.get(m.Notification, f"cloud-sms:{restarted.message_id}").state = "dispatching"
        session.commit()
    delivery.reconcile_interrupted(app.state)
    delivery.dispatch_pending(app.state)
    assert len(submitted) == 1


def test_exact_cloud_review_keeps_origin_and_content_proof(cloud):
    app, _, submitted, _ = cloud
    app.state.session_factory.configure(info={confirmations.MODE_KEY: True})
    result = reserve(cloud)
    assert result.approval_id and not result.message_id
    delivery.dispatch_pending(app.state)
    assert not submitted
    with app.state.session_factory() as session:
        a = session.get(m.Approval, result.approval_id)
        assert a.payload["transport"] == "twilio"
        gate = SendGate(session, app.state.clock, app.state.provider)
        confirmations.decide(session, gate, a, approve=True, actor="synthetic-admin",
                             expected=a.payload["content_hash"], now=NOW)
        session.commit()
    delivery.dispatch_pending(app.state)
    assert len(submitted) == 1


def test_ingress_is_signed_deduplicated_and_acked_before_interpretation(cloud):
    app, v, submitted, calls = cloud
    form = {"AccountSid": app.state.settings.twilio_account_sid,
            "From": v.phone, "To": app.state.settings.twilio_from_number,
            "MessageSid": SID, "Body": "STOP"}
    assert post(app, "/sms/inbound", form, signature="bad").status_code == 403
    assert post(app, "/sms/inbound", {**form, "AccountSid": "wrong"}).status_code == 403
    assert post(app, "/sms/inbound", {**form, "To": "+15555550199"}).status_code == 403
    for _ in range(2):
        assert post(app, "/sms/inbound", form).status_code == 200
    assert not calls and not submitted
    delivery.process_inbound(app.state)
    delivery.process_inbound(app.state)
    with app.state.session_factory() as session:
        assert session.get(m.Volunteer, v.id).sms_opt_in is False
        rows = session.scalars(select(m.Message).where(m.Message.direction == "in")).all()
        assert len(rows) == 1 and rows[0].provider_sid == SID


def test_signed_carrier_callbacks_prove_delivery_and_cannot_regress(cloud):
    app, v, _, _ = cloud
    result = reserve(cloud)
    delivery.dispatch_pending(app.state)
    path = f"/sms/status?message_id={result.message_id}"
    form = {"AccountSid": app.state.settings.twilio_account_sid,
            "From": app.state.settings.twilio_from_number, "To": v.phone,
            "MessageSid": SID, "MessageStatus": "delivered"}
    assert post(app, path, form, signature="bad").status_code == 403
    assert post(app, path, {**form, "MessageSid": "SM"+"2"*32}).status_code == 403
    assert post(app, path, form).status_code == 204
    assert post(app, path, {**form, "MessageStatus": "sent"}).status_code == 204
    with app.state.session_factory() as session:
        assert session.get(m.Message, result.message_id).status == "delivered"


def test_admin_cloud_queue_retry_and_worker_lifecycle_without_automation(cloud):
    app, v, submitted, calls = cloud
    with TestClient(app) as client:
        config = client.get("/api/config").json()
        assert config["cloudSmsWorkerRunning"] and config["messagingConnected"]
        payload = {"volunteer_id": v.id, "body": "Synthetic exact reply.",
                   "request_id": "11111111-1111-4111-8111-111111111111"}
        first = client.post("/api/reply", json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["delivery"] == "queued_for_cloud"
        assert client.post("/api/reply", json=payload).json() == first.json()
        assert len(calls) == 1
    assert app.state.cloud_sender_running is False
    assert not submitted


def test_callback_before_rest_response_survives_timeout(cloud, monkeypatch):
    app, v, submitted, _ = cloud
    result = reserve(cloud)
    def callback_then_timeout(message_id, phone, body):
        submitted.append(message_id)
        form = {"AccountSid": app.state.settings.twilio_account_sid,
                "From": app.state.settings.twilio_from_number, "To": v.phone,
                "MessageSid": SID, "MessageStatus": "delivered"}
        assert post(app, f"/sms/status?message_id={message_id}", form).status_code == 204
        raise TimeoutError("synthetic response lost")
    monkeypatch.setattr(app.state.provider, "submit", callback_then_timeout)
    delivery.dispatch_pending(app.state)
    delivery.dispatch_pending(app.state)
    assert len(submitted) == 1
    with app.state.session_factory() as session:
        assert session.get(m.Message, result.message_id).status == "delivered"


def test_failed_inbound_work_rolls_back_and_retries_safely(cloud, monkeypatch):
    from app.core import inbound
    app, v, submitted, _ = cloud
    form = {"AccountSid": app.state.settings.twilio_account_sid,
            "From": v.phone, "To": app.state.settings.twilio_from_number,
            "MessageSid": SID, "Body": "Synthetic request"}
    assert post(app, "/sms/inbound", form).status_code == 200
    def fail(session, *args, **kwargs):
        session.add(m.Message(direction="in", phone=v.phone, body="Partial", kind="inbound",
                              status="received", created_at=NOW))
        session.flush()
        raise GlooUnavailableError("synthetic outage")
    monkeypatch.setattr(inbound, "handle_inbound", fail)
    for attempt in range(3):
        app.state.clock = FakeClock(NOW + timedelta(minutes=attempt*2))
        delivery.process_inbound(app.state)
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None
        receipt = session.get(m.Notification, "cloud-inbound:"+SID)
        assert receipt.state == "blocked" and receipt.detail["attempts"] == 3
    assert not submitted


@pytest.mark.parametrize("override", [dict(demo_mode=True), dict(mac_bridge_enabled=True),
    dict(gloo_api_key=""), dict(gloo_signup_replies=False), dict(public_base_url="http://unsafe.test"),
    dict(admin_password="short"), dict(public_base_url="https://cloud.test/path"),
    dict(public_base_url="https://user:pass@cloud.test")])
def test_live_cloud_configuration_fails_closed(cloud, override):
    with pytest.raises(ValueError):
        create_app(replace(cloud[0].state.settings, **override))
