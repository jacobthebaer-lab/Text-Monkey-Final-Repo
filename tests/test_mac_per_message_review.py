"""Proof-bearing rows cannot lose native review when global mode is disabled."""
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.core import confirmations
from app.core.send_gate import SendGate
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from tests.test_demo_acceptance_review import acceptance_app, BRIDGE_TOKEN, pull
from tests.test_literal_reminder_mac_acceptance import (
    reminder_mac, book, queued, reschedule,
)


def disable_global_review(app):
    app.state.settings = replace(app.state.settings, competition_confirmation_required=False)
    app.state.session_factory.configure(info={confirmations.MODE_KEY: False})


@pytest.mark.parametrize("phase", ["claim", "native_verify"])
@pytest.mark.parametrize("defect", ["missing_approval", "mismatched_receipt", "changed_body"])
def test_invalid_existing_receipt_never_downgrades_to_unreviewed_send(reminder_mac, phase, defect):
    client, app, _, _ = reminder_mac
    book(app)
    digest = queued(client, app)
    disable_global_review(app)
    claim = pull(client).json()["messages"][0] if phase == "native_verify" else None
    with app.state.session_factory() as session:
        row = session.scalar(select(m.Message))
        proof = session.get(m.Notification, f"confirmation:{row.id}")
        if defect == "missing_approval":
            session.delete(session.get(m.Approval, proof.detail["approval_id"]))
        elif defect == "mismatched_receipt":
            proof.detail = {**proof.detail, "content_hash": "0" * 64}
        else:
            row.body += " Changed."
        session.commit()
    if claim:
        response = client.post(f"/mac/outbound/{claim['id']}/verify",
            json={"token": claim["token"], "content_hash": digest},
            headers={"Authorization": "Bearer " + BRIDGE_TOKEN})
        assert response.status_code == 409
    else:
        assert pull(client).json()["messages"] == []
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message.status)) == "blocked_confirmation"


def test_global_off_still_rechecks_rescheduled_source_after_claim(reminder_mac):
    client, app, _, _ = reminder_mac
    book(app)
    digest = queued(client, app)
    disable_global_review(app)
    claim = pull(client).json()["messages"][0]
    assert claim["confirmation_required"] is True
    reschedule(app)
    response = client.post(f"/mac/outbound/{claim['id']}/verify",
        json={"token": claim["token"], "content_hash": digest},
        headers={"Authorization": "Bearer " + BRIDGE_TOKEN})
    assert response.status_code == 409
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message.status)) == "blocked_confirmation"


def test_ordinary_gloo_signup_reply_without_review_receipt_stays_unconfirmed(reminder_mac):
    client, app, gloo, clock = reminder_mac
    disable_global_review(app)
    with app.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, app.state.reminder_person_id)
        body = compose_signup_reply(session, clock, gloo, "Thanks! When can you serve?",
            volunteer=volunteer, signup_conversation=True, require_gloo=True)
        gate = SendGate(session, clock, app.state.provider)
        gate.gloo = gloo
        result = gate.send(body=body, volunteer=volunteer, purpose="signup_reply", kind="agent")
        assert result.message_id
        assert session.get(m.Notification, f"confirmation:{result.message_id}") is None
        session.commit()
    messages = pull(client).json()["messages"]
    assert len(messages) == 1 and messages[0]["id"] == result.message_id
    assert "confirmation_required" not in messages[0]
