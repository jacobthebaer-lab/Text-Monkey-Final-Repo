"""Durable pending signup registration, scoped to one dedicated demo sender."""
import hashlib
import json
import re
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from fastapi import HTTPException

from app.db import models as m
from app.sms.google_voice_provider import GoogleVoiceTestSession
from app.integrations.google_voice_client import ConnectorUnavailable, connector_for

RECIPIENT_KEY = "google_voice:demo_recipient:"


def scope_fingerprint(sessions):
    def timestamp(value):
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    values = [[phone, spec.id, timestamp(spec.starts_at), timestamp(spec.expires_at)]
              for phone, spec in sorted(sessions.items())]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def sender_fingerprint(settings):
    return hashlib.sha256((settings.google_voice_expected_email.lower() + "\n" + settings.google_voice_expected_number).encode()).hexdigest()


def restore_demo_scope(state):
    if not state.settings.google_voice_demo_mode:
        return
    with state.session_factory() as session:
        records = list(session.scalars(select(m.Policy).where(m.Policy.key.startswith(RECIPIENT_KEY))))
    # Settings-scoped participants remain supported; durable registrations override
    # their own previous session, never another sender's consent.
    from app.integrations.test_sessions import parse_sessions
    from app.sms.mac_provider import demo_phones
    phones = demo_phones(state.settings.google_voice_demo_phones) if state.settings.google_voice_demo_phones else frozenset()
    configured = parse_sessions(state.settings.google_voice_test_sessions, phones)
    sessions = {phone: GoogleVoiceTestSession(spec.id, spec.starts_at, spec.expires_at) for phone, spec in configured.items()}
    expected_sender = sender_fingerprint(state.settings)
    for row in records:
        value = row.value
        if value.get("state") != "active" or value.get("sender_fingerprint") != expected_sender:
            continue
        phone, spec = value["phone"], value["session"]
        sessions[phone] = GoogleVoiceTestSession(spec["id"], datetime.fromisoformat(spec["starts_at"]), datetime.fromisoformat(spec["expires_at"]))
    state.provider.test_sessions = sessions
    state.provider.phones = frozenset(sessions)


def registered_participants(state):
    restore_demo_scope(state)
    now = state.google_voice_clock.now()
    with state.session_factory() as session:
        records = list(session.scalars(select(m.Policy).where(m.Policy.key.startswith(RECIPIENT_KEY))))
        return [{"phone": row.value["phone"], "name": row.value.get("name", "Demo participant"),
                 "state": row.value["state"], "consent_state": row.value.get("consent_state", "awaiting_name"), "expires_at": row.value["session"]["expires_at"],
                 "active": row.value["state"] == "active" and row.value.get("sender_fingerprint") == sender_fingerprint(state.settings) and row.value["phone"] in state.provider.test_sessions and
                           state.provider.test_sessions[row.value["phone"]].active(now)} for row in records]


