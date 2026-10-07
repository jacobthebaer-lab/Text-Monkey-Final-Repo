"""Historical offer receipts, bookkeeping and current native delivery guards."""
from datetime import timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.fill_agent import FillContext, advance_due
from app.core import offer_windows as offers
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim
from tests.test_fill_agent import ScriptedAgentGloo
from tests.test_mac_messages import mac_app, post
from tests.test_confirmations import mode_app


def queue_offer(app, clock, *, lead=timedelta(hours=6), exact=False):
    """Restore a synthetic historical queue receipt, never initiate outreach."""
    with app.state.session_factory() as session:
        if exact:
            session.info.update(competition_confirmation_required=True, record_authorized=True)
        volunteer = session.scalar(select(m.Volunteer))
        role = m.Role(name="Synthetic greeter", ministry="Welcome", required_qualifications=[], criticality="standard", fill_policy="auto")
        event = m.Event(title="Synthetic service", starts_at=clock.now()+lead, ends_at=clock.now()+lead+timedelta(hours=1), status="scheduled")
        session.add_all([role,event]);session.flush()
        shift = m.Shift(role_id=role.id,event_id=event.id,slot_index=0)
        session.add(shift);session.flush()
        fill = m.FillRequest(shift_id=shift.id,urgency="normal",state="in_progress",current_tranche=1,created_at=clock.now())
        session.add(fill);session.flush()
        outreach = m.Outreach(fill_request_id=fill.id,volunteer_id=volunteer.id,tranche=1)
        session.add(outreach);session.flush()
        meta = offers.prepare(session,outreach,"Can you serve this shift?",clock.now())
        selected = app.state.provider.test_sessions[volunteer.phone]
        message = m.Message(direction="out",volunteer_id=volunteer.id,phone=volunteer.phone,
            body=meta.body,purpose="outreach",kind="ai",status="queued",
            provider_sid=selected.outbound_prefix+"synthetic-history",created_at=clock.now())
        session.add(message);session.flush()
        outreach.message_id = message.id
        session.commit()
        return outreach.id,fill.id


def test_fresh_offers_and_historical_queue_cannot_trigger_native_delivery(mac_app, clock):
    from types import SimpleNamespace
    import pytest
    with mac_app.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        gate = SendGate(session,clock,mac_app.state.provider)
        gate.gloo = SimpleNamespace(create_response=lambda **kw: pytest.fail("Quiet policy must precede Gloo"))
        outcome = gate.send(body="Can you serve?",purpose="outreach",volunteer=volunteer)
        assert outcome.status == SendStatus.BLOCKED_POLICY
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(m.Approval)) is None
        session.commit()
    outreach_id,fill_id = queue_offer(mac_app,clock)
    with TestClient(mac_app) as client:
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message = session.get(m.Message,outreach.message_id)
        assert message.status == "blocked_policy"
        assert session.scalar(select(MacDeliveryClaim)) is None
        assert offers.metadata(session,outreach).state == "offer_review"
        assert session.get(m.FillRequest,fill_id).current_tranche == 1
        assert session.scalar(select(m.Assignment)) is None


def record_claim(session, outreach, clock):
    """Bookkeeping from an existing legacy claim; no native sender is invoked."""
    message = session.get(m.Message,outreach.message_id)
    assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock),claim=True) is None
    message.status = "dispatching"
    claim = MacDeliveryClaim(message_id=message.id,token="c"*64)
    session.add(claim);session.flush()
    return message,claim


def test_queue_and_claim_delays_never_start_or_extend_dispatch_timer(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock)
    clock.advance(timedelta(minutes=20))
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message,claim = record_claim(session,outreach,clock)
        meta = offers.metadata(session,outreach)
        assert meta.state == "offer_claimed" and "dispatched_at" not in meta.detail
        message_id,token = message.id,claim.token
        assert session.get(m.FillRequest,fill_id).next_action_at > meta.expires_at
        session.commit()
    with TestClient(mac_app) as client:
        # A restored claim is not proof of delivery and cannot be acknowledged.
        assert post(client,f"/mac/outbound/{message_id}/ack",{"token":token,"outcome":"submitted"}).status_code == 409
    clock.advance(timedelta(minutes=10))
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message = session.get(m.Message,message_id)
        old_body = message.body
        assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock)) is None
        meta = offers.metadata(session,outreach)
        deadline = meta.expires_at
        assert message.body != old_body
        assert meta.detail["dispatched_at"] == clock.now().astimezone(timezone.utc).isoformat()
        assert deadline == clock.now()+timedelta(minutes=55)
        assert session.get(m.FillRequest,fill_id).next_action_at == deadline
        clock.advance(timedelta(minutes=1))
        assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock)) == "offer already dispatched"
        assert meta.expires_at == deadline
        session.commit()
    with TestClient(mac_app) as client:
        # Current policy still rejects the historical invitation at native preflight.
        assert post(client,f"/mac/outbound/{message_id}/verify",{"token":token}).status_code == 409
        assert post(client,f"/mac/outbound/{message_id}/ack",{"token":token,"outcome":"submitted"}).status_code == 409
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message,message_id).status == "blocked_policy"
        assert offers.metadata(session,session.get(m.Outreach,outreach_id)).expires_at == deadline


