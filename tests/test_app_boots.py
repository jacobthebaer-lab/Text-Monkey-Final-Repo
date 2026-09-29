from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_healthz():
    app = create_app(Settings())
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sms_provider"] == "mock"


def test_demo_mode_uses_fake_clock():
    from app.clock import FakeClock, RealClock

    demo_app = create_app(Settings(demo_mode=True))
    assert isinstance(demo_app.state.clock, FakeClock)

    real_app = create_app(Settings(demo_mode=False))
    assert isinstance(real_app.state.clock, RealClock)
