"""Synthetic integration checks; no personal Messages DB or actual sends."""

import json
import sqlite3
from datetime import timedelta, datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.integrations.mac_messages import MacWorker, MessagesReader
from app.integrations.mac_models import MacInboundReceipt
from app.llm.parser import ParsedMessage
from app.main import create_app
from app.sms.mac_provider import MacMessagesProvider
from app.sms.provider import get_provider
from tests.session_fixtures import session_id, session_specs, session_json

PHONE = "+15555550101"
TOKEN = "test-credential-" + "x" * 40
PASSWORD = "test-admin-password-123"
HEADERS = {"Authorization": "Bearer " + TOKEN}
WORKER_NOW = datetime.now(timezone.utc)


@pytest.fixture
def mac_app(tmp_path, clock, monkeypatch):
    application = create_app(Settings(
        database_url=f"sqlite:///{tmp_path}/mac.db", sms_provider="mac_messages",
        mac_bridge_enabled=True, mac_bridge_token=TOKEN, mac_demo_phones=PHONE,
        admin_password=PASSWORD,
        mac_test_sessions=session_json([PHONE], clock.now()),
    ))
    application.state.clock = clock
    application.state.mac_delivery_clock = clock
    application.state.mac_delivery_clock = clock
    monkeypatch.setattr("app.web.mac_messages.parse_inbound", lambda gloo, body: ParsedMessage(intent="question", confidence=1))
    monkeypatch.setattr("app.web.routes.parse_inbound", lambda gloo, body: ParsedMessage(intent="question", confidence=1))
    with application.state.session_factory() as s:
        s.add(m.Volunteer(name="Synthetic Tester", phone=PHONE, sms_opt_in=True, status="active", preferences={}, created_at=clock.now()))
        s.commit()
    return application


def post(client, path, data=None):
    return client.post(path, json=data or {}, headers=HEADERS)


def incoming(guid="test-guid", body="What time?"):
    return {"guid": guid, "phone": PHONE, "body": body, "session_id": session_id(PHONE)}


def test_disabled_by_default_and_explicit_configuration_required():
    assert not isinstance(get_provider(Settings()), MacMessagesProvider)
    assert not isinstance(get_provider(Settings(sms_provider="mac_messages")), MacMessagesProvider)
    with pytest.raises(ValueError):
        get_provider(Settings(sms_provider="mac_messages", mac_bridge_enabled=True))


def test_no_unauthenticated_or_outside_number_ingress(mac_app):
    with TestClient(mac_app) as c:
        assert c.post("/mac/inbound", json=incoming()).status_code == 401
        data = {**incoming(), "phone": "+15555550999"}
        assert post(c, "/mac/inbound", data).status_code == 403
        assert post(c, "/mac/inbound", {**incoming(), "service": "SMS"}).status_code == 403
        assert post(c, "/mac/inbound", {**incoming(), "service": "RCS"}).status_code == 422
    with mac_app.state.session_factory() as s:
        assert s.scalar(select(m.Message)) is None


def test_incoming_retry_is_durable_and_content_conflicts_rejected(mac_app):
    with TestClient(mac_app) as c:
        assert post(c, "/mac/inbound", incoming()).json()["duplicate"] is False
        assert post(c, "/mac/inbound", incoming()).json()["duplicate"] is True
        assert post(c, "/mac/inbound", incoming(body="changed")).status_code == 409
    with mac_app.state.session_factory() as s:
        assert len(s.scalars(select(m.Message).where(m.Message.direction == "in")).all()) == 1
        assert len(s.scalars(select(m.Message).where(m.Message.direction == "out")).all()) == 1


