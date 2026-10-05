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
    if unresolved_google_voice_submissions(state):
        hold_signup(state, "Uncertain Google Voice submission requires delivery review, never a retry")
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


def refresh_unsent_invitations(session, state):
    """Recompose only invitations with durable proof of no browser submission."""
    from app.core import confirmations
    policy = session.get(m.Policy, KEY)
    if not policy or policy.value.get('state') != 'enabled':
        return
    now = _clock(state).now()
    for registration in session.scalars(select(m.Policy).where(m.Policy.key.startswith(RECIPIENT_KEY))):
        value = registration.value
        invitation = value.get('invitation', {})
        selected = state.provider.test_sessions.get(value['phone'])
        opted_out = session.get(m.Policy, 'sms_opt_out:' + value['phone'])
        if ((opted_out and opted_out.value.get('value')) or value.get('state') != 'active' or value.get('consent_state') != 'awaiting_name' or
                not selected or not selected.continuous or not invitation):
            continue
        approval = session.get(m.Approval, invitation.get('approval_id'))
        if (not approval or approval.status not in {'pending', 'approved', 'expired'} or
                approval.payload.get('transport') != 'google_voice' or
                approval.payload.get('purpose') != 'signup_reply' or
                approval.payload.get('phone') != value['phone'] or
                approval.payload.get('session_id') != selected.id or
                approval.payload.get('content_hash') != invitation.get('content_hash') or
                hashlib.sha256(approval.payload['body'].encode()).hexdigest() != invitation.get('body_hash')):
            continue
        receipt = session.get(m.Notification, 'google-signup-authority:' + str(approval.id))
        old_epoch = receipt.detail.get('authorization_id') if receipt else None
        if confirmations.valid(approval, now) and (not receipt or old_epoch == policy.value['id']):
            continue
        if not retire_unsent_attempt(session, state, approval, selected, invitation, policy):
            continue
        registration.value = {key: saved for key, saved in value.items() if key != 'invitation'}


def retire_unsent_attempt(session, state, approval, selected, invitation, policy):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    if session.get(m.Notification, 'google-signup-unsent-renewal:' + str(approval.id)):
        return False
    now = _clock(state).now()
    receipt = session.get(m.Notification, 'google-signup-authority:' + str(approval.id))
    old_epoch = receipt.detail.get('authorization_id') if receipt else None
    message_id = approval.payload.get('message_id')
    message = session.get(m.Message, message_id) if message_id else None
    if message_id and (not message or message.direction != 'out' or message.phone != approval.payload['phone'] or
            message.purpose != 'signup_reply' or not message.provider_sid.startswith(selected.outbound_prefix) or
            message.body != invitation['body'] or
            message.status not in {'queued', 'blocked_signup_authorization', 'blocked_stale', 'blocked_confirmation'} or
            session.get(GoogleVoiceDeliveryClaim, message_id)):
        return False
    reservations = [reservation for key in approval.payload.get('conversation', {}).get('keys', [])
        if (reservation := session.get(m.Notification, key)) is not None] if message else []
    if any(item.purpose != 'conversation_delivery' or item.message_id != message_id for item in reservations):
        return False
    # Retain the original exact body, hash, approval, session nonce and epoch
    # in an immutable audit record. Gloo produces a new composition/review
    # record under the current authority; never mutate the old reviewed body.
    session.add(m.Notification(key='google-signup-unsent-renewal:' + str(approval.id),
        purpose='signup_authorization', state='unsent_recomposition', body='',
        due_at=now, created_at=now, message_id=message_id,
        detail={'invitation': dict(invitation), 'session_id': selected.id,
            'old_authorization_id': old_epoch, 'new_authorization_id': policy.value['id'],
            'conversation_reservations': [{'key': item.key, 'state': item.state,
                'message_id': item.message_id, 'detail': dict(item.detail)} for item in reservations],
            'operator': policy.value['actor'], 'reason': 'No browser submission claim; fresh Gloo composition required'}))
    approval.status = 'expired'
    if message:
        message.status = 'superseded'
        for reservation in reservations:
            session.delete(reservation)
    return True


