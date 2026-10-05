"""Bounded cloud transport polling with durable receipts and one-shot claims.

A committed claim is never retried, even after a process dies. The connector
independently reserves the same idempotency key before browser submission.
"""

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial

from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError

from app.agents.fill_agent import FillContext
from app.core import confirmations, offer_windows as offers
from app.core.inbound import handle_inbound
from app.core.notifications import pre_event_delivery_problem
from app.core.message_style import outbound_style_problem
from app.core.cloud_composition import reviewed_composition
from app.core.policies import PolicyStore, in_quiet_hours
from app.core.send_gate import SendGate
from app.db import models as m
from app.integrations.google_voice_client import ConnectorUnavailable, connector_for, verified_health
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim, GoogleVoiceInboundReceipt
from app.integrations.test_sessions import STOP_WORDS
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import parse_inbound
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.integrations import google_voice_policy

_tick_lock = threading.Lock()
PAUSE_KEY = "google_voice:paused"
CURSOR_KEY = "google_voice:cursor"


class _TrackedGloo:
    """Observe failures even when existing domain handlers catch them."""
    def __init__(self, gloo):
        self.gloo = gloo
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.gloo, name)

    def create_response(self, **kwargs):
        try:
            return self.gloo.create_response(**kwargs)
        except GlooUnavailableError:
            self.failed = True
            raise


def is_paused(session):
    value = session.get(m.Policy, PAUSE_KEY)
    return value is None or value.value.get("value") is not False


def set_paused(session, paused):
    row = session.get(m.Policy, PAUSE_KEY)
    if row is None:
        session.add(m.Policy(key=PAUSE_KEY, value={"value": bool(paused)}))
    else:
        row.value = {"value": bool(paused)}


def _clock(state):
    return getattr(state, "google_voice_clock", state.clock)


def get_cloud_status(state, session=None):
    """Read safe cached readiness without contacting Google from public config."""
    if session is None:
        with state.session_factory() as session:
            return get_cloud_status(state, session)
    enabled = isinstance(state.provider, GoogleVoiceProvider) and state.settings.google_voice_enabled
    if isinstance(state.provider, GoogleVoiceProvider) and not google_voice_policy.google_voice_steps_allowed(state.settings):
        return {"state": "policy_hold", "reason_code": google_voice_policy.POLICY_HOLD_CODE,
                "paused": True, "ready": False, "connected": False, "last_checked_at": None}
    paused = is_paused(session)
    cached = getattr(state, "google_voice_status", {})
    fresh = time.monotonic() - cached.get("checked_monotonic", 0) < 90
    connected = enabled and fresh and cached.get("connected") is True
    status = ("disabled" if not enabled else "paused" if paused else
              "live_disabled" if not state.settings.live_sms else
              "gloo_unavailable" if not state.settings.gloo_api_key else
              "ready" if connected else "reconnect_required")
    return {"state": status, "paused": paused,
            "ready": bool(connected and not paused and state.settings.live_sms and state.settings.gloo_api_key),
            "connected": bool(connected), "last_checked_at": cached.get("last_checked_at")}