def test_atomic_rollback_leaves_no_receipt_or_outbound(mac_app, monkeypatch):
    def broken(*args, **kwargs):
        args[0].add(m.Message(direction="in", phone=PHONE, body="fixture", kind="inbound", status="received", created_at=mac_app.state.clock.now()))
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr("app.web.mac_messages.handle_inbound", broken)
    with TestClient(mac_app, raise_server_exceptions=False) as c:
        assert post(c, "/mac/inbound", incoming()).status_code == 500
    with mac_app.state.session_factory() as s:
        assert s.get(MacInboundReceipt, "test-guid") is None
        assert s.scalar(select(m.Message)) is None


def test_claim_and_ack_do_not_resend_after_restart(mac_app):
    with TestClient(mac_app) as c:
        post(c, "/mac/inbound", incoming())
        item = post(c, "/mac/outbound/pull").json()["messages"][0]
        assert post(c, "/mac/outbound/pull").json()["messages"] == []
        path = f"/mac/outbound/{item['id']}/ack"
        assert post(c, path, {"token": "z" * 64, "outcome": "submitted"}).status_code == 409
        data = {"token": item["token"], "outcome": "submitted"}
        assert post(c, path, data).json()["status"] == "submitted"
        assert post(c, path, data).status_code == 200
        assert post(c, path, {**data, "outcome": "uncertain"}).status_code == 409
    with TestClient(mac_app) as c:
        assert post(c, "/mac/outbound/pull").json()["messages"] == []


@pytest.mark.parametrize("block", ["opt_out", "sensitive", "quiet"])
def test_checks_repeated_at_delivery(mac_app, block):
    with TestClient(mac_app) as c:
        post(c, "/mac/inbound", incoming())
        with mac_app.state.session_factory() as s:
            v = s.scalar(select(m.Volunteer))
            if block == "opt_out":
                v.sms_opt_in = False
            elif block == "sensitive":
                s.add(m.Escalation(category="sensitive", severity="normal", summary="synthetic", related_ids={"volunteer_id": v.id}, status="open", created_at=mac_app.state.clock.now()))
            else:
                mac_app.state.mac_delivery_clock.advance(timedelta(hours=12))
            s.commit()
        assert post(c, "/mac/outbound/pull").json()["messages"] == []


def test_stop_confirmation_once_and_cancels_waiting_reply(mac_app):
    with TestClient(mac_app) as c:
        post(c, "/mac/inbound", incoming())
        assert post(c, "/mac/inbound", incoming("stop-guid", "STOP")).json()["intent"] == "stop"
        post(c, "/mac/inbound", incoming("stop-again-guid", "STOP"))
        batch = post(c, "/mac/outbound/pull").json()["messages"]
        assert len(batch) == 1
        assert "stop" in batch[0]["body"].lower() or "unsubscribed" in batch[0]["body"].lower()