def register_participant(state, actor, phone, name):
    restore_demo_scope(state)
    now = state.google_voice_clock.now()
    key = RECIPIENT_KEY + phone
    expected_sender = sender_fingerprint(state.settings)
    # Before renewing a previously registered scope, observe only this person's
    # thread and apply any withdrawal that arrived while its old session was off.
    with state.session_factory() as session:
        saved = session.get(m.Policy, key)
        renewal = bool(saved and saved.value.get("state") == "active" and
            datetime.fromisoformat(saved.value["session"]["expires_at"]) <= now)
    if renewal:
        from app.integrations.google_voice_client import verified_health
        from app.integrations.google_voice_runtime import poll_inbound
        try:
            health = connector_for(state).intake(phone)
            if not verified_health(health, state.settings, state.provider):
                raise ConnectorUnavailable("Renewal account/scope not verified")
            poll_inbound(state, connector_for(state))
        except (ConnectorUnavailable, ValueError):
            raise HTTPException(409, "Check this participant's prior scope for withdrawals before renewing. No scope was reset or text sent.") from None
    with state.session_factory() as session:
        opted_out = session.get(m.Policy, "sms_opt_out:" + phone)
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
        if (opted_out and opted_out.value.get("value")) or (volunteer and volunteer.status != "active"):
            raise HTTPException(409, "This participant is held by consent or eligibility rules. Registration cannot clear that hold.")
        row = session.get(m.Policy, key)
        previous = row.value if row else None
        # An explicit retry recovers the same pending registration; it cannot
        # manufacture a different consent time/session after an uncertain commit.
        if (previous and previous.get("sender_fingerprint") == expected_sender and
                datetime.fromisoformat(previous["session"]["expires_at"]) > now):
            value = previous
        else:
            spec = {"id": uuid4().hex, "starts_at": now.isoformat(), "expires_at": (now + timedelta(hours=2)).isoformat()}
            value = {"state": "pending", "phone": phone, "name": name, "sender_fingerprint": expected_sender,
                     "session": spec, "first_activation_at": now.isoformat(), "consent_state": "awaiting_name", "initiation": {"actor": actor,
                     "recorded_at": now.isoformat(), "phone": phone, "sender_fingerprint": expected_sender,
                     "scope": "one exact reviewed name invitation, then authenticated name-reply consent"},
                     "expected_scope": scope_fingerprint(state.provider.test_sessions)}
            if previous and previous.get("sender_fingerprint") == expected_sender:
                value = {**value, **{k: previous[k] for k in ("invitation", "consent", "consent_state", "first_activation_at") if k in previous}}
            if row is None:
                row = m.Policy(key=key, value=value)
                session.add(row)
            else:
                row.value = value
            session.commit()
    spec = value["session"]
    try:
        response = connector_for(state).register_recipient({"phone": phone, **spec, "expected_scope": value["expected_scope"]})
    except (ConnectorUnavailable, ValueError):
        raise HTTPException(503, "Participant registration is pending. No text was sent. Retry this same participant registration to reconcile its scope.") from None
    candidate = {**state.provider.test_sessions, phone: GoogleVoiceTestSession(spec["id"], datetime.fromisoformat(spec["starts_at"]), datetime.fromisoformat(spec["expires_at"]))}
    if response.get("registered") is not True or response.get("scope_fingerprint") != scope_fingerprint(candidate):
        raise HTTPException(409, "Participant scope could not be reconciled. No text was sent.")
    with state.session_factory() as session:
        row = session.get(m.Policy, key)
        if row.value["session"] != spec or row.value.get("sender_fingerprint") != expected_sender:
            raise HTTPException(409, "Participant registration changed. No text was sent.")
        opted_out = session.get(m.Policy, "sms_opt_out:" + phone)
        if opted_out and opted_out.value.get("value"):
            raise HTTPException(409, "Participant withdrew consent. No text was sent.")
        # Registration is pending signup, not opt-in or an imported identity.
        exact = session.get(m.Policy, "signup_exact_copy:" + phone)
        if exact is None:
            session.add(m.Policy(key="signup_exact_copy:" + phone, value={"value": True}))
        else:
            exact.value = {"value": True}
        row.value = {**row.value, "state": "active"}
        session.commit()
    restore_demo_scope(state)
    state.google_voice_status = {}
    return {"phone": phone, "name": value["name"], "expires_at": spec["expires_at"], "registered": True}