def _incoming(state, item):
    """Ignore unrelated personal texts; test traffic requires explicit origin."""
    if not isinstance(item, dict):
        raise ConnectorUnavailable("Invalid inbound envelope")
    guid, phone, body = item.get("id"), item.get("phone"), item.get("body")
    if (not isinstance(guid, str) or not 1 <= len(guid) <= 256 or
            not isinstance(phone, str) or not isinstance(body, str) or
            not 1 <= len(body) <= 1700):
        raise ConnectorUnavailable("Invalid inbound envelope")
    selected = state.provider.test_sessions.get(phone)
    if selected is None:
        return True
    try:
        received = datetime.fromisoformat(item["received_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ConnectorUnavailable("Invalid inbound timestamp") from None
    if received.tzinfo is None:
        raise ConnectorUnavailable("Invalid inbound timestamp")
    now = _clock(state).now()
    if received > now + timedelta(minutes=1):
        raise ConnectorUnavailable("Inbound timestamp is in the future")
    marked = body.startswith(selected.prefix)
    normalized = body[len(selected.prefix):] if marked else body
    is_stop = normalized.strip().upper() in STOP_WORDS
    if not is_stop:
        if (not selected.active(now) or not selected.starts_at <= received < selected.expires_at or
                (not marked and not google_voice_policy.google_voice_demo_allowed(state.settings))):
            return True
        if not normalized.strip() or len(normalized) > 1600:
            return True
    else:
        cutoff = selected.starts_at
        if state.settings.google_voice_demo_mode:
            from app.integrations.google_voice_demo import RECIPIENT_KEY
            with state.session_factory() as session:
                registration = session.get(m.Policy, RECIPIENT_KEY + phone)
                if registration:
                    cutoff = datetime.fromisoformat(registration.value.get("first_activation_at", registration.value["session"]["starts_at"]))
        if received < cutoff or received > now + timedelta(minutes=1):
            return True
    body = normalized
    fingerprint = hashlib.sha256((phone + "\0" + selected.id + "\0" + body).encode()).hexdigest()
    with state.session_factory() as session:
        receipt = session.scalar(select(GoogleVoiceInboundReceipt).where(
            GoogleVoiceInboundReceipt.id == guid).with_for_update())
        if receipt:
            if receipt.fingerprint != fingerprint:
                raise ConnectorUnavailable("Inbound identity conflict; reconnect for review")
            if receipt.result.get("state") != "held_gloo":
                return True
            # Lock existing retries as well as first-time GUID inserts. The
            # conditional update provides a write lock on SQLite too.
            taken = session.execute(update(GoogleVoiceInboundReceipt).where(
                GoogleVoiceInboundReceipt.id == guid,
                GoogleVoiceInboundReceipt.result["state"].as_string() == "held_gloo")
                .values(result={"state": "processing", "session_id": selected.id})).rowcount
            if taken != 1:
                session.rollback()
                return True
        else:
            receipt = GoogleVoiceInboundReceipt(id=guid, fingerprint=fingerprint, result={})
            session.add(receipt)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                receipt = session.get(GoogleVoiceInboundReceipt, guid)
                if receipt and receipt.fingerprint == fingerprint:
                    return True
                raise ConnectorUnavailable("Inbound identity conflict; reconnect for review") from None
        if not state.settings.gloo_api_key and not is_stop:
            raise GlooUnavailableError("Gloo is not configured")
        # Existing conversation scoping accepts this session interface. Its
        # outbound prefix is GV, so Mac and Google Voice cannot mix histories.
        session.info["mac_test_session"] = selected
        session.info["conversation_origin"] = "google_voice"
        session.info["google_voice_received_at"] = received
        if is_stop:
            # Consent withdrawal is independent of signup, roster membership,
            # Gloo availability and test expiry. This first bounded cloud mode
            # records the stop without staging another text to the sender.
            session.info["sender_phone"] = phone
            volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
            if volunteer:
                confirmations.authorize_sender_fields(session, volunteer, {"sms_opt_in", "preferences"})
                volunteer.sms_opt_in = False
                if volunteer.preferences.get("consent_pending"):
                    volunteer.preferences = {**volunteer.preferences, "consent_pending": False}
            opted_out = session.get(m.Policy, "sms_opt_out:" + phone)
            if opted_out is None:
                session.add(m.Policy(key="sms_opt_out:" + phone, value={"value": True}))
            else:
                opted_out.value = {"value": True}
            confirmations.suppress_phone(session, phone)
            session.add(m.Message(direction="in", phone=phone, volunteer_id=volunteer.id if volunteer else None,
                body=body, kind="google_voice_test_in", purpose="test:" + selected.id,
                status="received", created_at=now))
            receipt.result = {"intent": "stop", "session_id": selected.id}
            session.commit()
            return True
        tracked_gloo = _TrackedGloo(state.gloo)
        ctx = FillContext(session, _clock(state), state.provider, tracked_gloo)

        def origin(session, _context, _instances):
            for obj in session.new:
                if isinstance(obj, m.Escalation):
                    obj.related_ids = {**(obj.related_ids or {}), "transport": "google_voice",
                                       "phone": phone, "session_id": selected.id}
                if isinstance(obj, m.Approval) and obj.kind not in {"confirm_text", "confirm_record"}:
                    obj.payload = {**(obj.payload or {}), "transport": "google_voice"}
        event.listen(session, "before_flush", origin)
        try:
            result = handle_inbound(session, _clock(state), state.provider, phone, body,
                partial(parse_inbound, tracked_gloo), ctx=ctx,
                allow_signup=state.settings.allow_text_signup)
            if tracked_gloo.failed:
                raise GlooUnavailableError("Gloo could not complete inbound processing")
            session.flush()
        finally:
            event.remove(session, "before_flush", origin)
        receipt.result = {"intent": result.routed_to, "session_id": selected.id}
        session.commit()
    return True


def _process_incoming(state, item):
    try:
        return _incoming(state, item)
    except GlooUnavailableError:
        selected = state.provider.test_sessions.get(item["phone"])
        if selected is None:
            return True
        body = item["body"]
        if body.startswith(selected.prefix):
            body = body[len(selected.prefix):]
        fingerprint = hashlib.sha256((item["phone"] + "\0" + selected.id + "\0" + body).encode()).hexdigest()
        with state.session_factory() as session:
            receipt = session.get(GoogleVoiceInboundReceipt, item["id"])
            if receipt and receipt.fingerprint != fingerprint:
                raise ConnectorUnavailable("Inbound identity conflict") from None
            if receipt is None:
                receipt = GoogleVoiceInboundReceipt(id=item["id"], fingerprint=fingerprint, result={})
                session.add(receipt)
            receipt.result = {"state": "held_gloo", "session_id": selected.id, "incoming": item}
            session.commit()
        return True


def retry_held_inbound(state):
    with state.session_factory() as session:
        held = [(receipt.id, receipt.result) for receipt in session.scalars(
            select(GoogleVoiceInboundReceipt).where(
                GoogleVoiceInboundReceipt.result["state"].as_string() == "held_gloo").limit(20))]
    for receipt_id, result in held:
        item = result["incoming"]
        selected = state.provider.test_sessions.get(item["phone"])
        if selected is None or selected.id != result["session_id"] or not selected.active(_clock(state).now()):
            with state.session_factory() as session:
                receipt = session.get(GoogleVoiceInboundReceipt, receipt_id)
                receipt.result = {**receipt.result, "state": "held_expired_session"}
                session.commit()
            continue
        # A fresh STOP must suppress older held non-control work before Gloo.
        from app.core.consent_controls import START_WORDS
        body = item["body"]
        if body.startswith(selected.prefix):
            body = body[len(selected.prefix):]
        if body.strip().upper() not in STOP_WORDS | START_WORDS:
            with state.session_factory() as session:
                opted_out = session.get(m.Policy, "sms_opt_out:" + item["phone"])
                volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == item["phone"]))
                if ((opted_out and opted_out.value.get("value")) or (volunteer and not volunteer.sms_opt_in)):
                    receipt = session.get(GoogleVoiceInboundReceipt, receipt_id)
                    receipt.result = {**receipt.result, "state": "held_opt_out"}
                    session.commit()
                    continue
        _process_incoming(state, item)


