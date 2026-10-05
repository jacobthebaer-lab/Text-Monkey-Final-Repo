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
from app.integrations import google_voice_models  # register cloud transport receipts
from app.integrations.planning_center import PCOBase, PCOConfig
from app.integrations.planning_center import (
    PCOEventLink, PCOShiftLink, PCODelivery, PCOVolunteerPerson, PCOStaffingLink,
    PCOStaffingIntent, PCOPositionScope, PCOStaffingLease, PCOStaffingPoll,
)
from app.integrations.planning_center_staffing import CONTEXT as PCO_CONTEXT

APP_NAME = "Text Monkey"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not isinstance(settings.competition_confirmation_required, bool):
        raise ValueError("Human confirmation mode must be explicitly boolean")
    if settings.competition_confirmation_required and settings.sms_is_live:
        raise ValueError("Human confirmation mode supports the mock and reviewed Mac connector only; direct live Twilio is disabled")

    clock: Clock
    if settings.demo_mode:
        # Pinned to the seed anchor: the demo controls fast-forward from here.
        clock = FakeClock(SEED_ANCHOR, timezone=settings.church_timezone)
    else:
        clock = RealClock(settings.church_timezone)

    engine = make_engine(settings.database_url)
    init_db(engine)
    if settings.sms_provider == "google_voice":
        google_voice_models.prepare_google_voice_schema(engine)
    # Setup staging has separate metadata: never auto-create new Postgres tables.
    # Production requires review and application of its migration by the owner.
    if settings.database_url.startswith("sqlite"):
        from app.admin_setup.models import SetupBase

        SetupBase.metadata.create_all(engine)
        # Importing optional review models must never migrate feature tables.
        PCOBase.metadata.create_all(engine, tables=[model.__table__ for model in (
            PCOEventLink, PCOShiftLink, PCODelivery, PCOVolunteerPerson, PCOStaffingLink,
            PCOStaffingIntent, PCOPositionScope, PCOStaffingLease, PCOStaffingPoll)])
        from app.integrations.profile_models import ProfileBase
        ProfileBase.metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        pco_enabled = settings.pco_staffing_write_enabled or settings.pco_staffing_poll_enabled
        if not settings.demo_mode and (settings.automation_enabled or pco_enabled or (settings.sms_provider == "google_voice" and not settings.google_voice_demo_mode)):
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
            if settings.automation_enabled:
                scheduler.add_job(tick, "interval", seconds=30, id="fill_tick", max_instances=1, coalesce=True)
            from app.integrations.google_voice_policy import google_voice_automation_allowed
            if settings.sms_provider == "google_voice" and not settings.google_voice_demo_mode and google_voice_automation_allowed():
                from app.integrations.google_voice_runtime import tick_google_voice
                scheduler.add_job(tick_google_voice, "interval", seconds=15, args=[app.state],
                                  id="google_voice_tick", max_instances=1, coalesce=True)
            if pco_enabled:
                from app.jobs import process_pco_staffing
                scheduler.add_job(lambda: process_pco_staffing(app.state.session_factory, settings,
                    app.state.pco_config, app.state.clock), "interval", seconds=60, id="pco_staffing_tick",
                    max_instances=1, coalesce=True)
            scheduler.start()
        yield
        if settings.google_voice_demo_mode:
            from app.integrations.google_voice_demo_window import stop_window
            stop_window(app.state, "Backend stopped; explicit window required after restart")
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title=APP_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.pco_config = PCOConfig.from_env()
    app.state.clock = clock
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    from app.core import confirmations
    session_info = {confirmations.MODE_KEY: settings.competition_confirmation_required}
    if settings.pco_staffing_write_enabled:
        session_info[PCO_CONTEXT] = (settings, app.state.pco_config)
    app.state.session_factory.configure(info=session_info)
    app.state.provider = get_provider(settings)
    app.state.gloo = build_gloo(settings)
    app.state.mac_delivery_clock = RealClock(settings.church_timezone)
    app.state.google_voice_clock = app.state.mac_delivery_clock
    if settings.google_voice_demo_mode:
        from app.integrations.google_voice_demo import restore_demo_scope
        restore_demo_scope(app.state)

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
    from app.web.profile_sync import router as profile_sync_router
    app.include_router(profile_sync_router)
    from app.web.admin_setup import router as setup_router

    app.include_router(setup_router)
    app.include_router(web_router)
    from app.web.mac_messages import router as mac_router

    app.include_router(mac_router)
    from app.web.google_voice import router as google_voice_router

    app.include_router(google_voice_router)
    from app.web.operations import router as operations_router
    from app.web.planning_center import router as pco_router

    app.include_router(operations_router)
    app.include_router(pco_router)
    from app.web.onboarding_copy import router as onboarding_copy_router

    app.include_router(onboarding_copy_router)
    from app.web.planning_workflows import router as planning_workflows_router

    app.include_router(planning_workflows_router)
    from app.web.notification_status import router as notification_status_router

    app.include_router(notification_status_router)
    if settings.pco_review_enabled:
        from app.core.planning_center_held_preview import signing_key
        from app.web.planning_center_reviews import router as pco_review_router
        app.state.pco_review_signing_key = signing_key(settings.pco_review_signing_key_path)
        app.include_router(pco_review_router)
    return app


app = create_app()
