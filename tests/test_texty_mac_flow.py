"""The calendar/agent/transport seam, with synthetic data and no native sends."""

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.db.session import make_session_factory
from app.llm.parser import ParsedMessage
from app.main import create_app
from app.web.texty import admin
from tests.test_fill_agent import ScriptedAgentGloo, historical_invitation
from tests.session_fixtures import session_id, session_json


@pytest.mark.parametrize("service", ["iMessage", "SMS"])
def test_mac_replacement_acceptance_updates_admin_calendar(
    session, clock, make_volunteer, make_shift, assign, monkeypatch, service
):
    cancelled = make_volunteer("Synthetic Original")
    replacement = make_volunteer("Synthetic Replacement")
    shift = make_shift("Greeter")
    original = assign(cancelled, shift)
    session.commit()

    credential = "synthetic-bridge-credential-" + "x" * 32
    application = create_app(Settings(
        database_url="sqlite://", demo_mode=True,
        sms_provider="mac_messages", mac_bridge_enabled=True,
        mac_bridge_token=credential,
        mac_demo_phones=",".join([cancelled.phone, replacement.phone]),
        mac_message_services=service,
        admin_password="synthetic-admin-password",
        mac_test_sessions=session_json([cancelled.phone, replacement.phone], clock.now()),
    ))
    application.state.session_factory = make_session_factory(session.get_bind())
    application.state.clock = clock
    application.state.mac_delivery_clock = clock
    application.state.gloo = ScriptedAgentGloo()
    application.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
    monkeypatch.setattr("app.web.mac_messages.parse_inbound", lambda gloo, body:
        ParsedMessage(intent="cancel" if body == "Can't come Sunday" else "accept", confidence=1))
    headers = {"Authorization": "Bearer " + credential}

    with TestClient(application) as client:
        before = client.get("/api/state").json()
        assert before["assignments"][0]["volunteer_id"] == str(cancelled.id)
        cancellation = client.post("/mac/inbound", headers=headers, json={
            "guid": "synthetic-cancellation", "phone": cancelled.phone,
            "body": "Can't come Sunday", "service": service,
            "session_id": session_id(cancelled.phone),
        })
        assert cancellation.status_code == 200
        assert cancellation.json()["intent"] == "fill_agent"
        assert client.get("/api/state").json()["assignments"] == []
        batch = client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"]
        assert batch == []
        with application.state.session_factory() as s:
            fill = s.scalar(select(m.FillRequest))
            historical_invitation(s, clock, s.get(m.Volunteer, replacement.id), fill,
                                  provider=application.state.provider)
            s.commit()
        acceptance = {"guid": "synthetic-acceptance", "phone": replacement.phone, "body": "YES", "service": service,
                      "session_id": session_id(replacement.phone)}
        assert client.post("/mac/inbound", headers=headers, json=acceptance).status_code == 200
        assert client.post("/mac/inbound", headers=headers, json=acceptance).json()["duplicate"]
        after = client.get("/api/state").json()
        assert len(after["assignments"]) == 1
        assert after["assignments"][0]["shift_id"] == str(shift.id)
        assert after["assignments"][0]["volunteer_id"] == str(replacement.id)
        assert any(item["id"] == str(shift.id) for item in after["shifts"])
        assert any(item["body"] == "YES" for item in after["messages"])
        notices = client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"]
        assert len(notices) == 1 and notices[0]["phone"] == replacement.phone
        notice = notices[0]
        assert notice["conversation_preflight_required"]
        assert client.post(f"/mac/outbound/{notice['id']}/verify", headers=headers,
                           json={"token":notice["token"]}).status_code == 200
        assert client.post(f"/mac/outbound/{notice['id']}/ack", headers=headers,
                           json={"token":notice["token"],"outcome":"submitted"}).status_code == 200

    session.expire_all()
    assert session.get(m.Assignment, original.id).status == "cancelled"
    confirmed = session.scalar(select(m.Assignment).where(m.Assignment.volunteer_id == replacement.id))
    assert confirmed.status == "confirmed"
    assert session.scalar(select(m.FillRequest)).state == "filled"