def poll_inbound(state, connector):
    with state.session_factory() as session:
        stored = session.get(m.Policy, CURSOR_KEY)
        cursor = int(stored.value.get("value", 0)) if stored else 0
    batch = connector.inbound(cursor)
    messages = batch.get("messages")
    try:
        new_cursor = int(batch["cursor"])
    except (KeyError, TypeError, ValueError):
        raise ConnectorUnavailable("Invalid inbound cursor") from None
    if not isinstance(messages, list) or len(messages) > 100 or new_cursor < cursor:
        raise ConnectorUnavailable("Invalid inbound batch")
    # Process stop instructions first even when Gloo is unavailable. Any other
    # unprocessed text keeps the cursor in place for a subsequent recovery.
    def stop_first(item):
        if not isinstance(item, dict):
            return 1
        body = str(item.get("body", ""))
        selected = state.provider.test_sessions.get(item.get("phone"))
        if selected and body.startswith(selected.prefix):
            body = body[len(selected.prefix):]
        return 0 if body.strip().upper() in STOP_WORDS else 1
    messages.sort(key=stop_first)
    complete = True
    for item in messages:
        complete = _process_incoming(state, item) and complete
    if not complete:
        return
    with state.session_factory() as session:
        row = session.scalar(select(m.Policy).where(m.Policy.key == CURSOR_KEY).with_for_update())
        if row is None:
            session.add(m.Policy(key=CURSOR_KEY, value={"value": new_cursor}))
        else:
            row.value = {"value": max(int(row.value.get("value", 0)), new_cursor)}
        session.commit()