def demo_invitation_proof(session, clock, phone, *, reply_message_id, body):
    """An actual scoped name reply must follow this exact submitted disclosure."""
    from app.core import confirmations
    from app.core.conversation import scope
    from app.core.consent_controls import control_action
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    selected = session.info.get("mac_test_session")
    row = session.get(m.Policy, RECIPIENT_KEY + phone)
    if not row or not selected or not selected.active(clock.now()):
        return None
    value = row.value
    invitation = value.get("invitation", {})
    approval = session.get(m.Approval, invitation.get("approval_id"))
    if (not approval or approval.status != "approved" or not confirmations.valid(approval, clock.now()) or
            approval.payload.get("content_hash") != invitation.get("content_hash")):
        return None
    message = session.get(m.Message, approval.payload.get("message_id"))
    claim = session.get(GoogleVoiceDeliveryClaim, message.id) if message else None
    submitted = session.get(m.Notification, "google-demo-submission:" + str(message.id)) if message else None
    if (not message or not claim or not submitted or message.status != "submitted" or
            message.phone != phone or not message.provider_sid.startswith(selected.outbound_prefix) or
            hashlib.sha256(message.body.encode()).hexdigest() != invitation.get("body_hash") or
            submitted.detail.get("body_hash") != invitation.get("body_hash") or
            submitted.detail.get("sender_fingerprint") != value.get("sender_fingerprint") or
            submitted.detail.get("session_id") != selected.id):
        return None
    reply = session.scalar(scope(select(m.Message), selected).where(m.Message.id == reply_message_id,
        m.Message.direction == "in", m.Message.phone == phone, m.Message.body == body, m.Message.status == "received"))
    if not reply:
        return None
    received = session.info.get("google_voice_received_at", reply.created_at)
    submitted_at = datetime.fromisoformat(submitted.detail["submitted_at"])
    if received < submitted_at or received > clock.now() + timedelta(minutes=1):
        return None
    inputs = session.scalars(scope(select(m.Message), selected).where(m.Message.phone == phone,
        m.Message.direction == "in", m.Message.id > message.id, m.Message.id <= reply.id))
    if any(control_action(incoming.body) == "stop" for incoming in inputs):
        return None
    return {"disclosure_message_id": message.id, "reply_message_id": reply.id,
            "consent_at": received.isoformat(), "session_id": selected.id,
            "sender_fingerprint": value["sender_fingerprint"], "disclosure_body_hash": invitation["body_hash"]}


def demo_text_problem(session, provider, phone, body, purpose, now, *, message=None, reply_id=None):
    """Pending signup grants one invitation and sender-initiated name recovery only."""
    if not getattr(provider.settings, "google_voice_demo_mode", False):
        return None
    uncertain = select(m.Message.id).where(m.Message.phone == phone, m.Message.direction == "out",
        m.Message.provider_sid.startswith("GV"), m.Message.status.in_(("uncertain", "dispatching")))
    if message is not None:
        uncertain = uncertain.where(m.Message.id != message.id)
    if session.scalar(uncertain.limit(1)):
        return "Prior uncertain submission requires manual delivery review, never a fresh-key retry"
    row = session.get(m.Policy, RECIPIENT_KEY + phone)
    if not row:
        from app.core.consent_controls import prior_disclosed_consent
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
        return None if volunteer and prior_disclosed_consent(session, volunteer) else "Actual name-reply consent or pending signup registration is required"
    if row.value.get("sender_fingerprint") != sender_fingerprint(provider.settings):
        return "Participant consent belongs to another sender"
    invitation = row.value.get("invitation", {})
    initial = invitation.get("body_hash") == hashlib.sha256(body.encode()).hexdigest()
    if initial:
        if purpose != "signup_reply" or "Text STOP to stop." not in body:
            return "Initial demo invitation changed"
        previous = session.scalar(select(m.Message).where(m.Message.phone == phone, m.Message.direction == "out",
            m.Message.body == body, m.Message.provider_sid.startswith("GV")))
        if previous and (message is None or previous.id != message.id):
            return "Initial demo invitation already queued or submitted"
        return None
    # Reject repeated SMS command instructions, not ordinary volunteering
    # language such as "offering to help" or "stop by the welcome table".
    command = r"\b(?:text|reply|respond|type|send)\s+(?:(?:back|with|the\s+(?:word|keyword))\s+)?[\"'‘’“”]*"
    opt_out = r"(?:STOP(?:ALL)?|UNSUBSCRIBE|OPT[ -]?OUT)\b"
    if (re.search(command + opt_out, body, re.I) or
            re.search(r"\b" + opt_out + r"\s+to\s+(?:stop|unsubscribe|opt[ -]?out|end\s+(?:texts|messages))\b", body, re.I) or
            re.search(command + r"HELP\b(?=\s*(?:[.!;,]|$)|\s+for\s+(?:help|assistance|support|info)\b)", body, re.I)):
        return "Demo command notice belongs in the first invitation only"
    if row.value.get("consent_state") != "name_reply_opted_in":
        if message is not None:
            from app.core import confirmations
            approval = confirmations.proof_for(session, message)
            reply_id = approval.payload.get("reply_to_message_id") if approval else None
        incoming = session.get(m.Message, reply_id) if reply_id else None
        if purpose == "signup_reply" and incoming and demo_invitation_proof(session,
                SimpleNamespace(now=lambda: now), phone,
                reply_message_id=reply_id, body=incoming.body):
            return None
        return "An actual first-and-last-name reply is required before other demo texts"
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    if not volunteer or not registered_consent_provenance(session, volunteer):
        return "Recorded name reply lacks its original submitted invitation proof"
    return None


