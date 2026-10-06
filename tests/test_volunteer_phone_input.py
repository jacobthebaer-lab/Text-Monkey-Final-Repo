import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.main import create_app
from app.web.texty import admin


@pytest.fixture
def client():
    app = create_app(Settings(database_url="sqlite://", automation_enabled=False, sms_provider="mock", live_sms=False))
    app.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
    with TestClient(app) as client:
        yield client


def payload(phone):
    return {"first_name": "Alex", "last_name": "Example", "phone": phone, "consent": False}


@pytest.mark.parametrize("phone,expected", [
    ("2025550199", "+12025550199"),
    (" (202) 555-0199 ", "+12025550199"),
    ("202.555.0199", "+12025550199"),
    ("1-202-555-0199", "+12025550199"),
    ("+12025550199", "+12025550199"),
    ("+44 7700 900123", "+447700900123"),
])
def test_create_stores_normalized_phone_without_consent_or_texts(client, phone, expected):
    response = client.post("/api/volunteers", json=payload(phone))
    assert response.status_code == 200
    assert response.json()["phone"] == expected
    assert response.json()["consent"] is False
    with client.app.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        assert volunteer.phone == expected
        assert not volunteer.is_coordinator and not volunteer.is_pastor
        assert session.scalar(select(m.Message)) is None


@pytest.mark.parametrize("phone", ["", "202555019", "202555019999", "0202555019", "2025550199 ext 1", "++12025550199", "447700900123", None, 2025550199])
def test_invalid_phone_is_rejected_without_creating_profile(client, phone):
    assert client.post("/api/volunteers", json=payload(phone)).status_code == 400
    with client.app.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer)) is None


def test_alternate_format_deduplicates_and_edit_preserves_identity_review(client):
    original = client.post("/api/volunteers", json=payload("2025550199")).json()
    assert client.post("/api/volunteers", json=payload("+1 (202) 555-0199")).status_code == 409
    route = f"/api/volunteers/{original['id']}"
    response = client.post(route, json={**payload("(202) 555-0199"), "status": "active"})
    assert response.status_code == 200
    assert response.json()["phone"] == "+12025550199"
    assert client.post(route, json=payload("2025550198")).status_code == 400
    with client.app.state.session_factory() as session:
        assert len(session.scalars(select(m.Volunteer)).all()) == 1