def _delivery_problem(session, state, row, now, *, claim=False):
    """Recheck current account scope, exact approval and scheduling constraints."""
    selected = state.provider.test_sessions.get(row.phone)
    if selected is not None:
        session.info["mac_test_session"] = selected
    if state.settings.google_voice_demo_mode:
        runtime_id = getattr(state, "google_voice_demo_window_id", None)
        if runtime_id:
            from app.integrations.google_voice_demo_window import WINDOW_KEY
            window = session.get(m.Policy, WINDOW_KEY)
            if (not window or window.value.get("id") != runtime_id or window.value.get("state") != "active" or
                    now >= datetime.fromisoformat(window.value["until"])):
                return "blocked_demo_window"
        from app.integrations.google_voice_demo import demo_text_problem
        if demo_text_problem(session, state.provider, row.phone, row.body, row.purpose, now, message=row):
            return "blocked_consent"
    if outbound_style_problem(row.body):
        return "blocked_style"
    selected = state.provider.test_sessions.get(row.phone)
    if (selected is None or not selected.active(now) or
            not row.provider_sid.startswith(selected.outbound_prefix) or
            not selected.starts_at <= row.created_at < selected.expires_at):
        return "blocked_test_session"
    if not timedelta(0) <= now - row.created_at <= timedelta(seconds=state.settings.google_voice_max_queue_age_seconds):
        return "blocked_stale"
    if not confirmations.enabled(session):
        return "blocked_confirmation"
    approval = confirmations.proof_for(session, row)
    if approval is None or confirmations.delivery_problem(session, state.provider, approval, now, row):
        return "blocked_confirmation"
    from app.integrations.google_voice_signup import automatic_delivery_problem
    if automatic_delivery_problem(session, state, approval):
        return "blocked_signup_authorization"
    if not reviewed_composition(session, approval, selected):
        return "blocked_gloo"
    volunteer = session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else None
    if (volunteer and volunteer.preferences.get("admin_text_owner") and volunteer.status != "active"):
        return "blocked_eligibility"
    notification = session.scalar(select(m.Notification).where(m.Notification.message_id == row.id,
        m.Notification.purpose != "human_review"))
    if pre_event_delivery_problem(session, notification, now):
        if notification:
            notification.state = "expired"
        return "superseded"
    policies = PolicyStore(session)
    start, end = policies.urgent_quiet_hours() if approval.payload.get("urgent") else policies.quiet_hours()
    gate = SendGate(session, _clock(state), state.provider, approval.payload.get("reply_to_message_id"))
    if (row.purpose != "stop_confirm" and in_quiet_hours(now.astimezone(policies.church_tz()), start, end)
            and not gate._immediate_reply(row.phone, row.purpose, now)):
        return "blocked_quiet_hours"
    if row.purpose == "outreach":
        outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
        if outreach is None:
            return "blocked_eligibility"
        outreach = offers.lock(session, outreach)
        error = offers.dispatch(session, outreach, row, now, exact=True, claim=claim)
        if error:
            return "blocked_confirmation"
    return None


