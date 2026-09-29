"""FastAPI app and routes.

Routes are added phase by phase (see PLAN.md section 20). Phase 0 only wires
up the app factory, settings, the clock, and a health check.
"""

from fastapi import FastAPI

from app.clock import Clock, FakeClock, RealClock
from app.config import Settings, get_settings

APP_NAME = "ServFrictionless"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    clock: Clock
    if settings.demo_mode:
        # Demo mode starts on real time but can be fast-forwarded from the
        # demo controls (Phase 5).
        clock = FakeClock(
            RealClock(settings.church_timezone).now(),
            timezone=settings.church_timezone,
        )
    else:
        clock = RealClock(settings.church_timezone)

    app = FastAPI(title=APP_NAME)
    app.state.settings = settings
    app.state.clock = clock

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "app": APP_NAME,
            "demo_mode": settings.demo_mode,
            "sms_provider": settings.sms_provider,
            "time": app.state.clock.now().isoformat(),
        }

    return app


app = create_app()