def test_claim_becomes_too_late_before_native_send(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock,lead=timedelta(minutes=15))
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message,claim = record_claim(session,outreach,clock)
        message_id,token = message.id,claim.token
        clock.advance(timedelta(minutes=4))
        assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock)) == "too little time for an offer"
        session.commit()
    with TestClient(mac_app) as client:
        assert post(client,f"/mac/outbound/{message_id}/verify",{"token":token}).status_code == 409
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.FillRequest,fill_id).state == "escalated"
        assert session.get(m.Outreach,outreach_id).response == "revoked"
        assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_uncertain_ack_is_idempotent_and_never_resends_or_advances(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock)
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message,claim = record_claim(session,outreach,clock)
        # Restore a previously dispatched receipt, so its late outcome can reconcile.
        assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock)) is None
        message_id,token = message.id,claim.token
        session.commit()
    with TestClient(mac_app) as client:
        for _ in range(2):
            assert post(client,f"/mac/outbound/{message_id}/ack",{"token":token,"outcome":"uncertain"}).status_code == 200
        assert post(client,f"/mac/outbound/{message_id}/ack",{"token":token,"outcome":"submitted"}).status_code == 409
        clock.advance(timedelta(minutes=40))
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message,message_id).status == "uncertain"
        assert session.get(m.FillRequest,fill_id).state == "escalated"
        assert offers.metadata(session,session.get(m.Outreach,outreach_id)).state == "offer_uncertain"
        assert advance_due(FillContext(session,clock,mac_app.state.provider,ScriptedAgentGloo())) == []
        assert len(session.scalars(select(m.Outreach)).all()) == 1
        assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_shift_changes_after_claim_are_rejected_at_preflight(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock)
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message,claim = record_claim(session,outreach,clock)
        message_id,token = message.id,claim.token
        fill = session.get(m.FillRequest,fill_id)
        session.get(m.Shift,fill.shift_id).event.starts_at += timedelta(hours=1)
        assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock)) == "offer changed or closed"
        session.commit()
    with TestClient(mac_app) as client:
        assert post(client,f"/mac/outbound/{message_id}/verify",{"token":token}).status_code == 409
    with mac_app.state.session_factory() as session:
        assert session.get(m.Outreach,outreach_id).response == "revoked"