def _submission_deadline(session, state, row, now):
    """Carry the earliest changing time boundary all the way to the click."""
    approval = confirmations.proof_for(session, row)
    selected = state.provider.test_sessions[row.phone]
    deadlines = [now + timedelta(seconds=30), selected.expires_at,
                 datetime.fromisoformat(approval.payload["expires_at"]),
                 row.created_at + timedelta(seconds=state.settings.google_voice_max_queue_age_seconds)]
    if getattr(state, "google_voice_demo_window_id", None):
        from app.integrations.google_voice_demo_window import WINDOW_KEY
        window = session.get(m.Policy, WINDOW_KEY)
        if window:
            deadlines.append(datetime.fromisoformat(window.value["until"]))
    policies = PolicyStore(session)
    gate = SendGate(session, _clock(state), state.provider, approval.payload.get("reply_to_message_id"))
    immediate = gate._immediate_reply(row.phone, row.purpose, now)
    if immediate:
        incoming = session.get(m.Message, gate.reply_to_message_id)
        deadlines.append(incoming.created_at + timedelta(minutes=10))
    if row.purpose != "stop_confirm" and not immediate:
        start, _end = policies.urgent_quiet_hours() if approval.payload.get("urgent") else policies.quiet_hours()
        local = now.astimezone(policies.church_tz())
        quiet_start = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if quiet_start <= local:
            quiet_start += timedelta(days=1)
        deadlines.append(quiet_start)
    if row.purpose == "outreach":
        outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
        meta = offers.metadata(session, outreach) if outreach else None
        if meta and meta.expires_at:
            deadlines.append(meta.expires_at)
    return min(moment.astimezone(timezone.utc) for moment in deadlines).isoformat()


def demo_inbox_fresh(state):
    cached = getattr(state, "google_voice_status", {})
    return bool(cached.get("connected") is True and
                time.monotonic() - cached.get("checked_monotonic", 0) < 90)