@pytest.mark.parametrize("hold", ["stop", "sensitive", "new_opted_out_profile"])
def test_delivery_rechecks_phone_before_profile_exists(mac_app, hold):
    from dataclasses import replace
    mac_app.state.settings = replace(mac_app.state.settings, allow_text_signup=True)
    with mac_app.state.session_factory() as s:
        s.delete(s.scalar(select(m.Volunteer)))
        s.flush()
        result = SendGate(s, mac_app.state.clock, mac_app.state.provider).send(
            body="What is your name?", phone=PHONE, purpose="signup_reply")
        assert result.message_id
        if hold == "sensitive":
            s.add(m.Escalation(category="sensitive", severity="normal", status="acknowledged",
                summary="Synthetic phone hold", related_ids={"phone": PHONE}, created_at=mac_app.state.clock.now()))
        elif hold == "new_opted_out_profile":
            s.add(m.Volunteer(name="New Test Profile", phone=PHONE, sms_opt_in=False,
                status="inactive", preferences={}, created_at=mac_app.state.clock.now()))
        s.commit()
    with TestClient(mac_app) as c:
        if hold == "stop":
            assert post(c, "/mac/inbound", incoming("unknown-stop", "STOP")).json()["intent"] == "stop"
        assert post(c, "/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as s:
        assert s.get(m.Message, result.message_id).status.startswith("blocked_")


def test_simulator_never_queues_mac_delivery_and_reset_is_blocked(mac_app):
    with TestClient(mac_app) as c:
        assert c.post("/simulator/1/send", data={"body": "What time?"}, auth=("admin", PASSWORD), follow_redirects=False).status_code == 303
        assert post(c, "/mac/outbound/pull").json()["messages"] == []
        assert c.post("/demo/reset", auth=("admin", PASSWORD)).status_code == 409


def test_gate_keeps_approval_hold_and_blocks_other_numbers(mac_app):
    with mac_app.state.session_factory() as s:
        v = s.scalar(select(m.Volunteer))
        role = m.Role(name="Nursery", ministry="Kids", required_qualifications=[], criticality="critical", fill_policy="needs_approval")
        s.add(role)
        s.flush()
        gate = SendGate(s, mac_app.state.clock, mac_app.state.provider)
        result = gate.send(body="Synthetic ask", purpose="outreach", volunteer=v, role=role)
        assert result.status == SendStatus.HELD_FOR_APPROVAL
        assert s.scalar(select(m.Message)) is None
        assert gate.send(body="Synthetic", purpose="thanks", phone="+15555550999").status == SendStatus.BLOCKED_TRANSPORT


def config(tmp_path):
    return {"backend_url": "https://fixture.ngrok.app", "token": TOKEN, "phones": [PHONE],
            "receiving_number": "+15555550200", "test_sessions": session_specs([PHONE], WORKER_NOW),
            "state_path": str(tmp_path / "checkpoint.json")}


@pytest.mark.parametrize("origin,expected", [("mac_messages", "queued_for_mac"), ("mock_or_twilio", "simulated")])
def test_supabase_admin_review_preserves_message_origin(mac_app, origin, expected):
    from dataclasses import replace
    from app.web.texty import admin
    mac_app.state.settings = replace(mac_app.state.settings, supabase_url="https://fixture.supabase.co", supabase_publishable_key="public-fixture", admin_email_allowlist="coordinator@example.test")
    with mac_app.state.session_factory() as s:
        v = s.scalar(select(m.Volunteer))
        a = m.Approval(kind="send_outreach", payload={"volunteer_id":v.id,"phone":PHONE,"body":"Synthetic ask","purpose":"outreach","transport":origin}, status="pending", requested_at=mac_app.state.clock.now())
        s.add(a)
        s.commit()
        approval_id = a.id
    with TestClient(mac_app) as c:
        assert c.post(f"/api/proposals/{approval_id}/approve").status_code == 401
        mac_app.dependency_overrides[admin] = lambda: {"email":"coordinator@example.test"}
        response = c.post(f"/api/proposals/{approval_id}/approve")
        assert response.status_code == 200
        assert response.json()["delivery"] == expected
        batch = post(c, "/mac/outbound/pull").json()["messages"]
        assert len(batch) == (1 if origin == "mac_messages" else 0)
    mac_app.dependency_overrides.clear()


def test_native_coordinator_approval_skips_simulator_proposals(mac_app):
    with mac_app.state.session_factory() as s:
        v = s.scalar(select(m.Volunteer))
        v.is_coordinator = True
        for origin in ["mock_or_twilio", "mac_messages"]:
            s.add(m.Approval(kind="send_outreach", payload={"volunteer_id":v.id,"phone":PHONE,"body":"Synthetic ask","purpose":"outreach","transport":origin}, status="pending", requested_at=mac_app.state.clock.now()))
        s.commit()
    with TestClient(mac_app) as c:
        assert post(c, "/mac/inbound", incoming(body="YES")).json()["intent"] == "approval"
        assert len(post(c, "/mac/outbound/pull").json()["messages"]) == 1
    with mac_app.state.session_factory() as s:
        approvals = s.scalars(select(m.Approval).order_by(m.Approval.id)).all()
        assert [a.status for a in approvals] == ["pending", "approved"]


@pytest.mark.parametrize("service", ["iMessage", "SMS"])
def test_signup_uses_gloo_and_collects_consent_through_mac(mac_app, service):
    from dataclasses import replace
    from types import SimpleNamespace
    calls = []
    def response(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps({"signup":True,"first_name":"Synthetic","last_name":"Volunteer","sensitive":False}))
    mac_app.state.gloo = SimpleNamespace(settings=mac_app.state.settings, create_response=response)
    mac_app.state.settings = replace(mac_app.state.settings, allow_text_signup=True)
    mac_app.state.provider.services = frozenset({service})
    with mac_app.state.session_factory() as s:
        s.delete(s.scalar(select(m.Volunteer)))
        s.commit()
    with TestClient(mac_app) as c:
        result = post(c, "/mac/inbound", {**incoming(body="I am Synthetic Volunteer and want to help"), "service": service})
        assert result.json()["intent"] == "signup_consent_pending"
        batch = post(c, "/mac/outbound/pull").json()["messages"]
        assert len(batch) == 1
        assert "Reply YES" in batch[0]["body"]
        assert post(c, "/mac/inbound", {**incoming("consent-guid", "YES"), "service": service}).json()["intent"] == "signup_complete"
        assert len(post(c, "/mac/outbound/pull").json()["messages"]) == 1
    assert calls[0]["model"] == mac_app.state.settings.parser_model
    with mac_app.state.session_factory() as s:
        volunteer = s.scalar(select(m.Volunteer))
        assert volunteer.sms_opt_in is True
        assert volunteer.preferences["consent_pending"] is False
        assert volunteer.is_coordinator is False
        assert volunteer.qualifications == []


class ReaderFixture:
    def watermark(self):
        return 42
    def new_messages(self, after):
        assert after >= 42
        return []


def test_worker_defaults_to_no_send_or_claim(tmp_path):
    def reject(request):
        pytest.fail("Default worker must not claim outbound messages")
    worker = MacWorker(config(tmp_path), client=httpx.Client(transport=httpx.MockTransport(reject)), reader=ReaderFixture())
    worker.once()
    assert json.loads(worker.state_path.read_text())["after"] == 42


def test_worker_ack_failure_never_duplicates_native_send(tmp_path):
    sent = []
    acknowledgments = []
    calls = []
    item = {"id": 7, "token": "c" * 64, "phone": PHONE, "session_id": session_id(PHONE), "body": 'Synthetic quote " and apostrophe \' and newline\n'}
    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/pull"):
            return httpx.Response(200, json={"messages": [item]})
        acknowledgments.append(json.loads(request.content))
        return httpx.Response(503 if len(acknowledgments) == 1 else 200, json={})
    client = httpx.Client(transport=httpx.MockTransport(server))
    sender = lambda phone, body: sent.append((phone, body)) or "submitted"
    worker = MacWorker(config(tmp_path), live=True, client=client, reader=ReaderFixture(), sender=sender)
    with pytest.raises(httpx.HTTPStatusError):
        worker.once()
    recovered = MacWorker(config(tmp_path), live=True, client=client, reader=ReaderFixture(), sender=sender)
    recovered.once()
    assert sent == [(PHONE, item["body"])]
    assert calls.count("/mac/outbound/pull") == 1
    assert acknowledgments[-1]["outcome"] == "submitted"


def test_uncertain_native_failure_is_not_retried(tmp_path):
    def server(request):
        if request.url.path.endswith("/pull"):
            return httpx.Response(200, json={"messages": [{"id": 1, "token": "q" * 64, "phone": PHONE, "session_id": session_id(PHONE), "body": "synthetic"}]})
        assert json.loads(request.content)["outcome"] == "uncertain"
        return httpx.Response(200, json={})
    def fail(phone, body):
        raise TimeoutError("synthetic native timeout")
    worker = MacWorker(config(tmp_path), live=True, client=httpx.Client(transport=httpx.MockTransport(server)), reader=ReaderFixture(), sender=fail)
    worker.once()
    assert worker.state["dispatches"]["1"]["outcome"] == "uncertain"


def test_database_reader_fetches_only_new_selected_direct_imessages(tmp_path):
    path = tmp_path / "synthetic-messages.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE message(guid TEXT, handle_id INTEGER, text TEXT, attributedBody BLOB, is_from_me INTEGER, service TEXT);
            CREATE TABLE handle(id TEXT);
            CREATE TABLE chat(service_name TEXT);
            CREATE TABLE chat_message_join(message_id INTEGER, chat_id INTEGER);
            CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
            INSERT INTO handle VALUES ('+15555550101'), ('+15555550999');
            INSERT INTO chat VALUES ('iMessage'), ('iMessage'), ('iMessage'), ('SMS');
            INSERT INTO chat_handle_join VALUES (1,1), (2,2), (3,1), (3,2), (4,1);
            INSERT INTO message VALUES ('old',1,'old history',NULL,0,'iMessage');
            INSERT INTO chat_message_join VALUES (1,1);
        """)
    reader = MessagesReader(path, {PHONE}, tmp_path / "unused-helper")
    baseline = reader.watermark()
    with sqlite3.connect(path) as db:
        for guid, handle, text, outgoing, service, chat in [
            ("allowed",1,"synthetic inbound",0,"iMessage",1),
            ("private",2,"private fixture",0,"iMessage",2),
            ("group",1,"group fixture",0,"iMessage",3),
            ("self",1,"outbound fixture",1,"iMessage",1),
            ("sms",1,"SMS fixture",0,"SMS",4),
        ]:
            cur = db.execute("INSERT INTO message VALUES (?,?,?,NULL,?,?)", (guid,handle,text,outgoing,service))
            db.execute("INSERT INTO chat_message_join VALUES (?,?)", (cur.lastrowid,chat))
    rows = reader.new_messages(baseline)
    assert [row["guid"] for row in rows] == ["allowed"]
    reader.connection.close()


@pytest.mark.parametrize("service", ["iMessage", "SMS"])
def test_selected_receiving_line_filters_personal_threads_and_binds_sender(tmp_path, service):
    path = tmp_path / "routing.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE message(guid TEXT, handle_id INTEGER, text TEXT, attributedBody BLOB, is_from_me INTEGER, service TEXT, destination_caller_id TEXT);
            CREATE TABLE handle(id TEXT);
            CREATE TABLE chat(service_name TEXT, guid TEXT, last_addressed_handle TEXT);
            CREATE TABLE chat_message_join(message_id INTEGER, chat_id INTEGER);
            CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
            INSERT INTO handle VALUES ('+15555550101');
            INSERT INTO chat VALUES ('iMessage','selected-chat','+15555550200'), ('iMessage','personal-chat','+15555550300');
            INSERT INTO chat_handle_join VALUES (1,1),(2,1);
            INSERT INTO message VALUES ('allowed',1,'name reply',NULL,0,'iMessage','+15555550200'),('personal',1,'private',NULL,0,'iMessage','+15555550300');
            INSERT INTO chat_message_join VALUES (1,1),(2,2);
        """)
        if service == "SMS":
            db.execute("UPDATE message SET service = 'SMS'")
            db.execute("UPDATE chat SET service_name = 'SMS'")
        db.executescript("""
            INSERT INTO chat VALUES ('RCS','unsupported-chat','+15555550200'),('SMS','group-chat','+15555550200');
            INSERT INTO handle VALUES ('+15555550999');
            INSERT INTO chat_handle_join VALUES (3,1),(4,1),(4,2);
            INSERT INTO message VALUES ('rcs',1,'unsupported',NULL,0,'RCS','+15555550200'),('group',1,'group',NULL,0,'SMS','+15555550200');
            INSERT INTO chat_message_join VALUES (3,3),(4,4);
        """)
    reader = MessagesReader(path, {PHONE}, tmp_path / "unused", "+15555550200", [service])
    assert [row["guid"] for row in reader.new_messages(0)] == ["allowed"]
    assert reader.new_messages(0)[0]["service"] == service
    assert reader.outgoing_chat(PHONE) == "selected-chat"
    with pytest.raises(ValueError):
        reader.outgoing_chat("+15555550999")
    reader.connection.close()


