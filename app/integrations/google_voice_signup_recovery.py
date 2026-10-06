"""One original unknown-number receipt can resume its authorized signup.

No browser scan, new incoming message, model fallback, native send or cursor
mutation occurs here. Failed Gloo work rolls back to the original receipt.
"""
import hashlib

from fastapi import HTTPException

from app.db import models as m
from app.integrations.google_voice_client import connector_for, ConnectorUnavailable, verified_health
from app.integrations.google_voice_demo import restore_demo_scope
from app.integrations.google_voice_models import GoogleVoiceInboundReceipt
from app.integrations.google_voice_runtime import _tick_lock, _incoming
from app.integrations.google_voice_signup import registered_signup_intake_authority
from app.llm.gloo_client import GlooUnavailableError


def recover_signup_input(state, actor, receipt_id):
    if not _tick_lock.acquire(blocking=False):
        raise HTTPException(409, "Another cloud texting step is in progress.")
    try:
        restore_demo_scope(state)
        with state.session_factory() as session:
            receipt = session.get(GoogleVoiceInboundReceipt, receipt_id)
            source = session.get(m.Message, receipt.result.get("source_message_id")) if receipt else None
            saved = session.get(m.Notification, "google-signup-input-recovery:" + receipt_id)
            if saved:
                if (not receipt or not source or not receipt.result.get("recovered_signup") or
                        saved.message_id != source.id or saved.detail.get("fingerprint") != receipt.fingerprint or
                        saved.detail.get("session_id") != receipt.result.get("session_id") or
                        source.direction != "in" or source.kind != "google_voice_test_in" or
                        source.status != "received" or source.purpose != "test:" + saved.detail.get("session_id", "") or
                        source.created_at.isoformat() != saved.detail.get("source_created_at") or
                        hashlib.sha256((source.phone + "\0" + saved.detail.get("session_id", "") + "\0" + source.body).encode()).hexdigest() != receipt.fingerprint or
                        saved.detail.get("intent") != receipt.result.get("intent")):
                    raise HTTPException(409, "Stored signup recovery evidence changed.")
                return {"processed": True, "already_processed": True, "source_message_id": source.id,
                        "intent": receipt.result["intent"], "native_submission_attempted": False}
            selected = state.provider.test_sessions.get(source.phone) if source else None
            if (not source or not selected or not receipt or receipt.result.get("intent") != "unknown_number" or
                    receipt.result.get("session_id") != selected.id or
                    not registered_signup_intake_authority(session, state, source.phone, selected)):
                raise HTTPException(409, "Enable signup for the original registered participant before recovering its stored reply.")
            phone, session_id = source.phone, selected.id
        connector = connector_for(state)
        try:
            if not verified_health(connector.health(), state.settings, state.provider):
                raise ConnectorUnavailable()
            saved = connector.stored_signup_input(id=receipt_id, phone=phone, session_id=session_id)
            if (not isinstance(saved, dict) or saved.get("session_id") != session_id or
                    not isinstance(saved.get("message"), dict) or saved["message"].get("id") != receipt_id or
                    saved["message"].get("phone") != phone):
                raise ConnectorUnavailable()
            _incoming(state, saved["message"], recovery_actor=actor)
        except GlooUnavailableError:
            raise HTTPException(503, "Gloo connection requires attention. The original stored reply remains unchanged.") from None
        except (ConnectorUnavailable, ValueError):
            raise HTTPException(409, "Original stored signup evidence could not be verified. Nothing was resent.") from None
        with state.session_factory() as session:
            receipt = session.get(GoogleVoiceInboundReceipt, receipt_id)
            if not receipt or not receipt.result.get("recovered_signup"):
                raise HTTPException(409, "The original stored reply remains held.")
            return {"processed": True, "already_processed": False,
                    "source_message_id": receipt.result["source_message_id"], "intent": receipt.result["intent"],
                    "native_submission_attempted": False}
    finally:
        _tick_lock.release()
