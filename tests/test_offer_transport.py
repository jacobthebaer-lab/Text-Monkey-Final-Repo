"""Mac queue/preflight/uncertainty tests, using HTTP fixtures only."""
from datetime import timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.fill_agent import FillContext, advance_due
from app.core import offer_windows as offers
from app.core.send_gate import SendGate
from app.db import models as m
from tests.test_fill_agent import ScriptedAgentGloo
from tests.test_mac_messages import mac_app, post
from tests.test_confirmations import mode_app


def queue_offer(app, clock, *, lead=timedelta(hours=6), exact=False):
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
        gate = SendGate(session,clock,app.state.provider)
        result = gate.send(body="Can you serve this shift?",purpose="outreach",volunteer=volunteer,role=role,fill_request_id=fill.id,urgent=True)
        if exact:
            from app.core import confirmations
            approval = session.get(m.Approval,result.approval_id)
            confirmations.decide(session,gate,approval,approve=True,actor="Synthetic coordinator",
                expected=approval.payload["content_hash"],now=clock.now())
            outreach.message_id = approval.payload["message_id"]
        else:
            outreach.message_id = result.message_id
        session.commit()
        return outreach.id,fill.id


def test_queue_and_claim_delays_never_start_or_extend_dispatch_timer(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock)
    clock.advance(timedelta(minutes=20))
    with TestClient(mac_app) as client:
        item = post(client,"/mac/outbound/pull").json()["messages"][0]
        assert item["offer_preflight_required"]
        with mac_app.state.session_factory() as session:
            assert offers.metadata(session,session.get(m.Outreach,outreach_id)).state == "offer_claimed"
        assert post(client,f"/mac/outbound/{item['id']}/ack",{"token":item["token"],"outcome":"submitted"}).status_code == 409
        clock.advance(timedelta(minutes=10))
        proof = post(client,f"/mac/outbound/{item['id']}/verify",{"token":item["token"]})
        assert proof.status_code == 200 and proof.json()["body"] != item["body"]
        with mac_app.state.session_factory() as session:
            meta = offers.metadata(session,session.get(m.Outreach,outreach_id))
            deadline = meta.expires_at
            assert meta.detail["dispatched_at"] == clock.now().astimezone(timezone.utc).isoformat()
            assert deadline == clock.now()+timedelta(minutes=55)
            assert session.get(m.FillRequest,fill_id).next_action_at == deadline
        clock.advance(timedelta(minutes=1))
        assert post(client,f"/mac/outbound/{item['id']}/verify",{"token":item["token"]}).json()["body"] == proof.json()["body"]
        assert post(client,f"/mac/outbound/{item['id']}/ack",{"token":item["token"],"outcome":"submitted"}).status_code == 200
        with mac_app.state.session_factory() as session:
            assert offers.metadata(session,session.get(m.Outreach,outreach_id)).expires_at == deadline


def test_claim_becomes_too_late_before_native_send(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock,lead=timedelta(minutes=15))
    with TestClient(mac_app) as client:
        item = post(client,"/mac/outbound/pull").json()["messages"][0]
        clock.advance(timedelta(minutes=4))
        assert post(client,f"/mac/outbound/{item['id']}/verify",{"token":item["token"]}).status_code == 409
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.FillRequest,fill_id).state == "escalated"
        assert session.get(m.Outreach,outreach_id).response == "revoked"
        assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_uncertain_ack_is_idempotent_and_never_resends_or_advances(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock)
    with TestClient(mac_app) as client:
        item = post(client,"/mac/outbound/pull").json()["messages"][0]
        assert post(client,f"/mac/outbound/{item['id']}/verify",{"token":item["token"]}).status_code == 200
        for _ in range(2):
            assert post(client,f"/mac/outbound/{item['id']}/ack",{"token":item["token"],"outcome":"uncertain"}).status_code == 200
        clock.advance(timedelta(minutes=40))
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.FillRequest,fill_id).state == "escalated"
        assert offers.metadata(session,session.get(m.Outreach,outreach_id)).state == "offer_uncertain"
        assert advance_due(FillContext(session,clock,mac_app.state.provider,ScriptedAgentGloo())) == []
        assert len(session.scalars(select(m.Outreach)).all()) == 1
        assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_shift_changes_after_claim_are_rejected_at_preflight(mac_app, clock):
    outreach_id,fill_id = queue_offer(mac_app,clock)
    with TestClient(mac_app) as client:
        item = post(client,"/mac/outbound/pull").json()["messages"][0]
        with mac_app.state.session_factory() as session:
            fill = session.get(m.FillRequest,fill_id)
            session.get(m.Shift,fill.shift_id).event.starts_at += timedelta(hours=1)
            session.commit()
        assert post(client,f"/mac/outbound/{item['id']}/verify",{"token":item["token"]}).status_code == 409
    with mac_app.state.session_factory() as session:
        assert session.get(m.Outreach,outreach_id).response == "revoked"


def test_expiry_and_yes_race_use_the_same_exact_boundary(mac_app, clock):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.core.inbound import handle_inbound
    from app.sms.mock_provider import MockSMSProvider
    from tests.test_fill_agent import parser_returning
    outreach_id,fill_id = queue_offer(mac_app,clock,lead=timedelta(minutes=30))
    with TestClient(mac_app) as client:
        item = post(client,"/mac/outbound/pull").json()["messages"][0]
        assert post(client,f"/mac/outbound/{item['id']}/verify",{"token":item["token"]}).status_code == 200
        assert post(client,f"/mac/outbound/{item['id']}/ack",{"token":item["token"],"outcome":"submitted"}).status_code == 200
    with mac_app.state.session_factory() as session:
        clock.set_time(offers.metadata(session,session.get(m.Outreach,outreach_id)).expires_at)
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
    with TestClient(app) as client:
        item = client.post("/mac/outbound/pull",headers=headers,json={}).json()["messages"][0]
        assert item["confirmation_required"]
        old_body = item["body"]
        clock.advance(timedelta(minutes=1))
        blocked = client.post(f"/mac/outbound/{item['id']}/verify",headers=headers,
            json={"token":item["token"],"content_hash":item["content_hash"]})
        assert blocked.status_code == 409 and "fresh exact review" in blocked.json()["detail"]
        with app.state.session_factory() as session:
            session.info["record_authorized"] = True
            old_message = session.get(m.Message,item["id"])
            assert old_message.body == old_body and old_message.status == "blocked_confirmation"
            pending = session.scalar(select(m.Approval).where(m.Approval.status=="pending",m.Approval.kind=="confirm_text"))
            assert pending.payload["body"] != old_body
            refreshed_body = pending.payload["body"]
            confirmations.decide(session,SendGate(session,clock,app.state.provider),pending,
                approve=True,actor="Synthetic coordinator",expected=pending.payload["content_hash"],now=clock.now())
            session.commit()
        fresh = client.post("/mac/outbound/pull",headers=headers,json={}).json()["messages"][0]
        assert fresh["id"] != item["id"] and fresh["body"] == refreshed_body
        verified = client.post(f"/mac/outbound/{fresh['id']}/verify",headers=headers,
            json={"token":fresh["token"],"content_hash":fresh["content_hash"]})
        assert verified.status_code == 200 and verified.json()["body"] == refreshed_body


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
