"""Offline deployment-image proof, run in two containers by its shell wrapper.

Uses only fictional records and an explicit Gloo test double. This never calls
Google or Gloo and does not prove account compatibility or real delivery.
"""
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

assert os.environ.get("CLOUD_PROOF_RESTART_FIXTURE") == "synthetic"
assert os.getuid() != 0
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from sqlalchemy import func, select
from app.clock import FakeClock
from app.config import Settings
from app.core import confirmations
from app.core.cloud_composition import reviewed_composition
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
from app.integrations.google_voice_runtime import dispatch_outbound, get_cloud_status, set_paused
from app.llm.gloo_client import GlooUnavailableError, NullGloo
from app.main import create_app

phase = sys.argv[1]
assert phase in ("reserve", "recover")
phone = "+12025550101"
now = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
settings = Settings(database_url="sqlite:////data/backend-restart-proof.db",
    demo_mode=False, automation_enabled=False, sms_provider="google_voice",
    google_voice_enabled=True, live_sms=False, competition_confirmation_required=True,
    google_voice_connector_token="synthetic-no-network-" + "x" * 40,
    google_voice_expected_email="proof@example.invalid",
    google_voice_expected_number="+12025550100", google_voice_demo_phones=phone,
    google_voice_test_sessions=json.dumps({phone: {"id": "a" * 32,
        "starts_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat()}}),
    gloo_api_key="synthetic-no-network")
app = create_app(settings)
state = app.state
state.clock = state.google_voice_clock = FakeClock(now)

class LiteralGloo:
    """Explicit offline composition fixture, never a production fallback."""
    def create_response(self, **kwargs):
        return SimpleNamespace(output_text=json.loads(kwargs["input"])["approved_message"], usage=None)

class NoConnectorCalls:
    def __getattr__(self, name):
        raise AssertionError("Offline recovery must not contact the connector")

try:
    if phase == "reserve":
        with state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(m.Volunteer)) == 0
            session.info["record_authorized"] = True
            volunteer = m.Volunteer(name="Fictional Recovery Tester", phone=phone,
                sms_opt_in=True, status="active", preferences={}, created_at=now)
            session.add(volunteer)
            set_paused(session, True)
            session.flush()
            gate = SendGate(session, state.clock, state.provider)
            gate.gloo = LiteralGloo()
            outcome = gate.send(body="Synthetic cloud recovery check.", purpose="admin_reply",
                kind="ai", volunteer=volunteer)
            assert outcome.status is SendStatus.HELD_FOR_APPROVAL
            approval = session.get(m.Approval, outcome.approval_id)
            confirmations.decide(session, gate, approval, approve=True,
                actor="proof@example.invalid", expected=approval.payload["content_hash"], now=now)
            row = session.get(m.Message, approval.payload["message_id"])
            assert row.status == "queued"
            row.status = "dispatching"
            session.add(GoogleVoiceDeliveryClaim(message_id=row.id,
                idempotency_key=row.provider_sid, created_at=now))
            session.commit()
        with state.session_factory() as session:
            gate = SendGate(session, state.clock, state.provider)
            gate.gloo = NullGloo()
            try:
                gate.send(body="Synthetic outage check.", purpose="admin_reply", phone=phone, kind="ai")
            except GlooUnavailableError:
                session.rollback()
            else:
                raise AssertionError("Gloo outage must hold before another review or queue entry")
        print("PASS: backend persisted exact review/composition and dispatch claim; scripted Gloo outage created no additional review")
    else:
        with state.session_factory() as session:
            approval = session.scalar(select(m.Approval).where(m.Approval.kind == "confirm_text"))
            row = session.scalar(select(m.Message).where(m.Message.direction == "out"))
            assert session.scalar(select(func.count()).select_from(m.Approval)) == 1
            assert session.scalar(select(func.count()).select_from(m.Message)) == 1
            assert row.status == "dispatching" and approval.status == "approved"
            assert confirmations.proof_for(session, row).id == approval.id
            assert reviewed_composition(session, approval, state.provider.test_sessions[phone])
            claim = session.get(GoogleVoiceDeliveryClaim, row.id)
            assert claim.idempotency_key == row.provider_sid
            assert get_cloud_status(state, session)["paused"] is True
            assert get_cloud_status(state, session)["ready"] is False
            # Probe replay selection only, inside this offline process. No real
            # credentials or connector exist, and container networking is off.
            set_paused(session, False)
            session.commit()
        state.settings = replace(settings, live_sms=True)
        dispatch_outbound(state, NoConnectorCalls())
        state.settings = settings
        with state.session_factory() as session:
            assert session.scalar(select(m.Message.status)) == "dispatching"
            assert session.scalar(select(func.count()).select_from(GoogleVoiceDeliveryClaim)) == 1
            set_paused(session, True)
            session.commit()
        print("PASS: second backend container preserved review/composition, pause and claim; interrupted dispatch was not replayed")
finally:
    state.engine.dispose()
