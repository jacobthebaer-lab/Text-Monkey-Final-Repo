"""Durable operator-authorized signup conversations, separate from scheduling."""
import hashlib
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.db import models as m
from app.integrations.google_voice_demo import RECIPIENT_KEY, sender_fingerprint, restore_demo_scope
from app.integrations.google_voice_policy import google_voice_demo_allowed
from app.integrations.google_voice_client import connector_for, verified_identity, verified_health
from app.integrations.google_voice_runtime import (_tick_lock, _clock, poll_inbound, retry_held_inbound,
    dispatch_outbound, is_paused, set_paused)

KEY = "google_voice:signup_authorization"


def signup_status(state):
    available = bool(getattr(state.settings, "google_voice_signup_enabled", False) and
                     google_voice_demo_allowed(state.settings))
    with state.session_factory() as session:
        row = session.get(m.Policy, KEY)
        saved = row.value if row else {}
    enabled = bool(available and saved.get("enabled") is True and
                   saved.get("sender_fingerprint") == sender_fingerprint(state.settings))
    return {"available": available, "enabled": enabled,
            "active": enabled and saved.get("state") == "enabled" and
                      bool(getattr(state, "google_voice_signup_scheduler", None)),
            "state": saved.get("state", "off") if enabled else "off",
            "reason": saved.get("reason", ""), "automatic_signup_only": True}


def stop_service(state):
    scheduler = getattr(state, "google_voice_signup_scheduler", None)
    state.google_voice_signup_scheduler = None
    if scheduler:
        from apscheduler.schedulers import SchedulerNotRunningError
        try:
            scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            pass


def hold_signup(state, reason):
    stop_service(state)
    with state.session_factory() as session:
        row = session.get(m.Policy, KEY)
        if row and row.value.get("enabled"):
            row.value = {**row.value, "state": "held", "reason": reason}
            session.commit()


def start_service(state):
    status = signup_status(state)
    if (not status["enabled"] or status["state"] != "enabled" or
            not state.settings.live_sms or not state.settings.gloo_api_key or
            getattr(state, "google_voice_signup_scheduler", None)):
        return
    if unresolved_signup(state):
        hold_signup(state, "Uncertain signup submission requires delivery review, never a retry")
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(tick_signup, "interval", seconds=15, args=[state],
        id="registered_signup", max_instances=1, coalesce=True)
    state.google_voice_signup_scheduler = scheduler
    try:
        scheduler.start()
    except Exception:
        hold_signup(state, "Cloud signup worker requires attention")
        raise HTTPException(503, "Cloud signup worker could not start.") from None


def set_enabled(state, actor, enabled):
    if not signup_status(state)["available"]:
        raise HTTPException(409, "Continuous cloud signup is not configured.")
    restore_demo_scope(state)
    if enabled:
        if not state.settings.gloo_api_key:
            raise HTTPException(409, "Gloo connection requires attention.")
        if not verified_identity(connector_for(state).health(), state.settings, state.provider):
            raise HTTPException(409, "Verify the cloud sender sign-in first.")
    stop_service(state)
    now = _clock(state).now()
    with state.session_factory() as session:
        value = {"id": uuid4().hex, "enabled": enabled, "state": "enabled" if enabled else "off",
            "actor": actor, "at": now.isoformat(), "sender_fingerprint": sender_fingerprint(state.settings),
            "scope": "one initial name invitation and actual inbound signup replies to admin-registered participants"}
        row = session.get(m.Policy, KEY)
        if row:
            row.value = value
        else:
            session.add(m.Policy(key=KEY, value=value))
        set_paused(session, not enabled)
        session.commit()
    if enabled:
        start_service(state)


