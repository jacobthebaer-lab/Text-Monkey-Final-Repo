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
CONTINUOUS_END = datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)


def session_from_spec(spec):
    return GoogleVoiceTestSession(spec["id"], datetime.fromisoformat(spec["starts_at"]),
        datetime.fromisoformat(spec["expires_at"]), spec.get("continuous") is True)


def scope_fingerprint(sessions):
    def timestamp(value):
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    values = [[phone, spec.id, timestamp(spec.starts_at), timestamp(spec.expires_at)] +
              ([True] if getattr(spec, "continuous", False) else [])
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
        if spec.get("continuous") and not getattr(state.settings, "google_voice_signup_enabled", False):
            continue
        sessions[phone] = session_from_spec(spec)
    state.provider.test_sessions = sessions
    state.provider.phones = frozenset(sessions)


def registered_participants(state):
    restore_demo_scope(state)
    now = state.google_voice_clock.now()
    with state.session_factory() as session:
        records = list(session.scalars(select(m.Policy).where(m.Policy.key.startswith(RECIPIENT_KEY))))
        return [{"phone": row.value["phone"], "name": row.value.get("name", "Demo participant"),
                 "state": row.value["state"], "consent_state": row.value.get("consent_state", "awaiting_name"),
                 "continuous": row.value["session"].get("continuous") is True,
                 "expires_at": None if row.value["session"].get("continuous") else row.value["session"]["expires_at"],
                 "active": row.value["state"] == "active" and row.value.get("sender_fingerprint") == sender_fingerprint(state.settings) and row.value["phone"] in state.provider.test_sessions and
                           state.provider.test_sessions[row.value["phone"]].active(now)} for row in records]


def register_participant(state, actor, phone, name):
    from app.integrations.google_voice_signup import signup_status
    continuous = signup_status(state)["enabled"]
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
            if continuous and not value["session"].get("continuous"):
                value = {**value, "state": "pending", "expected_scope": scope_fingerprint(state.provider.test_sessions),
                    "session": {**value["session"], "expires_at": CONTINUOUS_END.isoformat(), "continuous": True},
                    "signup_authority": {"actor": actor, "at": now.isoformat(), "sender_fingerprint": expected_sender}}
                row.value = value
                session.commit()
        else:
            spec = {"id": uuid4().hex, "starts_at": now.isoformat(), "expires_at": (now + timedelta(hours=2)).isoformat()}
            if continuous:
                spec.update(expires_at=CONTINUOUS_END.isoformat(), continuous=True)
            value = {"state": "pending", "phone": phone, "name": name, "sender_fingerprint": expected_sender,
                     "session": spec, "first_activation_at": now.isoformat(), "consent_state": "awaiting_name", "initiation": {"actor": actor,
                     "recorded_at": now.isoformat(), "phone": phone, "sender_fingerprint": expected_sender,
                     "scope": ("one exact Gloo signup invitation under operator authorization, then authenticated name-reply consent" if continuous else "one exact reviewed name invitation, then authenticated name-reply consent")},
                     "expected_scope": scope_fingerprint(state.provider.test_sessions)}
            if continuous:
                value["signup_authority"] = {"actor": actor, "at": now.isoformat(), "sender_fingerprint": expected_sender}
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
    candidate = {**state.provider.test_sessions, phone: session_from_spec(spec)}
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
    if (not approval or approval.status != "approved" or
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
    # Continuous enrollment retains the original submitted disclosure authority;
    # its pre-send approval deadline does not expire a submitted disclosure.
    if not confirmations.valid(approval, submitted_at if getattr(selected, "continuous", False) else clock.now()):
        return None
    if received < submitted_at or received > clock.now() + timedelta(minutes=1):
        return None
    inputs = session.scalars(scope(select(m.Message), selected).where(m.Message.phone == phone,
        m.Message.direction == "in", m.Message.id > message.id, m.Message.id <= reply.id))
    if any(control_action(incoming.body) == "stop" for incoming in inputs):
        return None
    return {"disclosure_message_id": message.id, "reply_message_id": reply.id,
            "consent_at": received.isoformat(), "session_id": selected.id,
            "sender_fingerprint": value["sender_fingerprint"], "disclosure_body_hash": invitation["body_hash"]}


def normalized_name(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def meaningful_name_part(value):
    """Validate a self-reported name, never compare it with an admin identity."""
    return bool(isinstance(value, str) and 0 < len(normalized_name(value)) <= 80 and
        any(char.isalpha() for char in value) and
        not any(char.isdigit() or (ord(char) < 32 and not char.isspace()) for char in value))


def name_reply_recovery(session, clock, phone, reply_id):
    """A clarification may follow only this actual pending sender reply."""
    from app.core.consent_controls import control_action
    selected = session.info.get('mac_test_session')
    row = session.get(m.Policy, RECIPIENT_KEY + phone)
    incoming = session.get(m.Message, reply_id) if type(reply_id) is int else None
    if (not selected or not row or row.value.get('consent_state') != 'awaiting_name' or
            not incoming or incoming.phone != phone or incoming.direction != 'in' or
            incoming.status != 'received' or incoming.kind != 'google_voice_test_in' or
            incoming.purpose != 'test:' + selected.id or control_action(incoming.body) is not None or
            session.get(m.Policy, 'sms_opt_out:' + phone) or
            not demo_invitation_proof(session, clock, phone, reply_message_id=incoming.id, body=incoming.body)):
        return None
    return {'reply_id': incoming.id, 'body_hash': hashlib.sha256(incoming.body.encode()).hexdigest(),
        'session_id': selected.id}


def name_part_matches(body, field, value):
    prefix = r"(?:my name is|i am|i'm|my " + ("first" if field == "first_name" else "last") + r" name is)"
    actual = re.sub(r"^" + prefix + r"(?:\s+|$)", "", normalized_name(body), count=1, flags=re.I)
    return bool(re.fullmatch(re.escape(normalized_name(value)) + r"[.!]?", actual, re.I))


def full_name_matches(body, values):
    name = normalized_name(values.get("first_name")) + " " + normalized_name(values.get("last_name"))
    actual = re.sub(r"^(?:join|my name is|i am|i'm)(?:\s+|$)", "", normalized_name(body), count=1, flags=re.I)
    return bool(re.fullmatch(re.escape(name) + r"[.!]?", actual, re.I))


def name_evidence(session, phone, field, value, reply_id, body, session_id):
    """Persist a fact only after code validates the actual scoped name reply."""
    evidence = {"field": field, "value": normalized_name(value), "reply_message_id": reply_id,
                "body_hash": hashlib.sha256(body.encode()).hexdigest(), "session_id": session_id}
    source = session.get(m.Message, reply_id)
    registration = session.get(m.Policy, RECIPIENT_KEY + phone)
    detail = {"phone": phone, "sender_fingerprint": registration.value["sender_fingerprint"], "evidence": evidence}
    key = "google-demo-name:" + session_id + ":" + str(reply_id) + ":" + field
    receipt = session.get(m.Notification, key)
    if receipt is None:
        session.add(m.Notification(key=key, purpose="human_review", state="validated", body="",
            due_at=source.created_at, created_at=source.created_at, detail=detail))
    elif receipt.state != "validated" or receipt.detail != detail:
        raise ValueError("Original name validation changed")
    return evidence


def original_name_part(session, phone, session_id, field, evidence, *, full_values=None):
    """Recheck normalized facts against their original actual sender message."""
    if (not isinstance(session_id, str) or not isinstance(evidence, dict) or evidence.get("field") != field or
            evidence.get("session_id") != session_id or
            not meaningful_name_part(evidence.get("value")) or
            evidence["value"] != normalized_name(evidence["value"]) or
            type(evidence.get("reply_message_id")) is not int):
        return None
    source = session.get(m.Message, evidence["reply_message_id"])
    receipt = session.get(m.Notification, "google-demo-name:" + session_id + ":" + str(evidence["reply_message_id"]) + ":" + field)
    registration = session.get(m.Policy, RECIPIENT_KEY + phone)
    if (not receipt or receipt.state != "validated" or not registration or
            receipt.detail != {"phone": phone, "sender_fingerprint": registration.value["sender_fingerprint"], "evidence": evidence}):
        return None
    if (not source or source.phone != phone or source.direction != "in" or source.status != "received" or
            source.kind != "google_voice_test_in" or source.purpose != "test:" + session_id or
            hashlib.sha256(source.body.encode()).hexdigest() != evidence.get("body_hash") or
            not (name_part_matches(source.body, field, evidence["value"]) or
                 (full_values and full_name_matches(source.body, full_values)))):
        return None
    return source


def validated_name_draft(session, clock, phone, draft):
    selected = session.info.get("mac_test_session")
    if not selected or draft.get("session_id") != selected.id:
        return {}
    by_field = draft.get("name_evidence", {})
    if not isinstance(by_field, dict):
        return {}
    result = {}
    for field, evidence in by_field.items():
        if field not in {"first_name", "last_name"}:
            return {}
        source = original_name_part(session, phone, selected.id, field, evidence)
        if not source or not demo_invitation_proof(session, clock, phone,
                reply_message_id=source.id, body=source.body):
            return {}
        if normalized_name(draft.get(field)) != evidence["value"]:
            return {}
        result[field] = evidence["value"]
    return result


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
        from app.integrations.google_voice_signup import unsent_recomposition_proof
        selected = provider.test_sessions.get(phone)
        for previous in session.scalars(select(m.Message).where(m.Message.phone == phone, m.Message.direction == "out",
                m.Message.body == body, m.Message.provider_sid.startswith("GV"))):
            if message is not None and previous.id == message.id:
                continue
            if unsent_recomposition_proof(session, previous, selected):
                continue
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


def registered_consent_provenance(session, volunteer, *, require_current_consent=True):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    row = session.get(m.Policy, RECIPIENT_KEY + volunteer.phone)
    if not row or row.value.get("consent_state") != "name_reply_opted_in":
        return False
    if require_current_consent and (not volunteer.sms_opt_in or row.value.get("state") != "active" or
            session.get(m.Policy, "sms_opt_out:" + volunteer.phone)):
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
    evidence = proof.get("name_evidence", {})
    if not isinstance(evidence, dict) or set(evidence) != {"first_name", "last_name"}:
        return False
    values = {field: item.get("value") for field, item in evidence.items() if isinstance(item, dict)}
    if set(values) != set(evidence) or normalized_name(volunteer.name) != normalized_name(values.get("first_name")) + " " + normalized_name(values.get("last_name")):
        return False
    sources = [original_name_part(session, volunteer.phone, proof.get("session_id"), field,
        item, full_values=values) for field, item in evidence.items()]
    if any(source is None or not disclosure.id < source.id <= reply.id or source.created_at < submitted_at for source in sources):
        return False
    if max(source.id for source in sources) != reply.id:
        return False
    from app.core.consent_controls import control_action
    intervening = session.scalars(select(m.Message).where(m.Message.phone == volunteer.phone,
        m.Message.direction == "in", m.Message.kind == "google_voice_test_in",
        m.Message.purpose == reply.purpose, m.Message.id > disclosure.id, m.Message.id <= reply.id))
    if any(control_action(source.body) == "stop" for source in intervening):
        return False
    return bool(consent_at >= submitted_at and volunteer.preferences.get("consent_reply_message_id") == reply.id and volunteer.preferences.get("consent_disclosure_message_id") == disclosure.id)