def test_expiry_and_yes_race_use_the_same_exact_boundary(mac_app, clock):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.core.inbound import handle_inbound
    from app.sms.mock_provider import MockSMSProvider
    from tests.test_fill_agent import parser_returning
    outreach_id,fill_id = queue_offer(mac_app,clock,lead=timedelta(minutes=30))
    with mac_app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message = session.get(m.Message,outreach.message_id)
        assert offers.dispatch(session,outreach,message,offers.decision_time(session,clock)) is None
        message.status = "submitted"
        clock.set_time(offers.metadata(session,outreach).expires_at)
        session.commit()
    barrier = threading.Barrier(2)
    def decide(timer):
        with mac_app.state.session_factory() as session:
            barrier.wait(timeout=5)
            offers.begin_decision(session)
            ctx = FillContext(session,clock,MockSMSProvider(),ScriptedAgentGloo())
            if timer:
                advance_due(ctx)
            else:
                outreach = session.get(m.Outreach,outreach_id)
                volunteer = session.get(m.Volunteer,outreach.volunteer_id)
                handle_inbound(session,clock,ctx.provider,volunteer.phone,"YES",parser_returning(intent="accept"),ctx=ctx)
            session.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(decide,[True,False]))
    with mac_app.state.session_factory() as session:
        assert not session.scalars(select(m.Assignment)).all()
        assert session.get(m.Outreach,outreach_id).response == "expired"
        assert session.get(m.FillRequest,fill_id).state == "escalated"
        assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_two_overlapping_legacy_acceptances_cannot_double_book(mac_app, clock):
    """Migrated simultaneous offers still recheck the volunteer schedule."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.agents.fill_agent import on_outreach_reply
    from app.sms.mock_provider import MockSMSProvider
    ids = []
    with mac_app.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        role = m.Role(name="Synthetic role",ministry="Welcome",required_qualifications=[],criticality="standard",fill_policy="auto")
        session.add(role);session.flush()
        for _ in range(2):
            event = m.Event(title="Legacy synthetic shift",starts_at=clock.now()+timedelta(hours=6),ends_at=clock.now()+timedelta(hours=7),status="scheduled")
            session.add(event);session.flush()
            shift = m.Shift(role_id=role.id,event_id=event.id,slot_index=0)
            session.add(shift);session.flush()
            fill = m.FillRequest(shift_id=shift.id,urgency="normal",state="in_progress",current_tranche=1,created_at=clock.now())
            message = m.Message(direction="out",volunteer_id=volunteer.id,phone=volunteer.phone,body="Legacy synthetic invitation",purpose="outreach",kind="template",status="sent",created_at=clock.now())
            session.add_all([fill,message]);session.flush()
            outreach = m.Outreach(fill_request_id=fill.id,volunteer_id=volunteer.id,tranche=1,message_id=message.id)
            session.add(outreach);session.flush()
            meta = offers.prepare(session,outreach,message.body,clock.now())
            # Seed legacy metadata; the new dispatch gate forbids this state.
            meta.state,meta.message_id = "offer_active",message.id
            # The historical delivered source contains the saved exact deadline.
            message.body = meta.body
            fill.next_action_at = meta.expires_at
            ids.append(outreach.id)
        session.commit()
    barrier = threading.Barrier(2)
    def accept(outreach_id):
        with mac_app.state.session_factory() as session:
            barrier.wait(timeout=5)
            offers.begin_decision(session)
            outreach = session.get(m.Outreach,outreach_id)
            volunteer = session.get(m.Volunteer,outreach.volunteer_id)
            result = on_outreach_reply(FillContext(session,clock,MockSMSProvider(),ScriptedAgentGloo()),volunteer,outreach,"accept")
            session.commit()
            return result.action
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept,ids)) == ["filled","ineligible_yes"]
    with mac_app.state.session_factory() as session:
        assignments = session.scalars(select(m.Assignment)).all()
        assert len(assignments) == 1 and assignments[0].status == "confirmed"


def test_exact_deadline_change_blocks_old_body_until_refreshed_review(mode_app,clock):
    from app.core import confirmations
    app,volunteers,headers = mode_app
    outreach_id,fill_id = queue_offer(app,clock,exact=True)
    with app.state.session_factory() as session:
        outreach = session.get(m.Outreach,outreach_id)
        message = session.get(m.Message,outreach.message_id)
        old = confirmations.stage(session,clock.now(),{"phone":message.phone,
            "volunteer_id":message.volunteer_id,"body":message.body,"purpose":"outreach",
            "kind":"ai","outreach_id":outreach.id,"fill_request_id":fill_id,"transport":"mac"})
        old_body,old_hash = message.body,old.payload["content_hash"]
        clock.advance(timedelta(minutes=1))
        error = offers.dispatch(session,outreach,message,offers.decision_time(session,clock),exact=True)
        assert "fresh exact review" in error
        assert message.body == old_body and message.status == "blocked_confirmation"
        assert old.payload["content_hash"] == old_hash and confirmations.valid(old,clock.now())
        meta = offers.metadata(session,outreach)
        assert meta.body != old_body and meta.state == "offer_review"
        # Fresh content requires a separate review; the old digest cannot authorize it.
        refreshed = confirmations.stage(session,clock.now(),{**old.payload,"body":meta.body})
        assert refreshed.id != old.id and refreshed.payload["content_hash"] != old_hash
        assert not confirmations.valid(refreshed,clock.now(),old_hash)
        assert confirmations.valid(refreshed,clock.now(),refreshed.payload["content_hash"])
        assert len(session.scalars(select(m.Escalation)).all()) == 1
        session.commit()
    with TestClient(app) as client:
        # Updating review metadata does not revive unsolicited native delivery.
        assert client.post("/mac/outbound/pull",headers=headers,json={}).json()["messages"] == []


def test_worker_rejected_preflight_does_not_send_or_stall_fresh_review(tmp_path):
    import httpx
    from datetime import datetime
    from app.integrations.mac_messages import MacWorker
    from tests.test_mac_messages import config,ReaderFixture,PHONE
    from tests.session_fixtures import session_id
    expires = (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()
    old = {"id":90,"token":"t"*64,"phone":PHONE,"session_id":session_id(PHONE),"body":"Old exact body",
        "confirmation_required":True,"content_hash":"h"*64,"approval_expires_at":expires}
    fresh = {**old,"id":91,"token":"s"*64,"body":"Fresh exact body","content_hash":"f"*64}
    pulls=[];sent=[];acks=[]
    def server(request):
        if request.url.path.endswith("/pull"):
            pulls.append(True)
            return httpx.Response(200,json={"messages":[old] if len(pulls)==1 else [fresh]})
        if request.url.path.endswith("/90/verify"):
            return httpx.Response(409,json={"detail":"reply deadline changed; fresh exact review required"})
        if request.url.path.endswith("/91/verify"):
            return httpx.Response(200,json={"verified":True,"phone":PHONE,"body":fresh["body"],"content_hash":fresh["content_hash"]})
        acks.append(request.url.path)
        return httpx.Response(200,json={})
    worker = MacWorker({**config(tmp_path),"competition_confirmation_required":True},live=True,
        client=httpx.Client(transport=httpx.MockTransport(server)),reader=ReaderFixture(),
        sender=lambda phone,body:sent.append(body) or "submitted")
    worker.once()
    assert not sent and not acks and not worker.active_path.exists()
    assert worker.state["dispatches"]["90"]["outcome"] == "blocked"
    worker.once()
    assert sent == [fresh["body"]] and acks == ["/mac/outbound/91/ack"]
