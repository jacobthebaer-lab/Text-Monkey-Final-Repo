"""Twilio webhook: signature validation and routing. No real Twilio calls."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.config import Settings
from app.db import models as m
from app.main import create_app

AUTH_TOKEN = "test_auth_token_123"
PUBLIC = "https://demo.ngrok-free.app"


@pytest.fixture
def app(tmp_path):
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/hook.db",
            twilio_auth_token=AUTH_TOKEN,
            public_base_url=PUBLIC,
            demo_mode=True,
        )
    )
    with app.state.session_factory() as session:
        from datetime import timedelta

        from app.db.seed import SEED_ANCHOR

        session.add(
            m.Volunteer(
                name="Hook Tester", phone="+19995550001", sms_opt_in=True, status="active",
                preferences={}, created_at=SEED_ANCHOR - timedelta(days=100),
            )
        )
        session.commit()
    return app


def signed_post(client, form: dict, *, signature: str | None = None):
    url = PUBLIC + "/sms/inbound"
    sig = signature if signature is not None else RequestValidator(AUTH_TOKEN).compute_signature(url, form)
    return client.post("/sms/inbound", data=form, headers={"X-Twilio-Signature": sig})


def test_valid_signature_routes_message(app):
    with TestClient(app) as client:
        resp = signed_post(client, {"From": "+19995550001", "Body": "STOP"})
    assert resp.status_code == 200
    assert "<Response>" in resp.text
    with app.state.session_factory() as session:
        vol = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == "+19995550001"))
        assert vol.sms_opt_in is False  # STOP was processed
        inbound = session.scalar(select(m.Message).where(m.Message.direction == "in"))
        assert inbound.body == "STOP"


def test_bad_signature_rejected(app):
    with TestClient(app) as client:
        resp = signed_post(client, {"From": "+19995550001", "Body": "hi"}, signature="wrong")
    assert resp.status_code == 403
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None  # nothing processed


def test_missing_signature_rejected(app):
    with TestClient(app) as client:
        resp = client.post("/sms/inbound", data={"From": "+19995550001", "Body": "hi"})
    assert resp.status_code == 403


def test_unconfigured_twilio_returns_503(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/no.db"))
    with TestClient(app) as client:
        resp = client.post("/sms/inbound", data={"From": "+19995550001", "Body": "hi"})
    assert resp.status_code == 503


def test_webhook_not_behind_admin_auth(tmp_path):
    """Twilio can't do HTTP Basic; the webhook is protected by signatures instead."""
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/auth.db",
            admin_password="secret",
            twilio_auth_token=AUTH_TOKEN,
            public_base_url=PUBLIC,
        )
    )
    form = {"From": "+15550001111", "Body": "hello"}
    with TestClient(app) as client:
        resp = signed_post(client, form)
    assert resp.status_code == 200  # no 401: signature was enough


def test_get_provider_builds_twilio_when_live():
    from app.sms.provider import get_provider
    from app.sms.twilio_provider import TwilioSMSProvider

    live = Settings(
        sms_provider="twilio", live_sms=True,
        twilio_account_sid="ACxxx", twilio_auth_token="token", twilio_from_number="+15550009999",
    )
    assert isinstance(get_provider(live), TwilioSMSProvider)

    from app.sms.mock_provider import MockSMSProvider

    assert isinstance(get_provider(Settings(sms_provider="twilio", live_sms=False)), MockSMSProvider)
    assert isinstance(get_provider(Settings()), MockSMSProvider)


def test_twilio_provider_refuses_incomplete_config():
    from app.sms.twilio_provider import TwilioSMSProvider

    with pytest.raises(RuntimeError):
        TwilioSMSProvider(Settings(sms_provider="twilio", live_sms=True))  # no creds
    with pytest.raises(RuntimeError):
        TwilioSMSProvider(Settings(sms_provider="twilio", live_sms=False))


def test_demo_phone_overlay(tmp_path, monkeypatch):
    import app.db.seed as seed_mod

    (tmp_path / "demo_phones.json").write_text(
        '{"_comment": "x", "Jen Hartley": "+12025550187", "Maria Delgado": "PLACEHOLDER"}'
    )
    monkeypatch.setattr(seed_mod, "REPO_ROOT", tmp_path)

    from app.db.session import make_engine, make_session_factory, reset_db

    engine = make_engine(f"sqlite:///{tmp_path}/overlay.db")
    reset_db(engine)
    with make_session_factory(engine)() as session:
        seed_mod.seed(session)
        jen = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Jen Hartley"))
        maria = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Maria Delgado"))
    assert jen.phone == "+12025550187"  # synthetic overlay phone applied
    assert maria.phone == "+15550100001"  # invalid placeholder skipped