def test_sms_service_selection_requires_line_and_fresh_checkpoint(tmp_path):
    original = config(tmp_path)
    worker = MacWorker(original, reader=ReaderFixture())
    worker.client.close()
    with pytest.raises(ValueError, match="receiving line"):
        MacWorker({**original, "services": ["SMS"], "receiving_number": None}, reader=ReaderFixture())
    with pytest.raises(ValueError, match="fresh checkpoint"):
        MacWorker({**original, "services": ["SMS"], "receiving_number": "+15555550200"}, reader=ReaderFixture())
    sms_config = {**original, "state_path": str(tmp_path / "sms-checkpoint.json"),
                  "services": ["SMS"], "receiving_number": "+15555550200"}
    sms = MacWorker(sms_config, reader=ReaderFixture())
    assert sms.state["after"] == 42 and sms.state["services"] == ["SMS"]
    sms.client.close()


@pytest.mark.parametrize("services", ["RCS", "", "SMS,RCS"])
def test_backend_rejects_unsupported_message_service_configuration(services):
    with pytest.raises(ValueError, match="MAC_MESSAGE_SERVICES"):
        MacMessagesProvider(Settings(sms_provider="mac_messages", mac_bridge_enabled=True,
            mac_bridge_token=TOKEN, mac_demo_phones=PHONE, admin_password=PASSWORD,
            mac_message_services=services))