def refresh_unsent_signup_replies(session, state):
    """Refresh only still-needed intake questions bound to actual sender input."""
    from app.core import confirmations, outbound_conversation
    from app.core.cloud_composition import reviewed_composition
    from app.core.consent_controls import control_action
    from app.core.send_gate import SendGate
    from app.core.signup_responder import compose_signup_reply
    from app.core.signup_recovery import privacy_hold
    policy = session.get(m.Policy, KEY)
    now = _clock(state).now()
    if not policy or policy.value.get('state') != 'enabled':
        return
    approvals = list(session.scalars(select(m.Approval).where(m.Approval.kind == 'confirm_text',
        m.Approval.status.in_(('pending', 'approved', 'expired')),
        m.Approval.payload['transport'].as_string() == 'google_voice',
        m.Approval.payload['purpose'].as_string() == 'signup_reply')))
    for approval in approvals:
        p = approval.payload
        selected = state.provider.test_sessions.get(p.get('phone'))
        registration = session.get(m.Policy, RECIPIENT_KEY + str(p.get('phone', '')))
        receipt = session.get(m.Notification, 'google-signup-authority:' + str(approval.id))
        opted_out = session.get(m.Policy, 'sms_opt_out:' + str(p.get('phone', '')))
        if (not selected or not selected.continuous or not selected.active(now) or
                not registration or registration.value.get('state') != 'active' or
                registration.value.get('sender_fingerprint') != sender_fingerprint(state.settings) or
                (opted_out and opted_out.value.get('value')) or
                p.get('session_id') != selected.id or
                not confirmations.valid(approval, approval.requested_at) or
                not reviewed_composition(session, approval, selected) or
                (confirmations.valid(approval, now) and (not receipt or
                    receipt.detail.get('authorization_id') == policy.value['id']))):
            continue
        incoming = session.get(m.Message, p.get('reply_to_message_id')) if p.get('reply_to_message_id') else None
        if (not incoming or incoming.direction != 'in' or incoming.status != 'received' or
                incoming.kind != 'google_voice_test_in' or incoming.phone != p['phone'] or
                incoming.purpose != 'test:' + selected.id or control_action(incoming.body) is not None or
                not selected.starts_at <= incoming.created_at <= now):
            continue
        volunteer = session.get(m.Volunteer, p.get('volunteer_id')) if p.get('volunteer_id') else None
        if (volunteer and (volunteer.phone != p['phone'] or not volunteer.sms_opt_in)) or privacy_hold(session, p['phone'], volunteer):
            continue
        session.info['mac_test_session'] = selected
        supplied = p.get('conversation', {})
        fresh, problem = outbound_conversation.metadata(session, purpose='signup_reply', volunteer=volunteer,
            phone=p['phone'], now=now, supplied=supplied, reply_id=incoming.id)
        if problem or fresh != supplied:
            continue  # A newer reply or completed field has made this question obsolete.
        old = {'approval_id': approval.id, 'body': p['body'],
            'body_hash': hashlib.sha256(p['body'].encode()).hexdigest(), 'content_hash': p['content_hash'],
            'reply_to_message_id': incoming.id}
        if not retire_unsent_attempt(session, state, approval, selected, old, policy):
            continue
        session.flush()
        session.info['conversation_origin'] = 'google_voice'
        body = compose_signup_reply(session, _clock(state), state.gloo, p['body'],
            volunteer=volunteer, phone=p['phone'], signup_conversation=True, require_gloo=True, exact_copy=True,
            signup_source={'message_id': incoming.id, 'body': incoming.body, 'session_id': selected.id,
                'missing_fields': fresh['intake_fields'],
                'current_stage': volunteer.preferences.get('onboarding_stage') if volunteer else 'name'})
        gate = SendGate(session, _clock(state), state.provider)
        gate.gloo = state.gloo
        gate.reply_to_message_id = incoming.id
        gate.send(body=body, purpose='signup_reply', volunteer=volunteer, phone=p['phone'], kind='ai', conversation=fresh)


def unsent_recomposition_proof(session, message, selected):
    from app.core import confirmations
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    approval = confirmations.proof_for(session, message)
    audit = session.get(m.Notification, 'google-signup-unsent-renewal:' + str(approval.id)) if approval else None
    old = audit.detail.get('invitation', {}) if audit else {}
    return bool(message.status == 'superseded' and selected and
        not session.get(GoogleVoiceDeliveryClaim, message.id) and approval and approval.status == 'expired' and
        audit and audit.state == 'unsent_recomposition' and audit.message_id == message.id and
        audit.detail.get('session_id') == selected.id and old.get('approval_id') == approval.id and
        old.get('content_hash') == approval.payload.get('content_hash') and
        old.get('body_hash') == hashlib.sha256(message.body.encode()).hexdigest() and old.get('body') == message.body)


def unresolved_google_voice_submissions(state):
    # Manual and automatic texts share the same private Google transport.
    # A restart or intake must never release another conversation while any
    # durable browser submission still has an unknown outcome.
    with state.session_factory() as session:
        return session.scalar(select(m.Message.id).where(m.Message.direction == "out",
            m.Message.status.in_(("dispatching", "uncertain")),
            m.Message.provider_sid.startswith("GV")).limit(1)) is not None


def tick_signup(state):
    if not signup_status(state)["enabled"] or signup_status(state)["state"] != "enabled":
        return
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        if unresolved_google_voice_submissions(state):
            hold_signup(state, "Uncertain Google Voice submission requires delivery review, never a retry")
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
            refresh_unsent_invitations(session, state)
            refresh_unsent_signup_replies(session, state)
            session.commit()
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