def registered_consent_provenance(session, volunteer):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    row = session.get(m.Policy, RECIPIENT_KEY + volunteer.phone)
    if not row or row.value.get("consent_state") != "name_reply_opted_in":
        return False
    proof, invitation = row.value.get("consent", {}), row.value.get("invitation", {})
    disclosure = session.get(m.Message, proof.get("disclosure_message_id"))
    reply = session.get(m.Message, proof.get("reply_message_id"))
    submitted = session.get(m.Notification, "google-demo-submission:" + str(disclosure.id)) if disclosure else None
    claim = session.get(GoogleVoiceDeliveryClaim, disclosure.id) if disclosure else None
    if (not disclosure or not reply or not submitted or not claim or disclosure.status != "submitted" or
            disclosure.phone != volunteer.phone or reply.phone != volunteer.phone or reply.direction != "in" or
            reply.kind != "google_voice_test_in" or reply.status != "received" or
            reply.purpose != "test:" + str(proof.get("session_id")) or
            not disclosure.provider_sid.startswith("GV" + str(proof.get("session_id")) + ":") or
            hashlib.sha256(disclosure.body.encode()).hexdigest() != invitation.get("body_hash") or
            submitted.detail.get("body_hash") != invitation.get("body_hash") or
            submitted.detail.get("sender_fingerprint") != row.value.get("sender_fingerprint") or
            submitted.detail.get("session_id") != proof.get("session_id") or
            proof.get("sender_fingerprint") != row.value.get("sender_fingerprint")):
        return False
    try:
        consent_at = datetime.fromisoformat(proof["consent_at"])
        submitted_at = datetime.fromisoformat(submitted.detail["submitted_at"])
    except (KeyError, ValueError, TypeError):
        return False
    full_name = re.fullmatch(r"\s*(?:(?:join|my name is|i am|i'm)\s+)?" + re.escape(volunteer.name) + r"[.!]?\s*", reply.body, re.I)
    partial_id = proof.get("identity_parts_reply_message_id")
    partial = session.get(m.Message, partial_id) if partial_id else None
    combined_name = bool(partial and partial.phone == volunteer.phone and partial.kind == "google_voice_test_in" and
        partial.purpose == reply.purpose and partial.status == "received" and partial.id < reply.id and
        re.fullmatch(r"\s*" + re.escape(partial.body.strip().rstrip('.!') + " " + reply.body.strip().rstrip('.!')) + r"\s*", volunteer.name, re.I))
    return bool((full_name or combined_name) and consent_at >= submitted_at and volunteer.preferences.get("consent_reply_message_id") == reply.id and volunteer.preferences.get("consent_disclosure_message_id") == disclosure.id)