def test_sms_only_backend_rejects_imessage_even_for_allowlisted_phone(mac_app):
    mac_app.state.provider.services = frozenset({"SMS"})
    with TestClient(mac_app) as client:
        assert post(client, "/mac/inbound", incoming()).status_code == 403
        assert post(client, "/mac/inbound", {**incoming(), "service": "SMS"}).status_code == 200


def setup_invitation_app(application):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.web.texty import admin
    application.state.settings = replace(application.state.settings, gloo_signup_replies=True)
    application.state.gloo = SimpleNamespace(settings=application.state.settings,
        create_response=lambda **kwargs: SimpleNamespace(output_text="What would you like to help with? Reply ANY, or STOP to stop."))
    with application.state.session_factory() as session:
        session.add(m.Policy(key="full_text_onboarding", value={"value": True}))
        session.commit()
        volunteer_id = session.scalar(select(m.Volunteer.id))
    application.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
    return volunteer_id


@pytest.mark.parametrize("service", ["iMessage", "SMS"])
def test_admin_starts_gloo_text_setup_once_and_roster_updates(mac_app, service):
    from app.web.texty import admin
    mac_app.state.provider.services = frozenset({service})
    volunteer_id = setup_invitation_app(mac_app)
    with TestClient(mac_app) as client:
        mac_app.dependency_overrides.clear()
        assert client.post(f"/api/volunteers/{volunteer_id}/text-setup").status_code != 200
        mac_app.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
        route = f"/api/volunteers/{volunteer_id}/text-setup"
        result = client.post(route)
        assert result.status_code == 200
        assert result.json()["delivery"] == "queued_for_mac"
        assert result.json()["volunteer"]["onboarding_stage"] == "interests"
        assert client.post(route).status_code == 409
        roster = client.get("/api/state").json()["volunteers"]
        assert roster[0]["onboarding_stage"] == "interests"
        assert not roster[0]["can_start_text_setup"]
        messages = post(client, "/mac/outbound/pull").json()["messages"]
        assert len(messages) == 1 and messages[0]["phone"] == PHONE
    with mac_app.state.session_factory() as session:
        run = session.scalar(select(m.AgentRun))
        assert run.agent == "signup_reply" and run.outcome == "reply_composed"
    mac_app.dependency_overrides.clear()


