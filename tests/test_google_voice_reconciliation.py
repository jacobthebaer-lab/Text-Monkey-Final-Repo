"""Offline exact prior-submission observation, no account/model/provider access."""
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import confirmations
from app.db import models as m
from app.integrations.google_voice_demo import RECIPIENT_KEY, sender_fingerprint
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
from app.integrations.google_voice_signup import tick_signup
from tests.test_google_voice_demo import demo, dynamic_demo
from tests.test_google_voice_signup import signup, enable, inbound, PHONE


@pytest.fixture
def attempted(signup):
    enable(signup)
    from tests.test_google_voice_demo import register
    assert register(signup, PHONE).status_code == 200
    state = signup.state
    tick_signup(state)
    with state.session_factory() as session:
        row = session.scalar(select(m.Message).where(m.Message.phone == PHONE, m.Message.direction == "out"))
        claim = session.get(GoogleVoiceDeliveryClaim, row.id)
        start = claim.created_at.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=1)
        body_hash = hashlib.sha256(row.body.encode()).hexdigest()
        proof = {"body_hash": body_hash, "native_timestamp_interval": {
            "start": start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "end": end.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "precision": "minute"},
            "submission_key_hash": hashlib.sha256(claim.idempotency_key.encode()).hexdigest(),
            "session_id": state.provider.test_sessions[PHONE].id,
            "claim_created_at": claim.created_at.isoformat(), "ledger_created_at": claim.created_at.isoformat(),
            "original_digest": hashlib.sha256(b"opaque original signed request").hexdigest(),
            "digest_verification": "opaque_original_preserved", "original_status": "uncertain",
            "original_reason_code": "submission_unconfirmed", "observed_at": state.clock.now().isoformat(),
            "sender_fingerprint": sender_fingerprint(state.settings)}
        proof["provider_item_fingerprint"] = hashlib.sha256((row.phone + "\0" + proof["native_timestamp_interval"]["start"] + "\0" + row.body).encode()).hexdigest()
        row.status = "uncertain"
        session.delete(session.get(m.Notification, "google-demo-submission:" + str(row.id)))
        session.commit()
        payload = {"message_id": row.id, "body_hash": body_hash}
    reads = []
    def observe(request):
        reads.append(request)
        return {"status": "submitted", "proof": deepcopy(proof)}
    state.google_voice_connector.reconcile_submission = observe
    return signup, payload, proof, reads


