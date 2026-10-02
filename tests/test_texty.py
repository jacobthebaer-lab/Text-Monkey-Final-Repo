from types import SimpleNamespace
import json
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.main import create_app
from app.db import models as m
from app.core.signup import request_signup, approve_signup
from app.web.texty import admin, check_user
import pytest


class SignupGloo:
    settings = Settings()

    def __init__(self, data):
        self.data = data
        self.calls = 0

    def create_response(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(output_text=json.dumps(self.data))


def test_signup_waits_for_review_and_grants_no_privileges(session, clock):
    gloo = SignupGloo(
        {
            "signup": True,
            "first_name": "Alex",
            "last_name": "Morgan",
            "sensitive": False,
        }
    )
    assert (
        request_signup(session, clock, gloo, "+15555550199", "Join Alex Morgan")
        == "signup_pending"
    )
    assert session.scalar(select(m.Volunteer)) is None
    approval = session.scalar(select(m.Approval))
    v = approve_signup(session, clock, approval)
    assert v.name == "Alex Morgan"
    assert not v.is_coordinator and not v.is_pastor and not v.sms_opt_in
    assert v.qualifications == [] and v.status == "inactive"


def test_duplicate_signup_does_not_create_second_request(session, clock):
    gloo = SignupGloo({"signup": True, "first_name": "Alex", "last_name": "Morgan"})
    for _ in range(2):
        request_signup(session, clock, gloo, "+15555550199", "Join Alex Morgan")
    assert len(session.scalars(select(m.Approval)).all()) == 1
    assert gloo.calls == 1


def test_unknown_sensitive_sender_goes_to_human_without_signup(session, clock):
    gloo = SignupGloo(
        {
            "signup": True,
            "first_name": "Alex",
            "last_name": "Morgan",
            "sensitive": False,
        }
    )
    assert (
        request_signup(
            session,
            clock,
            gloo,
            "+15555550199",
            "Join Alex Morgan. My dad is in hospital.",
        )
        == "escalated_sensitive"
    )
    assert session.scalar(select(m.Approval)) is None
    assert session.scalar(select(m.Escalation)).category == "sensitive"
    assert session.scalar(select(m.Message)) is None


def test_signup_control_words_never_call_ai(session, clock):
    gloo = SignupGloo({})
    assert request_signup(session, clock, gloo, "+15555550199", "STOP") is None
    assert gloo.calls == 0


def test_signup_incomplete_name_is_not_invented(session, clock):
    gloo = SignupGloo({"signup": True, "first_name": "Alex", "last_name": ""})
    assert request_signup(session, clock, gloo, "+15555550199", "Join Alex") is None
    assert session.scalar(select(m.Approval)) is None


def test_unverified_or_unlisted_users_cannot_be_admins():
    settings = Settings(admin_email_allowlist="coordinator@example.test")
    for user in (
        {"email": "coordinator@example.test"},
        {"email": "outsider@example.test", "email_confirmed_at": "2026-10-01"},
    ):
        with pytest.raises(Exception) as error:
            check_user(user, settings)
        assert error.value.status_code == 403


def test_texty_login_fails_closed_without_supabase():
    app = create_app(Settings(database_url="sqlite://"))
    with TestClient(app) as client:
        assert client.get("/texty").status_code == 200
        assert client.get("/api/config").json()["provider"] == "gloo"
        assert client.get("/api/state").status_code == 503
        assert (
            client.post(
                "/api/login", json={"email": "someone@example.test", "password": "test"}
            ).status_code
            == 503
        )


def test_live_classifier_stops_on_failed_preflight(monkeypatch, capsys):
    from app.llm import classify_samples
    from app.llm.gloo_client import GlooUnavailableError

    class FailedGloo:
        def __init__(self, settings):
            pass

        def create_response(self, **kwargs):
            raise GlooUnavailableError("test authentication failure")

    monkeypatch.setattr(
        classify_samples,
        "get_settings",
        lambda: Settings(gloo_api_key="synthetic-test-only"),
    )
    monkeypatch.setattr(classify_samples, "GlooClient", FailedGloo)
    assert classify_samples.main() == 1
    assert "preflight failed" in capsys.readouterr().out


def test_texty_simulator_uses_mock_even_if_app_provider_is_live():
    app = create_app(Settings(database_url="sqlite://", allow_text_signup=True))
    app.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
    app.state.gloo = SignupGloo(
        {"signup": True, "first_name": "Alex", "last_name": "Morgan"}
    )

    class ForbiddenLiveProvider:
        def send(self, *args, **kwargs):
            raise AssertionError("Simulator called live SMS")

    app.state.provider = ForbiddenLiveProvider()
    with TestClient(app) as client:
        r = client.post(
            "/api/simulate", json={"phone": "+15555550199", "body": "Join Alex Morgan"}
        )
        assert (
            r.status_code == 200 and r.json()["decision"]["intent"] == "signup_pending"
        )
        state = client.get("/api/state").json()
        assert len(state["proposals"]) == 1 and state["volunteers"] == []
        pid = state["proposals"][0]["id"]
        assert client.post(f"/api/proposals/{pid}/approve").status_code == 200
        assert client.post(f"/api/proposals/{pid}/approve").status_code == 409
        volunteer = client.get("/api/state").json()["volunteers"][0]
        assert not volunteer["consent"] and not volunteer["qualified"]