@pytest.mark.parametrize("block", ["outside_phone", "opt_out", "sensitive", "quiet", "gloo_failure", "paused"])
def test_setup_invite_failure_never_changes_stage_or_queues_text(mac_app, block):
    from types import SimpleNamespace
    from app.llm.gloo_client import GlooUnavailableError
    from app.sms.mock_provider import MockSMSProvider
    volunteer_id = setup_invitation_app(mac_app)
    with mac_app.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, volunteer_id)
        if block == "outside_phone":
            volunteer.phone = "+15555550999"
        if block == "opt_out":
            volunteer.sms_opt_in = False
        if block == "sensitive":
            session.add(m.Escalation(category="sensitive", severity="normal", status="open",
                summary="Synthetic hold", related_ids={"volunteer_id": volunteer.id}, created_at=mac_app.state.clock.now()))
        session.commit()
    if block == "quiet":
        mac_app.state.clock.advance(timedelta(hours=12))
    if block == "paused":
        mac_app.state.provider = MockSMSProvider()
    if block == "gloo_failure":
        def fail(**kwargs):
            raise GlooUnavailableError("Synthetic outage")
        mac_app.state.gloo = SimpleNamespace(settings=mac_app.state.settings, create_response=fail)
    with TestClient(mac_app) as client:
        assert client.post(f"/api/volunteers/{volunteer_id}/text-setup").status_code in {403, 409, 503}
    with mac_app.state.session_factory() as session:
        assert not session.get(m.Volunteer, volunteer_id).preferences.get("onboarding_stage")
        assert session.scalar(select(m.Message)) is None
    mac_app.dependency_overrides.clear()