def automatic_authority(session, state, approval):
    """Exact body authority comes from recorded operator scope, not human review."""
    from app.core import confirmations
    from app.core.cloud_composition import reviewed_composition
    p = approval.payload
    policy = session.get(m.Policy, KEY)
    registration = session.get(m.Policy, RECIPIENT_KEY + str(p.get("phone", "")))
    selected = state.provider.test_sessions.get(p.get("phone"))
    optout = session.get(m.Policy, "sms_opt_out:" + str(p.get("phone", "")))
    if (not policy or policy.value.get("enabled") is not True or policy.value.get("state") != "enabled" or
            policy.value.get("sender_fingerprint") != sender_fingerprint(state.settings) or
            not registration or registration.value.get("state") != "active" or
            registration.value.get("sender_fingerprint") != sender_fingerprint(state.settings) or
            (optout and optout.value.get("value")) or
            not selected or not selected.continuous or not selected.active(_clock(state).now()) or
            not getattr(state.settings, "google_voice_signup_enabled", False) or
            p.get("transport") != "google_voice" or p.get("purpose") != "signup_reply" or
            approval.status != "pending" or not confirmations.valid(approval, _clock(state).now()) or
            not reviewed_composition(session, approval, selected)):
        return None
    authority = registration.value.get("signup_authority", {})
    if not authority.get("actor") or authority.get("sender_fingerprint") != sender_fingerprint(state.settings):
        return None
    invitation = registration.value.get("invitation", {})
    if (invitation.get("approval_id") == approval.id and
            invitation.get("body_hash") == hashlib.sha256(p["body"].encode()).hexdigest()):
        return authority
    incoming = session.get(m.Message, p.get("reply_to_message_id")) if p.get("reply_to_message_id") else None
    from app.core.consent_controls import control_action
    if (incoming and incoming.phone == p["phone"] and incoming.direction == "in" and
            control_action(incoming.body) is None and
            incoming.status == "received" and incoming.kind == "google_voice_test_in" and
            incoming.purpose == "test:" + selected.id and incoming.created_at >= selected.starts_at and
            incoming.created_at <= _clock(state).now()):
        return authority
    return None


def automatic_delivery_problem(session, state, approval):
    if approval.via != "signup_authorization":
        return None
    policy = session.get(m.Policy, KEY)
    registration = session.get(m.Policy, RECIPIENT_KEY + approval.payload["phone"])
    receipt = session.get(m.Notification, "google-signup-authority:" + str(approval.id))
    selected = state.provider.test_sessions.get(approval.payload["phone"])
    if (not getattr(state.settings, "google_voice_signup_enabled", False) or not policy or
            policy.value.get("enabled") is not True or policy.value.get("state") != "enabled" or
            policy.value.get("sender_fingerprint") != sender_fingerprint(state.settings) or
            not registration or registration.value.get("state") != "active" or
            not selected or not selected.continuous or not receipt or receipt.state != "authorized" or
            receipt.detail.get("authorization_id") != policy.value.get("id") or
            receipt.detail.get("session_id") != selected.id or
            receipt.detail.get("reply_to_message_id") != approval.payload.get("reply_to_message_id") or
            receipt.detail.get("body_hash") != hashlib.sha256(approval.payload["body"].encode()).hexdigest() or
            receipt.detail.get("operator") != registration.value.get("signup_authority", {}).get("actor") or
            receipt.detail.get("sender_fingerprint") != sender_fingerprint(state.settings)):
        return "Automatic signup authority changed or is held"
    return None


def authorize_pending(session, state):
    from app.core import confirmations
    from app.core.send_gate import SendGate
    for approval in session.scalars(select(m.Approval).where(m.Approval.kind == "confirm_text",
            m.Approval.status == "pending", m.Approval.payload["transport"].as_string() == "google_voice").order_by(m.Approval.id)):
        authority = automatic_authority(session, state, approval)
        if not authority:
            continue
        gate = SendGate(session, _clock(state), state.provider)
        gate.gloo = state.gloo
        confirmations.decide(session, gate, approval, approve=True,
            actor="signup authorization by " + authority["actor"], expected=approval.payload["content_hash"], now=_clock(state).now())
        approval.via = "signup_authorization"
        session.add(m.Notification(key="google-signup-authority:" + str(approval.id), purpose="signup_authorization",
            state="authorized", body="", due_at=_clock(state).now(), created_at=_clock(state).now(),
            detail={"operator": authority["actor"], "registration_at": authority["at"],
                "authorization_id": session.get(m.Policy, KEY).value["id"],
                "sender_fingerprint": sender_fingerprint(state.settings),
                "body_hash": hashlib.sha256(approval.payload["body"].encode()).hexdigest(),
                "reply_to_message_id": approval.payload.get("reply_to_message_id"), "session_id": approval.payload["session_id"]}))


