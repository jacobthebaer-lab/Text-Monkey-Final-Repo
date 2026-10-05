"""Operator-enabled temporary church workflow, never a production scheduler."""
import hashlib
import time
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from app.db import models as m
from app.integrations.google_voice_client import ConnectorUnavailable, connector_for, verified_health
from app.integrations.google_voice_demo import restore_demo_scope, scope_fingerprint, sender_fingerprint
from app.integrations.google_voice_runtime import (_clock, _tick_lock, set_paused, is_paused,
    poll_inbound, retry_held_inbound, dispatch_outbound)
from app.llm.gloo_client import GlooUnavailableError

WINDOW_KEY = "google_voice:demo_window"


def window_status(state):
    with state.session_factory() as session:
        row = session.get(m.Policy, WINDOW_KEY)
        saved = row.value if row else {}
    runtime_id = getattr(state, "google_voice_demo_window_id", None)
    active = bool(runtime_id and runtime_id == saved.get("id") and saved.get("state") == "active" and
                  _clock(state).now() < datetime.fromisoformat(saved["until"]))
    return {"active": active, "until": saved.get("until"), "submission_budget": saved.get("submission_budget"),
            "reserved_submissions": saved.get("reserved_submissions", 0),
            "reason": saved.get("reason") if runtime_id else "Stopped after restart or manual control"}


def stop_window(state, reason):
    state.google_voice_demo_window_id = None
    scheduler = getattr(state, "google_voice_demo_scheduler", None)
    state.google_voice_demo_scheduler = None
    if scheduler is not None:
        from apscheduler.schedulers import SchedulerNotRunningError
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass
    with state.session_factory() as session:
        row = session.get(m.Policy, WINDOW_KEY)
        if row and row.value.get("state") == "active":
            row.value = {**row.value, "state": "stopped", "reason": reason}
            session.commit()


def start_window(state, actor, minutes, submission_budget):
    restore_demo_scope(state)
    if not state.settings.live_sms or not state.settings.gloo_api_key:
        raise HTTPException(409, "Enable the dedicated demo transport and configure Gloo before starting a church demo window.")
    now = _clock(state).now()
    if not any(spec.active(now) for spec in state.provider.test_sessions.values()):
        raise HTTPException(409, "Register an actively consenting demo participant first.")
    health = connector_for(state).health()
    if health.get("demo_mode") is not True or health.get("scope_fingerprint") != scope_fingerprint(state.provider.test_sessions):
        raise HTTPException(409, "Reconcile the dedicated demo connector's participant scope first.")
    stop_window(state, "Replaced by explicit operator window")
    window_id = uuid4().hex
    with state.session_factory() as session:
        value = {"id": window_id, "state": "active", "actor": actor, "started_at": now.isoformat(),
                 "until": (now + timedelta(minutes=minutes)).isoformat(), "submission_budget": submission_budget,
                 "reserved_submissions": 0, "sender_fingerprint": sender_fingerprint(state.settings)}
        row = session.get(m.Policy, WINDOW_KEY)
        if row is None:
            session.add(m.Policy(key=WINDOW_KEY, value=value))
        else:
            row.value = value
        set_paused(session, False)
        session.commit()
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(tick_demo_window, "interval", seconds=15, args=[state],
                      id="bounded_church_demo", max_instances=1, coalesce=True)
    state.google_voice_demo_window_id = window_id
    state.google_voice_demo_scheduler = scheduler
    try:
        scheduler.start()
    except Exception:
        stop_window(state, "Demo timer could not start")
        raise HTTPException(503, "Demo window could not start. Outgoing work remains inactive.") from None


def reserve_submission_budget(session, state):
    """Same transaction as exact message claim, always before connector preparation."""
    runtime_id = getattr(state, "google_voice_demo_window_id", None)
    if not runtime_id:
        return True  # Explicit manual steps have their own per-message authorization.
    row = session.get(m.Policy, WINDOW_KEY)
    now = _clock(state).now()
    if (not row or row.value.get("id") != runtime_id or row.value.get("state") != "active" or
            now >= datetime.fromisoformat(row.value["until"]) or
            row.value.get("sender_fingerprint") != sender_fingerprint(state.settings) or
            row.value.get("reserved_submissions", 0) >= row.value["submission_budget"]):
        return False
    previous = row.value["reserved_submissions"]
    value = {**row.value, "reserved_submissions": previous + 1}
    taken = session.execute(update(m.Policy).where(m.Policy.key == WINDOW_KEY,
        m.Policy.value["id"].as_string() == runtime_id,
        m.Policy.value["state"].as_string() == "active",
        m.Policy.value["reserved_submissions"].as_integer() == previous).values(value=value)).rowcount
    return taken == 1


def tick_demo_window(state):
    from app.integrations.google_voice_policy import google_voice_demo_allowed
    if not google_voice_demo_allowed(state.settings) or not getattr(state, "google_voice_demo_window_id", None):
        return
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        if not window_status(state)["active"]:
            stop_window(state, "Demo window expired")
            return
        restore_demo_scope(state)
        if not any(spec.active(_clock(state).now()) for spec in state.provider.test_sessions.values()):
            stop_window(state, "All consenting demo sessions expired")
            return
        with state.session_factory() as session:
            if is_paused(session):
                stop_window(state, "Manual sending is paused")
                return
        connector = connector_for(state)
        health = connector.intake()
        if not verified_health(health, state.settings, state.provider):
            state.google_voice_status = {}
            stop_window(state, "Account or participant scope needs reconnection review")
            return
        state.google_voice_status = {"connected": True, "checked_monotonic": time.monotonic(),
            "last_checked_at": _clock(state).now().isoformat()}
        poll_inbound(state, connector)
        retry_held_inbound(state)
        with state.session_factory() as session:
            row = session.scalar(select(m.Message).where(m.Message.direction == "out", m.Message.status == "queued",
                m.Message.provider_sid.startswith("GV")).order_by(m.Message.id).limit(1))
            message_id = row.id if row else None
            body_hash = hashlib.sha256(row.body.encode()).hexdigest() if row else None
        if message_id is not None:
            dispatch_outbound(state, connector, message_id=message_id, expected_body_hash=body_hash)
            with state.session_factory() as session:
                outcome = session.get(m.Message, message_id).status
            if outcome in {"uncertain", "dispatching"}:
                stop_window(state, "Uncertain submission requires manual review")
                return
        status = window_status(state)
        if status["reserved_submissions"] >= status["submission_budget"]:
            stop_window(state, "Configured submission budget reached")
    except Exception:
        # Scheduler exceptions must not serialize private provider or page data.
        state.google_voice_status = {}
        stop_window(state, "Demo connection or Gloo requires operator review")
    finally:
        _tick_lock.release()
