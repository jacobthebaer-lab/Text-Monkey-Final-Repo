"""Observe one previously claimed submission. Never prepare, click or retry it."""
import hashlib
import re
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import update

from app.core import confirmations
from app.core.cloud_composition import reviewed_composition
from app.core.message_style import validate_outbound_style
from app.db import models as m
from app.integrations.google_voice_client import connector_for, ConnectorUnavailable, verified_identity
from app.integrations.google_voice_demo import RECIPIENT_KEY, sender_fingerprint, restore_demo_scope
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
from app.integrations.google_voice_runtime import _tick_lock, _clock


def original_claim(session, state, message_id, body_hash):
    row = session.get(m.Message, message_id)
    claim = session.get(GoogleVoiceDeliveryClaim, message_id)
    approval = confirmations.proof_for(session, row) if row else None
    selected = state.provider.test_sessions.get(row.phone) if row else None
    registration = session.get(m.Policy, RECIPIENT_KEY + row.phone) if row else None
    if (not row or not claim or not approval or not selected or not registration or
            row.direction != "out" or row.status not in {"uncertain", "dispatching", "submitted"} or
            claim.idempotency_key != row.provider_sid or not row.provider_sid.startswith(selected.outbound_prefix) or
            hashlib.sha256(row.body.encode()).hexdigest() != body_hash or
            registration.value.get("sender_fingerprint") != sender_fingerprint(state.settings) or
            registration.value.get("session", {}).get("id") != selected.id or
            approval.status != "approved" or not confirmations.valid(approval, claim.created_at) or
            approval.decided_at is None or approval.decided_at > claim.created_at or
            approval.payload.get("transport") != "google_voice" or approval.payload.get("session_id") != selected.id or
            approval.payload.get("message_id") != row.id or approval.payload.get("phone") != row.phone or
            approval.payload.get("body") != row.body or approval.payload.get("purpose") != row.purpose or
            not reviewed_composition(session, approval, selected)):
        raise HTTPException(409, "Original exact submission authorization could not be verified. Nothing will be retried.")
    validate_outbound_style(row.body)
    if row.purpose == "signup_reply" and not approval.payload.get("reply_to_message_id"):
        invitation = registration.value.get("invitation", {})
        if (invitation.get("approval_id") != approval.id or invitation.get("body_hash") != body_hash or
                invitation.get("content_hash") != approval.payload["content_hash"] or invitation.get("body") != row.body):
            raise HTTPException(409, "Original invitation proof changed. Nothing will be retried.")
    return row, claim, selected


def validate_observation(state, row, claim, selected, proof):
    try:
        interval = proof["native_timestamp_interval"]
        start = datetime.fromisoformat(interval["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(interval["end"].replace("Z", "+00:00"))
        reserved = datetime.fromisoformat(proof["ledger_created_at"].replace("Z", "+00:00"))
        observed = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
        body_hash = hashlib.sha256(row.body.encode()).hexdigest()
        item_hash = hashlib.sha256((row.phone + "\0" + interval["start"] + "\0" + row.body).encode()).hexdigest()
        valid = (interval["precision"] == "minute" and start.tzinfo is not None and end.tzinfo is not None and
            end - start == timedelta(minutes=1) and start.second == 0 and start.microsecond == 0 and
            start <= claim.created_at < end and start <= reserved < end and
            claim.created_at <= reserved <= claim.created_at + timedelta(seconds=30) and
            reserved <= observed <= _clock(state).now() + timedelta(minutes=1) and
            proof["claim_created_at"] == claim.created_at.isoformat() and
            proof["submission_key_hash"] == hashlib.sha256(claim.idempotency_key.encode()).hexdigest() and
            proof["session_id"] == selected.id and proof["sender_fingerprint"] == sender_fingerprint(state.settings) and
            proof["body_hash"] == body_hash and proof["provider_item_fingerprint"] == item_hash and
            proof["digest_verification"] == "opaque_original_preserved" and
            re.fullmatch(r"[a-f0-9]{64}", proof["original_digest"]) is not None and
            proof["original_status"] in {"uncertain", "pending"})
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise HTTPException(409, "Observed submission evidence did not match the original claim. Nothing will be retried.")
    return start


def reconcile_submission(state, actor, message_id, body_hash):
    if not _tick_lock.acquire(blocking=False):
        raise HTTPException(409, "Another cloud texting step is in progress.")
    try:
        restore_demo_scope(state)
        with state.session_factory() as session:
            row, claim, selected = original_claim(session, state, message_id, body_hash)
            saved = session.get(m.Notification, "google-demo-reconciliation:" + str(row.id))
            if row.status == "submitted":
                if not saved or saved.detail.get("body_hash") != body_hash:
                    raise HTTPException(409, "This claim has no matching reconciliation receipt.")
                return {"status": "submitted", "message_id": row.id, "delivery_verified": False, "reconciled": True}
            request = {"idempotency_key": claim.idempotency_key, "to": row.phone, "body": row.body,
                "session_id": selected.id, "claim_created_at": claim.created_at.isoformat()}
        connector = connector_for(state)
        try:
            result = connector.reconcile_submission(request)
            if result.get("status") != "submitted" or not verified_identity(connector.health(), state.settings, state.provider):
                raise ConnectorUnavailable()
        except (ConnectorUnavailable, ValueError):
            raise HTTPException(503, "Exact submission observation unavailable. The existing claim stays held, never retried.") from None
        proof = result.get("proof")
        with state.session_factory() as session:
            row, claim, selected = original_claim(session, state, message_id, body_hash)
            start = validate_observation(state, row, claim, selected, proof)
            if row.status == "submitted":
                raise HTTPException(409, "Submission state changed during observation.")
            previous = row.status
            changed = session.execute(update(m.Message).where(m.Message.id == row.id,
                m.Message.status.in_(("uncertain", "dispatching"))).values(status="submitted")).rowcount
            if changed != 1:
                raise HTTPException(409, "Submission state changed during observation.")
            now = _clock(state).now()
            key = "google-demo-submission:" + str(row.id)
            if session.get(m.Notification, key) is not None:
                raise HTTPException(409, "Conflicting original submission receipt.")
            session.add(m.Notification(key=key, purpose="human_review", body="", state="submitted",
                due_at=now, created_at=now, message_id=row.id, detail={
                    "body_hash": body_hash, "submitted_at": start.isoformat(), "submitted_at_precision": "minute",
                    "native_timestamp_interval": proof["native_timestamp_interval"],
                    "session_id": selected.id, "sender_fingerprint": sender_fingerprint(state.settings),
                    "reconciliation": proof}))
            session.add(m.Notification(key="google-demo-reconciliation:" + str(row.id), purpose="human_review", body="",
                state="submitted", due_at=now, created_at=now, message_id=row.id,
                detail={"actor": actor, "body_hash": body_hash, "previous_status": previous, "proof": proof}))
            session.commit()
        # No baseline reset, intake, model call, worker enable or provider submission.
        return {"status": "submitted", "message_id": message_id, "delivery_verified": False, "reconciled": True}
    finally:
        _tick_lock.release()