def unresolved_signup(state):
    from app.core import confirmations
    with state.session_factory() as session:
        for row in session.scalars(select(m.Message).where(m.Message.direction == "out",
                m.Message.purpose == "signup_reply", m.Message.status.in_(("dispatching", "uncertain")),
                m.Message.provider_sid.startswith("GV"))):
            approval = confirmations.proof_for(session, row)
            if approval and approval.via == "signup_authorization":
                return True
    return False


def tick_signup(state):
    if not signup_status(state)["enabled"] or signup_status(state)["state"] != "enabled":
        return
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        if unresolved_signup(state):
            hold_signup(state, "Uncertain signup submission requires delivery review, never a retry")
            return
        restore_demo_scope(state)
        phones = [phone for phone, spec in state.provider.test_sessions.items() if spec.continuous]
        if not phones:
            return
        with state.session_factory() as session:
            if is_paused(session):
                hold_signup(state, "Cloud signup paused by operator")
                return
        connector = connector_for(state)
        health = connector.intake(phones=phones)
        if not verified_health(health, state.settings, state.provider):
            state.google_voice_status = {}
            hold_signup(state, "Cloud sign-in or participant conversation requires attention")
            return
        state.google_voice_status = {"connected": True, "checked_monotonic": time.monotonic(),
            "last_checked_at": _clock(state).now().isoformat()}
        poll_inbound(state, connector)
        retry_held_inbound(state)
        from app.core.policies import PolicyStore, in_quiet_hours
        with state.session_factory() as session:
            policies = PolicyStore(session)
            if in_quiet_hours(_clock(state).now().astimezone(policies.church_tz()), *policies.quiet_hours()):
                return
            registrations = list(session.scalars(select(m.Policy).where(m.Policy.key.startswith(RECIPIENT_KEY))))
            suppressed = {row.key.removeprefix("sms_opt_out:") for row in
                session.scalars(select(m.Policy).where(m.Policy.key.startswith("sms_opt_out:"))) if row.value.get("value")}
        from app.web.google_voice import compose_demo_text
        for row in registrations:
            if (row.value["phone"] in phones and row.value["phone"] not in suppressed and not row.value.get("invitation") and
                    row.value.get("consent_state") == "awaiting_name"):
                compose_demo_text(state, row.value["signup_authority"]["actor"], row.value["phone"], "Initial name invitation")
        with state.session_factory() as session:
            from app.integrations.google_voice_models import GoogleVoiceInboundReceipt
            if session.scalar(select(GoogleVoiceInboundReceipt.id).where(
                    GoogleVoiceInboundReceipt.result["state"].as_string() == "held_gloo").limit(1)):
                hold_signup(state, "Gloo connection requires attention; signup replies are held")
                return
            authorize_pending(session, state)
            session.commit()
            # Only messages carrying this explicit automatic signup authority,
            # never unrelated reviewed manual texts or broad scheduler messages.
            from app.core import confirmations
            row = None
            for candidate in session.scalars(select(m.Message).where(m.Message.direction == "out",
                    m.Message.status == "queued", m.Message.purpose == "signup_reply", m.Message.phone.in_(phones)).order_by(m.Message.id)):
                approval = confirmations.proof_for(session, candidate)
                if approval and approval.via == "signup_authorization" and session.get(m.Notification, "google-signup-authority:" + str(approval.id)):
                    row = candidate
                    break
            selected_id = row.id if row else None
            body_hash = hashlib.sha256(row.body.encode()).hexdigest() if row else None
        if selected_id:
            dispatch_outbound(state, connector, message_id=selected_id, expected_body_hash=body_hash)
            with state.session_factory() as session:
                result = session.get(m.Message, selected_id).status
            if result in {"uncertain", "dispatching"}:
                hold_signup(state, "Uncertain submission requires delivery review, never a retry")
    except Exception:
        state.google_voice_status = {}
        hold_signup(state, "Gloo or cloud conversation connection requires attention; nothing is retried automatically")
    finally:
        _tick_lock.release()
