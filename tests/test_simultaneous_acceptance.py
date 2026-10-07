"""Two competing HTTP acceptances using independent file-SQLite transactions.

This verifies the supported one-backend SQLite demo. It does not exercise
PostgreSQL row locking or prove multi-process production behavior.
"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.db import models as m
from app.llm.parser import ParsedMessage
from app.main import create_app
from tests.test_fill_agent import ScriptedAgentGloo, historical_invitation
from tests.session_fixtures import session_id, session_json


class ReplyTrackingGloo(ScriptedAgentGloo):
    def __init__(self):
        super().__init__()
        self.reply_calls = []

    def create_response(self, *, input, **kwargs):
        if isinstance(input, str):
            facts = json.loads(input)
            if 'approved_message' in facts:
                assert facts['exact_copy'] is True
                self.reply_calls.append(facts)
        return super().create_response(input=input, **kwargs)


def test_two_simultaneous_http_acceptances_leave_one_confirmed_winner(tmp_path, clock, monkeypatch):
    phones = ["+12025550180", "+12025550181", "+12025550182"]
    token = "synthetic-bridge-"+"x"*40
    app = create_app(Settings(database_url="sqlite:///"+str(tmp_path/"race.db"),
        sms_provider="mac_messages", mac_bridge_enabled=True, mac_bridge_token=token,
        admin_password="synthetic-admin-password", mac_demo_phones=",".join(phones),
        mac_test_sessions=session_json(phones,clock.now()), automation_enabled=False))
    app.state.clock = app.state.mac_delivery_clock = clock
    app.state.gloo = ReplyTrackingGloo()
    monkeypatch.setattr("app.web.mac_messages.parse_inbound",lambda gloo,body:
        ParsedMessage(intent="cancel" if "can't" in body.lower() else "accept", confidence=1))
    headers = {"Authorization":"Bearer "+token}
    audit = {"data":"synthetic only", "model":"scripted fake Gloo; no live model calls", "database":"file SQLite",
             "native_delivery":"mock acknowledgments only", "events":[]}
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            people = [m.Volunteer(name=name,phone=p,sms_opt_in=True,status="active",preferences={},created_at=clock.now())
                      for name,p in zip(["Original Synthetic","Alpha Synthetic","Beta Synthetic"],phones)]
            role = m.Role(name="Greeter",ministry="Welcome",required_qualifications=[],criticality="standard",fill_policy="auto")
            event = m.Event(title="Synthetic Sunday Service",starts_at=clock.now()+timedelta(days=3),
                            ends_at=clock.now()+timedelta(days=3,hours=1),status="scheduled")
            session.add_all([*people,role,event]);session.flush()
            slot = m.Shift(role_id=role.id,event_id=event.id,slot_index=0)
            session.add(slot);session.flush()
            original = m.Assignment(shift_id=slot.id,volunteer_id=people[0].id,status="confirmed",source="admin",
                                    created_at=clock.now(),updated_at=clock.now())
            session.add(original)
            session.commit()
            shift_id, original_id, person_ids = slot.id,original.id,[p.id for p in people]
        def message(index,guid,body):
            return {"guid":guid,"phone":phones[index],"body":body,"session_id":session_id(phones[index])}
        cancelled = client.post("/mac/inbound",headers=headers,json=message(0,"race-cancel","I can't serve Sunday"))
        assert cancelled.status_code == 200 and cancelled.json()["intent"] == "fill_agent"
        audit["events"].append({"step":"cancellation","input":"I can't serve Sunday","result":cancelled.json()})
        notices = client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"]
        assert len(notices) == 1 and notices[0]['phone'] == phones[0]
        cancellation_ack = notices[0]
        with app.state.session_factory() as session:
            from app.core.cancellation_reply import copy_for
            from app.core.policies import PolicyStore
            notice = session.scalar(select(m.Notification).where(m.Notification.key.like('cancellation-reply:%')))
            proof = notice.detail['conversation_meta']['binding']
            source = session.get(m.Message, notice.detail['reply_id'])
            assert source.direction == 'in' and source.body == "I can't serve Sunday"
            assert source.phone == proof['phone'] == phones[0]
            assert source.volunteer_id == proof['volunteer_id'] == person_ids[0]
            assert proof['assignment_id'] == original_id and proof['cancelled']['shift_id'] == shift_id
            outgoing = session.get(m.Message, cancellation_ack['id'])
            expected = copy_for(proof, PolicyStore(session).church_tz())
            assert outgoing.body == expected and outgoing.volunteer_id == person_ids[0]
            assert len(app.state.gloo.reply_calls) == 1
            assert app.state.gloo.reply_calls[0]['approved_message'] == expected and '\u2014' not in expected
        assert cancellation_ack['conversation_preflight_required']
        assert client.post(f"/mac/outbound/{cancellation_ack['id']}/verify", headers=headers,
            json={'token': cancellation_ack['token']}).status_code == 200
        assert client.post(f"/mac/outbound/{cancellation_ack['id']}/ack", headers=headers,
            json={'token': cancellation_ack['token'], 'outcome': 'submitted'}).status_code == 200
        assert client.post('/mac/outbound/pull', headers=headers, json={}).json()['messages'] == []
        with app.state.session_factory() as session:
            fill = session.scalar(select(m.FillRequest))
            assert session.get(m.Assignment, original_id).status == "cancelled"
            historic = historical_invitation(session, clock, session.get(m.Volunteer, person_ids[1]), fill,
                                             provider=app.state.provider)
            assert session.get(m.Message, historic.message_id).status == "submitted"
            session.commit()
        audit["events"].append({"step":"historical invitation fixture", "phone":phones[1],
                                 "evidence":"synthetic submitted message and dispatch deadline; no new send"})
        barrier = threading.Barrier(2)
        def accept(index):
            barrier.wait(timeout=5)
            response = client.post("/mac/inbound",headers=headers,json=message(1,f"race-yes-{index}","YES"))
            return response.status_code,response.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept,[1,2]))
        assert [r[0] for r in results] == [200,200]
        assert sorted(r[1]["notes"][0] for r in results) == ["already_filled","filled"]
        audit["events"].append({"step":"simultaneous YES","responses":[r[1] for r in results]})
        with app.state.session_factory() as session:
            active = session.scalars(select(m.Assignment).where(m.Assignment.shift_id==shift_id,
                m.Assignment.status.in_(("approved","confirmed","proposed")))).all()
            assert len(active) == 1 and active[0].status == "confirmed" and active[0].volunteer_id in person_ids[1:]
            assert session.scalar(select(m.FillRequest)).state == "filled"
            messages = session.scalars(select(m.Message).where(m.Message.direction=="out")).all()
            confirmations = [r for r in messages if r.purpose=="confirmation"]
            assert len(confirmations) == 1 and "confirmed" in confirmations[0].body
            assert not any(r.volunteer_id==person_ids[2] and r.purpose=="outreach" for r in messages)
            audit["events"].append({"step":"persisted roster","shift_id":shift_id,
                "confirmed_volunteer_id":active[0].volunteer_id,"active_assignment_count":len(active),
                "confirmation_count":len(confirmations)})
        notices = client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"]
        assert len(notices) == 1 and notices[0]["phone"] == phones[1]
        item = notices[0]
        assert item["conversation_preflight_required"]
        assert client.post(f"/mac/outbound/{item['id']}/verify", headers=headers,
                           json={"token":item["token"]}).status_code == 200
        assert client.post(f"/mac/outbound/{item['id']}/ack", headers=headers,
                           json={"token":item["token"],"outcome":"submitted"}).status_code == 200
        # Same GUID replay is silent and durable.
        duplicate = client.post("/mac/inbound",headers=headers,json=message(1,"race-yes-1","YES"))
        assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
        audit["events"].append({"step":"duplicate replay","result":duplicate.json()})
    artifact = Path(os.environ.get("QA_AUDIT_PATH",str(tmp_path/"synthetic-session-log.json")))
    artifact.write_text(json.dumps(audit,indent=2))
