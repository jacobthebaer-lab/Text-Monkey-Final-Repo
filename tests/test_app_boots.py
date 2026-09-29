from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_healthz():
    app = create_app(Settings(database_url="sqlite://"))
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sms_provider"] == "mock"


def test_demo_mode_uses_fake_clock_pinned_to_seed_anchor():
    from app.clock import FakeClock, RealClock
    from app.db.seed import SEED_ANCHOR

    demo_app = create_app(Settings(demo_mode=True, database_url="sqlite://"))
    assert isinstance(demo_app.state.clock, FakeClock)
    assert demo_app.state.clock.now() == SEED_ANCHOR

    real_app = create_app(Settings(demo_mode=False, database_url="sqlite://"))
    assert isinstance(real_app.state.clock, RealClock)
