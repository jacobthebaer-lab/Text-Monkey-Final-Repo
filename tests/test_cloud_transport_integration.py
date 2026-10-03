"""Synthetic dashboard-to-cloud queue checks; no Google account or network."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import confirmations
from app.core.send_gate import SendGate
from app.db import models as m
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.web.texty import admin
from tests.test_confirmations import mode_app
from tests.session_fixtures import session_json
from tests.test_google_voice import ExactGloo


@pytest.fixture
def cloud_app(mode_app):
    app, volunteers, _ = mode_app
    from app.admin_setup.models import SetupBase
    from app.integrations.google_voice_models import prepare_google_voice_schema
    SetupBase.metadata.create_all(app.state.session_factory.kw["bind"])
    prepare_google_voice_schema(app.state.session_factory.kw["bind"])
    app.state.settings = replace(app.state.settings,
        sms_provider="google_voice", google_voice_enabled=True,
        google_voice_connector_token="synthetic-cloud-" + "x" * 40,
        google_voice_expected_email="owner@example.test",
        google_voice_expected_number="+15555550100",
        google_voice_demo_phones=",".join(v.phone for v in volunteers),
        google_voice_test_sessions=session_json([v.phone for v in volunteers], app.state.clock.now()),
        superadmin_email_allowlist="owner@example.test")
    app.state.provider = GoogleVoiceProvider(app.state.settings)
    app.state.gloo = ExactGloo(app.state.settings)
    app.state.google_voice_clock = app.state.clock
    app.dependency_overrides[admin] = lambda: {
        "id": "11111111-1111-4111-8111-111111111111",
        "email": "coordinator@example.test", "email_confirmed_at": "2026-10-01T00:00:00Z"}
    return app, volunteers


def test_cloud_exact_review_queues_without_sending_or_exposing_mac_history(cloud_app):
    app, volunteers = cloud_app
    v = volunteers[0]
    with app.state.session_factory() as session:
        selected = app.state.provider.test_sessions[v.phone]
        session.add(m.Message(direction="out", phone=v.phone, body="Private Mac history", kind="ai", purpose="admin_reply",
            provider_sid=f"MAC{selected.id}:old", status="submitted", created_at=app.state.clock.now()))
        session.add(m.Message(direction="in", phone=v.phone, body="Private Mac incoming", kind="mac_test_in",
            purpose=f"test:{selected.id}", status="received", created_at=app.state.clock.now()))
        old = m.Approval(kind="confirm_text", status="pending", requested_at=app.state.clock.now(),
            payload={"transport": "mac_messages", "phone": v.phone, "session_id": selected.id,
                     "body": "Mac-only approval"})
        session.add(old)
        session.commit()
        old_id = old.id
    with TestClient(app) as client:
        assert client.get("/api/config").json()["messagingTransport"] == "google_voice"
        assert client.get("/api/config").json()["cloudTextingAvailable"] is True
        assert client.get("/api/auth/me").json()["superadmin"] is False
        assert client.get("/api/cloud-texting").status_code == 403
        drafted = client.post("/api/reply", json={"volunteer_id": v.id, "body": "Exact cloud test."})
        assert drafted.status_code == 200, drafted.text
        item = drafted.json()
        assert item["delivery"] == "awaiting_confirmation"
        state = client.get("/api/state").json()
        assert not any(row["body"] == "Private Mac history" for row in state["messages"])
        assert not any(row["body"] == "Private Mac incoming" for row in state["messages"])
        from app.core.conversation import scope
        with app.state.session_factory() as session:
            assert session.scalars(scope(select(m.Message), selected)).all() == []
        assert not any(row["id"] == str(old_id) for row in state["proposals"])
        assert client.post(f"/api/proposals/{old_id}/approve", json={"content_hash": "x"}).status_code == 404
        approved = client.post(f"/api/proposals/{item['approval_id']}/approve",
            json={"content_hash": item["content_hash"]})
        assert approved.status_code == 200, approved.text
        assert approved.json()["delivery"] == "queued_for_google_voice"
        assert client.post(f"/api/proposals/{item['approval_id']}/approve",
            json={"content_hash": item["content_hash"]}).status_code == 409
        assert client.get("/mac/test-history").status_code == 503
    with app.state.session_factory() as session:
        message = session.scalar(select(m.Message).where(m.Message.provider_sid.startswith("GV")))
        assert message.status == "queued"
        assert message.body == "Exact cloud test."
        approval = session.get(m.Approval, item["approval_id"])
        assert approval.payload["transport"] == "google_voice"
        assert confirmations.proof_for(session, message).id == approval.id


def test_transport_switch_invalidates_approved_message_proof(cloud_app):
    app, volunteers = cloud_app
    with app.state.session_factory() as session:
        gate = SendGate(session, app.state.clock, app.state.provider)
        gate.gloo = app.state.gloo
        outcome = gate.send(body="Cloud only.", purpose="admin_reply",
                            volunteer=session.get(m.Volunteer, volunteers[0].id))
        approval = session.get(m.Approval, outcome.approval_id)
        approval.status = "approved"
        assert confirmations.delivery_problem(session, app.state.provider, approval, app.state.clock.now()) is None
        # Same recipient/session cannot make a Mac review authorize cloud SMS.
        approval.payload = {**approval.payload, "transport": "mac_messages"}
        approval.payload = {**approval.payload, "content_hash": confirmations.digest(approval.payload)}
        assert confirmations.delivery_problem(session, app.state.provider, approval, app.state.clock.now()) == "selected transport or test session changed"


def test_cloud_readiness_does_not_require_laptop_or_claim_delivery(cloud_app):
    app, _ = cloud_app
    with TestClient(app) as client:
        result = client.get("/api/setup/admin-texts")
        assert result.status_code == 200
        value = result.json()
        assert value["ready"] is False
        labels = {item["code"]: item["label"] for item in value["checks"]}
        assert labels["transport"] == "Cloud Google Voice"
        assert labels["bridge"] == "Cloud connection"
        assert "Laptop" not in " ".join(labels.values())
