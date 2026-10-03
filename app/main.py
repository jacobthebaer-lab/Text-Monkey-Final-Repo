"""FastAPI app factory.

Shared state on app.state: settings, clock, SMS provider, Gloo client, and
the DB engine/session factory. In demo mode the clock is a FakeClock pinned
to the seed anchor so the seeded data, demo controls, and evals line up; in
real mode APScheduler ticks the fill-request timers every 30 seconds.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.clock import Clock, FakeClock, RealClock
from app.config import Settings, get_settings
from app.db.seed import SEED_ANCHOR
from app.db.session import init_db, make_engine, make_session_factory
from app.llm.gloo_client import build_gloo
from app.sms.provider import get_provider
from app.integrations import mac_models  # register additive transport tables

APP_NAME = "ServFrictionless"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    clock: Clock
    if settings.demo_mode:
        # Pinned to the seed anchor: the demo controls fast-forward from here.
        clock = FakeClock(SEED_ANCHOR, timezone=settings.church_timezone)
    else:
        clock = RealClock(settings.church_timezone)

    engine = make_engine(settings.database_url)
    init_db(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if not settings.demo_mode:
            from apscheduler.schedulers.background import BackgroundScheduler

            from app.agents.fill_agent import FillContext
            from app.jobs import process_jobs

            def tick() -> None:
                with app.state.session_factory() as session:
                    process_jobs(
                        FillContext(session, app.state.clock, app.state.provider, app.state.gloo)
                    )
                    session.commit()

            scheduler = BackgroundScheduler()
            scheduler.add_job(tick, "interval", seconds=30, id="fill_tick")
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title=APP_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.provider = get_provider(settings)
    app.state.gloo = build_gloo(settings)
    app.state.mac_delivery_clock = RealClock(settings.church_timezone)

    @app.get("/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "app": APP_NAME,
            "demo_mode": settings.demo_mode,
            "sms_provider": settings.sms_provider,
            "time": app.state.clock.now().isoformat(),
        }

    from app.web.routes import router as web_router
    from app.web.webhook import router as webhook_router
    from app.web.texty import router as texty_router

    app.include_router(webhook_router)  # Twilio-signed, outside admin auth
    app.include_router(texty_router)
    app.include_router(web_router)
    from app.web.mac_messages import router as mac_router

    app.include_router(mac_router)
    from app.web.operations import router as operations_router
    app.include_router(operations_router)
    return app


app = create_app()
