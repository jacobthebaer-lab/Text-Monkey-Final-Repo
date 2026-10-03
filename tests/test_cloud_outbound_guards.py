"""Google transport boundary protections, entirely offline and synthetic."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import confirmations
from app.core.message_style import EM_DASH_CHARACTERS, OutboundStyleError
from app.core.send_gate import SendGate
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.integrations.google_voice_runtime import set_paused, tick_google_voice
from app.llm.gloo_client import GlooUnavailableError, NullGloo
from tests.test_google_voice import cloud, queued, status, PHONE, EMAIL


@pytest.mark.parametrize("dash", sorted(EM_DASH_CHARACTERS))
def test_all_forbidden_dashes_rejected_at_enqueue_and_dispatch(cloud, dash):
    with pytest.raises(OutboundStyleError):
        cloud.state.provider.send(PHONE, f"Synthetic{dash}text")
    message_id = queued(cloud)
    with cloud.state.session_factory() as session:
        session.get(m.Message, message_id).body = f"Synthetic{dash}text"
        session.commit()
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "blocked_style"
    assert cloud.state.google_voice_connector.preparations == []


@pytest.mark.parametrize("purpose,kind", [("manual", "template"), ("start_confirm", "template"),
                                         ("manual", "ai")])
def test_cloud_gate_cannot_stage_without_gloo_even_with_ai_label(cloud, purpose, kind):
    with cloud.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        gate = SendGate(session, cloud.state.clock, cloud.state.provider)
        with pytest.raises(GlooUnavailableError):
            gate.send(body="Synthetic local text", purpose=purpose, kind=kind, volunteer=volunteer)
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message)) is None


def test_essential_intake_composition_uses_gloo_with_optional_flag_disabled(cloud):
    state = cloud.state
    assert not state.settings.gloo_signup_replies
    with state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        body = compose_signup_reply(session, state.clock, state.gloo, "Which team would you like to serve on?", volunteer=volunteer)
        gate = SendGate(session, state.clock, state.provider)
        outcome = gate.send(body=body, purpose="signup_reply", volunteer=volunteer,
                            conversation={"intake_fields": ["interests"]})
        assert outcome.approval_id
        repeated = gate.send(body=body, purpose="signup_reply", volunteer=volunteer,
                             conversation={"intake_fields": ["interests"]})
        assert repeated.approval_id == outcome.approval_id
        session.flush()
        assert len(session.scalars(select(m.Notification).where(
            m.Notification.key == f"google-voice-gloo:{outcome.approval_id}")).all()) == 1
        assert len(state.gloo.calls) == 1
        approval = session.get(m.Approval, outcome.approval_id)
        confirmations.decide(session, gate, approval, approve=True, actor=EMAIL,
            expected=approval.payload["content_hash"], now=state.clock.now())
        assert approval.payload.get("message_id")
        assert len(state.gloo.calls) == 1


@pytest.mark.parametrize("change", ["body", "recipient", "transaction"])
def test_composition_proof_is_bound_to_context(cloud, change):
    state = cloud.state
    with state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        body = compose_signup_reply(session, state.clock, state.gloo, "Synthetic help.", volunteer=volunteer)
        if change == "transaction":
            session.commit()
        if change == "recipient":
            from app.core.cloud_composition import require_composition
            with pytest.raises(GlooUnavailableError):
                require_composition(session, state.clock, None, "+15555550999", body,
                                    state.provider.test_sessions[PHONE])
        else:
            gate = SendGate(session, state.clock, state.provider)
            with pytest.raises(GlooUnavailableError):
                gate.send(body=body + (" Changed." if change == "body" else ""),
                          purpose="manual", volunteer=volunteer)


@pytest.mark.parametrize("purpose", ["thanks", "cancellation_ack", "availability_ask"])
def test_cloud_transport_preserves_shared_quiet_policy(cloud, purpose):
    with cloud.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        gate = SendGate(session, cloud.state.clock, cloud.state.provider)
        gate.gloo = cloud.state.gloo
        outcome = gate.send(body="Routine synthetic update.", purpose=purpose, volunteer=volunteer)
        assert outcome.status.value == "blocked_policy"
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message)) is None
        assert cloud.state.gloo.calls == []


def test_missing_durable_gloo_receipt_blocks_old_queue(cloud):
    message_id = queued(cloud)
    with cloud.state.session_factory() as session:
        approval = confirmations.proof_for(session, session.get(m.Message, message_id))
        session.delete(session.get(m.Notification, f"google-voice-gloo:{approval.id}"))
        session.commit()
    tick_google_voice(cloud.state)
    assert status(cloud, message_id) == "blocked_gloo"
    assert cloud.state.google_voice_connector.preparations == []


@pytest.mark.parametrize("change,expected", [("paused", "blocked_paused"),
    ("consent", "blocked_confirmation"), ("approval", "blocked_confirmation"),
    ("body", "blocked_confirmation"), ("time", "blocked_stale")])
def test_rechecks_proof_after_browser_preparation(cloud, change, expected):
    state = cloud.state
    message_id = queued(cloud)
    def prepare(**kwargs):
        if change == "time":
            state.clock.advance(timedelta(seconds=31))
        else:
            with state.session_factory() as session:
                session.info["record_authorized"] = True
                if change == "paused":
                    set_paused(session, True)
                elif change == "consent":
                    session.scalar(select(m.Volunteer)).sms_opt_in = False
                elif change == "approval":
                    confirmations.proof_for(session, session.get(m.Message, message_id)).status = "rejected"
                else:
                    session.get(m.Message, message_id).body = "Changed while preparing."
                session.commit()
        return "prepared"
    state.google_voice_connector.prepare = prepare
    tick_google_voice(state)
    assert status(cloud, message_id) == expected
    assert state.google_voice_connector.calls == []


def test_manual_cloud_draft_holds_when_gloo_unavailable(cloud):
    cloud.state.gloo = NullGloo()
    with cloud.state.session_factory() as session:
        volunteer_id = session.scalar(select(m.Volunteer.id))
    with TestClient(cloud) as client:
        response = client.post("/api/reply", json={"volunteer_id": volunteer_id, "body": "Synthetic exact draft."})
        assert response.status_code == 503
    with cloud.state.session_factory() as session:
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message)) is None