def test_reconcile_original_claim_idempotently_then_process_saved_name_once(attempted):
    app, payload, proof, reads = attempted
    state = app.state
    native_calls = len(state.google_voice_connector.calls)
    gloo_calls = len(state.gloo.calls)
    scans = state.google_voice_connector.scans
    with state.session_factory() as session:
        original = session.get(m.Policy, RECIPIENT_KEY + PHONE).value.copy()
        claim = session.get(GoogleVoiceDeliveryClaim, payload["message_id"])
        original_key = claim.idempotency_key
    client = TestClient(app)
    for _ in range(2):
        response = client.post("/api/cloud-texting/demo/reconcile", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["delivery_verified"] is False
    assert len(reads) == 1
    assert len(state.gloo.calls) == gloo_calls and len(state.google_voice_connector.calls) == native_calls
    assert state.google_voice_connector.scans == scans
    with state.session_factory() as session:
        row = session.get(m.Message, payload["message_id"])
        assert row.status == "submitted"
        assert session.get(GoogleVoiceDeliveryClaim, row.id).idempotency_key == original_key
        assert session.get(m.Policy, RECIPIENT_KEY + PHONE).value == original
        receipt = session.get(m.Notification, "google-demo-submission:" + str(row.id))
        assert datetime.fromisoformat(receipt.detail["submitted_at"]) == datetime.fromisoformat(proof["native_timestamp_interval"]["start"].replace("Z", "+00:00"))
        assert receipt.detail["submitted_at_precision"] == "minute"
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
    # Ordinary original inbound remains eligible after the original native minute.
    state.clock.advance(timedelta(minutes=2))
    inbound(app, "Judge Example", "actual-synthetic-name-after-reconcile")
    tick_signup(state)
    with state.session_factory() as session:
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person and person.sms_opt_in and person.name == "Judge Example"
    assert len(state.google_voice_connector.calls) == native_calls + 1
    tick_signup(state)
    assert len(state.google_voice_connector.calls) == native_calls + 1


@pytest.mark.parametrize("change", ["body", "key", "session", "sender", "wrong_time", "precision", "digest", "item", "opaque_claim"])
def test_mismatched_native_evidence_cannot_release_existing_claim(attempted, change):
    app, payload, proof, reads = attempted
    if change == "body": proof["body_hash"] = "0" * 64
    elif change == "key": proof["submission_key_hash"] = "0" * 64
    elif change == "session": proof["session_id"] = "b" * 32
    elif change == "sender": proof["sender_fingerprint"] = "0" * 64
    elif change == "wrong_time": proof["native_timestamp_interval"]["end"] = proof["native_timestamp_interval"]["start"]
    elif change == "precision": proof["native_timestamp_interval"]["precision"] = "second"
    elif change == "digest": proof["original_digest"] = "invalid"
    elif change == "item": proof["provider_item_fingerprint"] = "0" * 64
    elif change == "opaque_claim": proof["digest_verification"] = "recomputed_with_new_deadline"
    assert TestClient(app).post("/api/cloud-texting/demo/reconcile", json=payload).status_code == 409
    with app.state.session_factory() as session:
        assert session.get(m.Message, payload["message_id"]).status == "uncertain"
        assert session.get(m.Notification, "google-demo-submission:" + str(payload["message_id"])) is None
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None


@pytest.mark.parametrize("change", ["body", "approval", "gloo", "invitation", "claim", "superadmin"])
def test_original_authority_changes_hold_before_observation(attempted, change):
    app, payload, proof, reads = attempted
    with app.state.session_factory() as session:
        row = session.get(m.Message, payload["message_id"])
        if change == "body": row.body += " changed"
        elif change == "approval": confirmations.proof_for(session, row).status = "revoked"
        elif change == "gloo": session.delete(session.get(m.Notification, "google-voice-gloo:" + str(confirmations.proof_for(session, row).id)))
        elif change == "invitation":
            registered = session.get(m.Policy, RECIPIENT_KEY + PHONE)
            registered.value = {**registered.value, "invitation": {}}
        elif change == "claim": session.get(GoogleVoiceDeliveryClaim, row.id).idempotency_key += "-changed"
        session.commit()
    if change == "superadmin":
        from app.web.texty import admin
        app.dependency_overrides[admin] = lambda: {"email": "unauthorized@example.test", "email_confirmed_at": "synthetic"}
    assert TestClient(app).post("/api/cloud-texting/demo/reconcile", json=payload).status_code in {403, 409}
    assert reads == []


@pytest.mark.parametrize("hold", ["STOP", "Gloo unavailable"])
def test_reconciliation_creates_no_consent_and_preserves_stop_or_model_hold(attempted, hold):
    app, payload, proof, reads = attempted
    state = app.state
    native_calls = len(state.google_voice_connector.calls)
    if hold == "STOP":
        with state.session_factory() as session:
            session.add(m.Policy(key="sms_opt_out:" + PHONE, value={"value": True}))
            row = session.get(m.Policy, RECIPIENT_KEY + PHONE)
            row.value = {**row.value, "consent_state": "withdrawn"}
            session.commit()
    else:
        from app.llm.gloo_client import GlooUnavailableError
        def unavailable(**kwargs):
            raise GlooUnavailableError("synthetic unavailable")
        state.gloo.create_response = unavailable
    response = TestClient(app).post("/api/cloud-texting/demo/reconcile", json=payload)
    assert response.status_code == 200, response.text
    state.clock.advance(timedelta(minutes=2))
    inbound(app, "Judge Example", "held-name-after-reconciliation")
    tick_signup(state)
    with state.session_factory() as session:
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
        if hold == "STOP":
            assert session.get(m.Policy, "sms_opt_out:" + PHONE).value["value"] is True
    assert len(state.google_voice_connector.calls) == native_calls