def dispatch_outbound(state, connector, *, message_id=None, expected_body_hash=None):
    # Each claim transaction commits before any request can reach the sidecar.
    if not google_voice_policy.google_voice_steps_allowed(state.settings):
        return
    demo = google_voice_policy.google_voice_demo_allowed(state.settings)
    if demo and (message_id is None or expected_body_hash is None or not demo_inbox_fresh(state)):
        return
    if not state.settings.live_sms or not state.settings.gloo_api_key:
        return
    with state.session_factory() as session:
        if session.scalar(select(GoogleVoiceInboundReceipt.id).where(
                GoogleVoiceInboundReceipt.result["state"].as_string() == "held_gloo").limit(1)):
            return
        query = select(m.Message.id).where(m.Message.direction == "out",
            m.Message.status == "queued", m.Message.provider_sid.startswith("GV"))
        if message_id is not None:
            query = query.where(m.Message.id == message_id)
        ids = list(session.scalars(query.order_by(m.Message.id).limit(1 if demo else 20)))
    for message_id in ids:
        now = _clock(state).now()
        with state.session_factory() as session:
            if is_paused(session):
                return
            row = session.scalar(select(m.Message).where(m.Message.id == message_id)
                .with_for_update(skip_locked=True))
            if row is None or row.status != "queued":
                continue
            if demo and hashlib.sha256(row.body.encode()).hexdigest() != expected_body_hash:
                return
            problem = _delivery_problem(session, state, row, now, claim=True)
            if problem:
                row.status = problem
                session.commit()
                continue
            if demo:
                from app.integrations.google_voice_demo_window import reserve_submission_budget
                if not reserve_submission_budget(session, state):
                    return
            # Conditional update also protects SQLite, whose FOR UPDATE is inert.
            claimed = session.execute(update(m.Message).where(m.Message.id == row.id,
                m.Message.status == "queued").values(status="dispatching")).rowcount
            if claimed != 1:
                session.rollback()
                continue
            session.add(GoogleVoiceDeliveryClaim(message_id=row.id,
                idempotency_key=row.provider_sid, created_at=now))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
        with state.session_factory() as session:
            row = session.scalar(select(m.Message).where(m.Message.id == message_id).with_for_update())
            if row.status != "dispatching":
                continue
            problem = "blocked_paused" if is_paused(session) else _delivery_problem(session, state, row, _clock(state).now())
            if problem:
                row.status = problem
                session.commit()
                continue
            outgoing = {"idempotency_key": row.provider_sid, "to": row.phone, "body": row.body,
                        "not_after": _submission_deadline(session, state, row, _clock(state).now())}
            session.commit()
        try:
            # Health is checked immediately before every individual submission.
            if not verified_health(connector.health(), state.settings, state.provider):
                outcome = "rejected"
            else:
                outcome = connector.prepare(**outgoing)
                if outcome == "prepared":
                    # Browser navigation can take seconds. Recheck mutable
                    # authorization after preparation, immediately before the
                    # connector's sole click. Never refresh the signed deadline
                    # or alter the reviewed body on this delivery claim.
                    with state.session_factory() as session:
                        row = session.scalar(select(m.Message).where(m.Message.id == message_id)
                            .with_for_update())
                        if row is None or row.status != "dispatching":
                            continue
                        now = _clock(state).now()
                        problem = ("blocked_paused" if is_paused(session) else
                                   _delivery_problem(session, state, row, now))
                        if not problem and (row.phone != outgoing["to"] or row.body != outgoing["body"] or
                                            row.provider_sid != outgoing["idempotency_key"]):
                            problem = "blocked_confirmation"
                        if not problem and now >= datetime.fromisoformat(outgoing["not_after"]):
                            problem = "blocked_stale"
                        if problem:
                            row.status = problem
                            session.commit()
                            continue
                        session.commit()
                    outcome = connector.send(**outgoing)
                if outcome not in {"submitted", "rejected", "uncertain"}:
                    outcome = "uncertain"
        except Exception:
            # A timeout may follow actual submission. Never replay this claim.
            outcome = "uncertain"
        with state.session_factory() as session:
            row = session.get(m.Message, message_id)
            if row and row.status == "dispatching":
                row.status = outcome
            if row and outcome == "submitted" and demo:
                from app.integrations.google_voice_demo import sender_fingerprint
                key = "google-demo-submission:" + str(row.id)
                if session.get(m.Notification, key) is None:
                    selected = state.provider.test_sessions[row.phone]
                    session.add(m.Notification(key=key, purpose="human_review", body="", state="submitted",
                        due_at=_clock(state).now(), created_at=_clock(state).now(), message_id=row.id,
                        detail={"body_hash": hashlib.sha256(row.body.encode()).hexdigest(), "submitted_at": _clock(state).now().isoformat(),
                            "session_id": selected.id, "sender_fingerprint": sender_fingerprint(state.settings)}))
            if row and row.purpose == "outreach" and outcome != "submitted":
                outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
                meta = offers.metadata(session, outreach) if outreach else None
                if meta:
                    meta.state = "offer_uncertain"
                    fill = session.get(m.FillRequest, outreach.fill_request_id)
                    fill.state, fill.next_action_at = "escalated", None
                    offers.task_once(session, fill, _clock(state).now(),
                        "Google Voice submission needs delivery review before another offer is sent.")
            session.commit()


def tick_google_voice(state):
    """Called by the server scheduler; no user browser or Mac is involved."""
    if state.settings.google_voice_demo_mode or not google_voice_policy.google_voice_automation_allowed():
        return
    if (not isinstance(state.provider, GoogleVoiceProvider) or not state.settings.google_voice_enabled or
            not _tick_lock.acquire(blocking=False)):
        return
    try:
        connector = connector_for(state)
        connected = verified_health(connector.health(), state.settings, state.provider)
        state.google_voice_status = {"connected": connected, "checked_monotonic": time.monotonic(),
            "last_checked_at": _clock(state).now().isoformat()}
        if not connected:
            return
        retry_held_inbound(state)
        poll_inbound(state, connector)
        if state.settings.gloo_api_key:
            dispatch_outbound(state, connector)
    except (ConnectorUnavailable, GlooUnavailableError):
        # Do not log request bodies, authentication material or personal texts.
        state.google_voice_status = {"connected": False, "checked_monotonic": time.monotonic(),
            "last_checked_at": _clock(state).now().isoformat()}
        return
    finally:
        _tick_lock.release()