def test_dashboard_roster_batches_latest_availability_and_clearance_reads(mac_app):
    from sqlalchemy import event
    from app.web.texty import admin
    with mac_app.state.session_factory() as session:
        for i in range(20):
            volunteer = m.Volunteer(name=f'Synthetic batch tester {i}', phone=f'+15555552{i:03}',
                sms_opt_in=False, status='active', preferences={}, created_at=mac_app.state.clock.now())
            session.add(volunteer); session.flush()
            session.add(m.Availability(volunteer_id=volunteer.id, month='2026-10', raw_reply='older preference'))
            session.flush()
            session.add(m.Availability(volunteer_id=volunteer.id, month='2026-11', raw_reply='latest preference'))
        session.commit()
    mac_app.dependency_overrides[admin] = lambda: {'email':'coordinator@example.test'}
    queries = []
    def count(*args): queries.append(args[2])
    event.listen(mac_app.state.engine, 'before_cursor_execute', count)
    try:
        with TestClient(mac_app) as client:
            response = client.get('/api/state')
            assert response.status_code == 200
            batch = [v for v in response.json()['volunteers'] if v['first_name'] == 'Synthetic' and v['phone'] != PHONE]
            assert len(batch) == 20
            assert all(v['availability'] == 'latest preference' and not v['qualified'] for v in batch)
            assert len(queries) < 25
    finally:
        event.remove(mac_app.state.engine, 'before_cursor_execute', count)
        mac_app.dependency_overrides.clear()


def test_test_signup_window_does_not_allow_other_purposes_or_numbers():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    provider = MacMessagesProvider(Settings(sms_provider="mac_messages", mac_bridge_enabled=True,
        mac_bridge_token=TOKEN, mac_demo_phones=PHONE, admin_password=PASSWORD,
        mac_test_signup_reply_until=(now + timedelta(minutes=30)).isoformat()))
    assert provider.allows_test_signup_reply(PHONE, "signup_reply", now)
    assert not provider.allows_test_signup_reply(PHONE, "outreach", now)
    assert not provider.allows_test_signup_reply("+15555550999", "signup_reply", now)
    assert not provider.allows_test_signup_reply(PHONE, "signup_reply", now + timedelta(hours=1))
